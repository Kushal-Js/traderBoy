"""
Tests for Swing/signals.py's WS/REST hybrid fetch (_get_intraday_series,
config.USE_WS_CANDLES) - added 23 Sep 2026 alongside Swing/candle_feed.py
to stop Swing's regime/Supertrend REST fetches from hitting Dhan's
account-wide DH-904 rate limit (see that module's own docstring).

Same `W._client` mocking pattern as tests/test_swing_v2_signals.py -
these tests focus on the NEW decision logic only (does it call WS or
REST, and does it ever pick WS when WS doesn't actually have enough
history), not the EMA/Supertrend math itself (already covered there).

Covers:
  1. USE_WS_CANDLES=False -> always REST, regardless of what the local
     candle feed holds (byte-identical to pre-23-Sep behavior).
  2. USE_WS_CANDLES=True + fresh + enough bars -> WS data used, REST
     never called.
  3. USE_WS_CANDLES=True + fresh but NOT enough bars -> falls back to
     REST (the hybrid switch must never be WORSE than pure REST).
  4. USE_WS_CANDLES=True + stale -> falls back to REST.
  5. End-to-end: get_regime_state/get_supertrend_state through the real
     public functions, with a fully warmed local WS series, produce the
     SAME reading REST would have for identical underlying data, and
     genuinely make zero REST calls doing it.

HOW TO RUN:
    uv run python tests/test_swing_ws_candle_hybrid.py
"""
import asyncio
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")

import Options.dhan_client as odc  # noqa: E402
import Swing.candle_feed as cf  # noqa: E402
import Swing.config as sc  # noqa: E402
import Swing.signals as signals  # noqa: E402

IST = odc.IST
W = odc.dhan_wrapper


def _t(h: int, m: int) -> datetime:
    return datetime(2026, 9, 23, h, m, tzinfo=IST)


def _seed_ws_bars(symbol: str, security_id: str, n: int) -> None:
    with cf._lock:
        st = cf._SymbolState(security_id)
        st.last_tick_at = datetime.now(IST)
        bars = []
        t = _t(9, 15)
        for i in range(n):
            bars.append({
                "candle_start": t, "open": 100.0 + i, "high": 101.0 + i,
                "low": 99.0 + i, "close": 100.0 + i, "volume": 10.0,
            })
            t = t + timedelta(minutes=5)
        st.bars = bars
        cf._state[symbol] = st
        cf._subscribed_ref[symbol] = (security_id, "NSE_EQ")


def _failing_rest_client():
    def intraday_minute_data(**kwargs):
        raise AssertionError("REST fetch_continuous_intraday must NOT be called on this path")
    return types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=intraday_minute_data))


def _counting_rest_client(closes: list[float]):
    """A REST stand-in whose interval=5 replies with `closes` verbatim, so
    tests that only ever request the 5-min series (1-4) can pass a flat
    list. Test 5 instead needs a REAL Dhan-shaped multi-interval mock -
    see _counting_rest_client_from_base_bars below - since Swing's regime
    fetch requests BOTH 5-min and 15-min series and a real REST endpoint
    would never reply with the same 5-min-spaced data for both."""
    calls = []

    def intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval):
        calls.append(interval)
        ts = [int((_t(9, 15) + timedelta(minutes=interval * i)).timestamp()) for i in range(len(closes))]
        return {"status": "success", "data": {
            "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
            "close": closes, "volume": [10.0] * len(closes), "timestamp": ts,
        }}
    return types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=intraday_minute_data)), calls


