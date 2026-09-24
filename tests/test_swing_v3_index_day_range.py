"""
Tests for the v3 additions to Swing's entry logic - config.INDEX_SYMBOLS
(NIFTY/BANKNIFTY) ONLY, user request 24 Sep 2026 (port
backtest_nifty_options_swing_v2_1min.py's best-performing config live -
see Swing/config.py's DAY_RANGE_RSI_PERIOD docstring and Swing/
trading_engine.py's _evaluate_entry_signal for the full spec).

Coverage:
  1. DayRangeState correctly derives today's open / yesterday's close from
     the same continuous 5-min series (day-boundary bucketing), computes
     RSI(14) crossing the bull/bear level, and combines all four Day
     Range Bull conditions (AND, not any-one-of).
  2. Insufficient history (no prior trading day in the fetched window, or
     not enough bars for RSI/Supertrend) returns None, not a guess.
  3. _evaluate_entry_signal: for an INDEX symbol, the "Regime Bullish" leg
     reads regime.is_bullish (a LEVEL, persisting) - fires even with NO
     fresh crossed_above edge. For a NON-index symbol, the exact same
     regime/Supertrend inputs must NOT fire (still requires the edge) -
     proves the v3 departure is correctly scoped to indices only, not a
     global regression of the 17 Sep COPPER-incident fix.
  4. _evaluate_entry_signal: INDEX symbol admits an entry via the Day
     Range branch ALONE (branch_a entirely false), only when Trend-aware
     Filter also agrees (AND-gated, not OR).
  5. _evaluate_entry_signal: get_day_range_state is never even called for
     a non-index symbol.
  6. A None DayRangeState (not enough history / fetch failure) never
     blocks branch_a - the index symbol still enters via the unchanged
     three-way OR filter.

HOW TO RUN:
    uv run python tests/test_swing_v3_index_day_range.py
"""
import asyncio
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.dhan_client as odc
import Swing.config as sc
import Swing.signals as signals
import Swing.trading_engine as ste
from Swing.signals import DayRangeState, RegimeState, SupertrendState

IST = odc.IST
W = odc.dhan_wrapper

_REAL_GET_REGIME_STATE = ste.signals.get_regime_state
_REAL_GET_SUPERTREND_STATE = ste.signals.get_supertrend_state
_REAL_GET_DAY_RANGE_STATE = ste.signals.get_day_range_state


# --------------------------------------------------------------------------- #
# Part 1: DayRangeState / _fetch_day_range_state_once (real candle-shape data)
# --------------------------------------------------------------------------- #
def _index_series(day1_closes, day2_closes, day1_opens=None, day2_opens=None):
    """Two trading days of 5-min bars, correctly day-bucketed (09:15-15:30
    IST each day, matching real NSE session bars) so today_first_idx /
    yesterday_close resolve against a genuine prior-day boundary, exactly
    as _fetch_day_range_state_once expects."""
    day1_opens = day1_opens or day1_closes
    day2_opens = day2_opens or day2_closes
    day1_start = datetime(2026, 9, 22, 9, 15, tzinfo=IST)  # Tuesday
    day2_start = datetime(2026, 9, 23, 9, 15, tzinfo=IST)  # Wednesday
    ts, opens, highs, lows, closes = [], [], [], [], []
    for i, c in enumerate(day1_closes):
        ts.append(int((day1_start + timedelta(minutes=5 * i)).timestamp()))
        opens.append(day1_opens[i]); highs.append(c + 1.0); lows.append(c - 1.0); closes.append(c)
    for i, c in enumerate(day2_closes):
        ts.append(int((day2_start + timedelta(minutes=5 * i)).timestamp()))
        opens.append(day2_opens[i]); highs.append(c + 1.0); lows.append(c - 1.0); closes.append(c)
    return {"open": opens, "high": highs, "low": lows, "close": closes,
            "volume": [1000.0] * len(closes), "timestamp": ts}


def _install_fake_client(data: dict):
    def intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval):
        return {"status": "success", "data": data}
    return types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=intraday_minute_data))


