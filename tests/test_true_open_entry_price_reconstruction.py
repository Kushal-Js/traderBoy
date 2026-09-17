"""
Tests for Options/dhan_client.py's _true_open_entry_price - a REAL live
incident (not theoretical), 17 Sep 2026: a Swing COPPER position was
manually opened at 9.0, closed by the bot at 10.14 (PROFIT_PROTECTION_HIT),
then manually reopened at 10.6 the same day. On the next restart,
reconciliation reported entry_price=9.8 - exactly (9.0+10.6)/2, because
every _get_open_*_positions_once used Dhan's own "buyAvg" field directly as
avg_price, and buyAvg is a DAY-CUMULATIVE average across every buy fill
today that does NOT reset when a position is fully squared off intraday.
This put target_price/hard_stop_loss on the wrong basis for the live,
reopened position (a wider, more permissive stop than the real 10.6 entry
warranted - real elevated downside risk, not just a cosmetic discrepancy).

The fix reconstructs the true cost basis of the CURRENTLY open quantity
directly from today's own filled orders (get_order_list()) instead of
trusting buyAvg/costPrice, walking them chronologically and resetting the
"open lot" pool whenever a fill in the opposite direction fully closes
(or partially closes, or flips) the running position.

Coverage:
  1. Reproduces the actual incident: BUY 1@9.0, SELL 1@10.14 (full close),
     BUY 1@10.6 (reopen) -> must return 10.6, not the buyAvg-style 9.8.
  2. Simple single-fill case (the overwhelmingly common case) - matches
     what buyAvg would have said anyway, confirming this isn't a
     regression for normal entries.
  3. Multiple same-direction fills before the position stabilizes
     (a partially-filled-then-topped-up entry) - correct weighted average.
  4. A partial close followed by adding back - the closed portion must not
     contaminate the remaining lots' average.
  5. A single fill big enough to flip the position's direction (closes the
     old leg AND opens a new one) - only the genuine leftover counts.
  6. Sanity-check fallback: if the reconstructed quantity doesn't match the
     broker's own reported net_qty, falls back to the old buyAvg-based
     value rather than risk a worse answer than reconciliation already had.
  7. Fetch-error fallback: get_order_list() failing must not raise - falls
     back gracefully, same philosophy as every other best-effort path in
     this file.
  8. MCX's own tradingSymbol-format mismatch (get_pending_order_id's
     already-documented "COPPER-23Sep2026-1400-CE" vs our own canonical
     "COPPER 23 SEP 1400 CALL") does not break this - fills are still
     found via security_id matching alone.

HOW TO RUN:
    uv run python tests/test_true_open_entry_price_reconstruction.py
"""
import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.dhan_client as odc

W = odc.dhan_wrapper


def _install_fake_orders(orders: list[dict]):
    """Installs a fake client exposing only Dhan.get_order_list() (all
    _true_open_entry_price needs) and returns the TRUE original W._client
    so the caller can restore it exactly."""
    class FakeDhan:
        @staticmethod
        def get_order_list():
            return {"status": "success", "data": orders}

    true_original = W._client
    W._client = types.SimpleNamespace(Dhan=FakeDhan())
    return true_original


def _order(price, qty, side, t, symbol="COPPER 23 SEP 1400 CALL", security_id="574836"):
    return {
        "tradingSymbol": symbol, "securityId": security_id, "transactionType": side,
        "filledQty": qty, "averageTradedPrice": price, "createTime": t,
    }


def test_1_reproduces_the_real_incident_full_close_then_reopen():
    orders = [
        _order(9.0, 1, "BUY", "2026-09-17 12:57:28"),
        _order(10.14, 1, "SELL", "2026-09-17 17:43:06"),
        _order(10.6, 1, "BUY", "2026-09-17 17:55:00"),
    ]
    original = _install_fake_orders(orders)
    try:
        result = W._true_open_entry_price("574836", "COPPER 23 SEP 1400 CALL", 1, fallback_avg_price=9.8)
        assert result == 10.6, f"expected the true reopened-leg entry 10.6, got {result}"
        print("1. Reproduces the real incident: full close then reopen correctly returns 10.6, "
              "not buyAvg's contaminated 9.8: PASSED")
    finally:
        W._client = original


def test_2_simple_single_fill_matches_buy_avg():
    orders = [_order(46.466667, 375, "BUY", "2026-09-17 06:59:00",
                      symbol="ATHERENERG 29 SEP 1540 PUT", security_id="111")]
    original = _install_fake_orders(orders)
    try:
        result = W._true_open_entry_price("111", "ATHERENERG 29 SEP 1540 PUT", 375, fallback_avg_price=999)
        assert abs(result - 46.466667) < 1e-6, \
            f"expected the real reconstructed price 46.466667 (not the fallback 999), got {result}"
        print("2. The overwhelmingly common case (one entry fill) matches what buyAvg already "
              "said - not a regression: PASSED")
    finally:
        W._client = original


def test_3_multiple_same_direction_fills_before_stabilizing():
    """A position built up across 2 buy fills before settling - weighted
    average of both, same as buyAvg would report (no intervening sell)."""
    orders = [
        _order(10.0, 1, "BUY", "2026-09-17 09:00:00", symbol="MARICO 29 SEP 820 CALL", security_id="222"),
        _order(10.4, 1, "BUY", "2026-09-17 09:05:00", symbol="MARICO 29 SEP 820 CALL", security_id="222"),
    ]
    original = _install_fake_orders(orders)
    try:
        result = W._true_open_entry_price("222", "MARICO 29 SEP 820 CALL", 2, fallback_avg_price=999)
        assert abs(result - 10.2) < 1e-6, \
            f"expected the real reconstructed average 10.2 (not the fallback 999), got {result}"
        print("3. Multiple same-direction fills before the position stabilizes are correctly "
              "weighted-averaged: PASSED")
    finally:
        W._client = original


