"""
Tests for Luxury's broker-side stop-loss order (added 8 Sep 2026, user
request: "broker-side stop order that fires instantly regardless of
polling interval would be a better approach" - a follow-up to the same
"longTerm" Chartink-alert backtest that found MAX_LOSS_HIT overshooting
its own rupee cap). A real SELL STOP-LOSS MARKET (SL-M) order is now
placed at Dhan immediately after every entry - Dhan/NSE's own matching
engine fires it the instant price trades through the trigger, independent
of this process's own poll/tick cadence.

Design (see config.BROKER_STOP_LOSS_ENABLED's own docstring for the
full rationale):
  - Options/dhan_client.py: place_stop_loss_market_order (places the
    real SL-M order) + check_if_order_filled (cheap, cache-first check
    for "has this order ALREADY reached a terminal status").
  - Luxury/trading_engine.py: _enter_single_position places the stop
    right after the real BUY fills, storing its order_id on the new
    Position.stop_loss_order_id field; _check_broker_stop_already_filled
    (called at the top of both _check_one_position and on_price_tick)
    detects a stop that fired ahead of our own reactive logic and closes
    the position directly (no fresh SELL needed - it's already flat).
  - Cleanup when a DIFFERENT exit reason fires first: deliberately NO
    new code was needed - _exit_position already has a pre-existing
    stale-pending-order check (get_pending_order_id + cancel_order,
    built for a different incident, BHARATFORG 26 Aug 2026) that finds
    and cancels ANY outstanding SELL for the same trading_symbol before
    placing its own - which already includes our resting stop-loss
    order for free.
  - Deliberately does NOT re-tighten the broker order's trigger at
    config.RISK_THRESHOLD_CUTOFF_TIME - it stays at whatever cap was
    active at entry (same "computed once" convention as target_price/
    hard_stop_loss), acting as a WIDER outer backstop while the existing
    poll/tick-driven MAX_LOSS_HIT check keeps enforcing the CURRENT
    (possibly tighter, post-cutoff) cap in real time regardless.
  - Placing the broker order is fail-open: an exception there is logged
    and swallowed, never blocks the entry - stop_loss_order_id simply
    stays None and the position is exactly as protected as it always
    was via the pre-existing reactive check.

Covers, against the REAL production functions (not reimplemented):
  1. A real entry places the stop-loss order with the correct trigger
     price (entry - cap/qty) and stores its order_id on the Position.
  2. A failure placing the stop-loss order does not block the entry -
     the position still opens, stop_loss_order_id stays None.
  3. _check_broker_stop_already_filled: a TRADED order closes the
     position directly at the real fill price, exit_reason=MAX_LOSS_HIT,
     with NO fresh SELL order placed.
  4. _check_broker_stop_already_filled: a still-resting (None) order
     returns False and leaves the position untouched.
  5. _check_broker_stop_already_filled: a REJECTED/CANCELLED order
     returns False (logged) rather than raising or crashing - the
     position falls through to the normal reactive check.
  6. Full real integration: a genuine TARGET_HIT exit correctly finds
     and cancels the still-resting broker stop-loss order (via the
     pre-existing get_pending_order_id/cancel_order mechanism) before
     placing its own SELL - proving the two mechanisms don't race or
     double-sell.
  7. BROKER_STOP_LOSS_ENABLED=False cleanly bypasses placing the order
     at all - stop_loss_order_id stays None, place_stop_loss_market_
     order is never called.

HOW TO RUN:
    uv run python tests/test_luxury_broker_stop_loss.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
import cross_strategy_registry

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_luxury_broker_stop_loss_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Luxury.position_store as lps
import Luxury.trading_engine as lte
from Luxury.position_store import Position
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP {option_type}", strike=1000.0,
                      option_type=option_type, lot_size=500, security_id=f"SECID-{symbol}",
                      expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks(stop_loss_order_id_factory=None, fail_stop_loss_placement=False):
    """Mocks every Dhan network call the entry/exit paths touch,
    including the two new broker-stop-loss calls. `stop_loss_order_id_
    factory` lets a test control exactly what order_id gets returned;
    `fail_stop_loss_placement` simulates place_stop_loss_market_order
    raising, to prove a placement failure never blocks the entry."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "has_open_position_for_underlying": odc.dhan_wrapper.has_open_position_for_underlying,
        "get_pending_order_id": odc.dhan_wrapper.get_pending_order_id,
        "cancel_order": odc.dhan_wrapper.cancel_order,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_bearish": odc.dhan_wrapper.get_cached_supertrend_bearish,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "place_stop_loss_market_order": odc.dhan_wrapper.place_stop_loss_market_order,
        "check_if_order_filled": odc.dhan_wrapper.check_if_order_filled,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 10_000_000.0}
    odc.dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type: None
    odc.dhan_wrapper.cancel_order = lambda order_id: None
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_bearish = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None

    placed_orders = []
    stop_loss_calls = []

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type})
        return {"order_id": order_id, "is_amo": False}

    def fake_place_stop_loss_market_order(trading_symbol, quantity, transaction_type, trigger_price, tag=None, product_type=None):
        stop_loss_calls.append({
            "trading_symbol": trading_symbol, "quantity": quantity,
            "transaction_type": transaction_type, "trigger_price": trigger_price,
        })
        if fail_stop_loss_placement:
            raise RuntimeError("simulated SL-M placement failure")
        order_id = stop_loss_order_id_factory() if stop_loss_order_id_factory else f"FAKE-SL-{trading_symbol}"
        return {"order_id": order_id}

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.place_stop_loss_market_order = fake_place_stop_loss_market_order
    odc.dhan_wrapper.check_if_order_filled = lambda order_id: None
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore, placed_orders, stop_loss_calls


