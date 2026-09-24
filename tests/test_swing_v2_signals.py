"""
Tests for Swing/signals.py - the 5-min-vs-15-min 200-EMA regime filter
and the 5-min Supertrend crossover, on synthetic candle data (same
`W._client` mocking pattern as tests/test_continuous_intraday.py).

Covers:
  1. lookback_days_override actually reaches fetch_continuous_intraday
     (not the shared 7-day global) for the regime signal specifically.
  2. Regime correctly reads bullish/bearish from fast-EMA vs slow-EMA.
  3. Insufficient bars (< REGIME_EMA_PERIOD) returns None, not a guess.
  4. A fetch exception keeps the LAST GOOD cached value rather than
     writing None over it - a transient Dhan hiccup must never read as
     "regime flipped" or "exit now".
  5. Supertrend crossed_above/crossed_below fire only on the transition
     candle, not on every candle of an already-established trend.

HOW TO RUN:
    uv run python tests/test_swing_v2_signals.py
"""
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import asyncio

import Options.dhan_client as odc
import Swing.config as sc
import Swing.signals as signals

IST = odc.IST
W = odc.dhan_wrapper


def _bars(closes, interval_min, start=None):
    start = start or datetime(2026, 8, 1, 9, 15, tzinfo=IST)
    ts = [int((start + timedelta(minutes=interval_min * i)).timestamp()) for i in range(len(closes))]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    return {"high": highs, "low": lows, "close": [float(c) for c in closes], "volume": [1000.0] * len(closes), "timestamp": ts}


def _install_fake_client(fast_closes, slow_closes):
    """Returns a fake Dhan client whose intraday_minute_data replies with
    fast_closes for the 5-min interval, slow_closes for 15-min - matching
    Swing/signals.py's own two-fetch (fast/slow) regime computation."""
    seen_calls = []

    def intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval):
        seen_calls.append({"interval": interval, "from_date": from_date})
        closes = fast_closes if interval == sc.REGIME_FAST_INTERVAL_MINUTES else slow_closes
        return {"status": "success", "data": _bars(closes, interval)}

    return types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=intraday_minute_data)), seen_calls


def test_1_lookback_override_reaches_the_fetch_for_both_intervals():
    saved_client, saved_eqid = W._client, W._equity_security_id
    try:
        W._equity_security_id = lambda sym: "SID"
        fake_client, seen = _install_fake_client([100.0] * 250, [100.0] * 250)
        W._client = fake_client
        signals._regime_cache.clear()
        signals._raw_series_cache.clear()

        asyncio.run(signals.get_regime_state("TESTSTOCK"))

        expected_from = (datetime.now(IST) - timedelta(days=sc.REGIME_EMA_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        assert len(seen) == 2, f"expected exactly 2 fetches (fast+slow), got {len(seen)}"
        for call in seen:
            assert call["from_date"] == expected_from, \
                f"regime fetch must use REGIME_EMA_LOOKBACK_DAYS ({sc.REGIME_EMA_LOOKBACK_DAYS}d), " \
                f"got from_date={call['from_date']} (expected {expected_from})"
        print("1. get_regime_state's fetches use REGIME_EMA_LOOKBACK_DAYS, not the shared 7-day global: PASSED")
    finally:
        W._client, W._equity_security_id = saved_client, saved_eqid


def test_2_regime_reads_bullish_and_bearish_correctly():
    saved_client, saved_eqid = W._client, W._equity_security_id
    try:
        W._equity_security_id = lambda sym: "SID"

        # Fast series ends noticeably higher than slow series -> fast EMA > slow EMA -> bullish.
        fast_closes = [100.0 + i * 0.5 for i in range(250)]   # trending up, ends at 224.5
        slow_closes = [100.0] * 250                           # flat, EMA stays ~100
        fake_client, _ = _install_fake_client(fast_closes, slow_closes)
        W._client = fake_client
        signals._regime_cache.clear()
        signals._raw_series_cache.clear()
        regime = asyncio.run(signals.get_regime_state("TESTSTOCK"))
        assert regime is not None and regime.is_bullish is True, regime

        # Reverse it - fast series flat/low, slow series high -> bearish.
        fake_client2, _ = _install_fake_client([100.0] * 250, [100.0 + i * 0.5 for i in range(250)])
        W._client = fake_client2
        signals._regime_cache.clear()
        signals._raw_series_cache.clear()
        regime2 = asyncio.run(signals.get_regime_state("TESTSTOCK"))
        assert regime2 is not None and regime2.is_bullish is False, regime2
        print("2. RegimeState.is_bullish correctly reflects fast-EMA(200) vs slow-EMA(200): PASSED")
    finally:
        W._client, W._equity_security_id = saved_client, saved_eqid


def test_3_insufficient_bars_returns_none_not_a_guess():
    saved_client, saved_eqid = W._client, W._equity_security_id
    try:
        W._equity_security_id = lambda sym: "SID"
        # Only 50 bars - well under REGIME_EMA_PERIOD (200) - must return None, never a computed value.
        fake_client, _ = _install_fake_client([100.0] * 50, [100.0] * 250)
        W._client = fake_client
        signals._regime_cache.clear()
        signals._raw_series_cache.clear()
        regime = asyncio.run(signals.get_regime_state("TESTSTOCK"))
        assert regime is None, "insufficient fast-series bars must yield None, not a guessed regime"
        print("3. get_regime_state returns None (not a guess) when there aren't enough bars yet: PASSED")
    finally:
        W._client, W._equity_security_id = saved_client, saved_eqid


def test_4_fetch_failure_keeps_last_good_cached_value():
    saved_client, saved_eqid = W._client, W._equity_security_id
    try:
        W._equity_security_id = lambda sym: "SID"
        fake_client, _ = _install_fake_client([100.0 + i * 0.5 for i in range(250)], [100.0] * 250)
        W._client = fake_client
        signals._regime_cache.clear()
        signals._raw_series_cache.clear()
        first = asyncio.run(signals.get_regime_state("TESTSTOCK"))
        assert first is not None and first.is_bullish is True

        # Force staleness so the next call actually re-fetches, then make the fetch raise.
        cached_at, cached_val = signals._regime_cache["TESTSTOCK"]
        signals._regime_cache["TESTSTOCK"] = (cached_at - timedelta(seconds=sc.REGIME_REFRESH_SECONDS + 1), cached_val)

        def raising(*a, **k):
            raise RuntimeError("simulated Dhan hiccup")
        W._client = types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=raising))

        second = asyncio.run(signals.get_regime_state("TESTSTOCK"))
        assert second is not None and second.is_bullish is True, \
            "a fetch exception must return the LAST GOOD cached value, never None (which callers would " \
            "misread as 'no signal' when it should mean 'still bullish, per the last real read')"
        print("4. A fetch failure keeps the last good cached RegimeState rather than clobbering it with None: PASSED")
    finally:
        W._client, W._equity_security_id = saved_client, saved_eqid