def test_1_today_open_and_yesterday_close_from_day_boundary():
    saved_client, saved_idx = W._client, W.index_security_id
    try:
        W.index_security_id = lambda sym: "13"
        # Day1: 40 bars flat at 100 (yesterday). Day2: opens at 105 (gap up),
        # drifts, needs enough bars for RSI(14)/Supertrend(10) warm-up too.
        day2 = [105.0 + i * 0.3 for i in range(40)]
        data = _index_series([100.0] * 40, day2, day2_opens=[105.0] + day2[1:])
        W._client = _install_fake_client(data)
        signals._day_range_cache.clear()
        signals._raw_series_cache.clear()
        state = asyncio.run(signals.get_day_range_state("NIFTY"))
        assert state is not None, "expected a resolvable DayRangeState with 2 full days of bars"
        assert state.today_open == 105.0, state.today_open
        assert state.yesterday_close == 100.0, state.yesterday_close
        assert state.gap_up_day is True and state.gap_down_day is False
        print("1. DayRangeState.today_open/yesterday_close derived from the real day boundary: PASSED")
    finally:
        W._client, W.index_security_id = saved_client, saved_idx


def test_2_insufficient_history_returns_none():
    saved_client, saved_idx = W._client, W.index_security_id
    try:
        W.index_security_id = lambda sym: "13"
        # Only today's bars, no prior day at all in the fetched window.
        data = _index_series([], [100.0 + i * 0.2 for i in range(40)])
        data["timestamp"] = data["timestamp"][-40:]  # drop the (empty) day-1 contribution cleanly
        W._client = _install_fake_client(data)
        signals._day_range_cache.clear()
        signals._raw_series_cache.clear()
        state = asyncio.run(signals.get_day_range_state("NIFTY"))
        assert state is None, "no prior trading day in the fetched window must yield None, not a guess"
        print("2. DayRangeState is None (not a guess) when there's no prior-day bar to compare against: PASSED")
    finally:
        W._client, W.index_security_id = saved_client, saved_idx


def test_3_rsi_crossed_above_bull_level_and_combined_bullish_entry():
    saved_client, saved_idx = W._client, W.index_security_id
    try:
        W.index_security_id = lambda sym: "13"
        # Day2 built (verified via _compute_rsi directly) so RSI(14) sits at
        # 59.10 one bar back and 64.67 on the LAST bar - a genuine crossed-
        # above-60 edge on the final candle, not just "currently above 60".
        # Close (106.1) stays above both today's open (105) and its own
        # Supertrend, and today's open (105) > yesterday's close (100).
        day2 = [105, 104.4, 103.8, 104.3, 105.0, 104.2, 103.4, 104.1, 104.6, 104.0, 103.4, 104.1,
                104.8, 105.5, 104.9, 104.3, 103.7, 104.4, 103.6, 102.8, 102.2, 101.4, 101.9, 101.1,
                101.6, 103.1, 104.6, 106.1]
        data = _index_series([100.0] * 30, day2)
        W._client = _install_fake_client(data)
        signals._day_range_cache.clear()
        signals._raw_series_cache.clear()
        state = asyncio.run(signals.get_day_range_state("NIFTY"))
        assert state is not None, "expected enough bars for RSI(14)+Supertrend(10) to warm up"
        assert state.rsi is not None and state.prev_rsi is not None
        assert state.is_above_supertrend is True
        assert state.close > state.today_open
        assert state.bullish_entry is True, (
            f"expected Day Range Bull to fire: gap_up_day={state.gap_up_day} "
            f"close>open={state.close > state.today_open} is_above_st={state.is_above_supertrend} "
            f"rsi={state.prev_rsi}->{state.rsi} crossed={state.crossed_above_bull_level}"
        )
        print("3. DayRangeState.bullish_entry fires when all four Day Range Bull conditions hold together: PASSED")
    finally:
        W._client, W.index_security_id = saved_client, saved_idx


def test_4_one_missing_condition_blocks_bullish_entry():
    """Same shape as test_3 (verified via _compute_rsi directly: RSI crosses
    58.53 -> 63.83 on the last bar, close stays above its own Supertrend)
    but today's open (95) is BELOW yesterday's close (100) - no gap-up day -
    proves bullish_entry is a genuine AND across all four conditions, not
    any one of them alone."""
    saved_client, saved_idx = W._client, W.index_security_id
    try:
        W.index_security_id = lambda sym: "13"
        day2 = [95, 94.4, 93.8, 94.3, 95.0, 94.2, 93.4, 94.1, 94.6, 94.0, 93.4, 94.1, 94.8, 95.5,
                94.9, 94.3, 93.7, 94.4, 93.6, 92.8, 92.2, 91.4, 91.9, 91.1, 91.6, 93.1, 94.6, 96.1, 97.6]
        data = _index_series([100.0] * 30, day2)
        W._client = _install_fake_client(data)
        signals._day_range_cache.clear()
        signals._raw_series_cache.clear()
        state = asyncio.run(signals.get_day_range_state("NIFTY"))
        assert state is not None
        assert state.gap_up_day is False, "today's open (95) must read below yesterday's close (100)"
        assert state.crossed_above_bull_level is True, "RSI side of the setup must still have fired"
        assert state.bullish_entry is False, "gap_up_day is required - one true condition alone must not be enough"
        print("4. DayRangeState.bullish_entry correctly stays False when only 3 of 4 conditions hold: PASSED")
    finally:
        W._client, W.index_security_id = saved_client, saved_idx


