"""
Integration tests for Swing v2's entry and exit flow, against the REAL
production functions (enter_position_for_stock / _exit_position /
_check_broker_stop_already_filled) with only the Dhan network boundary
mocked - same convention as tests/test_options_broker_stop_loss.py.

Highest-priority coverage (per the Swing v2 rewrite plan):
  1. Each basket-type x regime combination resolves to the correct
     instrument/side/transaction-type, INCLUDING the never-before-
     exercised SHORT side (FUTURES+BEARISH).
  2. The equity basket-type is long-only - a bearish regime places zero
     orders and consumes no capacity.
  3. The broker-side SL-L is placed with the correct (direction-aware)
     trigger/limit, and a real square-off finds and cancels it before
     placing its own exit - for BOTH a LONG and a SHORT position (a
     SHORT's resting order is a BUY, not a SELL - get_pending_order_id
     must be scanned with the right side or this silently does nothing).
  4. Partial-fill and fully-filled-during-the-cancel-race reconciliation,
     ported from Options' own proven logic.
  5. A non-TRADED entry result is a FAILED entry (no AMO promotion path
     for entries in this design) and insufficient primary-bucket funds
     blocks the entry with zero orders placed.

HOW TO RUN:
    uv run python tests/test_swing_v2_entry_exit.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_v2_entry_exit_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
from Options.dhan_client import AtmOption, FuturesContract, OrderResult, OrderStatus

import Swing.config as sc
import Swing.signals as signals
import Swing.trading_engine as ste
from Swing.position_store import SwingPositionStore, exit_transaction_type

FUTURE_EXPIRY = date.today() + timedelta(days=25)


async def _fake_get_supertrend_state(symbol):
    """enter_position_for_stock calls signals.get_supertrend_state to
    capture entry_candle_start - MUST be mocked in every test here, or
    it falls through to a REAL Dhan network call (fetch_continuous_
    intraday), which is exactly the class of accidental-real-call risk
    this codebase's test suite has had real incidents from before."""
    return None


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE {option_type}", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"OPTSEC-{symbol}", expiry_date=FUTURE_EXPIRY)


def fake_futures_contract(symbol: str) -> FuturesContract:
    return FuturesContract(trading_symbol=f"{symbol} FUT", security_id=f"FUTSEC-{symbol}",
                            lot_size=250, expiry_date=FUTURE_EXPIRY)


def install_mocks(broker_net_quantity=250, fail_stop_loss_placement=False, entry_fill_status=OrderStatus.TRADED,
                   funds_sufficient=True):
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_futures_contract": odc.dhan_wrapper.get_futures_contract,
        "_equity_instrument_meta": odc.dhan_wrapper._equity_instrument_meta,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "get_pending_order_id": odc.dhan_wrapper.get_pending_order_id,
        "get_broker_net_quantity": odc.dhan_wrapper.get_broker_net_quantity,
        "cancel_order": odc.dhan_wrapper.cancel_order,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "place_equity_market_order": odc.dhan_wrapper.place_equity_market_order,
        "place_stop_loss_limit_order": odc.dhan_wrapper.place_stop_loss_limit_order,
        "place_equity_stop_loss_limit_order": odc.dhan_wrapper.place_equity_stop_loss_limit_order,
        "check_if_order_filled": odc.dhan_wrapper.check_if_order_filled,
        "refresh_order_status": odc.dhan_wrapper.refresh_order_status,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract
    odc.dhan_wrapper._equity_instrument_meta = lambda sym: {"security_id": f"EQSEC-{sym}", "lot_size": 1, "tick_size": 0.05}
    odc.dhan_wrapper.get_option_ltp = lambda ts: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 100.0 if funds_sufficient else 10_000_000.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 1_000_000.0}
    odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type: None
    odc.dhan_wrapper.get_broker_net_quantity = lambda trading_symbol, segment="NSE_FNO": broker_net_quantity
    odc.dhan_wrapper.cancel_order = lambda order_id: None
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_order_status = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=45.0, filled_quantity=broker_net_quantity, is_amo=False)

    placed_orders, stop_loss_calls = [], []

    def _place(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type, "quantity": quantity})
        return {"order_id": order_id, "is_amo": False}

    def _place_sl(trading_symbol, quantity, transaction_type, trigger_price, limit_price, tag=None, product_type=None):
        stop_loss_calls.append({"trading_symbol": trading_symbol, "quantity": quantity, "transaction_type": transaction_type,
                                 "trigger_price": trigger_price, "limit_price": limit_price})
        if fail_stop_loss_placement:
            raise RuntimeError("simulated SL-L placement failure")
        return {"order_id": f"FAKE-SL-{trading_symbol}"}

    odc.dhan_wrapper.place_market_order = _place
    odc.dhan_wrapper.place_equity_market_order = _place
    odc.dhan_wrapper.place_stop_loss_limit_order = _place_sl
    odc.dhan_wrapper.place_equity_stop_loss_limit_order = _place_sl
    odc.dhan_wrapper.check_if_order_filled = lambda order_id: None
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=entry_fill_status, remark="", fill_price=50.0, filled_quantity=broker_net_quantity, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore, placed_orders, stop_loss_calls