async def test_1_real_entry_places_broker_stop_with_correct_trigger():
    store = lps.PositionStore()
    lte.position_store = store
    real_enabled = lte.config.BROKER_STOP_LOSS_ENABLED
    real_cutoff = lte.config.RISK_THRESHOLD_CUTOFF_TIME
    lte.config.BROKER_STOP_LOSS_ENABLED = True
    lte.config.RISK_THRESHOLD_CUTOFF_TIME = "23:59"  # force "before cutoff" -> MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks()
    try:
        result = await lte._process_one_entry("RELIANCE", "CE")
        assert result["status"] == "entered", result

        assert len(stop_loss_calls) == 1, stop_loss_calls
        call = stop_loss_calls[0]
        assert call["transaction_type"] == "SELL", call
        assert call["quantity"] == result["quantity"], call
        entry_price = result["entry_price"]
        expected_trigger = entry_price - (lte.config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF / result["quantity"])
        assert abs(call["trigger_price"] - expected_trigger) < 0.001, \
            f"expected trigger {expected_trigger}, got {call['trigger_price']}"

        position = store.live_positions["RELIANCE"]
        assert position.stop_loss_order_id is not None, "the real order_id must be stored on the Position"

        print("1. A real entry places a broker-side SELL stop-loss order with the correct trigger price "
              "(entry - cap/qty) and stores its real order_id on the Position: PASSED")
    finally:
        restore()
        lte.config.BROKER_STOP_LOSS_ENABLED = real_enabled
        lte.config.RISK_THRESHOLD_CUTOFF_TIME = real_cutoff


