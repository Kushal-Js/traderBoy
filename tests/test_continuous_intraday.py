"""
Every intraday indicator in the codebase must compute on a CONTINUOUS
multi-session candle series - not a fresh today-only fetch that leaves
recursive indicators (Supertrend / EMA / RSI / ATR) unusable or unreliable
until ~an hour of fresh candles has warmed them up. User request 10 Sep
2026: "no lag across ALL strategies, calculations run continuously across
sessions, not with a fresh day start like a charting platform".

The single chokepoint is dhan_wrapper.fetch_continuous_intraday(), which
pulls config.INTRADAY_CONTINUOUS_LOOKBACK_DAYS calendar days through today.
This test proves:

  1. fetch_continuous_intraday requests the multi-day window (from_date =
     today - INTRADAY_CONTINUOUS_LOOKBACK_DAYS), wraps the call in _retry,
     and returns resp["data"].
  2. refresh_supertrend_signal routes through it - so the Supertrend line
     is computed on the continuous series (warm bands from bar 1), and a
     bearish read on today's first bar registers immediately.
  3. Swing's intraday _fetch_supertrend_state_once routes through it too.
  4. Every paper engine's intraday fetch helper (IndexScalping /
     CopperOptions / K01) routes through it - no lingering today-only
     intraday_minute_data call anywhere.

HOW TO RUN:
    uv run python tests/test_continuous_intraday.py
"""
import inspect
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import os
os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.dhan_client as odc
import Options.config as ocfg

IST = odc.IST
W = odc.dhan_wrapper


def _bars(day, closes, interval_min):
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    ts, hi, lo = [], [], []
    for i, c in enumerate(closes):
        ts.append(int((start + timedelta(minutes=interval_min * i)).timestamp()))
        hi.append(float(c) + 0.5)
        lo.append(float(c) - 0.5)
    return {"high": hi, "low": lo, "close": [float(c) for c in closes],
            "open": [float(c) for c in closes], "volume": [1000.0] * len(closes), "timestamp": ts}


def test_1_fetch_continuous_intraday_requests_the_multi_day_window():
    saved = W._client
    try:
        seen = {}

        def intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval):
            seen.update(from_date=from_date, to_date=to_date, seg=exchange_segment, itype=instrument_type, interval=interval)
            return {"status": "success", "data": {"close": [1.0, 2.0], "timestamp": [1, 2]}}

        W._client = types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=intraday_minute_data))
        data = W.fetch_continuous_intraday("SID", "NSE_EQ", "EQUITY", 5)

        expected_from = (datetime.now(IST) - timedelta(days=ocfg.INTRADAY_CONTINUOUS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        assert seen["from_date"] == expected_from, f"{seen['from_date']} != {expected_from}"
        assert seen["to_date"] == datetime.now(IST).strftime("%Y-%m-%d")
        assert seen["seg"] == "NSE_EQ" and seen["itype"] == "EQUITY" and seen["interval"] == 5
        assert data == {"close": [1.0, 2.0], "timestamp": [1, 2]}
        assert ocfg.INTRADAY_CONTINUOUS_LOOKBACK_DAYS >= 5, "lookback must span several trading sessions"
        print("1. fetch_continuous_intraday requests today - INTRADAY_CONTINUOUS_LOOKBACK_DAYS .. today: PASSED")
    finally:
        W._client = saved


def test_2_fetch_continuous_intraday_retries_and_survives_a_transient_failure():
    saved = W._client
    try:
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("DH-904 rate limit")
            return {"status": "success", "data": {"close": [9.0], "timestamp": [1]}}

        W._client = types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=flaky))
        data = W.fetch_continuous_intraday("SID", "NSE_EQ", "EQUITY", 5)
        assert calls["n"] == 2, "must retry once after a transient failure"
        assert data == {"close": [9.0], "timestamp": [1]}
        print("2. fetch_continuous_intraday retries a transient Dhan failure (via _retry): PASSED")
    finally:
        W._client = saved


