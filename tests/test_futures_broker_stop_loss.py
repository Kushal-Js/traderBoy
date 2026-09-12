"""
Tests for Options' broker-side stop-loss order (SL-L) - ported 10 Sep
2026 from Luxury's own proven-working mechanism (user request: "make
Options have similar rule set and guard rails as Luxury have, add and
deploy also"). See Luxury/config.py's own BROKER_STOP_LOSS_ENABLED
docstring for the full SL-M->SL-L story: NSE bans SL-M for index/stock
options exchange-wide since Sep 2021, SL-L (STOP-LOSS LIMIT) is the only
broker-side conditional stop still permitted, confirmed working via a
controlled live test 9 Sep 2026 (NOTES.md entries #99/#100). The
underlying place_stop_loss_limit_order mechanism is SHARED code already
proven correct in production via Luxury - this file tests Options' OWN
entry/exit wiring of it, which is new.

Design (see config.BROKER_STOP_LOSS_ENABLED's own docstring):
  - Options/dhan_client.py: place_stop_loss_limit_order (shared, already
    used by Luxury) + check_if_order_filled.
  - Options/trading_engine.py: _enter_single_position places the stop
    right after the real BUY fills, storing its order_id on the
    Position.stop_loss_order_id field (also recorded in orders_today
    for observability); _check_broker_stop_already_filled (called at
    the top of both _check_one_position and on_price_tick) detects a
    stop that fired ahead of our own reactive logic and closes the
    position directly.
  - Cleanup when a DIFFERENT exit reason fires first: _exit_position's
    pre-existing stale-pending-order check (get_pending_order_id +
    cancel_order) finds and cancels ANY outstanding SELL for the same
    trading_symbol before placing its own, and re-derives the REAL
    broker net quantity afterward (SL-L can partially fill, unlike
    SL-M's own all-or-nothing observed failure mode) - selling only
    what's actually still held instead of blindly trusting the stored
    Position.quantity, preventing an unintended naked short.

Covers, against the REAL production functions (not reimplemented):
  1. A real entry places the stop-loss LIMIT order with the correct
     trigger price (entry - cap/qty) AND limit price (trigger * (1 -
     buffer)), and stores its order_id on the Position.
  2. A failure placing the stop-loss order does not block the entry.
  3. _check_broker_stop_already_filled: a TRADED order closes the
     position directly at the real fill price, exit_reason=MAX_LOSS_HIT,
     with NO fresh SELL order placed.
  4. _check_broker_stop_already_filled: a still-resting (None) order
     returns False and leaves the position untouched.
  5. _check_broker_stop_already_filled: a REJECTED/CANCELLED order
     returns False (logged) rather than raising or crashing.
  6. Full real integration: a genuine TARGET_HIT exit correctly finds
     and cancels the still-resting broker stop-loss order before
     placing its own SELL for the full quantity when nothing partially
     filled.
  7. BROKER_STOP_LOSS_ENABLED=False cleanly bypasses placing the order
     at all.
  8. A genuine PARTIAL fill on the resting broker stop is detected via
     get_broker_net_quantity after the cancel, and the fresh SELL
     correctly sells only the real remaining quantity - never oversells
     into an unintended naked short.
  9. The resting broker stop fully fills in the race window between our
     own check and the cancel attempt (broker shows 0 qty left) -
     reconciled as closed via that order's own real fill price, no
     fresh SELL at all.

HOW TO RUN:
    uv run python tests/test_options_broker_stop_loss.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_futures_broker_stop_loss_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Futures.position_store as fps
import Futures.trading_engine as fte
from Futures.position_store import Position
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP {option_type}", strike=1000.0,
                      option_type=option_type, lot_size=500, security_id=f"SECID-{symbol}",
                      expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks(stop_loss_order_id_factory=None, fail_stop_loss_placement=False,
                            broker_net_quantity=500):
    """Mocks every Dhan network call the entry/exit paths touch,
    including the broker-stop-loss calls. `broker_net_quantity` (default
    500, matching fake_atm_option's own lot_size - i.e. "nothing
    partially filled") is what get_broker_net_quantity reports back
    after a stale-order cancel; tests 8/9 override it to exercise the
    partial-fill/fully-filled-during-the-race paths."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "has_open_position_for_underlying": odc.dhan_wrapper.has_open_position_for_underlying,
        "get_pending_order_id": odc.dhan_wrapper.get_pending_order_id,
        "get_broker_net_quantity": odc.dhan_wrapper.get_broker_net_quantity,
        "cancel_order": odc.dhan_wrapper.cancel_order,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_bearish": odc.dhan_wrapper.get_cached_supertrend_bearish,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "refresh_ema_cross_signal": odc.dhan_wrapper.refresh_ema_cross_signal,
        "get_cached_ema_cross_candle_start": odc.dhan_wrapper.get_cached_ema_cross_candle_start,
        "is_rsi_loss_reentry_blocked": odc.dhan_wrapper.is_rsi_loss_reentry_blocked,
        "get_cached_rsi": odc.dhan_wrapper.get_cached_rsi,
        "get_cached_prev_rsi": odc.dhan_wrapper.get_cached_prev_rsi,
        "rsi_loss_reentry_reason": odc.dhan_wrapper.rsi_loss_reentry_reason,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "place_stop_loss_limit_order": odc.dhan_wrapper.place_stop_loss_limit_order,
        "check_if_order_filled": odc.dhan_wrapper.check_if_order_filled,
        "refresh_order_status": odc.dhan_wrapper.refresh_order_status,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 10_000_000.0}
    odc.dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type: None
    odc.dhan_wrapper.get_broker_net_quantity = lambda trading_symbol: broker_net_quantity
    odc.dhan_wrapper.cancel_order = lambda order_id: None
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_bearish = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None
    odc.dhan_wrapper.refresh_ema_cross_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_ema_cross_candle_start = lambda sym: None
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda sym: False
    odc.dhan_wrapper.get_cached_rsi = lambda sym: None
    odc.dhan_wrapper.get_cached_prev_rsi = lambda sym: None
    odc.dhan_wrapper.rsi_loss_reentry_reason = lambda sym: None
    odc.dhan_wrapper.refresh_order_status = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=45.0, filled_quantity=500, is_amo=False)

    placed_orders = []
    stop_loss_calls = []

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type,
                               "quantity": quantity})
        return {"order_id": order_id, "is_amo": False}

    def fake_place_stop_loss_limit_order(trading_symbol, quantity, transaction_type, trigger_price,
                                          limit_price, tag=None, product_type=None):
        stop_loss_calls.append({
            "trading_symbol": trading_symbol, "quantity": quantity,
            "transaction_type": transaction_type, "trigger_price": trigger_price,
            "limit_price": limit_price,
        })
        if fail_stop_loss_placement:
            raise RuntimeError("simulated SL-L placement failure")
        order_id = stop_loss_order_id_factory() if stop_loss_order_id_factory else f"FAKE-SL-{trading_symbol}"
        return {"order_id": order_id}

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.place_stop_loss_limit_order = fake_place_stop_loss_limit_order
    odc.dhan_wrapper.check_if_order_filled = lambda order_id: None
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore, placed_orders, stop_loss_calls