def _set(basket_type, gap_multiple=0.05, broker_stop=False, cap=1500.0, max_concurrent=2, funds_buffer=0.0):
    sc.BASKET_TYPE = basket_type
    sc.BROKER_STOP_LOSS_ENABLED = broker_stop
    sc.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE = gap_multiple
    sc.MAX_LOSS_PROTECTION_RS = cap
    sc.MAX_CONCURRENT_TRADES = max_concurrent
    sc.FUNDS_CHECK_BUFFER_RS = funds_buffer
    ste.position_store.__init__()  # fresh store per test
    ste.signals.get_supertrend_state = _fake_get_supertrend_state


async def test_1_futures_bearish_shorts_to_open():
    _set("futures")
    restore, placed, _ = install_mocks()
    try:
        result = await ste.enter_position_for_stock("RELIANCE", "BEARISH")
        assert result["status"] == "entered", result
        pos = ste.position_store.live_positions["RELIANCE"]
        assert pos.instrument_side == "SHORT", pos
        assert placed[0]["transaction_type"] == "SELL", "a FUTURES+BEARISH entry must SELL to open (short), not BUY"
        assert pos.target_price < pos.entry_price, "a SHORT's target must be below entry"
        assert pos.hard_stop_loss > pos.entry_price, "a SHORT's hard stop must be above entry"
        print("1. FUTURES+BEARISH entry correctly SHORTS to open (SELL), target below/stop above entry: PASSED")
    finally:
        restore()


async def test_2_options_bearish_buys_pe_long():
    _set("options")
    restore, placed, _ = install_mocks()
    try:
        result = await ste.enter_position_for_stock("RELIANCE", "BEARISH")
        assert result["status"] == "entered", result
        pos = ste.position_store.live_positions["RELIANCE"]
        assert pos.instrument_side == "LONG", "a PE position is itself always entered LONG"
        assert pos.resolved_option_type == "PE"
        assert placed[0]["transaction_type"] == "BUY"
        assert "PE" in placed[0]["trading_symbol"]
        print("2. OPTIONS+BEARISH entry buys a PE (LONG side), not a short: PASSED")
    finally:
        restore()


async def test_3_equity_bearish_is_skipped_entirely():
    _set("equity")
    restore, placed, _ = install_mocks()
    try:
        result = await ste.enter_position_for_stock("RELIANCE", "BEARISH")
        assert result["status"] == "skipped" and result["reason"] == "equity_long_only", result
        assert len(placed) == 0, "no order should ever be placed for an equity+bearish combination"
        assert "RELIANCE" not in ste.position_store.reserved_symbols, \
            "the skip must happen BEFORE reserve_symbol - no capacity should be consumed"
        print("3. EQUITY+BEARISH is skipped entirely - zero orders, zero capacity consumed: PASSED")
    finally:
        restore()


async def test_4_equity_bullish_buys_shares_via_equity_order_path():
    _set("equity")
    restore, placed, _ = install_mocks()
    try:
        result = await ste.enter_position_for_stock("RELIANCE", "BULLISH")
        assert result["status"] == "entered", result
        pos = ste.position_store.live_positions["RELIANCE"]
        assert pos.exchange_segment == "NSE_EQ" and pos.product_type == sc.EQUITY_PRODUCT
        assert placed[0]["quantity"] == sc.EQUITY_QUANTITY
        print("4. EQUITY+BULLISH buys EQUITY_QUANTITY shares via the equity order path: PASSED")
    finally:
        restore()


async def test_5_capacity_at_cap_ten_concurrent_claims_two_winners():
    _set("options", max_concurrent=2)
    restore, _, _ = install_mocks()
    try:
        results = await asyncio.gather(*[ste.position_store.reserve_symbol(f"SYM{i}") for i in range(10)])
        assert sum(results) == 2, f"expected exactly 2 winners against MAX_CONCURRENT_TRADES=2, got {sum(results)}"
        print("5. 10 concurrent reserve_symbol() callers against MAX_CONCURRENT_TRADES=2 yield exactly 2 winners: PASSED")
    finally:
        restore()


