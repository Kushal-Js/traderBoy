"""
Tests for Swing's SWING_ENTRY_TIMING switch (Swing/config.py ENTRY_TIMING,
read by Swing/trading_engine.py's _tick_supertrend_touch / _evaluate_entry_signal).

Background: 28 Sep 2026 the real NATURALGAS 23 OCT 300 CALL was entered on a
one-tick intrabar poke above the 5-min Supertrend line while every 5-min candle
closed back below it. bar_close (the default again) must ignore such pokes.

Coverage:
  1. bar_close: an intrabar poke above the line (last closed candle below it,
     no closed-candle crossover) does NOT produce an entry.
  2. tick (control): the exact same inputs DO produce BULLISH - proves test 1
     exercises the tick path rather than passing for an unrelated reason.
  3. bar_close: a real closed-candle crossover still produces BULLISH.
  4. bar_close: the bearish mirror of 1 (intrabar poke below the line) -> None.
  5. Config: SWING_ENTRY_TIMING unset -> "bar_close"; "tick" is still accepted.

HOW TO RUN:
    uv run python tests/test_swing_entry_timing_bar_close.py
"""
import asyncio
import importlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Swing.config as sc
import Swing.trading_engine as ste
from Swing.signals import RegimeState, SupertrendState
from Options.dhan_client import IST

_REAL = {
    "get_regime_state": ste.signals.get_regime_state,
    "get_supertrend_state": ste.signals.get_supertrend_state,
    "get_day_range_state": ste.signals.get_day_range_state,
}
_REAL_IS_FRESH = ste.candle_feed.is_fresh
_REAL_FORMING_BAR = ste.candle_feed.forming_bar
_REAL_NOW = ste._now_ist

CANDLE = datetime(2026, 9, 28, 15, 30)       # last CLOSED 5-min candle (naive, like the live cache)
NOW = datetime(2026, 9, 28, 15, 36, 41, tzinfo=IST)
LINE = 301.81


async def _resolved(value):
    return value


def _supertrend(is_above: bool, prev_is_above: bool) -> SupertrendState:
    return SupertrendState(candle_start=CANDLE, close=301.9 if is_above else 301.7, supertrend=LINE,
                           is_above=is_above, prev_close=301.9 if prev_is_above else 301.2,
                           prev_supertrend=LINE, prev_is_above=prev_is_above)


def _install(st5, forming_high: float, forming_low: float, regime_bullish: bool = True):
    regime = RegimeState(fast_ema=101.0 if regime_bullish else 99.0, slow_ema=100.0, is_bullish=regime_bullish,
                         fast_candle_start=CANDLE, slow_candle_start=CANDLE,
                         prev_is_bullish=regime_bullish, gap_widened=False)
    ste.signals.get_regime_state = lambda symbol: _resolved(regime)
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _resolved(st5)
    ste.signals.get_day_range_state = lambda symbol: _resolved(None)
    ste.signals._regime_entry_consumed.clear()
    ste.candle_feed.is_fresh = lambda symbol, max_age: True
    ste.candle_feed.forming_bar = lambda symbol: {
        "candle_start": CANDLE + timedelta(minutes=sc.SUPERTREND_INTERVAL_MINUTES),
        "open": 301.7, "high": forming_high, "low": forming_low, "close": 301.7}
    ste._now_ist = lambda: NOW


def _restore():
    for name, fn in _REAL.items():
        setattr(ste.signals, name, fn)
    ste.candle_feed.is_fresh = _REAL_IS_FRESH
    ste.candle_feed.forming_bar = _REAL_FORMING_BAR
    ste._now_ist = _REAL_NOW
    ste.signals._regime_entry_consumed.clear()


def _run(timing: str, *install_args, **install_kwargs):
    real_timing, real_version = sc.ENTRY_TIMING, sc.ENTRY_STRATEGY_VERSION
    sc.ENTRY_TIMING, sc.ENTRY_STRATEGY_VERSION = timing, "v3"
    try:
        _install(*install_args, **install_kwargs)
        return asyncio.run(ste._evaluate_entry_signal("NATURALGAS"))
    finally:
        sc.ENTRY_TIMING, sc.ENTRY_STRATEGY_VERSION = real_timing, real_version
        _restore()


def test_1_bar_close_ignores_intrabar_poke():
    # Last closed candle below the line, no closed crossover, forming high 301.9 > 301.81.
    result = _run("bar_close", _supertrend(is_above=False, prev_is_above=False), 301.9, 301.6)
    assert result is None, f"bar_close must ignore an intrabar poke, got {result!r}"
    print("1. bar_close ignores a one-tick intrabar poke above the Supertrend line: PASSED")


def test_2_tick_control_takes_the_same_poke():
    result = _run("tick", _supertrend(is_above=False, prev_is_above=False), 301.9, 301.6)
    assert result == "BULLISH", f"tick mode should take the poke (control), got {result!r}"
    print("2. control: tick mode takes the same poke as BULLISH: PASSED")


def test_3_bar_close_keeps_closed_candle_crossover():
    # The last closed candle crossed above the line (prev below, now above); forming bar irrelevant.
    result = _run("bar_close", _supertrend(is_above=True, prev_is_above=False), 301.95, 301.85)
    assert result == "BULLISH", f"a closed-candle crossover must still enter, got {result!r}"
    print("3. bar_close still enters on a real closed-candle crossover: PASSED")


def test_4_bar_close_ignores_bearish_poke():
    # Last closed candle above the line, forming low pokes below it, regime bearish.
    result = _run("bar_close", _supertrend(is_above=True, prev_is_above=True), 302.0, 301.7,
                  regime_bullish=False)
    assert result is None, f"bar_close must ignore a bearish intrabar poke, got {result!r}"
    print("4. bar_close ignores the bearish mirror (intrabar poke below the line): PASSED")


def test_5_config_default_is_bar_close():
    real_env = os.environ.pop("SWING_ENTRY_TIMING", None)
    try:
        importlib.reload(sc)
        assert sc.ENTRY_TIMING == "bar_close", f"default must be bar_close, got {sc.ENTRY_TIMING!r}"
        os.environ["SWING_ENTRY_TIMING"] = "tick"
        importlib.reload(sc)
        assert sc.ENTRY_TIMING == "tick", f"'tick' must still be accepted, got {sc.ENTRY_TIMING!r}"
        print("5. SWING_ENTRY_TIMING defaults to bar_close and still accepts tick: PASSED")
    finally:
        if real_env is None:
            os.environ.pop("SWING_ENTRY_TIMING", None)
        else:
            os.environ["SWING_ENTRY_TIMING"] = real_env
        importlib.reload(sc)


if __name__ == "__main__":
    test_1_bar_close_ignores_intrabar_poke()
    test_2_tick_control_takes_the_same_poke()
    test_3_bar_close_keeps_closed_candle_crossover()
    test_4_bar_close_ignores_bearish_poke()
    test_5_config_default_is_bar_close()
    print("\nAll tests passed.")