def test_4_partial_close_then_add_back_excludes_the_closed_portion():
    """BUY 2@10 (avg 10, qty 2) -> SELL 1@12 (partial close, 1 unit @10
    remains) -> BUY 1@14 (add) -> remaining should be avg(10, 14) = 12, NOT
    contaminated by the 12 the closed unit sold at."""
    orders = [
        _order(10.0, 2, "BUY", "2026-09-17 09:00:00", security_id="333"),
        _order(12.0, 1, "SELL", "2026-09-17 09:10:00", security_id="333"),
        _order(14.0, 1, "BUY", "2026-09-17 09:20:00", security_id="333"),
    ]
    original = _install_fake_orders(orders)
    try:
        result = W._true_open_entry_price("333", "SOME 29 SEP 100 CALL", 2, fallback_avg_price=999)
        assert abs(result - 12.0) < 1e-6, f"expected (10+14)/2=12, got {result}"
        print("4. A partial close followed by adding back correctly excludes the closed "
              "portion's own sell price from the remaining average: PASSED")
    finally:
        W._client = original


def test_5_single_fill_flips_direction():
    """BUY 1@9 (long 1) -> SELL 3@10 (closes the long 1, then opens a NEW
    short 2) -> remaining open leg is short 2 @ 10, not blended with the 9."""
    orders = [
        _order(9.0, 1, "BUY", "2026-09-17 09:00:00", security_id="444"),
        _order(10.0, 3, "SELL", "2026-09-17 09:10:00", security_id="444"),
    ]
    original = _install_fake_orders(orders)
    try:
        result = W._true_open_entry_price("444", "SOME 29 SEP 100 PUT", -2, fallback_avg_price=999)
        assert abs(result - 10.0) < 1e-6, f"expected the flip-leftover price 10.0, got {result}"
        print("5. A single fill big enough to flip the position's direction correctly keeps "
              "only the genuine leftover, not blended with the closed leg: PASSED")
    finally:
        W._client = original


def test_6_quantity_mismatch_falls_back_to_broker_avg():
    """Reconstructed quantity (1, from a single BUY fill) doesn't match the
    broker's own reported net_qty (5, e.g. because an order is missing from
    today's list) - must fall back rather than trust an incomplete picture."""
    orders = [_order(10.0, 1, "BUY", "2026-09-17 09:00:00", security_id="555")]
    original = _install_fake_orders(orders)
    try:
        result = W._true_open_entry_price("555", "SOME 29 SEP 100 CALL", 5, fallback_avg_price=42.0)
        assert result == 42.0, f"expected the fallback_avg_price on a qty mismatch, got {result}"
        print("6. A reconstructed quantity that doesn't match the broker's own net_qty falls "
              "back to buyAvg/costPrice rather than trusting an incomplete reconstruction: PASSED")
    finally:
        W._client = original


def test_7_fetch_error_falls_back_gracefully():
    class FailingDhan:
        @staticmethod
        def get_order_list():
            raise RuntimeError("simulated transient Dhan API failure")

    original = W._client
    W._client = types.SimpleNamespace(Dhan=FailingDhan())
    try:
        result = W._true_open_entry_price("666", "SOME 29 SEP 100 CALL", 1, fallback_avg_price=17.5)
        assert result == 17.5, f"a fetch failure must fall back, not raise or return something else, got {result}"
        print("7. A get_order_list() fetch failure falls back gracefully instead of raising or "
              "blocking reconciliation: PASSED")
    finally:
        W._client = original


def test_8_mcx_hyphenated_symbol_mismatch_still_matches_via_security_id():
    """Mirrors get_pending_order_id's own already-documented MCX format
    mismatch: Dhan's order list echoes a hyphenated tradingSymbol that
    never equals our canonical space-separated form - only security_id
    matching finds these fills."""
    orders = [
        _order(9.0, 1, "BUY", "2026-09-17 12:57:28", symbol="COPPER-23Sep2026-1400-CE", security_id="574836"),
        _order(10.14, 1, "SELL", "2026-09-17 17:43:06", symbol="COPPER-23Sep2026-1400-CE", security_id="574836"),
        _order(10.6, 1, "BUY", "2026-09-17 17:55:00", symbol="COPPER-23Sep2026-1400-CE", security_id="574836"),
    ]
    original = _install_fake_orders(orders)
    try:
        result = W._true_open_entry_price("574836", "COPPER 23 SEP 1400 CALL", 1, fallback_avg_price=9.8)
        assert result == 10.6, \
            f"security_id matching should find these fills despite the tradingSymbol format mismatch, got {result}"
        print("8. MCX's hyphenated tradingSymbol format (never equal to our own canonical form) "
              "does not break this - fills are still found via security_id: PASSED")
    finally:
        W._client = original


def main():
    print("=== _true_open_entry_price reconstruction fix test suite ===\n")
    test_1_reproduces_the_real_incident_full_close_then_reopen()
    test_2_simple_single_fill_matches_buy_avg()
    test_3_multiple_same_direction_fills_before_stabilizing()
    test_4_partial_close_then_add_back_excludes_the_closed_portion()
    test_5_single_fill_flips_direction()
    test_6_quantity_mismatch_falls_back_to_broker_avg()
    test_7_fetch_error_falls_back_gracefully()
    test_8_mcx_hyphenated_symbol_mismatch_still_matches_via_security_id()
    print("\nALL _true_open_entry_price FIX CHECKS PASSED")


if __name__ == "__main__":
    main()