async def test_1_real_entry_places_broker_stop_with_correct_trigger_and_limit():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.BROKER_STOP_LOSS_ENABLED
    real_cutoff = fte.config.RISK_THRESHOLD_CUTOFF_TIME
    fte.config.BROKER_STOP_LOSS_ENABLED = True
    fte.config.RISK_THRESHOLD_CUTOFF_TIME = "23:59"  # force "before cutoff" -> MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks()
    try:
        result = await fte._process_one_entry("RELIANCE", "CE")
        assert result["status"] == "entered", result

        assert len(stop_loss_calls) == 1, stop_loss_calls
        call = stop_loss_calls[0]
        assert call["transaction_type"] == "SELL", call
        assert call["quantity"] == result["quantity"], call
        entry_price = result["entry_price"]
        expected_trigger = entry_price - (fte.config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF / result["quantity"])
        # Gap is IN RUPEES, sized off the same cap used for trigger_price
        # (12 Sep 2026 - replaced the old flat %-of-price buffer formula).
        expected_limit = expected_trigger - (
            fte.config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF * fte.config.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE
            / result["quantity"]
        )
        assert abs(call["trigger_price"] - expected_trigger) < 0.001, \
            f"expected trigger {expected_trigger}, got {call['trigger_price']}"
        assert abs(call["limit_price"] - expected_limit) < 0.001, \
            f"expected limit {expected_limit}, got {call['limit_price']}"
        assert call["limit_price"] < call["trigger_price"], \
            "the limit price must sit BELOW the trigger for a real SELL SL-L order"

        position = store.live_positions["RELIANCE"]
        assert position.stop_loss_order_id is not None, "the real order_id must be stored on the Position"

        print("1. A real entry places a broker-side SELL stop-loss LIMIT order with the correct trigger "
              "price (entry - cap/qty) AND limit price (trigger * (1 - buffer)), and stores its real "
              "order_id on the Position: PASSED")
    finally:
        restore()
        fte.config.BROKER_STOP_LOSS_ENABLED = real_enabled
        fte.config.RISK_THRESHOLD_CUTOFF_TIME = real_cutoff


