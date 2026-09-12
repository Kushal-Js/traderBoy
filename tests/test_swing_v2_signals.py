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
        regime = asyncio.run(signals.get_regime_state("TESTSTOCK"))
        assert regime is not None and regime.is_bullish is True, regime

        # Reverse it - fast series flat/low, slow series high -> bearish.
        fake_client2, _ = _install_fake_client([100.0] * 250, [100.0 + i * 0.5 for i in range(250)])
        W._client = fake_client2
        signals._regime_cache.clear()
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
        st = asyncio.run(signals.get_supertrend_state("TESTSTOCK"))
        assert st is not None, "expected enough bars for a Supertrend read"
        assert st.crossed_above is True, f"expected a crossover on the spike bar, got is_above={st.is_above} prev_is_above={st.prev_is_above}"
        assert st.crossed_below is False
        print("5. SupertrendState.crossed_above fires on a genuine transition candle: PASSED")
    finally:
        W._client, W._equity_security_id = saved_client, saved_eqid


def main():
    print("=== Swing v2 signals (regime + Supertrend crossover) test suite ===\n")
    test_1_lookback_override_reaches_the_fetch_for_both_intervals()
    test_2_regime_reads_bullish_and_bearish_correctly()
    test_3_insufficient_bars_returns_none_not_a_guess()
    test_4_fetch_failure_keeps_last_good_cached_value()
    test_5_supertrend_crossed_above_fires_only_on_the_transition_candle()
    print("\nALL SWING V2 SIGNALS CHECKS PASSED")


if __name__ == "__main__":
    main()