# --------------------------------------------------------------------------- #
# Part 2: _evaluate_entry_signal - index-scoping of both v3 changes
# --------------------------------------------------------------------------- #
def _regime(is_bullish: bool, prev_is_bullish: Optional[bool] = None, gap_widened: Optional[bool] = None) -> RegimeState:
    return RegimeState(fast_ema=101.0 if is_bullish else 99.0, slow_ema=100.0, is_bullish=is_bullish,
                        fast_candle_start=datetime(2026, 9, 24, 9, 20), slow_candle_start=datetime(2026, 9, 24, 9, 15),
                        prev_is_bullish=prev_is_bullish, gap_widened=gap_widened)


def _supertrend(is_above: bool, prev_is_above: bool) -> SupertrendState:
    return SupertrendState(candle_start=datetime(2026, 9, 24, 9, 20), close=101.0 if is_above else 99.0,
                            supertrend=100.0, is_above=is_above, prev_close=99.0 if prev_is_above else 101.0,
                            prev_supertrend=100.0, prev_is_above=prev_is_above)


def _day_range(bull_entry: bool = False, bear_entry: bool = False) -> DayRangeState:
    return DayRangeState(
        today_open=105.0, yesterday_close=100.0 if bull_entry else 110.0,
        close=106.0 if bull_entry else 94.0, supertrend=100.0,
        is_above_supertrend=bull_entry, rsi=65.0 if bull_entry else 35.0,
        prev_rsi=55.0 if bull_entry else 45.0, candle_start=datetime(2026, 9, 24, 9, 20),
    )


async def _resolved(value):
    return value


def _install(regime_state, st5_state, st15_state, day_range_state=None, day_range_raises=False):
    ste.signals.get_regime_state = lambda symbol: _resolved(regime_state)

    def fake_get_supertrend_state(symbol, interval_minutes=None):
        if interval_minutes is not None and interval_minutes != sc.SUPERTREND_INTERVAL_MINUTES:
            return _resolved(st15_state)
        return _resolved(st5_state)
    ste.signals.get_supertrend_state = fake_get_supertrend_state

    def fake_get_day_range_state(symbol):
        if day_range_raises:
            raise AssertionError(f"get_day_range_state must never be called for a non-index symbol ({symbol!r})")
        return _resolved(day_range_state)
    ste.signals.get_day_range_state = fake_get_day_range_state


def _restore():
    sc.ENTRY_STRATEGY_VERSION = "v2"
    ste.signals.get_regime_state = _REAL_GET_REGIME_STATE
    ste.signals.get_supertrend_state = _REAL_GET_SUPERTREND_STATE
    ste.signals.get_day_range_state = _REAL_GET_DAY_RANGE_STATE


def test_5_index_regime_leg_uses_level_not_edge():
    """is_bullish=True but prev_is_bullish=True too (NO fresh crossed_above
    edge), gap NOT widened (Trend-aware Filter leg off), 15-min ST
    disagrees (off) - isolates the Regime-Bullish leg alone. NIFTY must
    fire via regime.is_bullish (a LEVEL); the identical inputs for a
    non-index symbol must NOT fire, since it still requires the edge."""
    sc.ENTRY_STRATEGY_VERSION = "v2"
    try:
        _install(_regime(is_bullish=True, prev_is_bullish=True, gap_widened=False),
                 _supertrend(is_above=True, prev_is_above=False),
                 _supertrend(is_above=False, prev_is_above=False))
        result_index = asyncio.run(ste._evaluate_entry_signal("NIFTY"))
        assert result_index == "BULLISH", f"NIFTY: expected BULLISH via regime.is_bullish (level), got {result_index!r}"

        result_equity = asyncio.run(ste._evaluate_entry_signal("TESTSTOCK"))
        assert result_equity is None, (
            f"TESTSTOCK (non-index): must still require a FRESH crossed_above edge - "
            f"the 17 Sep COPPER-incident fix must stay untouched, got {result_equity!r}"
        )
        print("5. Index-only Regime-leg-as-LEVEL is correctly scoped: NIFTY fires on level alone, "
              "TESTSTOCK still requires the edge: PASSED")
    finally:
        _restore()


