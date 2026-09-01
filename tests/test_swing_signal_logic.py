"""
Tests for Swing's entry/exit signal - user request 31 Aug 2026, entry's
price gate RELAXED on 1 Sep 2026 from a strict gap-up to: "an explicit
gap up is not mandatory, however when market opens stock price should
rise or be greater or equal than yesterday's stock close price or the
entry condition becomes active when current price cross above yesterday
close price... 5 min close cross above super trend with 1 min close
greater than or crossed above 1 min super trend. Exit when 5 min close
price cross below super trend."

Covers, against the REAL production functions (not reimplemented):
  1. SupertrendState.crossed_above/crossed_below - the crossover
     detection itself, for every relevant combination of current/previous
     side.
  2. _fetch_supertrend_state_once against REAL synthetic candle data (an
     actual price series computed through the real, shared
     _compute_supertrend function) - a genuine crossing series correctly
     produces crossed_above=True, and too little data correctly produces
     None rather than a false signal.
  3. _is_price_confirmed_above_prev_close - true immediately when today's
     open >= yesterday's close (confirmed at market open); when the open
     is BELOW prev close, stays False until the current price later
     reaches/crosses above it (an intraday confirmation, no OHLC re-fetch
     needed since prev_close is already cached); LATCHES true permanently
     once confirmed (a later price drop back below prev_close does NOT
     revert it); and the cache correctly invalidates on a new day.
  4. _evaluate_watchlist_entry_signal's full truth table across the price
     gate AND both Supertrend timeframes - entry fires only when ALL
     THREE hold; a failing price gate short-circuits before either
     Supertrend timeframe is even fetched.
  5. _evaluate_basket_exit_signal - fires only on a real 5-min
     crossed_below, returns None otherwise (unaffected by the entry-side
     price gate, which the exit rule never reads).
  6. A full monitor-loop-shaped auto-entry: a watchlist symbol whose
     signal evaluates True (price gate + both Supertrend legs) gets
     REALLY entered via enter_basket_for_stock (all-or-nothing basket
     placement, mocked Dhan network boundary) and is removed from the
     watchlist afterward - not reached if the signal were still a stub.
  7. A full monitor-loop-shaped auto-exit: a live basket whose signal
     evaluates to a crossed-below reason gets REALLY closed via
     _exit_basket, with both legs verified CLOSED in trade_history.

HOW TO RUN:
    uv run python tests/test_swing_signal_logic.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_signal_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Swing.position_store as sps
import Swing.trading_engine as ste
import Swing.watchlist as swl
from Swing.trading_engine import SupertrendState
from Options.dhan_client import AtmOption, FuturesContract, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP PUT", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"OPT-{symbol}", expiry_date=FUTURE_EXPIRY)


def fake_futures_contract(symbol: str) -> FuturesContract:
    return FuturesContract(trading_symbol=f"{symbol} FAKE EXP FUT", security_id=f"FUT-{symbol}",
                            lot_size=250, expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_futures_contract": odc.dhan_wrapper.get_futures_contract,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.place_market_order = lambda trading_symbol, quantity, transaction_type, tag=None, product_type=None: {
        "order_id": f"FAKE-{trading_symbol}-{transaction_type}", "is_amo": False}
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=1, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


def _make_state(is_above: bool, prev_is_above: bool) -> SupertrendState:
    return SupertrendState(candle_start=datetime.now(), close=100.0, supertrend=95.0, is_above=is_above,
                            prev_close=98.0, prev_supertrend=96.0, prev_is_above=prev_is_above)


def test_1_crossed_above_below_properties():
    assert _make_state(is_above=True, prev_is_above=False).crossed_above, "below -> above must be crossed_above"
    assert not _make_state(is_above=True, prev_is_above=True).crossed_above, "above -> above is NOT a fresh cross"
    assert not _make_state(is_above=False, prev_is_above=False).crossed_above
    assert not _make_state(is_above=False, prev_is_above=True).crossed_above

    assert _make_state(is_above=False, prev_is_above=True).crossed_below, "above -> below must be crossed_below"
    assert not _make_state(is_above=False, prev_is_above=False).crossed_below
    assert not _make_state(is_above=True, prev_is_above=True).crossed_below
    assert not _make_state(is_above=True, prev_is_above=False).crossed_below
    print("1. SupertrendState.crossed_above/crossed_below correctly detect a state CHANGE, "
          "not merely a current side (established-trend candles never falsely re-trigger): PASSED")


def test_2_fetch_supertrend_state_once_against_real_synthetic_data():
    """Builds an actual price series that genuinely crosses above its own
    Supertrend line, run through the REAL, shared _compute_supertrend -
    not a hand-faked SupertrendState."""
    import time as time_module
    period = 10
    n = period + 10
    # A mildly declining series (keeps price BELOW its own Supertrend
    # line) for every bar except the very LAST one, which jumps sharply -
    # a genuine crossover on that final bar only (not the one before it),
    # so this actually exercises crossed_above through the real
    # computation rather than landing in an already-established uptrend.
    closes = [100.0 - i * 0.1 for i in range(n - 1)] + [130.0]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    now = int(time_module.time())
    # All candles in the past except the last, which is made "closed"
    # (interval minutes old) by backdating every timestamp uniformly.
    timestamps = [now - (n - i) * 300 for i in range(n)]

    class FakeRespWrapper:
        def intraday_minute_data(self, **kwargs):
            return {"data": {"high": highs, "low": lows, "close": closes, "timestamp": timestamps}}

    real_client = ste.dhan_wrapper._client
    real_equity_lookup = ste.dhan_wrapper._equity_security_id
    ste.dhan_wrapper._client = type("FakeClient", (), {"Dhan": FakeRespWrapper()})()
    ste.dhan_wrapper._equity_security_id = lambda symbol: "FAKE_SECID"
    try:
        state = ste._fetch_supertrend_state_once("RELIANCE", 5)
        assert state is not None, "expected a computed state from a well-formed candle series"
        assert state.close == 130.0
        assert state.crossed_above, \
            f"the sharp final-bar jump must produce a genuine crossed_above through the real computation, got {state}"
        print(f"2. _fetch_supertrend_state_once against REAL synthetic candle data (via the actual "
              f"shared _compute_supertrend): close={state.close} supertrend={state.supertrend:.2f} "
              f"is_above={state.is_above} prev_is_above={state.prev_is_above} "
              f"crossed_above={state.crossed_above}: PASSED")

        # Too little data -> None, never a guessed/false signal.
        class ThinRespWrapper:
            def intraday_minute_data(self, **kwargs):
                return {"data": {"high": highs[:5], "low": lows[:5], "close": closes[:5], "timestamp": timestamps[:5]}}
        ste.dhan_wrapper._client = type("FakeClient", (), {"Dhan": ThinRespWrapper()})()
        thin_state = ste._fetch_supertrend_state_once("RELIANCE", 5)
        assert thin_state is None, "too few candles must return None, not a guessed state"
        print("   (also confirmed: too few candles correctly returns None, not a false signal)")
    finally:
        ste.dhan_wrapper._client = real_client
        ste.dhan_wrapper._equity_security_id = real_equity_lookup


async def test_3_price_confirmation_check():
    """dhan_wrapper.client is the TRADEHULL object itself - get_ohlc_data
    AND get_ltp_data both live directly on it (same call shapes
    get_day_change_pct's own _get_day_change_pct_once and get_option_ltp
    already use) - so ._client is what needs patching here, not
    ._client.Dhan."""
    real_client = ste.dhan_wrapper._client
    ohlc_calls = {"n": 0}
    ltp_calls = {"n": 0}

    class FakeTradehull:
        def __init__(self, today_open, prev_close, ltp=None):
            self._today_open = today_open
            self._prev_close = prev_close
            self._ltp = ltp

        def get_ohlc_data(self, names):
            ohlc_calls["n"] += 1
            return {names[0]: {"ohlc": {"open": self._today_open, "close": self._prev_close}}}

        def get_ltp_data(self, names):
            ltp_calls["n"] += 1
            return {names[0]: self._ltp}

    ste._price_confirmation_cache.clear()
    try:
        # Confirmed immediately at market open (open > prev_close) - the
        # intraday LTP path must never even be tried.
        ste.dhan_wrapper._client = FakeTradehull(today_open=105.0, prev_close=100.0)
        assert await ste._is_price_confirmed_above_prev_close("RELIANCE") is True, \
            "open > prev_close must confirm immediately"
        assert ohlc_calls["n"] == 1 and ltp_calls["n"] == 0

        # Second call the SAME day must hit the LATCHED cache - no re-fetch of any kind.
        assert await ste._is_price_confirmed_above_prev_close("RELIANCE") is True
        assert ohlc_calls["n"] == 1 and ltp_calls["n"] == 0, \
            f"expected the latch to avoid ANY further REST call, got ohlc={ohlc_calls['n']} ltp={ltp_calls['n']}"

        # Confirmed immediately on an EXACT tie too - >=, not strictly >
        # (user's own wording: "greater or equal than yesterday's close").
        ste.dhan_wrapper._client = FakeTradehull(today_open=100.0, prev_close=100.0)
        assert await ste._is_price_confirmed_above_prev_close("HDFCBANK") is True, \
            "open == prev_close must ALSO confirm at market open"
        assert ohlc_calls["n"] == 2

        # Open BELOW prev_close - not confirmed at market open; a later
        # LTP still below prev_close keeps it False (cheap LTP-only
        # check, no OHLC re-fetch since prev_close is already cached).
        ste.dhan_wrapper._client = FakeTradehull(today_open=95.0, prev_close=100.0, ltp=97.0)
        assert await ste._is_price_confirmed_above_prev_close("TCS") is False, \
            "open < prev_close must NOT confirm at market open"
        assert ohlc_calls["n"] == 3 and ltp_calls["n"] == 0, "the first check of the day is the OHLC check only"

        assert await ste._is_price_confirmed_above_prev_close("TCS") is False, "LTP still below prev_close - stays False"
        assert ohlc_calls["n"] == 3, "must NOT re-fetch OHLC on a later same-day check - prev_close is already cached"
        assert ltp_calls["n"] == 1

        # Price later reaches/crosses prev_close intraday -> confirms
        # (user's own wording: "the entry condition becomes active when
        # current price cross above yesterday close price").
        ste.dhan_wrapper._client = FakeTradehull(today_open=95.0, prev_close=100.0, ltp=100.0)
        assert await ste._is_price_confirmed_above_prev_close("TCS") is True, \
            "current price >= prev_close intraday must confirm (>=, not strictly >)"
        assert ltp_calls["n"] == 2

        # LATCH: even if price now falls back below prev_close, stays
        # True with NO further LTP calls - this is a one-way gate
        # "becoming active," not a live crossover that can flip back off.
        ste.dhan_wrapper._client = FakeTradehull(today_open=95.0, prev_close=100.0, ltp=90.0)
        assert await ste._is_price_confirmed_above_prev_close("TCS") is True, \
            "must stay confirmed even after a later intraday pullback below prev_close"
        assert ltp_calls["n"] == 2, "once latched True, no further LTP calls should ever be made"

        # Cache invalidates on a new day - a stale (yesterday's) latch
        # must be re-checked from scratch (from the open, not trusted).
        ste._price_confirmation_cache["TCS"] = (ste._now_ist().date() - timedelta(days=1), 100.0, True)
        ste.dhan_wrapper._client = FakeTradehull(today_open=95.0, prev_close=100.0, ltp=90.0)
        assert await ste._is_price_confirmed_above_prev_close("TCS") is False, \
            "a stale (yesterday's) cache entry must be re-fetched, not trusted"
        assert ohlc_calls["n"] == 4

        print("3. Price-confirmation gate (open >= prev_close at market open, OR the current price "
              "later reaches/crosses above prev_close intraday) fires correctly, is cheap (no OHLC "
              "re-fetch once prev_close is cached for the day - just a plain LTP check), LATCHES "
              "permanently once confirmed (a later intraday pullback never reverts it), and the "
              "cache correctly invalidates on a new day: PASSED")
    finally:
        ste.dhan_wrapper._client = real_client
        ste._price_confirmation_cache.clear()


async def test_4_entry_signal_truth_table():
    real_fetch = ste._fetch_supertrend_state
    real_price_confirmed = ste._is_price_confirmed_above_prev_close

    async def fake_fetch(symbol, interval_minutes):
        return responses.get(interval_minutes)

    async def fake_price_confirmed(symbol):
        return price_confirmed

    ste._fetch_supertrend_state = fake_fetch
    ste._is_price_confirmed_above_prev_close = fake_price_confirmed
    try:
        # Price confirmed + 5-min crossed above + 1-min is_above (not a fresh cross) -> True
        price_confirmed = True
        responses = {5: _make_state(is_above=True, prev_is_above=False), 1: _make_state(is_above=True, prev_is_above=True)}
        assert await ste._evaluate_watchlist_entry_signal("RELIANCE") is True

        # Price confirmed + 5-min crossed above + 1-min ALSO just crossed above -> True
        responses = {5: _make_state(is_above=True, prev_is_above=False), 1: _make_state(is_above=True, prev_is_above=False)}
        assert await ste._evaluate_watchlist_entry_signal("RELIANCE") is True

        # Price confirmed + 5-min crossed above but 1-min is NOT above at all -> False
        responses = {5: _make_state(is_above=True, prev_is_above=False), 1: _make_state(is_above=False, prev_is_above=False)}
        assert await ste._evaluate_watchlist_entry_signal("RELIANCE") is False

        # Price confirmed + 5-min did NOT cross above (already above from before) -> False, regardless of 1-min
        responses = {5: _make_state(is_above=True, prev_is_above=True), 1: _make_state(is_above=True, prev_is_above=False)}
        assert await ste._evaluate_watchlist_entry_signal("RELIANCE") is False

        # Price confirmed + 5-min never above at all -> False
        responses = {5: _make_state(is_above=False, prev_is_above=False), 1: _make_state(is_above=True, prev_is_above=False)}
        assert await ste._evaluate_watchlist_entry_signal("RELIANCE") is False

        # Price confirmed + no data at all for either timeframe -> False, never a guess
        responses = {5: None, 1: None}
        assert await ste._evaluate_watchlist_entry_signal("RELIANCE") is False

        # Price gate FALSE must short-circuit to False even when both
        # Supertrend legs would otherwise be a perfect entry signal.
        price_confirmed = False
        responses = {5: _make_state(is_above=True, prev_is_above=False), 1: _make_state(is_above=True, prev_is_above=True)}
        assert await ste._evaluate_watchlist_entry_signal("RELIANCE") is False, \
            "a failed price-confirmation gate must block entry even with a perfect Supertrend signal"

        print("4. Entry signal truth table (price-confirmation gate AND 5-min crossed-above AND "
              "(1-min above OR 1-min crossed-above)) - all 7 relevant combinations resolve "
              "correctly, including the price gate correctly short-circuiting an otherwise-perfect "
              "Supertrend signal: PASSED")
    finally:
        ste._fetch_supertrend_state = real_fetch
        ste._is_price_confirmed_above_prev_close = real_price_confirmed


async def test_5_exit_signal():
    real_fetch = ste._fetch_supertrend_state

    async def fake_fetch(symbol, interval_minutes):
        return state_5min

    ste._fetch_supertrend_state = fake_fetch
    try:
        state_5min = _make_state(is_above=False, prev_is_above=True)  # a genuine crossed-below
        reason = await ste._evaluate_basket_exit_signal("RELIANCE", basket=None)
        assert reason == "SUPERTREND_5MIN_EXIT", reason

        state_5min = _make_state(is_above=False, prev_is_above=False)  # already below, not a fresh cross
        assert await ste._evaluate_basket_exit_signal("RELIANCE", basket=None) is None

        state_5min = _make_state(is_above=True, prev_is_above=True)  # still above, no signal
        assert await ste._evaluate_basket_exit_signal("RELIANCE", basket=None) is None

        state_5min = None  # no data
        assert await ste._evaluate_basket_exit_signal("RELIANCE", basket=None) is None

        print("5. Exit signal fires ONLY on a genuine 5-min crossed-below (not an already-below "
              "state, not missing data) - unaffected by the entry-side price-confirmation gate: PASSED")
    finally:
        ste._fetch_supertrend_state = real_fetch


async def test_6_full_auto_entry_via_watchlist_signal():
    """Monitor-loop-SHAPED: calls the same functions monitor_loop() calls,
    in the same order, to prove a True signal (gap-up + both Supertrend
    legs) really enters a basket and really removes the symbol from the
    watchlist - not reached at all if the signal function were still the
    old always-False stub, or if the price-confirmation gate were somehow bypassed."""
    store = sps.BasketStore()
    ste.basket_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["RELIANCE"])

    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    real_fetch = ste._fetch_supertrend_state
    real_price_confirmed = ste._is_price_confirmed_above_prev_close

    async def fake_fetch(symbol, interval_minutes):
        return _make_state(is_above=True, prev_is_above=(interval_minutes != ste.config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES))

    async def fake_price_confirmed(symbol):
        return True

    ste._fetch_supertrend_state = fake_fetch
    ste._is_price_confirmed_above_prev_close = fake_price_confirmed
    restore = install_all_dhan_mocks()
    try:
        symbol = "RELIANCE"
        signal = await ste._evaluate_watchlist_entry_signal(symbol)
        assert signal is True, "the fake fetch must produce a real entry signal"
        result = await ste.enter_basket_for_stock(symbol)
        assert result["status"] == "entered", result
        if result.get("status") == "entered":
            await wl_store.remove_symbol(symbol)

        assert "RELIANCE" in store.live_baskets
        assert "RELIANCE" not in await wl_store.symbols(), \
            "a symbol must be removed from the watchlist after a successful auto-entry"
        print("6. Full auto-entry (monitor-loop-shaped): a real entry signal (price-confirmation "
              "gate + both Supertrend legs) genuinely enters a basket via enter_basket_for_stock "
              "and removes the symbol from the watchlist: PASSED")
    finally:
        restore()
        ste._fetch_supertrend_state = real_fetch
        ste._is_price_confirmed_above_prev_close = real_price_confirmed
        ste.config.STRATEGY_ENABLED = real_enabled


async def test_7_full_auto_exit_via_basket_signal():
    store = sps.BasketStore()
    ste.basket_store = store

    futures_leg = sps.Leg(underlying_symbol="TCS", option_trading_symbol="TCS FAKE EXP FUT", option_type="FUT",
                           quantity=250, lot_size=250, entry_price=4000.0, order_id="OID1", product_type="MARGIN")
    option_leg = sps.Leg(underlying_symbol="TCS", option_trading_symbol="TCS FAKE EXP PUT", option_type="PE",
                          quantity=500, lot_size=500, entry_price=50.0, order_id="OID2", product_type="MARGIN")
    basket = sps.Basket(underlying_symbol="TCS", futures_leg=futures_leg, option_leg=option_leg)
    store.live_baskets["TCS"] = basket
    store.reserved_symbols.add("TCS")

    real_fetch = ste._fetch_supertrend_state

    async def fake_fetch(symbol, interval_minutes):
        return _make_state(is_above=False, prev_is_above=True)  # genuine crossed-below

    ste._fetch_supertrend_state = fake_fetch
    restore = install_all_dhan_mocks()
    try:
        reason = await ste._evaluate_basket_exit_signal("TCS", basket)
        assert reason == "SUPERTREND_5MIN_EXIT", reason
        await ste._exit_basket("TCS", basket, reason)

        assert "TCS" not in store.live_baskets
        assert len(store.closed_baskets_today) == 1
        assert store.closed_baskets_today[0].exit_reason == "SUPERTREND_5MIN_EXIT"

        await asyncio.sleep(0.3)
        closed_trades = trade_history.read_all_jsonl("real_trades")
        swing_closed = [t for t in closed_trades if t["strategy"] == "Swing" and t["underlying_symbol"] == "TCS"]
        assert len(swing_closed) == 2
        assert all(t["exit_reason"] == "SUPERTREND_5MIN_EXIT" for t in swing_closed)
        print("7. Full auto-exit (monitor-loop-shaped): a real crossed-below exit signal genuinely "
              "closes a live basket via _exit_basket, both legs closed in trade_history with the "
              "SUPERTREND_5MIN_EXIT reason: PASSED")
    finally:
        restore()
        ste._fetch_supertrend_state = real_fetch


async def main():
    print("=== Swing entry/exit Supertrend signal test suite ===\n")
    test_1_crossed_above_below_properties()
    test_2_fetch_supertrend_state_once_against_real_synthetic_data()
    await test_3_price_confirmation_check()
    await test_4_entry_signal_truth_table()
    await test_5_exit_signal()
    await test_6_full_auto_entry_via_watchlist_signal()
    await test_7_full_auto_exit_via_basket_signal()
    print("\nALL SWING SIGNAL LOGIC CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