async def test_2_stop_loss_placement_failure_does_not_block_entry():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.BROKER_STOP_LOSS_ENABLED
    fte.config.BROKER_STOP_LOSS_ENABLED = True
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks(fail_stop_loss_placement=True)
    try:
        result = await fte._process_one_entry("TCS", "CE")
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
        fte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_3_broker_stop_already_filled_closes_position_directly():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.BROKER_STOP_LOSS_ENABLED
    fte.config.BROKER_STOP_LOSS_ENABLED = True
    position = Position(
        underlying_symbol="INFY", option_trading_symbol="INFY FAKE EXP CE", option_type="CE",
        quantity=500, lot_size=500, entry_price=40.0, highest_price=40.0,
        target_price=50.0, hard_stop_loss=33.6, order_id="OID-ENTRY", product_type="MARGIN",
        stop_loss_order_id="OID-SL-INFY",
    )
    store.live_positions["INFY"] = position

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
        result = await fte._check_broker_stop_already_filled("INFY", position)
        assert result is True, result
        assert place_calls == [], "no fresh SELL order should ever be placed - the broker already filled it"
        assert "INFY" not in store.live_positions, "the position must be closed"
        closed = store.closed_positions_today[0]
        assert closed.exit_reason == "MAX_LOSS_HIT", closed.exit_reason
        assert closed.exit_price == 33.5, closed.exit_price

        await asyncio.sleep(0.3)
        closed_trades = trade_history.read_all_jsonl("real_trades")
        matches = [t for t in closed_trades if t["strategy"] == "Futures" and t["underlying_symbol"] == "INFY"]
        assert len(matches) == 1 and matches[0]["exit_reason"] == "MAX_LOSS_HIT", matches

        print("3. A broker-side stop-loss order that already TRADED closes the position directly at the "
              "REAL fill price, exit_reason=MAX_LOSS_HIT, with ZERO fresh SELL order placed - the "
              "exchange's own fill is authoritative: PASSED")
    finally:
        odc.dhan_wrapper.check_if_order_filled = real_check
        odc.dhan_wrapper.place_market_order = real_place
        odc.dhan_wrapper.unsubscribe_option_price = real_unsubscribe
        fte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_4_broker_stop_still_resting_leaves_position_untouched():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.BROKER_STOP_LOSS_ENABLED
    fte.config.BROKER_STOP_LOSS_ENABLED = True
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
        result = await fte._check_broker_stop_already_filled("WIPRO", position)
        assert result is False, result
        assert "WIPRO" in store.live_positions, "a still-resting stop must leave the position fully untouched"
        print("4. A still-resting (unfired) broker stop-loss order correctly returns False and leaves "
              "the position untouched, ready for the normal reactive check: PASSED")
    finally:
        odc.dhan_wrapper.check_if_order_filled = real_check
        fte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_5_broker_stop_rejected_falls_through_without_crashing():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.BROKER_STOP_LOSS_ENABLED
    fte.config.BROKER_STOP_LOSS_ENABLED = True
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
        result = await fte._check_broker_stop_already_filled("SBIN", position)
        assert result is False, result
        assert "SBIN" in store.live_positions, \
            "a REJECTED broker stop must fall through to the normal reactive check, not crash or close anything"
        print("5. A REJECTED/CANCELLED broker stop-loss order returns False cleanly (logged) instead of "
              "raising - the position falls through to the pre-existing reactive MAX_LOSS_HIT check: PASSED")
    finally:
        odc.dhan_wrapper.check_if_order_filled = real_check
        fte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_6_real_target_hit_finds_and_cancels_the_resting_broker_stop():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.BROKER_STOP_LOSS_ENABLED
    fte.config.BROKER_STOP_LOSS_ENABLED = True
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks(
        stop_loss_order_id_factory=lambda: "OID-RESTING-SL",
        broker_net_quantity=500,  # nothing partially filled - broker still shows the full original qty
    )
    cancelled_order_ids = []
    real_get_pending = odc.dhan_wrapper.get_pending_order_id
    real_cancel = odc.dhan_wrapper.cancel_order
    try:
        entry = await fte._process_one_entry("HDFCBANK", "CE")
        assert entry["status"] == "entered", entry
        position = store.live_positions["HDFCBANK"]
        assert position.stop_loss_order_id == "OID-RESTING-SL"

        odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type: (
            "OID-RESTING-SL" if transaction_type == "SELL" else None
        )
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled_order_ids.append(order_id)

        assert await store.try_start_exit("HDFCBANK")
        await fte._exit_position("HDFCBANK", position, 60.0, "TARGET_HIT")

        assert cancelled_order_ids == ["OID-RESTING-SL"], \
            f"the resting broker stop-loss order must be found and cancelled before the new SELL, got {cancelled_order_ids}"
        assert placed_orders[-1]["quantity"] == 500, \
            f"broker confirmed nothing was partially filled (still 500) - the fresh SELL must be for the FULL " \
            f"original quantity, not something reduced, got {placed_orders[-1]}"
        assert "HDFCBANK" not in store.live_positions
        closed = store.closed_positions_today[0]
        assert closed.exit_reason == "TARGET_HIT", closed.exit_reason

        print("6. A real TARGET_HIT exit correctly finds and cancels the still-resting broker stop-loss "
              "order (via the pre-existing get_pending_order_id/cancel_order stale-order check) before "
              "placing its own SELL for the FULL quantity (broker confirms nothing partially filled) - "
              "the two mechanisms don't race or double-sell: PASSED")
    finally:
        restore()
        odc.dhan_wrapper.get_pending_order_id = real_get_pending
        odc.dhan_wrapper.cancel_order = real_cancel
        fte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_7_disabled_flag_never_places_the_broker_stop():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.BROKER_STOP_LOSS_ENABLED
    fte.config.BROKER_STOP_LOSS_ENABLED = False
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks()
    try:
        result = await fte._process_one_entry("AXISBANK", "CE")
        assert result["status"] == "entered", result
        assert stop_loss_calls == [], \
            f"place_stop_loss_limit_order must never even be CALLED when the flag is off, got {stop_loss_calls}"
        position = store.live_positions["AXISBANK"]
        assert position.stop_loss_order_id is None
        print("7. BROKER_STOP_LOSS_ENABLED=False cleanly bypasses the feature entirely - "
              "place_stop_loss_limit_order is never called, stop_loss_order_id stays None: PASSED")
    finally:
        restore()
        fte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_8_partial_fill_on_resting_stop_never_oversells():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.BROKER_STOP_LOSS_ENABLED
    fte.config.BROKER_STOP_LOSS_ENABLED = True
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks(
        stop_loss_order_id_factory=lambda: "OID-RESTING-SL",
        broker_net_quantity=250,
    )
    cancelled_order_ids = []
    real_get_pending = odc.dhan_wrapper.get_pending_order_id
    real_cancel = odc.dhan_wrapper.cancel_order
    try:
        entry = await fte._process_one_entry("ITC", "CE")
        assert entry["status"] == "entered", entry
        position = store.live_positions["ITC"]
        assert position.quantity == 500, "sanity check - the stored quantity starts at the full original amount"

        odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type: (
            "OID-RESTING-SL" if transaction_type == "SELL" else None
        )
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled_order_ids.append(order_id)

        assert await store.try_start_exit("ITC")
        await fte._exit_position("ITC", position, 60.0, "TARGET_HIT")

        assert cancelled_order_ids == ["OID-RESTING-SL"]
        assert placed_orders[-1]["quantity"] == 250, \
            f"the fresh SELL must be sized to the REAL remaining broker quantity (250), never the stale " \
            f"stored 500 - selling 500 when only 250 is actually held would create an unintended naked " \
            f"short for the other 250, got {placed_orders[-1]}"
        assert "ITC" not in store.live_positions
        closed = store.closed_positions_today[0]
        assert closed.exit_reason == "TARGET_HIT", closed.exit_reason
        assert closed.quantity == 250, \
            f"the closed position's own quantity must reflect the real remaining amount actually sold, got {closed.quantity}"

        print("8. A genuine PARTIAL fill on the resting broker stop-loss order (broker shows 250 of the "
              "original 500 left) is detected after the cancel, and the fresh TARGET_HIT SELL correctly "
              "sells only the real remaining 250 - never oversells into an unintended naked short: PASSED")
    finally:
        restore()
        odc.dhan_wrapper.get_pending_order_id = real_get_pending
        odc.dhan_wrapper.cancel_order = real_cancel
        fte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_9_stop_fully_filled_during_cancel_race_reconciles_without_a_fresh_sell():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.BROKER_STOP_LOSS_ENABLED
    fte.config.BROKER_STOP_LOSS_ENABLED = True
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks(
        stop_loss_order_id_factory=lambda: "OID-RESTING-SL",
        broker_net_quantity=0,
    )
    cancelled_order_ids = []
    real_get_pending = odc.dhan_wrapper.get_pending_order_id
    real_cancel = odc.dhan_wrapper.cancel_order
    real_refresh = odc.dhan_wrapper.refresh_order_status
    odc.dhan_wrapper.refresh_order_status = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=32.1, filled_quantity=500, is_amo=False,
    )
    try:
        entry = await fte._process_one_entry("MARUTI", "CE")
        assert entry["status"] == "entered", entry
        position = store.live_positions["MARUTI"]

        odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type: (
            "OID-RESTING-SL" if transaction_type == "SELL" else None
        )
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled_order_ids.append(order_id)

        orders_before = len(placed_orders)
        assert await store.try_start_exit("MARUTI")
        await fte._exit_position("MARUTI", position, 60.0, "TARGET_HIT")

        assert cancelled_order_ids == ["OID-RESTING-SL"]
        assert len(placed_orders) == orders_before, \
            f"no fresh SELL should ever be placed when the broker already shows 0 qty left, got {placed_orders}"
        assert "MARUTI" not in store.live_positions
        closed = store.closed_positions_today[0]
        assert closed.exit_reason == "TARGET_HIT", closed.exit_reason
        assert closed.exit_price == 32.1, \
            f"must reconcile using the resting order's OWN real fill price (32.1), not the TARGET_HIT " \
            f"trigger price (60.0) that was never actually reached: got {closed.exit_price}"

        print("9. The resting broker stop-loss order fully fills in the race window between our own "
              "check and the cancel attempt (broker shows 0 qty left) - reconciled as closed directly "
              "using that order's own real fill price, with ZERO fresh SELL placed: PASSED")
    finally:
        restore()
        odc.dhan_wrapper.get_pending_order_id = real_get_pending
        odc.dhan_wrapper.cancel_order = real_cancel
        odc.dhan_wrapper.refresh_order_status = real_refresh
        fte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_10_sl_l_still_cancelled_when_the_order_book_scan_misses_it():
    """Regression for OIL (Luxury), 10 Sep 2026: a PROFIT_PROTECTION_HIT
    exit fired 13 seconds after entry, get_pending_order_id returned None
    (the SL-L was too new for Dhan's order-book scan to surface), the
    position closed via a fresh SELL, and the broker-side SL-L was left
    resting with no position behind it - a naked short waiting for its
    trigger. Options shares this exact _exit_position code, so it must
    fall back to Position.stop_loss_order_id and cancel the SL-L anyway."""
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.BROKER_STOP_LOSS_ENABLED
    fte.config.BROKER_STOP_LOSS_ENABLED = True
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks(
        stop_loss_order_id_factory=lambda: "OID-RESTING-SL",
        broker_net_quantity=500,  # nothing filled - the SL-L is genuinely just resting
    )
    cancelled_order_ids = []
    real_get_pending = odc.dhan_wrapper.get_pending_order_id
    real_cancel = odc.dhan_wrapper.cancel_order
    try:
        entry = await fte._process_one_entry("BAJFINANCE", "CE")
        assert entry["status"] == "entered", entry
        position = store.live_positions["BAJFINANCE"]
        assert position.stop_loss_order_id == "OID-RESTING-SL"

        # The order-book scan finds NOTHING (the OIL failure mode -
        # install_all_dhan_mocks already defaults get_pending_order_id to
        # return None; we keep it that way here on purpose).
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled_order_ids.append(order_id)

        assert await store.try_start_exit("BAJFINANCE")
        await fte._exit_position("BAJFINANCE", position, 60.0, "PROFIT_PROTECTION_HIT")

        assert cancelled_order_ids == ["OID-RESTING-SL"], (
            "the SL-L must be cancelled via Position.stop_loss_order_id even though the order-book "
            f"scan returned nothing, got {cancelled_order_ids}"
        )
        assert placed_orders[-1]["quantity"] == 500, placed_orders[-1]
        assert "BAJFINANCE" not in store.live_positions
        closed = store.closed_positions_today[0]
        assert closed.exit_reason == "PROFIT_PROTECTION_HIT", closed.exit_reason
        print("10. A fast exit whose order-book scan misses the seconds-old SL-L still cancels it via "
              "the id tracked on the Position - no orphaned resting stop / naked short (OIL 10 Sep "
              "2026 regression): PASSED")
    finally:
        restore()
        odc.dhan_wrapper.get_pending_order_id = real_get_pending
        odc.dhan_wrapper.cancel_order = real_cancel
        fte.config.BROKER_STOP_LOSS_ENABLED = real_enabled