def test_6_index_day_range_branch_admits_entry_branch_a_false():
    """branch_a entirely false (all three legs off, no 5-min crossover
    either) - only the Day Range branch, ANDed with Trend-aware Filter,
    can admit this entry."""
    sc.ENTRY_STRATEGY_VERSION = "v2"
    try:
        _install(_regime(is_bullish=True, prev_is_bullish=True, gap_widened=True),  # Trend-aware Filter: ON
                 _supertrend(is_above=False, prev_is_above=False),  # no 5-min crossover -> branch_a impossible
                 _supertrend(is_above=False, prev_is_above=False),  # 15-min ST bearish -> that leg also off
                 day_range_state=_day_range(bull_entry=True))
        result = asyncio.run(ste._evaluate_entry_signal("NIFTY"))
        assert result == "BULLISH", f"expected BULLISH via the Day Range branch alone, got {result!r}"
        print("6. NIFTY admits a BULLISH entry via the Day Range branch alone (branch_a impossible, "
              "Trend-aware Filter agrees): PASSED")
    finally:
        _restore()


def test_7_day_range_branch_requires_trend_aware_filter_too():
    """Day Range Bull fires, but the Trend-aware Filter leg is OFF (gap not
    widened) - the AND must block it, proving this isn't secretly an OR."""
    sc.ENTRY_STRATEGY_VERSION = "v2"
    try:
        _install(_regime(is_bullish=True, prev_is_bullish=True, gap_widened=False),  # Trend-aware Filter: OFF
                 _supertrend(is_above=False, prev_is_above=False),
                 _supertrend(is_above=False, prev_is_above=False),
                 day_range_state=_day_range(bull_entry=True))
        result = asyncio.run(ste._evaluate_entry_signal("NIFTY"))
        assert result is None, f"Day Range Bull without Trend-aware Filter agreeing must not enter, got {result!r}"
        print("7. Day Range branch correctly requires Trend-aware Filter too (AND, not OR): PASSED")
    finally:
        _restore()


def test_8_day_range_never_consulted_for_non_index_symbol():
    """get_day_range_state must not even be CALLED for a non-index symbol -
    not just 'its result is ignored'."""
    sc.ENTRY_STRATEGY_VERSION = "v2"
    try:
        _install(_regime(is_bullish=False, prev_is_bullish=False, gap_widened=False),
                 _supertrend(is_above=False, prev_is_above=False),
                 _supertrend(is_above=False, prev_is_above=False),
                 day_range_raises=True)
        result = asyncio.run(ste._evaluate_entry_signal("TESTSTOCK"))
        assert result is None
        print("8. get_day_range_state is never called at all for a non-index symbol: PASSED")
    finally:
        _restore()


def test_9_none_day_range_state_never_blocks_branch_a():
    """A None DayRangeState (not enough history / a fetch failure) must not
    prevent the unchanged three-way-OR filter from still working for an
    index symbol."""
    sc.ENTRY_STRATEGY_VERSION = "v2"
    try:
        _install(_regime(is_bullish=False, prev_is_bullish=False, gap_widened=False),
                 _supertrend(is_above=True, prev_is_above=False),
                 _supertrend(is_above=True, prev_is_above=True),  # 15-min ST bullish -> branch_a leg
                 day_range_state=None)
        result = asyncio.run(ste._evaluate_entry_signal("NIFTY"))
        assert result == "BULLISH", f"expected branch_a (15-min ST leg) to still admit the entry, got {result!r}"
        print("9. A None DayRangeState never blocks the index symbol's own branch_a: PASSED")
    finally:
        _restore()


def main():
    print("=== Swing v3 (NIFTY/BANKNIFTY Day Range + index-scoped Regime leg) test suite ===\n")
    test_1_today_open_and_yesterday_close_from_day_boundary()
    test_2_insufficient_history_returns_none()
    test_3_rsi_crossed_above_bull_level_and_combined_bullish_entry()
    test_4_one_missing_condition_blocks_bullish_entry()
    test_5_index_regime_leg_uses_level_not_edge()
    test_6_index_day_range_branch_admits_entry_branch_a_false()
    test_7_day_range_branch_requires_trend_aware_filter_too()
    test_8_day_range_never_consulted_for_non_index_symbol()
    test_9_none_day_range_state_never_blocks_branch_a()
    print("\nALL SWING V3 INDEX DAY RANGE CHECKS PASSED")


if __name__ == "__main__":
    main()