async def test_6_insufficient_funds_blocks_entry():
    _set("options")
    restore, placed, _ = install_mocks(funds_sufficient=False)
    try:
        result = await ste.enter_position_for_stock("RELIANCE", "BULLISH")
        assert result["status"] == "skipped" and result["reason"] == "insufficient_funds", result
        assert len(placed) == 0
        assert "RELIANCE" not in ste.position_store.reserved_symbols
        print("6. Insufficient primary-bucket funds blocks the entry - zero orders, symbol released: PASSED")
    finally:
        restore()


async def test_7_non_traded_fill_is_a_failed_entry_not_a_promotion():
    _set("options")
    restore, placed, _ = install_mocks(entry_fill_status=OrderStatus.PENDING)
    try:
        result = await ste.enter_position_for_stock("RELIANCE", "BULLISH")
        assert result["status"] == "failed", result
        assert "RELIANCE" not in ste.position_store.live_positions
        assert "RELIANCE" not in ste.position_store.reserved_symbols, "a failed entry must release the symbol"
        print("7. A non-TRADED entry result is a FAILED entry (no AMO promotion path), symbol released: PASSED")
    finally:
        restore()


async def test_8_broker_sl_placed_with_correct_trigger_limit_long_and_short():
    for basket_type, regime, expect_side in [("futures", "BULLISH", "LONG"), ("futures", "BEARISH", "SHORT")]:
        _set(basket_type, broker_stop=True, cap=1500.0, gap_multiple=0.05)
        restore, placed, sl_calls = install_mocks()
        try:
            result = await ste.enter_position_for_stock("RELIANCE", regime)
            assert result["status"] == "entered", result
            pos = ste.position_store.live_positions["RELIANCE"]
            assert pos.instrument_side == expect_side
            assert len(sl_calls) == 1, sl_calls
            call = sl_calls[0]
            entry = pos.entry_price
            if expect_side == "LONG":
                assert call["trigger_price"] < entry and call["limit_price"] < call["trigger_price"], call
                assert call["transaction_type"] == "SELL"
            else:
                assert call["trigger_price"] > entry and call["limit_price"] > call["trigger_price"], call
                assert call["transaction_type"] == "BUY", "a SHORT's resting stop must be a BUY order"
            assert pos.stop_loss_order_id == f"FAKE-SL-{call['trading_symbol']}"
        finally:
            restore()
    print("8. Broker-side SL-L trigger/limit are correctly direction-aware for both LONG and SHORT: PASSED")


async def test_9_squareoff_cancels_resting_sl_before_placing_its_own_exit():
    for basket_type, regime, expect_exit_side in [("futures", "BULLISH", "SELL"), ("futures", "BEARISH", "BUY")]:
        _set(basket_type, broker_stop=True)
        restore, placed, sl_calls = install_mocks(broker_net_quantity=250)  # nothing partially filled
        cancelled = []
        try:
            odc.dhan_wrapper.cancel_order = lambda order_id: cancelled.append(order_id)
            odc.dhan_wrapper.get_pending_order_id = lambda ts, tt: (sl_calls[-1]["trading_symbol"] if tt == expect_exit_side else None)

            result = await ste.enter_position_for_stock("RELIANCE", regime)
            assert result["status"] == "entered", result
            pos = ste.position_store.live_positions["RELIANCE"]
            sl_order_id = pos.stop_loss_order_id
            assert await ste.position_store.try_start_exit("RELIANCE")
            await ste._exit_position("RELIANCE", pos, exit_price=pos.entry_price, reason="TARGET_HIT")

            assert len(cancelled) == 1, "the resting SL-L must be cancelled exactly once before the fresh exit"
            exit_orders = [o for o in placed if o["transaction_type"] == expect_exit_side]
            assert len(exit_orders) >= 1, f"expected a {expect_exit_side} exit order, got {placed}"
            assert "RELIANCE" not in ste.position_store.live_positions, "position must be closed after the exit"
        finally:
            restore()
    print("9. A square-off finds and cancels the resting broker-side SL-L (via the order-book scan, correct "
          "side for both LONG and SHORT) before placing its own exit: PASSED")


