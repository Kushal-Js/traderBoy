"""
Tests for Swing's Chartink entry webhook gaining REAL signal evaluation +
ranking - user request 7 Sep 2026 (verbatim): "I need a chartink webhook
to be directly integrated in Swing strategy bot which can continuously
sends json feeds and bot should consume them over for placing order
using data send from webhook url. The bot should calculate best stock to
place trade upon based on conditions already predefined in our code
under 'Swing' strategy."

POST /chartink/webhook-swing-enter already existed (built earlier) but,
per its own original docstring, entered EVERY stock in the payload
directly with NO evaluation at all - "the decision of WHICH stock to
send and WHEN lives outside the bot." This closes that exact gap: the
webhook now runs each incoming stock through Swing's own REAL entry
signal (_evaluate_watchlist_entry_signal - the identical price-
confirmation + dual-timeframe Supertrend crossover check monitor_loop's
own tick already uses) and ranks whichever ones qualify
(_rank_and_enter_candidates - freshest crossover, higher volume as the
tiebreak, the SAME ranking already used to pick among several watchlist
symbols firing in the same tick) before attempting entry - so an alert
listing several stocks results in the BEST-qualifying one(s) actually
being traded, not every single one blindly.

New shared helper: Swing/trading_engine.py's own _rank_and_enter_
candidates - a NEW, standalone function (not a refactor of the already-
live monitor ticks, to avoid any regression risk to the currently-
deployed real-money monitor loop) built from the same trusted, already-
tested primitives those ticks use.

Covers, against the REAL production functions (not reimplemented):
  1. _rank_and_enter_candidates only attempts entry for candidates whose
     REAL entry signal actually fires - a non-qualifying candidate is
     never even ranked, let alone entered.
  2. When multiple candidates qualify and capacity is scarce, the
     FRESHEST crossover wins (same ranking basket_hedge_monitor_tick
     already uses) - proving this new function's own ranking is
     genuinely wired to the same _entry_candidate_rank_key, not
     reimplemented.
  3. remove_from_watchlist_on_entry=True (the default, basket/
     basket_hedge's own behavior) removes a winner from the watchlist;
     =False (sequential's own behavior) leaves it there.
  4. Mode dispatch - the SAME candidate list is routed to
     enter_basket_for_stock/_enter_basket_hedge_for_stock/
     _enter_futures_for_stock depending on config.STRATEGY_MODE.
  5. Full webhook-level integration through the REAL chartink_webhook_
     swing_enter: a payload with a mix of qualifying and non-qualifying
     stocks reports BOTH correctly (entered vs skipped/entry_signal_not_
     confirmed) - every stock in the alert is accounted for in the
     response, not just the winners; STRATEGY_ENABLED=False and zero
     capacity both still short-circuit before any signal is even
     evaluated, exactly as before this change.

HOW TO RUN:
    uv run python tests/test_swing_chartink_entry_webhook.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_chartink_entry_webhook_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Swing.position_store as sps
import Swing.trading_engine as ste
import Swing.swing_main as sm
import Swing.watchlist as swl
from Options.dhan_client import AtmOption, FuturesContract, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP PUT", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"OPT-{symbol}", expiry_date=FUTURE_EXPIRY)


def fake_futures_contract(symbol: str) -> FuturesContract:
    return FuturesContract(trading_symbol=f"{symbol} FAKE EXP FUT", security_id=f"FUT-{symbol}",
                            lot_size=250, expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks(fill_prices=None):
    fill_prices = list(fill_prices or [])
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_futures_contract": odc.dhan_wrapper.get_futures_contract,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "has_open_position_for_underlying": odc.dhan_wrapper.has_open_position_for_underlying,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract
    odc.dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 100000.0}

    placed_orders = []
    fills_iter = iter(fill_prices)

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type})
        return {"order_id": order_id, "is_amo": False}

    def fake_wait_for_order_result(order_id, is_amo=False):
        fill_price = next(fills_iter, 50.0)
        return OrderResult(order_id=order_id, status=OrderStatus.TRADED, remark="",
                            fill_price=fill_price, filled_quantity=1, is_amo=False)

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.wait_for_order_result = fake_wait_for_order_result

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore, placed_orders


def _entry_state(candle_start, volume=100.0) -> ste.SupertrendState:
    return ste.SupertrendState(candle_start=candle_start, close=110.0, supertrend=100.0, is_above=True,
                                prev_close=95.0, prev_supertrend=100.0, prev_is_above=False, volume=volume)


def _confirm_state() -> ste.SupertrendState:
    return ste.SupertrendState(candle_start=datetime.now(ste.IST), close=10.0, supertrend=9.0, is_above=True,
                                prev_close=9.5, prev_supertrend=9.0, prev_is_above=True, volume=0.0)


def install_fake_signal_fetch(entry_states: dict):
    """entry_states: {symbol: SupertrendState or None} for the ENTRY
    timeframe. A symbol mapped to None (or simply absent) never
    qualifies - price confirmation is faked True only for symbols
    present with a non-None state, so a real _evaluate_watchlist_entry_
    signal call genuinely returns False for anything not in this dict,
    through the REAL production function, not a stubbed shortcut."""
    real_fetch = ste._fetch_supertrend_state
    real_price_confirmed = ste._is_price_confirmed_above_prev_close

    async def fake_fetch(symbol, interval_minutes):
        state = entry_states.get(symbol)
        if state is None:
            return None
        if interval_minutes == ste.config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES:
            return state
        return _confirm_state()

    async def fake_price_confirmed(symbol):
        return entry_states.get(symbol) is not None

    ste._fetch_supertrend_state = fake_fetch
    ste._is_price_confirmed_above_prev_close = fake_price_confirmed

    def restore():
        ste._fetch_supertrend_state = real_fetch
        ste._is_price_confirmed_above_prev_close = real_price_confirmed
    return restore


async def test_1_non_qualifying_candidate_is_never_entered():
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store

    real_enabled, real_mode = ste.config.STRATEGY_ENABLED, ste.config.STRATEGY_MODE
    ste.config.STRATEGY_ENABLED = True
    ste.config.STRATEGY_MODE = "basket_hedge"
    restore_signal = install_fake_signal_fetch({"QUALIFIES": _entry_state(datetime.now(ste.IST))})
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        results = await ste._rank_and_enter_candidates(["QUALIFIES", "DOESNOTQUALIFY"])
        symbols_attempted = {r["symbol"] for r in results}
        assert symbols_attempted == {"QUALIFIES"}, \
            f"a non-qualifying candidate must never even be ranked/attempted, got {symbols_attempted}"
        assert results[0]["status"] == "entered"
        assert "QUALIFIES" in store.live_positions
        assert "DOESNOTQUALIFY" not in store.live_positions

        entered_symbols = {o["trading_symbol"].split(" ")[0] for o in placed_orders}
        assert entered_symbols == {"QUALIFIES"}, f"zero orders for the non-qualifying candidate, got {placed_orders}"
        print("1. _rank_and_enter_candidates only attempts entry for a candidate whose REAL entry "
              "signal actually fires - a non-qualifying one is never even ranked: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED, ste.config.STRATEGY_MODE = real_enabled, real_mode


async def test_2_freshest_crossover_wins_when_capacity_is_scarce():
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["STALESTOCK", "FRESHSTOCK"])

    real_enabled, real_mode, real_cap = ste.config.STRATEGY_ENABLED, ste.config.STRATEGY_MODE, ste.config.MAX_LIVE_BASKETS
    ste.config.STRATEGY_ENABLED = True
    ste.config.STRATEGY_MODE = "basket_hedge"
    ste.config.MAX_LIVE_BASKETS = 1
    older = datetime(2026, 9, 1, 10, 5, tzinfo=ste.IST)
    newer = datetime(2026, 9, 1, 10, 10, tzinfo=ste.IST)
    restore_signal = install_fake_signal_fetch({
        "STALESTOCK": _entry_state(older, volume=999999.0),
        "FRESHSTOCK": _entry_state(newer, volume=1.0),
    })
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        results = await ste._rank_and_enter_candidates(["STALESTOCK", "FRESHSTOCK"])
        entered = [r for r in results if r["status"] == "entered"]
        assert len(entered) == 1 and entered[0]["symbol"] == "FRESHSTOCK", \
            f"the FRESHER crossover must win the single available slot, got {results}"
        assert "FRESHSTOCK" in store.live_positions
        assert "STALESTOCK" not in store.live_positions
        assert "STALESTOCK" in await wl_store.symbols(), "the loser stays on the watchlist for a later tick"
        print("2. When multiple candidates qualify and capacity is scarce, the FRESHEST crossover "
              "wins - this new function's own ranking is genuinely wired to the SAME "
              "_entry_candidate_rank_key the monitor ticks already use: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED, ste.config.STRATEGY_MODE, ste.config.MAX_LIVE_BASKETS = real_enabled, real_mode, real_cap


async def test_3_remove_from_watchlist_flag_controls_watchlist_removal():
    store = sps.SequentialPositionStore()
    ste.sequential_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["LOOPSTOCK"])

    real_enabled, real_mode = ste.config.STRATEGY_ENABLED, ste.config.STRATEGY_MODE
    ste.config.STRATEGY_ENABLED = True
    ste.config.STRATEGY_MODE = "sequential"
    restore_signal = install_fake_signal_fetch({"LOOPSTOCK": _entry_state(datetime.now(ste.IST))})
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0])
    try:
        results = await ste._rank_and_enter_candidates(["LOOPSTOCK"], remove_from_watchlist_on_entry=False)
        assert results[0]["status"] == "entered", results
        assert "LOOPSTOCK" in await wl_store.symbols(), \
            "sequential mode's own remove_from_watchlist_on_entry=False must leave it on the watchlist " \
            "(it still needs continuous evaluation for its own FUTURES<->PE loop)"
        print("3. remove_from_watchlist_on_entry=False (sequential mode's own behavior) leaves an "
              "entered symbol on the watchlist, unlike the True default: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED, ste.config.STRATEGY_MODE = real_enabled, real_mode


async def test_4_mode_dispatch_routes_to_the_right_real_entry_function():
    basket_store = sps.BasketStore()
    ste.basket_store = basket_store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store

    real_enabled, real_mode = ste.config.STRATEGY_ENABLED, ste.config.STRATEGY_MODE
    ste.config.STRATEGY_ENABLED = True
    ste.config.STRATEGY_MODE = "basket"
    restore_signal = install_fake_signal_fetch({"BASKETSTOCK": _entry_state(datetime.now(ste.IST))})
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        results = await ste._rank_and_enter_candidates(["BASKETSTOCK"])
        assert results[0]["status"] == "entered", results
        assert "BASKETSTOCK" in basket_store.live_baskets, \
            "plain 'basket' mode must route through enter_basket_for_stock into basket_store, not some other store"
        print("4. Mode dispatch correctly routes 'basket' mode's own candidates through "
              "enter_basket_for_stock into basket_store: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED, ste.config.STRATEGY_MODE = real_enabled, real_mode


async def test_5_full_webhook_reports_both_qualifying_and_non_qualifying_stocks():
    store = sps.BasketHedgeStore()
    sm.basket_hedge_store = store
    ste.basket_hedge_store = store
    wl_store = swl.WatchlistStore()
    sm.watchlist_store = wl_store
    ste.watchlist_store = wl_store

    real_enabled, real_mode = ste.config.STRATEGY_ENABLED, ste.config.STRATEGY_MODE
    ste.config.STRATEGY_ENABLED = sm.config.STRATEGY_ENABLED = True
    ste.config.STRATEGY_MODE = "basket_hedge"
    restore_signal = install_fake_signal_fetch({"WINNER": _entry_state(datetime.now(ste.IST))})
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        payload = sm.SwingWebhookPayload(stocks="WINNER,LOSER", alert_name="entry-webhook-test-5")
        result = await sm.chartink_webhook_swing_enter(payload)
        assert result["status"] == "processed", result

        by_symbol = {e["symbol"]: e for e in result["entries"]}
        assert by_symbol["WINNER"]["status"] == "entered", by_symbol
        assert by_symbol["LOSER"]["status"] == "skipped", by_symbol
        assert by_symbol["LOSER"]["reason"] == "entry_signal_not_confirmed", by_symbol
        assert "WINNER" in store.live_positions
        assert "LOSER" not in store.live_positions

        print("5. The full real webhook reports BOTH the entered winner AND the non-qualifying "
              "stock (entry_signal_not_confirmed) - every stock in the alert is accounted for, "
              "not just the winners: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED = sm.config.STRATEGY_ENABLED = real_enabled
        ste.config.STRATEGY_MODE = real_mode


async def test_6_strategy_disabled_short_circuits_before_any_signal_check():
    real_enabled = ste.config.STRATEGY_ENABLED
    sm.config.STRATEGY_ENABLED = ste.config.STRATEGY_ENABLED = False
    restore_dhan, placed_orders = install_all_dhan_mocks()
    try:
        payload = sm.SwingWebhookPayload(stocks="ANYSTOCK", alert_name="entry-webhook-test-6")
        result = await sm.chartink_webhook_swing_enter(payload)
        assert result["status"] == "ignored" and result["reason"] == "strategy_disabled", result
        assert placed_orders == []
        print("6. STRATEGY_ENABLED=False still short-circuits immediately, unchanged from before "
              "this webhook gained real signal evaluation: PASSED")
    finally:
        restore_dhan()
        sm.config.STRATEGY_ENABLED = ste.config.STRATEGY_ENABLED = real_enabled


async def test_7_zero_capacity_short_circuits_before_any_signal_check():
    store = sps.BasketHedgeStore()
    sm.basket_hedge_store = store
    real_enabled, real_mode, real_cap = ste.config.STRATEGY_ENABLED, ste.config.STRATEGY_MODE, ste.config.MAX_LIVE_BASKETS
    sm.config.STRATEGY_ENABLED = ste.config.STRATEGY_ENABLED = True
    sm.config.STRATEGY_MODE = ste.config.STRATEGY_MODE = "basket_hedge"
    sm.config.MAX_LIVE_BASKETS = ste.config.MAX_LIVE_BASKETS = 0
    restore_dhan, placed_orders = install_all_dhan_mocks()
    try:
        payload = sm.SwingWebhookPayload(stocks="ANYSTOCK", alert_name="entry-webhook-test-7")
        result = await sm.chartink_webhook_swing_enter(payload)
        assert result["status"] == "ignored" and result["reason"] == "max_live_baskets_reached", result
        assert placed_orders == []
        print("7. Zero capacity still short-circuits immediately before any signal is evaluated, "
              "unchanged from before this webhook gained real signal evaluation: PASSED")
    finally:
        restore_dhan()
        sm.config.STRATEGY_ENABLED, ste.config.STRATEGY_ENABLED = real_enabled, real_enabled
        sm.config.STRATEGY_MODE, ste.config.STRATEGY_MODE = real_mode, real_mode
        sm.config.MAX_LIVE_BASKETS, ste.config.MAX_LIVE_BASKETS = real_cap, real_cap


async def main():
    print("=== Swing Chartink entry webhook (real signal evaluation + ranking) test suite ===\n")
    await test_1_non_qualifying_candidate_is_never_entered()
    await test_2_freshest_crossover_wins_when_capacity_is_scarce()
    await test_3_remove_from_watchlist_flag_controls_watchlist_removal()
    await test_4_mode_dispatch_routes_to_the_right_real_entry_function()
    await test_5_full_webhook_reports_both_qualifying_and_non_qualifying_stocks()
    await test_6_strategy_disabled_short_circuits_before_any_signal_check()
    await test_7_zero_capacity_short_circuits_before_any_signal_check()
    print("\nALL SWING CHARTINK ENTRY WEBHOOK CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