def test_3_refresh_supertrend_signal_uses_the_continuous_series():
    saved_client, saved_eqid, saved_cache = W._client, W._equity_security_id, dict(W._supertrend_cache)
    try:
        W._equity_security_id = lambda sym: "SID"
        today = datetime.now(IST).date()
        yest = today - timedelta(days=1)

        # 30 rising bars yesterday + a hard drop today -> today's last closed
        # bar is well below a WARM Supertrend line. A today-only fetch (4
        # bars) could not have computed this at all (period+1 = 11 needed).
        closes = list(range(100, 130)) + [129, 120, 108, 96, 84]
        d = _bars(yest, list(range(100, 130)), 5)
        d2 = _bars(today, [129, 120, 108, 96, 84], 5)
        merged = {k: d[k] + d2[k] for k in d}

        seen = {}

        def intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval):
            seen["from_date"] = from_date
            return {"status": "success", "data": merged}

        W._client = types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=intraday_minute_data))
        W._supertrend_cache.clear()
        W.refresh_supertrend_signal("STTEST")

        assert seen["from_date"] == (
            datetime.now(IST) - timedelta(days=ocfg.INTRADAY_CONTINUOUS_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d"), "Supertrend fetch must be the continuous multi-day window"
        assert W.get_cached_supertrend_bearish("STTEST") is True, \
            "with a warm Supertrend line, today's slumping close reads bearish immediately"
        assert W.get_cached_supertrend_candle_start("STTEST").date() == today
        print("3. refresh_supertrend_signal computes on the continuous series - warm bands, bearish from bar 1: PASSED")
    finally:
        W._client, W._equity_security_id = saved_client, saved_eqid
        W._supertrend_cache.clear()
        W._supertrend_cache.update(saved_cache)


def test_4_no_today_only_intraday_fetch_remains_in_any_strategy():
    """Every strategy's intraday indicator fetch must go through
    fetch_continuous_intraday. Guard against a regression that reintroduces
    a `from_date=today, to_date=today` intraday_minute_data call."""
    import Swing.signals as ssig
    import IndexScalping.paper_engine as ise
    import CopperOptions.paper_engine as cop
    import K01.paper_engine as k01

    offenders = []
    checks = [
        # Moved from Swing/trading_engine.py to Swing/signals.py in the Swing
        # v2 rewrite (12 Sep 2026) - same function, new home. _fetch_regime_
        # state_once is new in that rewrite (the 200-EMA regime signal) and
        # gets the identical guard from day one.
        (ssig, ["_fetch_supertrend_state_once", "_fetch_regime_state_once"]),
        (ise, ["_fetch_index_intraday"]),
        (cop, ["_fetch_future_5min"]),
        (k01, ["_fetch_intraday"]),
    ]
    for mod, fns in checks:
        for fn in fns:
            src = inspect.getsource(getattr(mod, fn))
            if "fetch_continuous_intraday" not in src:
                offenders.append(f"{mod.__name__}.{fn} does not call fetch_continuous_intraday")
            if "intraday_minute_data" in src:
                offenders.append(f"{mod.__name__}.{fn} still calls intraday_minute_data directly")
    # dhan_client's own signal refreshers
    for fn in ["refresh_supertrend_signal", "refresh_ema_cross_signal", "refresh_liquidity_signal", "refresh_rsi_signal"]:
        src = inspect.getsource(getattr(odc.DhanWrapper, fn))
        if "fetch_continuous_intraday" not in src:
            offenders.append(f"dhan_client.{fn} does not call fetch_continuous_intraday")
        if "intraday_minute_data" in src:
            offenders.append(f"dhan_client.{fn} still calls intraday_minute_data directly")

    assert not offenders, "today-only / direct intraday fetch still present:\n  " + "\n  ".join(offenders)
    print("4. every strategy + dhan_client signal refresher routes through fetch_continuous_intraday: PASSED")


if __name__ == "__main__":
    print("=== Continuous multi-session intraday history test suite ===\n")
    test_1_fetch_continuous_intraday_requests_the_multi_day_window()
    test_2_fetch_continuous_intraday_retries_and_survives_a_transient_failure()
    test_3_refresh_supertrend_signal_uses_the_continuous_series()
    test_4_no_today_only_intraday_fetch_remains_in_any_strategy()
    print("\nALL CONTINUOUS INTRADAY CHECKS PASSED")
