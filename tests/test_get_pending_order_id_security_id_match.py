"""
Tests for Options/dhan_client.py's get_pending_order_id fix - a REAL live
incident (not theoretical), 15 Sep 2026: JSWENERGY's broker-side SL-L stop
order was left orphaned at the broker (resting, with no position behind
it, after the position closed via a different exit path) because this
scan's tradingSymbol-only string match missed a genuinely-resting order.
Root cause: this codebase's own trading_symbol values are always in
SEM_CUSTOM_SYMBOL format (see _instrument_meta's own docstring), but
Dhan's order-list API can echo tradingSymbol in a different format - a
plain string-equality match can miss a real, live match a security_id
comparison would catch. The position involved had also been reconciled
from the broker earlier that session, so its own Position.stop_loss_
order_id was empty (reconciliation has no way to discover a pre-existing
resting order) - this scan was its ONLY remaining safety net, and it
missed.

The fix has two parts, both covered here:
  1. get_pending_order_id now ALSO resolves trading_symbol to its real
     security_id (via the already-existing _instrument_meta, which
     already matches on EITHER SEM_TRADING_SYMBOL or SEM_CUSTOM_SYMBOL)
     and matches broker orders on EITHER the tradingSymbol string OR
     that security_id.
  2. The status check switched from an explicit allow-list (which
     included "TRIGGER_PENDING" - not one of DhanHQ's own documented
     order statuses) to OrderStatus.TERMINAL_STATUSES as a deny-list, so
     an unanticipated non-terminal status string is still caught.

Coverage:
  1. Reproduces the actual incident: a resting order whose tradingSymbol
     does NOT match position.option_trading_symbol (different format)
     but whose securityId DOES match the resolved one - found via the
     security_id path, which the old string-only match would have missed.
  2. A resting order matched purely by tradingSymbol (old behavior) still
     works - the fix is additive, not a regression for the common case.
  3. An order in a non-terminal status NOT in the old explicit allow-list
     (e.g. a hypothetical "OPEN") is still caught by the new deny-list.
  4. Orders in every genuinely TERMINAL status (REJECTED/CANCELLED/
     TRADED/EXPIRED) are correctly excluded even when the symbol/
     security_id would otherwise match.
  5. A resolution failure for trading_symbol (not in today's instrument
     master) falls back to the string-only match rather than raising -
     this is a defensive improvement, not a hard requirement.

HOW TO RUN:
    uv run python tests/test_get_pending_order_id_security_id_match.py

KNOWN TEST-ISOLATION ISSUE (documented 15 Sep 2026, not yet root-caused,
deliberately deferred - the 5 tests below all pass standalone, see the
run command above): running this file together with
tests/test_futures_broker_stop_loss.py in the SAME pytest session (e.g.
the full `pytest tests/` suite) causes test_3 and test_5 below to fail -
bisected via `pytest tests/test_futures_broker_stop_loss.py
tests/test_get_pending_order_id_security_id_match.py`, confirmed that
file is the source. Something in test_futures_broker_stop_loss.py leaves
dhan_wrapper (likely `.instruments`/`._instrument_meta`/`._client` or a
cached instrument lookup) in a state that survives its own test
teardown and affects _instrument_meta resolution for tests run
afterward - same general CLASS of bug as the Swing.signals cross-test-
file leak found and fixed earlier this session (a monkeypatch/fixture
not restored in a finally block), just not yet pinpointed to the exact
line. Does not affect the PRODUCTION fix these tests cover - confirmed
via a full `git stash` before/after comparison that Options/dhan_client.py's
get_pending_order_id fix introduces zero regressions anywhere else in
the suite (identical 16-failed baseline with or without it). Follow-up:
find and fix the actual unrestored mock in test_futures_broker_stop_loss.py.
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

import pandas as pd

import Options.dhan_client as odc

W = odc.dhan_wrapper


def _instrument_row(exch, instrument_name, trading_symbol, custom_symbol, security_id,
                     strike=520.0, expiry="2026-09-29 15:30:00", lot_units=1075.0, tick_size=5.0):
    return {
        "SEM_EXM_EXCH_ID": exch, "SEM_INSTRUMENT_NAME": instrument_name,
        "SEM_TRADING_SYMBOL": trading_symbol, "SEM_CUSTOM_SYMBOL": custom_symbol,
        "SEM_SMST_SECURITY_ID": security_id, "SEM_STRIKE_PRICE": strike,
        "SEM_EXPIRY_DATE": expiry, "SEM_LOT_UNITS": lot_units, "SEM_TICK_SIZE": tick_size,
    }


def _install_fake_client(instrument_df: pd.DataFrame, orders: list[dict]):
    """Installs ONE fake client exposing both instrument_df (for
    _instrument_meta) and Dhan.get_order_list() (for the order-list scan),
    and returns the TRUE original W._client so the caller can restore it
    exactly - restoring to an intermediate fake instead of the real
    original would leak fake state into every later test in the suite."""
    class FakeDhan:
        @staticmethod
        def get_order_list():
            return {"status": "success", "data": orders}

    true_original = W._client
    W._client = types.SimpleNamespace(Dhan=FakeDhan(), instrument_df=instrument_df)
    return true_original


def test_1_reproduces_the_real_incident_security_id_catches_what_string_match_misses():
    """JSWENERGY: our own trading_symbol is SEM_CUSTOM_SYMBOL format
    ("JSWENERGY 29 SEP 520 PUT"); Dhan's order-list echoes tradingSymbol
    in a DIFFERENT format ("JSWENERGY-Sep2026-520-PE") - the old
    string-only match would return None here."""
    original = _install_fake_client(
        pd.DataFrame([_instrument_row("NSE", "OPTSTK", "JSWENERGY-Sep2026-520-PE",
                                       "JSWENERGY 29 SEP 520 PUT", 34226091514907)]),
        [{"tradingSymbol": "JSWENERGY-Sep2026-520-PE", "securityId": "34226091514907",
          "transactionType": "SELL", "orderStatus": "TRIGGER_PENDING", "orderId": "ORDER-SLL-1"}],
    )
    try:
        result = W.get_pending_order_id("JSWENERGY 29 SEP 520 PUT", "SELL")
        assert result == "ORDER-SLL-1", \
            f"security_id match should have found the resting SL-L order, got {result!r}"
        print("1. Reproduces the JSWENERGY incident: security_id match catches a resting order "
              "the old tradingSymbol-only string match would have missed: PASSED")
    finally:
        W._client = original


def test_2_plain_trading_symbol_match_still_works():
    """The common case (order-list echoes the SAME symbol format we use) -
    the fix is additive, not a regression."""
    original = _install_fake_client(
        pd.DataFrame([_instrument_row("NSE", "OPTSTK", "VEDL-Sep2026-260-PE", "VEDL 29 SEP 260 PUT", 99999)]),
        [{"tradingSymbol": "VEDL 29 SEP 260 PUT", "securityId": "99999",
          "transactionType": "SELL", "orderStatus": "PENDING", "orderId": "ORDER-2"}],
    )
    try:
        result = W.get_pending_order_id("VEDL 29 SEP 260 PUT", "SELL")
        assert result == "ORDER-2", result
        print("2. Plain tradingSymbol match (the common case) still works: PASSED")
    finally:
        W._client = original


def test_3_unanticipated_non_terminal_status_still_caught():
    """The old explicit allow-list (TRANSIT/PENDING/PART_TRADED/
    TRIGGER_PENDING) would have missed a genuinely non-terminal status it
    didn't happen to list. The new deny-list (OrderStatus.TERMINAL_STATUSES)
    catches ANY status that isn't definitively terminal."""
    original = _install_fake_client(
        pd.DataFrame([_instrument_row("NSE", "OPTSTK", "INFY-Sep2026-1500-CE", "INFY 29 SEP 1500 CALL", 55555)]),
        [{"tradingSymbol": "INFY 29 SEP 1500 CALL", "securityId": "55555",
          "transactionType": "SELL", "orderStatus": "OPEN", "orderId": "ORDER-3"}],
    )
    try:
        result = W.get_pending_order_id("INFY 29 SEP 1500 CALL", "SELL")
        assert result == "ORDER-3", \
            f"a non-terminal status not in the old allow-list must still be caught, got {result!r}"
        print("3. An unanticipated non-terminal status ('OPEN', not in the old explicit allow-list) "
              "is still caught by the new deny-list: PASSED")
    finally:
        W._client = original