def _counting_rest_client_from_base_bars(base_5m_bars: list[dict]):
    """Real multi-interval REST stand-in: resamples the SAME base 5-min
    bars to whatever interval is actually requested (via candle_feed's
    own, already-tested _resample), exactly like a real Dhan REST fetch
    for a genuinely different candle interval would return genuinely
    different (aggregated) data - never the same series reinterpreted at
    a different spacing."""
    calls = []

    def intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval):
        calls.append(interval)
        resampled = cf._resample(base_5m_bars, interval)
        return {"status": "success", "data": {
            "open": [b["open"] for b in resampled], "high": [b["high"] for b in resampled],
            "low": [b["low"] for b in resampled], "close": [b["close"] for b in resampled],
            "volume": [b["volume"] for b in resampled],
            "timestamp": [b["candle_start"].timestamp() for b in resampled],
        }}
    return types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=intraday_minute_data)), calls


def test_1_flag_off_always_uses_rest_regardless_of_ws_state():
    saved_flag = sc.USE_WS_CANDLES
    saved_client = W._client
    try:
        sc.USE_WS_CANDLES = False
        _seed_ws_bars("SYM1", "SID1", 500)  # plenty of WS history available
        client, calls = _counting_rest_client([100.0] * 50)
        W._client = client
        data = signals._get_intraday_series("SYM1", "SID1", "NSE_EQ", "EQUITY", 5, min_bars=10)
        assert calls == [5], "flag off must always go to REST, never touch the WS-populated series"
        assert len(data["close"]) == 50
        print("1. USE_WS_CANDLES=False always uses REST, even with ample local WS history: PASSED")
    finally:
        sc.USE_WS_CANDLES = saved_flag
        W._client = saved_client


def test_2_flag_on_fresh_and_enough_bars_uses_ws_zero_rest_calls():
    saved_flag = sc.USE_WS_CANDLES
    saved_client = W._client
    try:
        sc.USE_WS_CANDLES = True
        _seed_ws_bars("SYM2", "SID2", 300)
        W._client = _failing_rest_client()  # any REST call here is itself a test failure
        data = signals._get_intraday_series("SYM2", "SID2", "NSE_EQ", "EQUITY", 5, min_bars=200)
        assert len(data["close"]) == 300, "must return the full local WS series, not a truncated one"
        print("2. USE_WS_CANDLES=True + fresh + enough bars: WS series used, zero REST calls: PASSED")
    finally:
        sc.USE_WS_CANDLES = saved_flag
        W._client = saved_client


def test_3_flag_on_but_not_enough_ws_bars_falls_back_to_rest():
    saved_flag = sc.USE_WS_CANDLES
    saved_client = W._client
    try:
        sc.USE_WS_CANDLES = True
        _seed_ws_bars("SYM3", "SID3", 50)  # below the 200 min_bars this call requires
        client, calls = _counting_rest_client([100.0] * 250)
        W._client = client
        data = signals._get_intraday_series("SYM3", "SID3", "NSE_EQ", "EQUITY", 5, min_bars=200)
        assert calls == [5], "insufficient WS history must fall back to REST, not silently under-serve the caller"
        assert len(data["close"]) == 250
        print("3. USE_WS_CANDLES=True but not enough WS bars yet: correctly falls back to REST: PASSED")
    finally:
        sc.USE_WS_CANDLES = saved_flag
        W._client = saved_client


def test_4_flag_on_but_stale_falls_back_to_rest():
    saved_flag = sc.USE_WS_CANDLES
    saved_client = W._client
    try:
        sc.USE_WS_CANDLES = True
        _seed_ws_bars("SYM4", "SID4", 300)
        with cf._lock:
            cf._state["SYM4"].last_tick_at = datetime.now(IST) - timedelta(seconds=999)  # way stale
        client, calls = _counting_rest_client([100.0] * 250)
        W._client = client
        data = signals._get_intraday_series("SYM4", "SID4", "NSE_EQ", "EQUITY", 5, min_bars=200)
        assert calls == [5], "a stale local feed must fall back to REST even with plenty of (stale) bars"
        assert len(data["close"]) == 250
        print("4. USE_WS_CANDLES=True but the local feed is stale: correctly falls back to REST: PASSED")
    finally:
        sc.USE_WS_CANDLES = saved_flag
        W._client = saved_client


