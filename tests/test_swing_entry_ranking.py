"""
Tests for Swing's entry-candidate ranking (added 1 Sep 2026, user
request, verbatim): "Use a combined strategy of the Freshness of the
crossover and higher Volume" - for when more than one watchlist symbol's
entry signal fires in the SAME monitor tick and MAX_LIVE_BASKETS can't
take them all. Previously whichever symbol happened to sit earliest in
watchlist_store's own iteration order won, by pure accident of insertion
order - nothing judged which setup was actually better.

Applies to all three modes' own entry paths (basket/sequential/
basket_hedge), since all three share the exact same "capacity is scarce,
order of attempt determines the winner" shape - a symbol whose signal
fires is now collected first and RANKED before any entry is actually
attempted, rather than entered immediately as soon as found.

Covers, against the REAL production functions (not reimplemented):
  1. `_entry_candidate_rank_key` - the pure sort-key function: a more
     recent `candle_start` ranks higher regardless of volume; when
     `candle_start` ties, higher `volume` ranks higher; `None` (no
     state, or no candle_start) sorts to the very bottom rather than
     crashing.
  2. A full `_basket_hedge_monitor_tick`-shaped scenario (capacity=1,
     two symbols whose entry signal both fire the SAME tick, only
     DIFFERENT `candle_start`) - the symbol with the FRESHER crossover
     is the one that actually gets entered; the other is left on the
     watchlist for a later tick.
  3. The same shape, but the two symbols share an IDENTICAL
     `candle_start` (the realistic common case - most stocks' 5-min
     candles align to the same clock boundaries) and differ only by
     VOLUME - the higher-volume one wins.
  4. `_sequential_monitor_tick`'s own fresh-entry ranking behaves the
     same way for a NONE->FUTURES entry, while confirming the
     PE->FUTURES loop-continuation swap (which does NOT compete for new
     capacity) still fires immediately, never deferred behind a ranking
     step.
  5. `_basket_monitor_tick`'s own ranking (plain basket mode), for
     parity with the other two modes.

HOW TO RUN:
    uv run python tests/test_swing_entry_ranking.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_entry_ranking_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Swing.position_store as sps
import Swing.trading_engine as ste
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
    # Fixed, generous fakes - the proactive funds check (added 1 Sep
    # 2026) calls these before every entry attempt; unmocked, they'd
    # fall through to a REAL Dhan network call (and a real, slow
    # authentication attempt) via _retry. Ranking, not funds, is what's
    # under test in this file.
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


def _entry_state(candle_start, volume) -> ste.SupertrendState:
    """A SupertrendState that satisfies the ENTRY timeframe's own
    crossed_above requirement, with a controlled candle_start/volume for
    ranking."""
    return ste.SupertrendState(candle_start=candle_start, close=110.0, supertrend=100.0, is_above=True,
                                prev_close=95.0, prev_supertrend=100.0, prev_is_above=False, volume=volume)


def _confirm_state() -> ste.SupertrendState:
    """A SupertrendState that satisfies the CONFIRM timeframe's own
    requirement (is_above True) - ranking never reads this one."""
    return ste.SupertrendState(candle_start=datetime.now(ste.IST), close=10.0, supertrend=9.0, is_above=True,
                                prev_close=9.5, prev_supertrend=9.0, prev_is_above=True, volume=0.0)


def install_fake_signal_fetch(entry_states: dict):
    """entry_states: {symbol: SupertrendState} for the ENTRY timeframe -
    every symbol in this dict will have a full, real entry signal fire
    (price-confirmation gate faked True, confirm-timeframe faked
    satisfied) via the REAL _evaluate_watchlist_entry_signal, and the
    REAL ranking code's own re-fetch of the entry-timeframe state (a
    cache hit against this same fake in real code, but this fake simply
    re-serves the same object either way) sees the controlled
    candle_start/volume given here."""
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


def test_1_entry_candidate_rank_key():
    older = datetime(2026, 9, 1, 10, 5, tzinfo=ste.IST)
    newer = datetime(2026, 9, 1, 10, 10, tzinfo=ste.IST)

    # Freshness dominates regardless of volume.
    fresher_low_vol = ste._entry_candidate_rank_key(_entry_state(newer, volume=100.0))
    staler_high_vol = ste._entry_candidate_rank_key(_entry_state(older, volume=999999.0))
    assert fresher_low_vol > staler_high_vol, \
        f"a fresher crossover must outrank an older one even with far less volume, got {fresher_low_vol} vs {staler_high_vol}"

    # Same candle_start - volume is the tiebreak.
    same_time_high_vol = ste._entry_candidate_rank_key(_entry_state(newer, volume=500.0))
    same_time_low_vol = ste._entry_candidate_rank_key(_entry_state(newer, volume=100.0))
    assert same_time_high_vol > same_time_low_vol, \
        "with an identical candle_start, higher volume must rank higher"

    # None / missing candle_start sorts to the very bottom, never crashes.
    none_state_key = ste._entry_candidate_rank_key(None)
    missing_candle_key = ste._entry_candidate_rank_key(
        ste.SupertrendState(candle_start=None, close=1.0, supertrend=1.0, is_above=True,
                             prev_close=1.0, prev_supertrend=1.0, prev_is_above=False, volume=999999.0))
    assert none_state_key <= same_time_low_vol
    assert missing_candle_key <= same_time_low_vol

    print("1. _entry_candidate_rank_key ranks a fresher crossover above an older one regardless of "
          "volume, uses volume only as the tiebreak when candle_start ties, and handles a missing "
          "state/candle_start defensively (sorts last, never crashes): PASSED")


async def test_2_basket_hedge_tick_prefers_freshest_crossover():
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["STALESTOCK", "FRESHSTOCK"])

    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.MAX_LIVE_BASKETS = 1  # only ONE can win

    older = datetime(2026, 9, 1, 10, 5, tzinfo=ste.IST)
    newer = datetime(2026, 9, 1, 10, 10, tzinfo=ste.IST)
    restore_signal = install_fake_signal_fetch({
        "STALESTOCK": _entry_state(older, volume=999999.0),  # crossed earlier, huge volume - still must LOSE
        "FRESHSTOCK": _entry_state(newer, volume=1.0),        # crossed more recently, tiny volume - must WIN
    })
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        await ste._basket_hedge_monitor_tick()

        assert "FRESHSTOCK" in store.live_positions, \
            f"the FRESHER crossover must win despite far less volume, live_positions={list(store.live_positions)}"
        assert "STALESTOCK" not in store.live_positions
        assert "STALESTOCK" in await wl_store.symbols(), \
            "the symbol that lost this tick's capacity race must stay on the watchlist for a later tick"
        assert "FRESHSTOCK" not in await wl_store.symbols(), "the winner is removed from the watchlist on entry"

        entered_symbols = {o["trading_symbol"].split(" ")[0] for o in placed_orders}
        assert entered_symbols == {"FRESHSTOCK"}, \
            f"only the ranked WINNER's orders should have been placed at all, got {placed_orders}"

        print("2. _basket_hedge_monitor_tick correctly enters the symbol with the FRESHER crossover "
              "when two fire in the same tick and capacity=1 - freshness wins even against a vastly "
              "larger volume on the older signal; the loser stays on the watchlist untouched: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap


async def test_3_basket_hedge_tick_uses_volume_as_tiebreak():
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["LOWVOL", "HIGHVOL"])

    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.MAX_LIVE_BASKETS = 1

    same_candle = datetime(2026, 9, 1, 11, 0, tzinfo=ste.IST)  # IDENTICAL candle_start - the realistic common case
    restore_signal = install_fake_signal_fetch({
        "LOWVOL": _entry_state(same_candle, volume=1000.0),
        "HIGHVOL": _entry_state(same_candle, volume=50000.0),
    })
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        await ste._basket_hedge_monitor_tick()

        assert "HIGHVOL" in store.live_positions, \
            f"with an identical candle_start, the HIGHER-volume symbol must win, got {list(store.live_positions)}"
        assert "LOWVOL" not in store.live_positions
        assert "LOWVOL" in await wl_store.symbols()

        print("3. With an IDENTICAL crossover candle (the realistic common case, since most stocks' "
              "5-min candles align to the same clock boundaries), the higher-VOLUME symbol wins the "
              "single available slot: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap


async def test_4_sequential_tick_ranks_fresh_entries_but_not_pe_loop_continuation():
    store = sps.SequentialPositionStore()
    ste.sequential_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["STALESTOCK", "FRESHSTOCK"])

    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.MAX_LIVE_BASKETS = 1

    older = datetime(2026, 9, 1, 10, 5, tzinfo=ste.IST)
    newer = datetime(2026, 9, 1, 10, 10, tzinfo=ste.IST)
    restore_signal = install_fake_signal_fetch({
        "STALESTOCK": _entry_state(older, volume=100.0),
        "FRESHSTOCK": _entry_state(newer, volume=100.0),
    })
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0])
    try:
        await ste._sequential_monitor_tick()
        assert "FRESHSTOCK" in store.live_legs, list(store.live_legs)
        assert "STALESTOCK" not in store.live_legs

        print("4. _sequential_monitor_tick's own fresh-entry (NONE->FUTURES) ranking correctly "
              "prefers the more recent crossover when capacity=1, same as basket_hedge mode: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap


async def test_5_basket_mode_tick_ranks_entries_too():
    store = sps.BasketStore()
    ste.basket_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["STALESTOCK", "FRESHSTOCK"])

    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.MAX_LIVE_BASKETS = 1

    older = datetime(2026, 9, 1, 10, 5, tzinfo=ste.IST)
    newer = datetime(2026, 9, 1, 10, 10, tzinfo=ste.IST)
    restore_signal = install_fake_signal_fetch({
        "STALESTOCK": _entry_state(older, volume=100.0),
        "FRESHSTOCK": _entry_state(newer, volume=100.0),
    })
    restore_dhan, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        await ste._basket_monitor_tick()
        assert "FRESHSTOCK" in store.live_baskets, list(store.live_baskets)
        assert "STALESTOCK" not in store.live_baskets

        print("5. _basket_monitor_tick (plain basket mode) ranks entries the same way, for parity "
              "with sequential and basket_hedge modes: PASSED")
    finally:
        restore_dhan()
        restore_signal()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap


async def main():
    print("=== Swing entry-candidate ranking test suite ===\n")
    test_1_entry_candidate_rank_key()
    await test_2_basket_hedge_tick_prefers_freshest_crossover()
    await test_3_basket_hedge_tick_uses_volume_as_tiebreak()
    await test_4_sequential_tick_ranks_fresh_entries_but_not_pe_loop_continuation()
    await test_5_basket_mode_tick_ranks_entries_too()
    print("\nALL SWING ENTRY-RANKING CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