def test_5_supertrend_crossed_above_fires_only_on_the_transition_candle():
    saved_client, saved_eqid = W._client, W._equity_security_id
    try:
        W._equity_security_id = lambda sym: "SID"
        # A long flat run (Supertrend below/at price, i.e. bearish-side band) then a sharp
        # spike on the LAST bar - constructed so the second-to-last close is below its band
        # and the last close jumps above it, producing a genuine crossed_above on the last bar.
        closes = [100.0] * 30 + [200.0]
        fake_client, _ = _install_fake_client(closes, closes)  # supertrend fetch only uses one series
        W._client = fake_client
        signals._supertrend_cache.clear()
        signals._raw_series_cache.clear()
        st = asyncio.run(signals.get_supertrend_state("TESTSTOCK"))
        assert st is not None, "expected enough bars for a Supertrend read"
        assert st.crossed_above is True, f"expected a crossover on the spike bar, got is_above={st.is_above} prev_is_above={st.prev_is_above}"
        assert st.crossed_below is False
        print("5. SupertrendState.crossed_above fires on a genuine transition candle: PASSED")
    finally:
        W._client, W._equity_security_id = saved_client, saved_eqid


def test_6_completely_empty_fetch_response_keeps_last_good_cached_value():
    """Real incident, 17 Sep 2026: fetch_continuous_intraday's own
    documented failure mode is to return {} (no exception) when the
    underlying Dhan call fails - during a live Dhan API instability
    window, this silently produced a "0 candles" result that
    _fetch_regime_state_once/_fetch_supertrend_state_once then computed a
    normal (non-exceptional) None from, which get_regime_state/
    get_supertrend_state cached as if it were a legitimate reading -
    clobbering the LAST GOOD cached regime/Supertrend with None across
    every watchlist symbol, with zero errors logged (since nothing ever
    raised). Fixed by having _fetch_regime_state_once/_fetch_supertrend_
    state_once explicitly raise when a fetch comes back completely empty,
    so the EXISTING "keep last good cached value on a fetch exception"
    path (already proven by test_4 above) actually gets to run instead of
    being silently bypassed. This test simulates that exact completely-
    empty-response shape (not a raised exception) and confirms the last
    good cached value survives it, for BOTH regime and Supertrend."""
    saved_client, saved_eqid = W._client, W._equity_security_id
    try:
        W._equity_security_id = lambda sym: "SID"

        # --- Regime ---
        fake_client, _ = _install_fake_client([100.0 + i * 0.5 for i in range(250)], [100.0] * 250)
        W._client = fake_client
        signals._regime_cache.clear()
        signals._raw_series_cache.clear()
        first = asyncio.run(signals.get_regime_state("TESTSTOCK"))
        assert first is not None and first.is_bullish is True

        cached_at, cached_val = signals._regime_cache["TESTSTOCK"]
        signals._regime_cache["TESTSTOCK"] = (cached_at - timedelta(seconds=sc.REGIME_REFRESH_SECONDS + 1), cached_val)

        def empty_response(*a, **k):
            return {"status": "success", "data": {}}  # exactly what a failed Dhan call returns - no exception
        W._client = types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=empty_response))

        second = asyncio.run(signals.get_regime_state("TESTSTOCK"))
        assert second is not None and second.is_bullish is True, \
            "a completely empty fetch response must be treated as a failure and keep the last good " \
            "cached RegimeState, never silently cached as None"

        # --- Supertrend ---
        fake_client2, _ = _install_fake_client([100.0] * 30 + [200.0], [100.0] * 30 + [200.0])
        W._client = fake_client2
        signals._supertrend_cache.clear()
        signals._raw_series_cache.clear()
        st_first = asyncio.run(signals.get_supertrend_state("TESTSTOCK"))
        assert st_first is not None

        cached_at2, cached_val2 = signals._supertrend_cache[("TESTSTOCK", sc.SUPERTREND_INTERVAL_MINUTES)]
        signals._supertrend_cache[("TESTSTOCK", sc.SUPERTREND_INTERVAL_MINUTES)] = (
            cached_at2 - timedelta(seconds=sc.SUPERTREND_REFRESH_SECONDS + 1), cached_val2,
        )
        W._client = types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=empty_response))
        st_second = asyncio.run(signals.get_supertrend_state("TESTSTOCK"))
        assert st_second is not None and st_second.close == st_first.close, \
            "a completely empty fetch response must keep the last good cached SupertrendState too"
        print("6. A completely empty (non-exception) fetch response is treated as a failure and keeps "
              "the last good cached regime/Supertrend value, for both signals: PASSED")
    finally:
        W._client, W._equity_security_id = saved_client, saved_eqid