def test_5_end_to_end_regime_and_supertrend_match_rest_with_zero_rest_calls():
    saved_flag = sc.USE_WS_CANDLES
    saved_client, saved_eqid = W._client, W._equity_security_id
    try:
        sc.USE_WS_CANDLES = True
        W._equity_security_id = lambda sym: "SID_E2E"
        signals._regime_cache.clear()
        signals._supertrend_cache.clear()

        # Enough bars for BOTH the 5-min (>=200) and 15-min-after-resample (>=200*3=600 base
        # bars) regime EMA, plus Supertrend's own much smaller requirement - a clear uptrend
        # so both fast/slow EMAs and Supertrend have an unambiguous, non-degenerate reading.
        n = 650
        with cf._lock:
            st = cf._SymbolState("SID_E2E")
            st.last_tick_at = datetime.now(IST)
            bars = []
            t = _t(9, 15)
            price = 100.0
            for i in range(n):
                price += 0.1
                bars.append({
                    "candle_start": t, "open": price - 0.05, "high": price + 0.5,
                    "low": price - 0.5, "close": price, "volume": 10.0,
                })
                t = t + timedelta(minutes=5)
            st.bars = bars
            cf._state["SYM_E2E"] = st
            cf._subscribed_ref["SYM_E2E"] = ("SID_E2E", "NSE_EQ")

        W._client = _failing_rest_client()
        ws_regime = asyncio.run(signals.get_regime_state("SYM_E2E"))
        ws_supertrend = asyncio.run(signals.get_supertrend_state("SYM_E2E"))
        assert ws_regime is not None and ws_supertrend is not None, \
            "a fully warmed local WS series must produce a real reading, not None"

        # Now recompute the SAME underlying series via the REST path directly (flag off) and
        # confirm the two agree - the hybrid switch must never change WHAT is computed, only
        # where the candles come from. The REST mock resamples the SAME base 5m bars per
        # requested interval (see _counting_rest_client_from_base_bars) - a flat single-series
        # stand-in would wrongly feed the SAME 5-min-spaced data to both the fast AND slow
        # request, which a real REST endpoint never would.
        sc.USE_WS_CANDLES = False
        signals._regime_cache.clear()
        signals._supertrend_cache.clear()
        client, _ = _counting_rest_client_from_base_bars(bars)
        W._client = client
        rest_regime = asyncio.run(signals.get_regime_state("SYM_E2E"))
        rest_supertrend = asyncio.run(signals.get_supertrend_state("SYM_E2E"))

        assert ws_regime.is_bullish == rest_regime.is_bullish
        assert abs(ws_regime.fast_ema - rest_regime.fast_ema) < 1e-6
        assert ws_supertrend.is_above == rest_supertrend.is_above
        print("5. End-to-end regime+Supertrend via WS match the REST-computed reading exactly, zero REST calls: PASSED")
    finally:
        sc.USE_WS_CANDLES = saved_flag
        W._client, W._equity_security_id = saved_client, saved_eqid


def main():
    with cf._lock:
        cf._state.clear()
        cf._subscribed_ref.clear()
    try:
        print("=== Swing WS/REST hybrid fetch (_get_intraday_series) test suite ===\n")
        test_1_flag_off_always_uses_rest_regardless_of_ws_state()
        test_2_flag_on_fresh_and_enough_bars_uses_ws_zero_rest_calls()
        test_3_flag_on_but_not_enough_ws_bars_falls_back_to_rest()
        test_4_flag_on_but_stale_falls_back_to_rest()
        test_5_end_to_end_regime_and_supertrend_match_rest_with_zero_rest_calls()
        print("\nALL SWING WS/REST HYBRID TESTS PASSED")
    finally:
        with cf._lock:
            cf._state.clear()
            cf._subscribed_ref.clear()


if __name__ == "__main__":
    main()