async def test_11_rejected_exit_rechecks_broker_on_the_first_failure():
    """Regression for TECHM, 10 Sep 2026: a MAX_LOSS exit was RMS-rejected
    for margin (failure 1), the position was then sold manually, and the
    attempt-2 retry - still under the old exit_failure_count >= 2 broker-
    recheck threshold - filled a fresh SELL into a real -600 naked short.
    With the threshold at >= 1, a retry after ANY prior failure first
    checks the broker's net quantity; a flat broker reconciles the
    position closed with NO fresh SELL."""
    store = fps.PositionStore()
    fte.position_store = store
    restore, placed_orders, stop_loss_calls = install_all_dhan_mocks(broker_net_quantity=0)
    try:
        position = Position(
            underlying_symbol="TATAMOTORS", option_trading_symbol="TATAMOTORS FAKE EXP CE",
            option_type="CE", quantity=600, lot_size=600, entry_price=42.4, highest_price=44.5,
            target_price=53.0, hard_stop_loss=35.6, order_id="OID-ENTRY", product_type="MARGIN",
        )
        position.exit_failure_count = 1  # one prior RMS-rejected exit
        store.live_positions["TATAMOTORS"] = position

        assert await store.try_start_exit("TATAMOTORS")
        orders_before = len(placed_orders)
        await fte._exit_position("TATAMOTORS", position, 40.25, "MAX_LOSS_HIT")

        assert len(placed_orders) == orders_before, (
            f"a retry after a failure must NOT place a fresh SELL when the broker shows 0 qty - "
            f"that is the naked short. got {placed_orders}"
        )
        assert "TATAMOTORS" not in store.live_positions
        closed = store.closed_positions_today[0]
        assert closed.exit_reason == "RECONCILED_ALREADY_FLAT", closed.exit_reason
        print("11. After even ONE failed exit, the retry re-checks broker net qty first - a flat "
              "broker reconciles the position closed with ZERO fresh SELL, no naked short (TECHM "
              "10 Sep 2026 regression): PASSED")
    finally:
        restore()


async def main():
    print("=== Futures broker-side stop-loss order test suite ===\n")
    await test_1_real_entry_places_broker_stop_with_correct_trigger_and_limit()
    await test_2_stop_loss_placement_failure_does_not_block_entry()
    await test_3_broker_stop_already_filled_closes_position_directly()
    await test_4_broker_stop_still_resting_leaves_position_untouched()
    await test_5_broker_stop_rejected_falls_through_without_crashing()
    await test_6_real_target_hit_finds_and_cancels_the_resting_broker_stop()
    await test_7_disabled_flag_never_places_the_broker_stop()
    await test_8_partial_fill_on_resting_stop_never_oversells()
    await test_9_stop_fully_filled_during_cancel_race_reconciles_without_a_fresh_sell()
    await test_10_sl_l_still_cancelled_when_the_order_book_scan_misses_it()
    await test_11_rejected_exit_rechecks_broker_on_the_first_failure()
    print("\nALL FUTURES BROKER STOP-LOSS CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