async def test_10_squareoff_cancel_falls_back_to_stored_stop_loss_order_id():
    """Simulates the exact OMS-lag gap that orphaned a real Luxury SL-L on
    10 Sep 2026: get_pending_order_id's scan misses the just-placed
    order, so _exit_position must fall back to position.stop_loss_order_id."""
    _set("futures", broker_stop=True)
    restore, placed, sl_calls = install_mocks(broker_net_quantity=250)
    cancelled = []
    try:
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled.append(order_id)
        odc.dhan_wrapper.get_pending_order_id = lambda ts, tt: None  # scan misses it every time

        result = await ste.enter_position_for_stock("RELIANCE", "BULLISH")
        pos = ste.position_store.live_positions["RELIANCE"]
        assert pos.stop_loss_order_id is not None
        assert await ste.position_store.try_start_exit("RELIANCE")
        await ste._exit_position("RELIANCE", pos, exit_price=pos.entry_price, reason="TARGET_HIT")

        assert cancelled == [pos.stop_loss_order_id], \
            f"expected the fallback to cancel the stored stop_loss_order_id, got {cancelled}"
        print("10. When get_pending_order_id's scan misses the resting SL-L (OMS lag), _exit_position falls "
              "back to the stored stop_loss_order_id and still cancels it: PASSED")
    finally:
        restore()


async def test_11_partial_fill_reconciliation_on_squareoff():
    _set("futures", broker_stop=True)
    restore, placed, sl_calls = install_mocks(broker_net_quantity=100)  # broker shows only 100 of 250 left
    try:
        odc.dhan_wrapper.cancel_order = lambda order_id: None
        result = await ste.enter_position_for_stock("RELIANCE", "BULLISH")
        pos = ste.position_store.live_positions["RELIANCE"]
        odc.dhan_wrapper.get_pending_order_id = lambda ts, tt: pos.stop_loss_order_id if tt == "SELL" else None

        assert await ste.position_store.try_start_exit("RELIANCE")
        await ste._exit_position("RELIANCE", pos, exit_price=pos.entry_price, reason="TARGET_HIT")

        sell_orders = [o for o in placed if o["transaction_type"] == "SELL" and o is not placed[0]]
        assert sell_orders and sell_orders[-1]["quantity"] == 100, \
            f"the fresh exit must sell only the real remaining 100, not the stale 250: {sell_orders}"
        print("11. A partial fill on the resting SL-L is reconciled - the fresh exit sells only the real "
              "remainder, never the stale stored quantity: PASSED")
    finally:
        restore()


async def test_12_fully_filled_during_cancel_race_closes_with_no_fresh_order():
    _set("futures", broker_stop=True)
    restore, placed, sl_calls = install_mocks(broker_net_quantity=0)  # broker shows fully flat already
    try:
        odc.dhan_wrapper.cancel_order = lambda order_id: None
        result = await ste.enter_position_for_stock("RELIANCE", "BULLISH")
        pos = ste.position_store.live_positions["RELIANCE"]
        n_orders_before_exit = len(placed)
        odc.dhan_wrapper.get_pending_order_id = lambda ts, tt: pos.stop_loss_order_id if tt == "SELL" else None

        assert await ste.position_store.try_start_exit("RELIANCE")
        await ste._exit_position("RELIANCE", pos, exit_price=pos.entry_price, reason="TARGET_HIT")

        assert len(placed) == n_orders_before_exit, "no fresh exit order should be placed when the broker already shows flat"
        assert "RELIANCE" not in ste.position_store.live_positions
        closed = ste.position_store.closed_positions_today[-1]
        assert closed.exit_price == 45.0, "must close at the stale order's own real fill price (45.0 from refresh_order_status)"
        print("12. A resting SL-L that fully fills during the cancel race closes the position at its own "
              "real fill price, with NO fresh exit order placed: PASSED")
    finally:
        restore()


async def main():
    print("=== Swing v2 entry/exit/broker-stop-loss integration test suite ===\n")
    await test_1_futures_bearish_shorts_to_open()
    await test_2_options_bearish_buys_pe_long()
    await test_3_equity_bearish_is_skipped_entirely()
    await test_4_equity_bullish_buys_shares_via_equity_order_path()
    await test_5_capacity_at_cap_ten_concurrent_claims_two_winners()
    await test_6_insufficient_funds_blocks_entry()
    await test_7_non_traded_fill_is_a_failed_entry_not_a_promotion()
    await test_8_broker_sl_placed_with_correct_trigger_limit_long_and_short()
    await test_9_squareoff_cancels_resting_sl_before_placing_its_own_exit()
    await test_10_squareoff_cancel_falls_back_to_stored_stop_loss_order_id()
    await test_11_partial_fill_reconciliation_on_squareoff()
    await test_12_fully_filled_during_cancel_race_closes_with_no_fresh_order()
    print("\nALL SWING V2 ENTRY/EXIT/BROKER-STOP-LOSS CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
