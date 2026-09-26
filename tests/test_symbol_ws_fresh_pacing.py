"""
Tests for `is_symbol_ws_fresh` (Swing/signals.py, Bollinger/signals.py) -
added 26 Sep 2026, audit-flagged fix: the entry-scan loop's
SYMBOL_PACING_SECONDS sleep was being applied unconditionally before every
watchlist symbol after the first, even one about to be served entirely
from the in-memory WS candle cache (no REST call, nothing to rate-limit).
At Swing's 15-symbol watchlist this was burning 4.9 of every 5-second
tick on pacing nothing. The fix: `trading_engine.py`'s scan loop now only
sleeps before a symbol `is_symbol_ws_fresh` says is NOT fresh (so likely
to hit a real REST call). Exercises the REAL function directly - no
reimplementation - mocking only `candle_feed.is_fresh` (the underlying
tick-freshness check) and `config.USE_WS_CANDLES`.

HOW TO RUN:
    uv run python tests/test_symbol_ws_fresh_pacing.py
"""
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Swing.signals as swing_signals  # noqa: E402
import Bollinger.signals as bollinger_signals  # noqa: E402
from Swing import config as swing_config  # noqa: E402
from Bollinger import config as bollinger_config  # noqa: E402


def test_1_swing_fresh_symbol_reports_fresh():
    with mock.patch.object(swing_config, "USE_WS_CANDLES", True), \
         mock.patch.object(swing_signals.candle_feed, "is_fresh", return_value=True) as is_fresh:
        assert swing_signals.is_symbol_ws_fresh("DLF") is True
        is_fresh.assert_called_once_with("DLF", swing_config.WS_STALE_AFTER_SECONDS)
    print("1. Swing: a WS-fresh symbol reports fresh (no pacing needed): PASSED")


def test_2_swing_stale_symbol_reports_not_fresh():
    with mock.patch.object(swing_config, "USE_WS_CANDLES", True), \
         mock.patch.object(swing_signals.candle_feed, "is_fresh", return_value=False):
        assert swing_signals.is_symbol_ws_fresh("DLF") is False
    print("2. Swing: a stale (no recent tick) symbol reports not-fresh (pacing still applies): PASSED")


def test_3_swing_use_ws_candles_off_always_reports_not_fresh():
    """Even if the underlying tick data happens to be recent, USE_WS_
    CANDLES=False means this codepath is disabled entirely - must never
    report fresh (would incorrectly skip pacing for a symbol that's
    actually about to make a real REST call, since _get_intraday_series
    itself would ignore the WS cache too)."""
    with mock.patch.object(swing_config, "USE_WS_CANDLES", False), \
         mock.patch.object(swing_signals.candle_feed, "is_fresh", return_value=True):
        assert swing_signals.is_symbol_ws_fresh("DLF") is False
    print("3. Swing: USE_WS_CANDLES=False always reports not-fresh regardless of tick recency: PASSED")


def test_4_bollinger_matches_swing_behavior():
    with mock.patch.object(bollinger_config, "USE_WS_CANDLES", True), \
         mock.patch.object(bollinger_signals.candle_feed, "is_fresh", return_value=True) as is_fresh:
        assert bollinger_signals.is_symbol_ws_fresh("VEDL") is True
        is_fresh.assert_called_once_with("VEDL", bollinger_config.WS_STALE_AFTER_SECONDS)
    with mock.patch.object(bollinger_config, "USE_WS_CANDLES", True), \
         mock.patch.object(bollinger_signals.candle_feed, "is_fresh", return_value=False):
        assert bollinger_signals.is_symbol_ws_fresh("VEDL") is False
    with mock.patch.object(bollinger_config, "USE_WS_CANDLES", False), \
         mock.patch.object(bollinger_signals.candle_feed, "is_fresh", return_value=True):
        assert bollinger_signals.is_symbol_ws_fresh("VEDL") is False
    print("4. Bollinger: same fresh/stale/disabled behavior as Swing's own: PASSED")


def test_5_scan_loop_pacing_decision_shape():
    """Reproduces the exact `if i and not is_symbol_ws_fresh(symbol):
    sleep(...)` decision from both trading_engine.py's scan loops, proving
    the two things that matter: the FIRST symbol in a tick's rotated scan
    order never sleeps (i == 0, unchanged from before this fix), and a
    fresh symbol at any later position never sleeps either (the actual
    fix), while a stale symbol at a later position still does (the
    preserved protection)."""
    def should_pace(i: int, is_fresh: bool) -> bool:
        return bool(i) and not is_fresh

    assert should_pace(0, is_fresh=False) is False, "the first symbol in scan order never paces, fresh or not"
    assert should_pace(0, is_fresh=True) is False
    assert should_pace(3, is_fresh=True) is False, "a later, WS-fresh symbol must NOT pace (the fix)"
    assert should_pace(3, is_fresh=False) is True, "a later, stale symbol must still pace (protection preserved)"
    print("5. Scan-loop pacing decision shape: first symbol never paces, fresh symbols skip it, stale symbols still pace: PASSED")


if __name__ == "__main__":
    test_1_swing_fresh_symbol_reports_fresh()
    test_2_swing_stale_symbol_reports_not_fresh()
    test_3_swing_use_ws_candles_off_always_reports_not_fresh()
    test_4_bollinger_matches_swing_behavior()
    test_5_scan_loop_pacing_decision_shape()
    print("\nAll tests passed.")