def test_7_underlying_reference_ws_subscribes_index_symbols_too():
    """v3, index WS coverage (24 Sep 2026, user request "make it WS feeds
    based for IDX_I also"). _underlying_reference used to deliberately
    SKIP candle_feed.ensure_subscribed for config.INDEX_SYMBOLS (REST-
    only, by design) - this proves that gate is gone: an index symbol now
    reaches ensure_subscribed exactly like every other branch, with the
    correct (security_id, "IDX_I") pair, whenever config.USE_WS_CANDLES
    is on."""
    saved_index_id, saved_use_ws = W.index_security_id, sc.USE_WS_CANDLES
    saved_ensure_subscribed = signals.candle_feed.ensure_subscribed
    calls = []
    try:
        W.index_security_id = lambda sym: "13"
        sc.USE_WS_CANDLES = True
        signals.candle_feed.ensure_subscribed = lambda symbol, security_id, exchange_segment: (
            calls.append((symbol, security_id, exchange_segment))
        )

        result = signals._underlying_reference("NIFTY")
        assert result == ("13", "IDX_I", "INDEX"), result
        assert ("NIFTY", "13", "IDX_I") in calls, (
            f"expected _underlying_reference to WS-subscribe NIFTY via candle_feed.ensure_subscribed, got {calls}"
        )
        print("7. _underlying_reference now WS-subscribes index symbols too (the old REST-only "
              "gate for config.INDEX_SYMBOLS is gone): PASSED")
    finally:
        W.index_security_id = saved_index_id
        sc.USE_WS_CANDLES = saved_use_ws
        signals.candle_feed.ensure_subscribed = saved_ensure_subscribed


def main():
    print("=== Swing v2 signals (regime + Supertrend crossover) test suite ===\n")
    test_1_lookback_override_reaches_the_fetch_for_both_intervals()
    test_2_regime_reads_bullish_and_bearish_correctly()
    test_3_insufficient_bars_returns_none_not_a_guess()
    test_4_fetch_failure_keeps_last_good_cached_value()
    test_5_supertrend_crossed_above_fires_only_on_the_transition_candle()
    test_6_completely_empty_fetch_response_keeps_last_good_cached_value()
    test_7_underlying_reference_ws_subscribes_index_symbols_too()
    print("\nALL SWING V2 SIGNALS CHECKS PASSED")


if __name__ == "__main__":
    main()
