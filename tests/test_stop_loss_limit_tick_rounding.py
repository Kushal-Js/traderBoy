"""
Tests for place_stop_loss_limit_order()'s tick-size rounding - found live
9 Sep 2026 via a controlled live test of the new SL-L broker stop-loss
feature (see Luxury/config.py's BROKER_STOP_LOSS_ENABLED docstring / NOTES.md
entry #99 for the full SL-M->SL-L story). The COMPUTED trigger_price
(entry - cap/qty) and limit_price (trigger * (1 - buffer)) landed on
values that aren't multiples of the contract's real exchange tick size
(e.g. trigger=3.88 for a Rs.0.05-tick option) - the exchange REJECTED
the order outright with "EXCH:16283: The order price is not multiple of
the tick size" rather than silently accepting/rounding it itself.

Unlike a normal MARKET/LIMIT order at a self-chosen price (a human
would naturally pick a tick-aligned value, or the order type doesn't
care), a stop order's trigger/limit values come from arithmetic on a
rupee-cap and a percentage buffer - they land on an arbitrary,
tick-misaligned value far more often than not. This is exactly why
_round_to_tick()/place_stop_loss_limit_order()'s own tick lookup exist,
and why this needed a dedicated test rather than trusting the existing
Luxury-level tests (which mock place_stop_loss_limit_order() itself,
never exercising ITS OWN internal rounding logic - same "mock at the
wrapper's public boundary, never its own internals" pattern as every
other test file in this repo, which is exactly why this specific bug
slipped past the full test suite and only surfaced in a real live
order).

Covers, against the REAL production function (not reimplemented):
  1. _round_to_tick: the exact real-world failure case (trigger=3.88,
     tick=0.05) rounds to a genuinely valid multiple (3.90).
  2. _round_to_tick: an already-tick-aligned price is left unchanged
     (no unnecessary drift from repeated rounding).
  3. _round_to_tick: a missing/invalid tick_size (None, 0, negative)
     falls back to plain 2-decimal rounding rather than raising or
     dividing by zero.
  4. place_stop_loss_limit_order: the REAL function, against a faked
     Tradehull client, sends TICK-ROUNDED trigger/limit prices to
     order_placement - not the raw computed values.
  5. place_stop_loss_limit_order: a tick_size lookup failure (the
     instrument meta lookup raises) falls back to plain rounding rather
     than blocking the order placement entirely.

HOW TO RUN:
    uv run python tests/test_stop_loss_limit_tick_rounding.py
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

import Options.dhan_client as dc


def _wrapper_with_order_placement(tick_size, order_id="999"):
    """A DhanWrapper with a faked Tradehull client whose order_placement
    captures its own kwargs (so the test can inspect exactly what price/
    trigger_price were actually sent), and a faked _instrument_meta
    returning the given tick_size - same pattern as
    test_luxury_corrective_actions.py's own _instrument_meta override,
    and test_wait_for_order_result_price_fix.py's own faked-client
    approach for testing a dhan_client.py function directly."""
    wrapper = dc.DhanWrapper.__new__(dc.DhanWrapper)
    calls = []

    def fake_order_placement(**kwargs):
        calls.append(kwargs)
        return order_id

    wrapper._client = types.SimpleNamespace(order_placement=fake_order_placement)
    wrapper._instrument_meta = lambda trading_symbol: {"tick_size": tick_size}
    return wrapper, calls


def test_1_round_to_tick_the_real_failure_case():
    result = dc._round_to_tick(3.88, 0.05)
    assert result == 3.90, f"3.88 rounded to the nearest 0.05 tick must be 3.90, got {result}"
    result2 = dc._round_to_tick(3.76, 0.05)
    assert result2 == 3.75, f"3.76 rounded to the nearest 0.05 tick must be 3.75, got {result2}"
    print("1. _round_to_tick correctly rounds the EXACT real-world failure case (trigger=3.88, "
          "limit=3.76 against a Rs.0.05 tick) to genuinely valid multiples (3.90, 3.75): PASSED")


def test_2_round_to_tick_already_aligned_price_unchanged():
    result = dc._round_to_tick(45.05, 0.05)
    assert result == 45.05, f"an already tick-aligned price must not drift, got {result}"
    print("2. An already tick-aligned price is left unchanged - no unnecessary rounding drift: PASSED")


def test_3_round_to_tick_missing_or_invalid_tick_size_falls_back():
    for bad_tick in (None, 0, -0.05):
        result = dc._round_to_tick(3.876, bad_tick)
        assert result == 3.88, \
            f"tick_size={bad_tick} must fall back to plain 2-decimal rounding (3.88), got {result}"
    print("3. A missing/zero/negative tick_size falls back to plain 2-decimal rounding rather than "
          "raising or dividing by zero: PASSED")


def test_4_place_stop_loss_limit_order_sends_tick_rounded_prices():
    wrapper, calls = _wrapper_with_order_placement(tick_size=0.05)
    result = wrapper.place_stop_loss_limit_order(
        trading_symbol="COALINDIA 29 SEP 435 PUT", quantity=1350, transaction_type="SELL",
        trigger_price=3.88, limit_price=3.76, tag="TEST",
    )
    assert result == {"order_id": "999"}
    assert len(calls) == 1, calls
    call = calls[0]
    assert call["trigger_price"] == 3.90, \
        f"the REAL function must round trigger_price to the tick before sending it, got {call['trigger_price']}"
    assert call["price"] == 3.75, \
        f"the REAL function must round limit_price (sent as 'price') to the tick before sending it, got {call['price']}"
    assert call["order_type"] == "STOPLIMIT"
    print("4. place_stop_loss_limit_order (the REAL function, not a mock) sends TICK-ROUNDED trigger/"
          "limit prices to order_placement - not the raw computed 3.88/3.76 that got rejected live: PASSED")


def test_5_tick_lookup_failure_falls_back_without_blocking_the_order():
    wrapper, calls = _wrapper_with_order_placement(tick_size=0.05)
    def raising_meta(trading_symbol):
        raise ValueError("simulated instrument lookup failure")
    wrapper._instrument_meta = raising_meta

    result = wrapper.place_stop_loss_limit_order(
        trading_symbol="COALINDIA 29 SEP 435 PUT", quantity=1350, transaction_type="SELL",
        trigger_price=3.876, limit_price=3.761, tag="TEST",
    )
    assert result == {"order_id": "999"}
    assert len(calls) == 1
    call = calls[0]
    assert call["trigger_price"] == 3.88, \
        f"a tick lookup failure must fall back to plain 2-decimal rounding, not block the order, got {call['trigger_price']}"
    assert call["price"] == 3.76, call["price"]
    print("5. A tick_size lookup failure falls back to plain 2-decimal rounding rather than blocking "
          "the SL-L order placement entirely: PASSED")


def main():
    print("=== place_stop_loss_limit_order tick-rounding test suite ===\n")
    test_1_round_to_tick_the_real_failure_case()
    test_2_round_to_tick_already_aligned_price_unchanged()
    test_3_round_to_tick_missing_or_invalid_tick_size_falls_back()
    test_4_place_stop_loss_limit_order_sends_tick_rounded_prices()
    test_5_tick_lookup_failure_falls_back_without_blocking_the_order()
    print("\nALL STOP-LOSS LIMIT TICK-ROUNDING CHECKS PASSED")


if __name__ == "__main__":
    main()
