"""
Tests for Swing's durable event log (added 1 Sep 2026, user request:
"keep an eye on first entry and log everything under history folder for
'Swing' package also," made right after Swing's real trading went live).

Closes the same "only in journald, limited retention, not queryable" gap
`trade_history.record_webhook_alert` already closed for incoming alerts -
`position_opened`/`real_trades` already capture the bare entry/exit price
per LEG; this new `history/<date>_swing_events.log`
(trading_engine.SWING_EVENTS_LOG_NAME) captures the higher-level STATE
TRANSITION and its own reasoning (which action fired, and why) in one
row per event, for both basket mode (BASKET_ENTERED/BASKET_EXITED) and
sequential mode (SEQUENTIAL_ENTERED_FUTURES/_SWAPPED_TO_PE/
_SWAPPED_TO_FUTURES/_EXITED_TO_WATCHING/_LEFT_FLAT).

Covers, against the REAL production functions (not reimplemented):
  1. A full real sequential loop (enter -> swap to PE -> swap back to
     futures -> exit to watching) produces exactly the 4 expected events,
     in order, each carrying the right reasoning/price detail.
  2. Basket mode's own entry/exit produce BASKET_ENTERED/BASKET_EXITED.
  3. A failure mid-swap (PE leg can't be resolved after futures is
     already sold) still produces a SEQUENTIAL_LEFT_FLAT event - the
     failure path is not silent, matching the "silence is not success"
     principle for anything that changes real capital exposure.
  4. Every event's own `logged_at`/`strategy_mode` fields are present,
     and events round-trip through the real on-disk log via
     read_all_jsonl, not just the in-memory call.

HOW TO RUN:
    uv run python tests/test_swing_events_log.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_events_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Swing.position_store as sps
import Swing.trading_engine as ste
from Options.dhan_client import AtmOption, FuturesContract, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP PUT", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"OPT-{symbol}", expiry_date=FUTURE_EXPIRY)


def fake_futures_contract(symbol: str) -> FuturesContract:
    return FuturesContract(trading_symbol=f"{symbol} FAKE EXP FUT", security_id=f"FUT-{symbol}",
                            lot_size=250, expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks(fill_prices=None, fail_atm_option=False):
    fill_prices = list(fill_prices or [])
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_futures_contract": odc.dhan_wrapper.get_futures_contract,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }

    def maybe_fake_atm_option(symbol, option_type):
        if fail_atm_option:
            raise ValueError("simulated ATM lookup failure")
        return fake_atm_option(symbol, option_type)

    odc.dhan_wrapper.get_atm_option = maybe_fake_atm_option
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None

    fills_iter = iter(fill_prices)

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        import time
        return {"order_id": f"FAKE-{trading_symbol}-{transaction_type}-{time.time_ns()}", "is_amo": False}

    def fake_wait_for_order_result(order_id, is_amo=False):
        fill_price = next(fills_iter, 50.0)
        return OrderResult(order_id=order_id, status=OrderStatus.TRADED, remark="",
                            fill_price=fill_price, filled_quantity=1, is_amo=False)

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.wait_for_order_result = fake_wait_for_order_result

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


async def test_1_full_sequential_loop_produces_expected_events_in_order():
    store = sps.SequentialPositionStore()
    ste.sequential_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    real_cap = ste.config.MAX_LIVE_BASKETS
    real_pe_cap = ste.config.PE_MAX_LOSS_RS
    ste.config.STRATEGY_ENABLED = True
    ste.config.MAX_LIVE_BASKETS = 10
    ste.config.PE_MAX_LOSS_RS = 2000.0
    symbol = "RELIANCE"
    restore = install_all_dhan_mocks(fill_prices=[100.0, 110.0, 20.0, 15.0, 112.0])
    try:
        # NONE -> FUTURES
        result = await ste._enter_futures_for_stock(symbol)
        assert result["status"] == "entered", result

        # FUTURES -> PE
        leg = store.live_legs[symbol]
        await ste._swap_futures_to_pe(symbol, leg)
        assert store.live_legs[symbol].option_type == "PE"

        # PE -> FUTURES (loop continues)
        pe_leg = store.live_legs[symbol]
        await ste._swap_pe_to_futures(symbol, pe_leg)
        assert store.live_legs[symbol].option_type == "FUT"

        # FUTURES -> ... -> exit to watching, via the manual square-off path
        # (exercises the OTHER event-emitting call site, not just the
        # loop-continuation swap functions).
        await ste._square_off_all("MANUAL_SQUARE_OFF")
        assert symbol not in store.live_legs

        await asyncio.sleep(0.3)
        events = [e for e in trade_history.read_all_jsonl(ste.SWING_EVENTS_LOG_NAME)
                  if e["underlying_symbol"] == symbol]
        event_names = [e["event"] for e in events]
        assert event_names == [
            "SEQUENTIAL_ENTERED_FUTURES", "SEQUENTIAL_SWAPPED_TO_PE",
            "SEQUENTIAL_SWAPPED_TO_FUTURES", "SEQUENTIAL_EXITED_TO_WATCHING",
        ], event_names

        assert events[0]["trading_symbol"] == "RELIANCE FAKE EXP FUT"
        assert events[0]["entry_price"] == 100.0
        assert events[1]["exit_reason"] == "SUPERTREND_5MIN_EXIT"
        assert events[1]["pe_trading_symbol"] == "RELIANCE FAKE EXP PUT"
        assert events[2]["exit_reason"] == "ENTRY_SIGNAL_REFIRED"
        assert events[3]["exit_reason"] == "MANUAL_SQUARE_OFF"
        assert all("logged_at" in e and "strategy_mode" in e for e in events)
        assert all(e["strategy_mode"] == "sequential" for e in events)

        print("1. A full real sequential loop (enter -> swap to PE -> swap back to futures -> "
              "manual square-off) produces exactly the 4 expected durable events, in order, each "
              "carrying the right reasoning: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap
        ste.config.PE_MAX_LOSS_RS = real_pe_cap


async def test_2_basket_mode_entry_and_exit_events():
    store = sps.BasketStore()
    ste.basket_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.STRATEGY_ENABLED = True
    ste.config.MAX_LIVE_BASKETS = 10
    symbol = "TCS"
    restore = install_all_dhan_mocks(fill_prices=[200.0, 30.0, 210.0, 25.0])
    try:
        result = await ste.enter_basket_for_stock(symbol)
        assert result["status"] == "entered", result
        basket = store.live_baskets[symbol]
        await ste._exit_basket(symbol, basket, "SUPERTREND_5MIN_EXIT")

        await asyncio.sleep(0.3)
        events = [e for e in trade_history.read_all_jsonl(ste.SWING_EVENTS_LOG_NAME)
                  if e["underlying_symbol"] == symbol]
        event_names = [e["event"] for e in events]
        assert event_names == ["BASKET_ENTERED", "BASKET_EXITED"], event_names
        assert events[0]["futures_entry_price"] == 200.0
        assert events[0]["option_entry_price"] == 30.0
        assert events[1]["exit_reason"] == "SUPERTREND_5MIN_EXIT"
        assert events[1]["futures_sell_ok"] is True and events[1]["option_sell_ok"] is True

        print("2. Basket mode's own entry/exit produce BASKET_ENTERED/BASKET_EXITED durable "
              "events with the right detail: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap


async def test_3_left_flat_event_on_mid_swap_failure():
    store = sps.SequentialPositionStore()
    ste.sequential_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.STRATEGY_ENABLED = True
    ste.config.MAX_LIVE_BASKETS = 10
    symbol = "SBIN"
    restore = install_all_dhan_mocks(fill_prices=[100.0, 105.0])
    try:
        await ste._enter_futures_for_stock(symbol)
        leg = store.live_legs[symbol]
    finally:
        restore()

    # Now force the ATM PE lookup to fail during the swap.
    restore = install_all_dhan_mocks(fill_prices=[105.0], fail_atm_option=True)
    try:
        await ste._swap_futures_to_pe(symbol, leg)
        assert symbol not in store.live_legs, "must be left flat (no leg held) after the failed swap"
        assert symbol not in store.reserved_symbols

        await asyncio.sleep(0.3)
        events = [e for e in trade_history.read_all_jsonl(ste.SWING_EVENTS_LOG_NAME)
                  if e["underlying_symbol"] == symbol]
        assert any(e["event"] == "SEQUENTIAL_LEFT_FLAT" for e in events), events
        left_flat = next(e for e in events if e["event"] == "SEQUENTIAL_LEFT_FLAT")
        assert left_flat["reason"] == "pe_lookup_failed_after_futures_sold"
        print("3. A failure mid-swap (PE leg unresolvable after futures already sold) still "
              "produces a durable SEQUENTIAL_LEFT_FLAT event - not silent: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap


async def main():
    print("=== Swing durable events log test suite ===\n")
    await test_1_full_sequential_loop_produces_expected_events_in_order()
    await test_2_basket_mode_entry_and_exit_events()
    await test_3_left_flat_event_on_mid_swap_failure()
    print("\nALL SWING EVENTS LOG CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