def test_4_terminal_statuses_are_correctly_excluded():
    instrument_df = pd.DataFrame([
        _instrument_row("NSE", "OPTSTK", "TCS-Sep2026-4000-CE", "TCS 29 SEP 4000 CALL", 77777),
    ])
    for terminal_status in ("REJECTED", "CANCELLED", "TRADED", "EXPIRED"):
        original = _install_fake_client(
            instrument_df,
            [{"tradingSymbol": "TCS 29 SEP 4000 CALL", "securityId": "77777",
              "transactionType": "SELL", "orderStatus": terminal_status, "orderId": "ORDER-TERMINAL"}],
        )
        try:
            result = W.get_pending_order_id("TCS 29 SEP 4000 CALL", "SELL")
            assert result is None, f"a {terminal_status} order must never be treated as still-resting, got {result!r}"
        finally:
            W._client = original
    print("4. Every genuinely terminal status (REJECTED/CANCELLED/TRADED/EXPIRED) is correctly "
          "excluded even when symbol/security_id would otherwise match: PASSED")


def test_5_instrument_resolution_failure_falls_back_to_string_match():
    """trading_symbol not found in today's instrument master (e.g. an
    expired/delisted contract) must not break the scan entirely - falls
    back to the pre-existing string-only match."""
    original = _install_fake_client(
        pd.DataFrame([]),  # empty master - security_id resolution will fail
        [{"tradingSymbol": "UNKNOWNSYM 29 SEP 100 CALL", "securityId": "12345",
          "transactionType": "SELL", "orderStatus": "PENDING", "orderId": "ORDER-5"}],
    )
    try:
        result = W.get_pending_order_id("UNKNOWNSYM 29 SEP 100 CALL", "SELL")
        assert result == "ORDER-5", \
            f"a failed security_id resolution must fall back to the string match, got {result!r}"
        print("5. A failed instrument-master resolution falls back to the string-only match "
              "instead of breaking the scan: PASSED")
    finally:
        W._client = original


def main():
    print("=== get_pending_order_id security_id-match fix test suite ===\n")
    test_1_reproduces_the_real_incident_security_id_catches_what_string_match_misses()
    test_2_plain_trading_symbol_match_still_works()
    test_3_unanticipated_non_terminal_status_still_caught()
    test_4_terminal_statuses_are_correctly_excluded()
    test_5_instrument_resolution_failure_falls_back_to_string_match()
    print("\nALL get_pending_order_id FIX CHECKS PASSED")


if __name__ == "__main__":
    main()