async def test_2_stop_loss_placement_failure_does_not_block_entry():
    store = lps.PositionStore()
    lte.position_store = store
    real_enabled = lte.config.BROKER_STOP_LOSS_ENABLED
    lte.config.BROKER_STOP_LOSS_ENABLED = True
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks(fail_stop_loss_placement=True)
    try:
        result = await lte._process_one_entry("TCS", "CE")
        assert result["status"] == "entered", \
            f"a failed broker-stop placement must NEVER block the real entry, got {result}"
        assert len(stop_loss_calls) == 1, "the placement must still have been attempted"
        position = store.live_positions["TCS"]
        assert position.stop_loss_order_id is None, \
            "a failed placement must leave stop_loss_order_id None, not a stale/fake id"
        print("2. A broker-stop placement failure is logged and swallowed - the entry still succeeds, "
              "stop_loss_order_id stays None (the position is exactly as protected as before this "
              "feature existed): PASSED")
    finally:
        restore()
        lte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_3_broker_stop_already_filled_closes_position_directly():
    store = lps.PositionStore()
    lte.position_store = store
    real_enabled = lte.config.BROKER_STOP_LOSS_ENABLED
    lte.config.BROKER_STOP_LOSS_ENABLED = True
    position = Position(
        underlying_symbol="INFY", option_trading_symbol="INFY FAKE EXP CE", option_type="CE",
        quantity=500, lot_size=500, entry_price=40.0, highest_price=40.0,
        target_price=50.0, hard_stop_loss=33.6, order_id="OID-ENTRY", product_type="MARGIN",
        stop_loss_order_id="OID-SL-INFY",
    )
    store.live_positions["INFY"] = position
    store.reserved_symbols["INFY"] = "CE"

    real_check = odc.dhan_wrapper.check_if_order_filled
    odc.dhan_wrapper.check_if_order_filled = lambda order_id: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=33.5, filled_quantity=500, is_amo=False,
    ) if order_id == "OID-SL-INFY" else None
    real_place = odc.dhan_wrapper.place_market_order
    real_unsubscribe = odc.dhan_wrapper.unsubscribe_option_price
    place_calls = []
    odc.dhan_wrapper.place_market_order = lambda *a, **k: place_calls.append((a, k)) or {"order_id": "SHOULD-NOT-HAPPEN", "is_amo": False}
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    try:
        result = await lte._check_broker_stop_already_filled("INFY", position)
        assert result is True, result
        assert place_calls == [], "no fresh SELL order should ever be placed - the broker already filled it"
        assert "INFY" not in store.live_positions, "the position must be closed"
        closed = store.closed_positions_today[0]
        assert closed.exit_reason == "MAX_LOSS_HIT", closed.exit_reason
        assert closed.exit_price == 33.5, closed.exit_price

        await asyncio.sleep(0.3)
        closed_trades = trade_history.read_all_jsonl("real_trades")
        matches = [t for t in closed_trades if t["strategy"] == "Luxury" and t["underlying_symbol"] == "INFY"]
        assert len(matches) == 1 and matches[0]["exit_reason"] == "MAX_LOSS_HIT", matches

        print("3. A broker-side stop-loss order that already TRADED closes the position directly at the "
              "REAL fill price, exit_reason=MAX_LOSS_HIT, with ZERO fresh SELL order placed - the "
              "exchange's own fill is authoritative: PASSED")
    finally:
        odc.dhan_wrapper.check_if_order_filled = real_check
        odc.dhan_wrapper.place_market_order = real_place
        odc.dhan_wrapper.unsubscribe_option_price = real_unsubscribe
        lte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_4_broker_stop_still_resting_leaves_position_untouched():
    store = lps.PositionStore()
    lte.position_store = store
    real_enabled = lte.config.BROKER_STOP_LOSS_ENABLED
    lte.config.BROKER_STOP_LOSS_ENABLED = True
    position = Position(
        underlying_symbol="WIPRO", option_trading_symbol="WIPRO FAKE EXP CE", option_type="CE",
        quantity=500, lot_size=500, entry_price=40.0, highest_price=40.0,
        target_price=50.0, hard_stop_loss=33.6, order_id="OID-ENTRY", product_type="MARGIN",
        stop_loss_order_id="OID-SL-WIPRO",
    )
    store.live_positions["WIPRO"] = position
    real_check = odc.dhan_wrapper.check_if_order_filled
    odc.dhan_wrapper.check_if_order_filled = lambda order_id: None  # still resting, no push yet
    try:
        result = await lte._check_broker_stop_already_filled("WIPRO", position)
        assert result is False, result
        assert "WIPRO" in store.live_positions, "a still-resting stop must leave the position fully untouched"
        print("4. A still-resting (unfired) broker stop-loss order correctly returns False and leaves "
              "the position untouched, ready for the normal reactive check: PASSED")
    finally:
        odc.dhan_wrapper.check_if_order_filled = real_check
        lte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_5_broker_stop_rejected_falls_through_without_crashing():
    store = lps.PositionStore()
    lte.position_store = store
    real_enabled = lte.config.BROKER_STOP_LOSS_ENABLED
    lte.config.BROKER_STOP_LOSS_ENABLED = True
    position = Position(
        underlying_symbol="SBIN", option_trading_symbol="SBIN FAKE EXP CE", option_type="CE",
        quantity=500, lot_size=500, entry_price=40.0, highest_price=40.0,
        target_price=50.0, hard_stop_loss=33.6, order_id="OID-ENTRY", product_type="MARGIN",
        stop_loss_order_id="OID-SL-SBIN",
    )
    store.live_positions["SBIN"] = position
    real_check = odc.dhan_wrapper.check_if_order_filled
    odc.dhan_wrapper.check_if_order_filled = lambda order_id: OrderResult(
        order_id=order_id, status=OrderStatus.REJECTED, remark="RMS rejection", fill_price=0.0,
        filled_quantity=0, is_amo=False,
    )
    try:
        result = await lte._check_broker_stop_already_filled("SBIN", position)
        assert result is False, result
        assert "SBIN" in store.live_positions, \
            "a REJECTED broker stop must fall through to the normal reactive check, not crash or close anything"
        print("5. A REJECTED/CANCELLED broker stop-loss order returns False cleanly (logged) instead of "
              "raising - the position falls through to the pre-existing reactive MAX_LOSS_HIT check: PASSED")
    finally:
        odc.dhan_wrapper.check_if_order_filled = real_check
        lte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_6_real_target_hit_finds_and_cancels_the_resting_broker_stop():
    store = lps.PositionStore()
    lte.position_store = store
    real_enabled = lte.config.BROKER_STOP_LOSS_ENABLED
    lte.config.BROKER_STOP_LOSS_ENABLED = True
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks(
        stop_loss_order_id_factory=lambda: "OID-RESTING-SL",
    )
    cancelled_order_ids = []
    real_get_pending = odc.dhan_wrapper.get_pending_order_id
    real_cancel = odc.dhan_wrapper.cancel_order
    try:
        entry = await lte._process_one_entry("HDFCBANK", "CE")
        assert entry["status"] == "entered", entry
        position = store.live_positions["HDFCBANK"]
        assert position.stop_loss_order_id == "OID-RESTING-SL"

        # Simulate the broker stop still genuinely resting (unfilled) -
        # _exit_position's own pre-existing stale-order check must find
        # it via get_pending_order_id and cancel it BEFORE placing its
        # own fresh SELL for the real TARGET_HIT exit below.
        odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type: (
            "OID-RESTING-SL" if transaction_type == "SELL" else None
        )
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled_order_ids.append(order_id)

        assert await store.try_start_exit("HDFCBANK")
        await lte._exit_position("HDFCBANK", position, 60.0, "TARGET_HIT")

        assert cancelled_order_ids == ["OID-RESTING-SL"], \
            f"the resting broker stop-loss order must be found and cancelled before the new SELL, got {cancelled_order_ids}"
        assert "HDFCBANK" not in store.live_positions
        closed = store.closed_positions_today[0]
        assert closed.exit_reason == "TARGET_HIT", closed.exit_reason

        print("6. A real TARGET_HIT exit correctly finds and cancels the still-resting broker stop-loss "
              "order (via the pre-existing get_pending_order_id/cancel_order stale-order check) before "
              "placing its own SELL - the two mechanisms don't race or double-sell: PASSED")
    finally:
        restore()
        odc.dhan_wrapper.get_pending_order_id = real_get_pending
        odc.dhan_wrapper.cancel_order = real_cancel
        lte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_7_disabled_flag_never_places_the_broker_stop():
    store = lps.PositionStore()
    lte.position_store = store
    real_enabled = lte.config.BROKER_STOP_LOSS_ENABLED
    lte.config.BROKER_STOP_LOSS_ENABLED = False
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks()
    try:
        result = await lte._process_one_entry("AXISBANK", "CE")
        assert result["status"] == "entered", result
        assert stop_loss_calls == [], \
            f"place_stop_loss_market_order must never even be CALLED when the flag is off, got {stop_loss_calls}"
        position = store.live_positions["AXISBANK"]
        assert position.stop_loss_order_id is None
        print("7. BROKER_STOP_LOSS_ENABLED=False cleanly bypasses the feature entirely - "
              "place_stop_loss_market_order is never called, stop_loss_order_id stays None: PASSED")
    finally:
        restore()
        lte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def main():
    print("=== Luxury broker-side stop-loss order test suite ===\n")
    await test_1_real_entry_places_broker_stop_with_correct_trigger()
    await test_2_stop_loss_placement_failure_does_not_block_entry()
    await test_3_broker_stop_already_filled_closes_position_directly()
    await test_4_broker_stop_still_resting_leaves_position_untouched()
    await test_5_broker_stop_rejected_falls_through_without_crashing()
    await test_6_real_target_hit_finds_and_cancels_the_resting_broker_stop()
    await test_7_disabled_flag_never_places_the_broker_stop()
    print("\nALL LUXURY BROKER STOP-LOSS CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
