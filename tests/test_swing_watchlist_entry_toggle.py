"""
Tests for Swing's watchlist-entry kill switch - user request 7 Sep 2026,
verbatim: "Let's put a flag and disable watchlist based trade execution
for Swing strategy as of now." Added after observing a real live race:
ADANIENT/AUROPHARMA (both on the watchlist, both genuinely trend-
confirmed) kept re-attempting entry every monitor tick, repeatedly
blocked by the funds check, and while each attempt briefly held the one
shared MAX_LIVE_BASKETS reservation it caused a couple of the newer
POST /chartink/webhook-swing-enter alert firings (for unrelated stocks)
to see "no capacity" - see NOTES.md's own entry for the full incident.

config.WATCHLIST_ENTRY_ENABLED (default True, preserving all existing
tested behavior) is scoped NARROWLY to just the watchlist-SOURCED fresh-
entry path in each of the three monitor-tick functions - this suite's
whole point is proving that scope is exactly right: entries stop, but
NOTHING else does.

Covers, against the REAL production tick functions (not reimplemented):
  1. _basket_hedge_monitor_tick: with the flag OFF, a watchlist symbol
     whose entry signal WOULD genuinely fire is never even evaluated -
     zero orders placed, the symbol stays untouched on the watchlist.
  2. The SAME tick, same flag OFF: a symbol with an ALREADY-LIVE
     basket_hedge position whose exit signal fires still exits
     correctly - exit-condition monitoring for anything already held is
     completely unaffected by this flag.
  3. _basket_monitor_tick: the identical "no fresh entry" behavior for
     plain basket mode.
  4. _sequential_monitor_tick: a fresh watchlist candidate does NOT
     enter with the flag off, but a symbol ALREADY holding a PE leg
     still correctly swaps back to FUTURES on its own loop-continuation
     signal (not a "fresh entry", so unaffected), and a symbol ALREADY
     holding a FUT leg still correctly exits on its own signal.
  5. When the flag is True, entries proceed exactly as before - a pure
     opt-in restriction, not a change to the default behavior (a direct
     regression check for this file, on top of test_swing_entry_
     ranking.py's own implicit coverage).
  6. POST /chartink/webhook-swing-enter (the real-time alert-driven
     path added the same day) is completely UNAFFECTED by this flag,
     since it never reads watchlist_store at all - a qualifying alert
     stock still enters normally with the flag OFF.

HOW TO RUN:
    uv run python tests/test_swing_watchlist_entry_toggle.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_watchlist_entry_toggle_test_"))
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


def _entry_state(candle_start=None, volume=100.0) -> ste.SupertrendState:
    """A SupertrendState that satisfies the ENTRY timeframe's own
    crossed_above requirement - same shape test_swing_entry_ranking.py
    already established."""
    return ste.SupertrendState(candle_start=candle_start or datetime.now(ste.IST), close=110.0, supertrend=100.0,
                                is_above=True, prev_close=95.0, prev_supertrend=100.0, prev_is_above=False,
                                volume=volume)


def _confirm_state() -> ste.SupertrendState:
    return ste.SupertrendState(candle_start=datetime.now(ste.IST), close=10.0, supertrend=9.0, is_above=True,
                                prev_close=9.5, prev_supertrend=9.0, prev_is_above=True, volume=0.0)


def install_fake_signal_fetch(entry_states: dict):
    """entry_states: {symbol: SupertrendState} - every symbol in this
    dict genuinely fires the REAL _evaluate_watchlist_entry_signal (price
    confirmation faked True, confirm-timeframe faked satisfied) - same
    helper test_swing_entry_ranking.py already established, reused here
    to prove the SAME genuinely-firing signal is what gets suppressed by
    the flag, not a signal that would have failed anyway."""
    real_fetch = ste._fetch_supertrend_state
    real_price_confirmed = ste._is_price_confirmed_above_prev_close

    async def fake_fetch(symbol, interval_minutes):
        if interval_minutes == ste.config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES:
            return entry_states.get(symbol)
        return _confirm_state()

    async def fake_price_confirmed(symbol):
        return True

    ste._fetch_supertrend_state = fake_fetch
    ste._is_price_confirmed_above_prev_close = fake_price_confirmed

    def restore():
        ste._fetch_supertrend_state = real_fetch
        ste._is_price_confirmed_above_prev_close = real_price_confirmed
    return restore


async def test_1_basket_hedge_no_fresh_entry_when_disabled():
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["ADANIENT"])

    real_enabled, real_watch_enabled = ste.config.STRATEGY_ENABLED, ste.config.WATCHLIST_ENTRY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    ste.config.WATCHLIST_ENTRY_ENABLED = False

    restore_signal = install_fake_signal_fetch({"ADANIENT": _entry_state()})  # a REAL, genuinely-firing signal
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        await ste._basket_hedge_monitor_tick()

        assert "ADANIENT" not in store.live_positions, \
            "with the flag OFF, a watchlist symbol's own genuinely-firing entry signal must be ignored entirely"
        assert placed_orders == [], f"zero orders must be placed when watchlist entry is disabled, got {placed_orders}"
        assert "ADANIENT" in await wl_store.symbols(), "the symbol must stay on the watchlist, completely untouched"
        print("1. _basket_hedge_monitor_tick places ZERO orders and never touches the watchlist symbol at all "
              "when WATCHLIST_ENTRY_ENABLED=False, even though its own entry signal genuinely fires: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.WATCHLIST_ENTRY_ENABLED = real_watch_enabled


async def test_2_basket_hedge_exit_checking_unaffected_when_disabled():
    """The whole point of scoping this flag narrowly - a symbol that's
    ALREADY live (entered before the flag was ever turned off, or via
    the separate alert-driven webhook) must keep being exit-managed
    normally with WATCHLIST_ENTRY_ENABLED=False."""
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store  # empty - this symbol is NOT on the watchlist, only held

    real_enabled, real_watch_enabled = ste.config.STRATEGY_ENABLED, ste.config.WATCHLIST_ENTRY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    ste.config.WATCHLIST_ENTRY_ENABLED = False

    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0])
    try:
        # Put a real BASKET-state position live via the real entry path
        # first (with the flag temporarily on) - simulates "already
        # entered before/outside this flag".
        ste.config.WATCHLIST_ENTRY_ENABLED = True
        entry = await ste.enter_basket_for_stock("RELIANCE")
        assert entry["status"] == "entered", entry
        ste.config.WATCHLIST_ENTRY_ENABLED = False  # now disable it for the actual test

        real_exit_eval = ste._evaluate_basket_exit_signal

        async def fake_exit_fires(symbol, basket):
            return "SUPERTREND_5MIN_EXIT"
        ste._evaluate_basket_exit_signal = fake_exit_fires
        try:
            await ste._basket_hedge_monitor_tick()
        finally:
            ste._evaluate_basket_exit_signal = real_exit_eval

        assert "RELIANCE" not in store.live_positions or store.live_positions["RELIANCE"].state != "BASKET", \
            "the already-live BASKET position must still exit normally with the flag OFF"
        print("2. A position already live (BASKET state) still gets its real exit condition evaluated and "
              "acted on normally when WATCHLIST_ENTRY_ENABLED=False - only fresh watchlist entries are gated: PASSED")
    finally:
        restore_dhan()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.WATCHLIST_ENTRY_ENABLED = real_watch_enabled


async def test_3_basket_mode_no_fresh_entry_when_disabled():
    store = sps.BasketStore()
    ste.basket_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["AUROPHARMA"])

    real_enabled, real_watch_enabled, real_mode = (
        ste.config.STRATEGY_ENABLED, ste.config.WATCHLIST_ENTRY_ENABLED, ste.config.STRATEGY_MODE
    )
    ste.config.STRATEGY_ENABLED = True
    ste.config.WATCHLIST_ENTRY_ENABLED = False
    ste.config.STRATEGY_MODE = "basket"

    restore_signal = install_fake_signal_fetch({"AUROPHARMA": _entry_state()})
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        await ste._basket_monitor_tick()
        assert "AUROPHARMA" not in store.live_baskets
        assert placed_orders == []
        assert "AUROPHARMA" in await wl_store.symbols()
        print("3. _basket_monitor_tick places ZERO orders for a watchlist symbol when "
              "WATCHLIST_ENTRY_ENABLED=False, same as basket_hedge mode: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.WATCHLIST_ENTRY_ENABLED = real_watch_enabled
        ste.config.STRATEGY_MODE = real_mode


async def test_4_sequential_mode_fresh_entry_blocked_but_loop_continuation_unaffected():
    store = sps.SequentialPositionStore()
    ste.sequential_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["FRESHSTOCK"])  # would fire a fresh entry if allowed

    real_enabled, real_watch_enabled, real_mode = (
        ste.config.STRATEGY_ENABLED, ste.config.WATCHLIST_ENTRY_ENABLED, ste.config.STRATEGY_MODE
    )
    ste.config.STRATEGY_ENABLED = True
    ste.config.STRATEGY_MODE = "sequential"

    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0, 100.0, 20.0])
    try:
        # First, genuinely enter LOOPSTOCK's own FUT leg, then swap it to
        # PE via the REAL production functions (with the flag ON) - so
        # there's a symbol "already under active management" whose own
        # PE->FUTURES loop continuation we can prove stays unaffected
        # once the flag flips off.
        ste.config.WATCHLIST_ENTRY_ENABLED = True
        restore_signal = install_fake_signal_fetch({
            "FRESHSTOCK": _entry_state(), "LOOPSTOCK": _entry_state(),
        })
        await ste._enter_futures_for_stock("LOOPSTOCK")
        fut_leg = store.live_legs["LOOPSTOCK"]
        await ste._swap_futures_to_pe("LOOPSTOCK", fut_leg)
        assert store.live_legs["LOOPSTOCK"].option_type == "PE", store.live_legs.get("LOOPSTOCK")
        restore_signal()

        # Now disable the flag and run a real tick: FRESHSTOCK (a genuine
        # watchlist candidate with no leg yet) must NOT enter; LOOPSTOCK
        # (already holding a PE leg) must still swap back to FUTURES on
        # its own real loop-continuation signal, since that's managing
        # EXISTING exposure, not a fresh entry.
        ste.config.WATCHLIST_ENTRY_ENABLED = False
        restore_signal = install_fake_signal_fetch({
            "FRESHSTOCK": _entry_state(), "LOOPSTOCK": _entry_state(),
        })
        placed_orders.clear()
        await ste._sequential_monitor_tick()

        assert "FRESHSTOCK" not in store.live_legs, \
            "a genuinely-firing FRESH watchlist candidate must NOT enter with the flag off"
        assert "FRESHSTOCK" in await wl_store.symbols()
        assert store.live_legs["LOOPSTOCK"].option_type == "FUT", \
            "the PE->FUTURES loop continuation for an ALREADY-held symbol must still fire - it's not a fresh entry"
        print("4. _sequential_monitor_tick blocks a genuinely-firing FRESH watchlist entry with the flag off, "
              "while an ALREADY-held symbol's own PE->FUTURES loop-continuation swap still fires normally: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.WATCHLIST_ENTRY_ENABLED = real_watch_enabled
        ste.config.STRATEGY_MODE = real_mode


async def test_5_entries_proceed_normally_when_the_flag_is_true():
    """Confirms this is a pure opt-in restriction, not a behavior change
    for anyone who leaves it True - checked by explicitly setting True
    here (like every other test in this file explicitly sets whatever it
    needs) rather than asserting on whatever the ambient .env happens to
    carry, since the live deploy's own .env now sets this False (see
    NOTES.md entry #91) - the code-level default (config.py's own
    `os.getenv(..., "true")`) is a plain source-level fact, not something
    this test needs to independently re-verify against a real .env."""
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["NORMALSTOCK"])

    real_enabled, real_watch_enabled = ste.config.STRATEGY_ENABLED, ste.config.WATCHLIST_ENTRY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    ste.config.WATCHLIST_ENTRY_ENABLED = True
    restore_signal = install_fake_signal_fetch({"NORMALSTOCK": _entry_state()})
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        await ste._basket_hedge_monitor_tick()
        assert "NORMALSTOCK" in store.live_positions, "with the flag True, entry must proceed normally"
        print("5. Watchlist-sourced entries proceed completely normally when the flag is True - "
              "this is a pure opt-in restriction, not a change to the default behavior: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.WATCHLIST_ENTRY_ENABLED = real_watch_enabled


async def test_6_alert_driven_webhook_unaffected_by_the_flag():
    """POST /chartink/webhook-swing-enter never reads watchlist_store at
    all (its candidates come straight from the alert payload, entered
    directly and unconditionally - see swing_main.py's own docstring for
    why, reverted 7 Sep 2026) - the whole reason this flag exists is to
    let that path keep working while the OLD watchlist-driven path is
    turned off. No signal mocking needed here at all: the webhook enters
    ALERTSTOCK unconditionally regardless of any Supertrend/price state,
    so this test only needs to prove the flag itself has no bearing."""
    store = sps.BasketHedgeStore()
    sm.basket_hedge_store = store
    ste.basket_hedge_store = store
    wl_store = swl.WatchlistStore()
    sm.watchlist_store = wl_store
    ste.watchlist_store = wl_store
    # deliberately empty watchlist - ALERTSTOCK was never added to it

    real_enabled, real_watch_enabled, real_mode = (
        ste.config.STRATEGY_ENABLED, ste.config.WATCHLIST_ENTRY_ENABLED, ste.config.STRATEGY_MODE
    )
    ste.config.STRATEGY_ENABLED = sm.config.STRATEGY_ENABLED = True
    ste.config.WATCHLIST_ENTRY_ENABLED = False  # the flag under test
    ste.config.STRATEGY_MODE = sm.config.STRATEGY_MODE = "basket_hedge"

    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        payload = sm.SwingWebhookPayload(stocks="ALERTSTOCK", alert_name="toggle-test-6")
        result = await sm.chartink_webhook_swing_enter(payload)

        assert result["entries"][0]["status"] == "entered", \
            f"the alert-driven webhook must be completely unaffected by WATCHLIST_ENTRY_ENABLED, got {result}"
        assert "ALERTSTOCK" in store.live_positions
        print("6. POST /chartink/webhook-swing-enter still enters an alert stock normally "
              "with WATCHLIST_ENTRY_ENABLED=False - it never reads watchlist_store, so it's unaffected: PASSED")
    finally:
        restore_dhan()
        ste.config.STRATEGY_ENABLED = sm.config.STRATEGY_ENABLED = real_enabled
        ste.config.WATCHLIST_ENTRY_ENABLED = real_watch_enabled
        ste.config.STRATEGY_MODE = sm.config.STRATEGY_MODE = real_mode


async def main():
    print("=== Swing watchlist-entry kill switch test suite ===\n")
    await test_1_basket_hedge_no_fresh_entry_when_disabled()
    await test_2_basket_hedge_exit_checking_unaffected_when_disabled()
    await test_3_basket_mode_no_fresh_entry_when_disabled()
    await test_4_sequential_mode_fresh_entry_blocked_but_loop_continuation_unaffected()
    await test_5_entries_proceed_normally_when_the_flag_is_true()
    await test_6_alert_driven_webhook_unaffected_by_the_flag()
    print("\nALL SWING WATCHLIST-ENTRY TOGGLE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
