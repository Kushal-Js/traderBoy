"""
Tests for Swing/trading_engine.py's _evaluate_entry_signal, specifically
the "v1"/"v2" branch added 14 Sep 2026 (config.ENTRY_STRATEGY_VERSION -
see Swing/config.py's own docstring for the full user request and
backtest numbers behind this).

Coverage:
  1. v1 (default) behaves byte-identically to the original 12 Sep design -
     the 15-min Supertrend's state is fetched at all under v2 only, and
     has ZERO effect on v1's decision.
  2/3. v2 admits a BULLISH entry when EITHER higher-timeframe filter
     agrees (15-min Supertrend alone, or EMA-regime alone) - this is the
     whole point of the OR-combination, so both single-filter-agreeing
     cases need their own explicit coverage, not just the "both agree"
     case v1 already exercises.
  4. v2's mirrored BEARISH case.
  5. v2 correctly REJECTS when both higher-timeframe filters disagree
     with the crossover's own direction, even though a crossover event
     did occur - the OR only ever admits MORE entries than v1, it never
     invents a direction neither filter supports.

HOW TO RUN:
    uv run python tests/test_swing_v2_entry_strategy_version.py
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Swing.config as sc
import Swing.trading_engine as ste
from Swing.signals import RegimeState, SupertrendState

# Captured at import time, before any test (in this file or any other
# test_swing_v2_*.py file collected in the same pytest run) can have
# monkeypatched it - ste.signals IS the real Swing.signals module object,
# not a copy, so a test that overwrites ste.signals.get_regime_state/
# get_supertrend_state and never restores it leaks that fake into every
# OTHER test file that runs afterward in the same pytest process (see
# tests/test_swing_v2_exit_reason.py's own _REAL_GET_SUPERTREND_STATE for
# the real incident - 14 Sep 2026 - this pattern fixes across the whole
# suite). Every test below restores both from these two references.
_REAL_GET_REGIME_STATE = ste.signals.get_regime_state
_REAL_GET_SUPERTREND_STATE = ste.signals.get_supertrend_state


def _regime(is_bullish: bool) -> RegimeState:
    return RegimeState(fast_ema=101.0 if is_bullish else 99.0, slow_ema=100.0, is_bullish=is_bullish,
                        fast_candle_start=datetime(2026, 9, 1, 9, 20), slow_candle_start=datetime(2026, 9, 1, 9, 15))


def _supertrend(is_above: bool, prev_is_above: bool) -> SupertrendState:
    return SupertrendState(candle_start=datetime(2026, 9, 1, 9, 20), close=101.0 if is_above else 99.0,
                            supertrend=100.0, is_above=is_above, prev_close=99.0 if prev_is_above else 101.0,
                            prev_supertrend=100.0, prev_is_above=prev_is_above)


async def _resolved(value):
    return value


def _install(regime_state, st5_state, st15_state):
    """Mocks BOTH signals.get_regime_state and get_supertrend_state.
    get_supertrend_state's real signature is (symbol, interval_minutes=
    None) since 14 Sep 2026 (the v2 15-min instance) - accept and ignore
    the second arg so v1's own single-arg call pattern keeps working
    unchanged and v2's two-arg call is handled by returning st15_state
    whenever a non-default interval is actually passed."""
    ste.signals.get_regime_state = lambda symbol: _resolved(regime_state)

    def fake_get_supertrend_state(symbol, interval_minutes=None):
        if interval_minutes is not None and interval_minutes != sc.SUPERTREND_INTERVAL_MINUTES:
            return _resolved(st15_state)
        return _resolved(st5_state)

    ste.signals.get_supertrend_state = fake_get_supertrend_state


def test_1_v1_ignores_15min_supertrend_entirely():
    real_version = sc.ENTRY_STRATEGY_VERSION
    sc.ENTRY_STRATEGY_VERSION = "v1"
    try:
        # Regime bearish (blocks BULLISH under v1), 15-min ST bullish (would
        # admit BULLISH under v2), 5-min crossed_above fires.
        _install(_regime(is_bullish=False), _supertrend(is_above=True, prev_is_above=False),
                  _supertrend(is_above=True, prev_is_above=True))
        result = asyncio.run(ste._evaluate_entry_signal("TESTSTOCK"))
        assert result is None, f"v1 must ignore the 15-min Supertrend and only look at regime, got {result!r}"
        print("1. v1 ignores the 15-min Supertrend entirely (regime alone gates it, unchanged): PASSED")
    finally:
        sc.ENTRY_STRATEGY_VERSION = real_version
        ste.signals.get_regime_state = _REAL_GET_REGIME_STATE
        ste.signals.get_supertrend_state = _REAL_GET_SUPERTREND_STATE


def test_2_v2_admits_bullish_via_15min_supertrend_alone():
    real_version = sc.ENTRY_STRATEGY_VERSION
    sc.ENTRY_STRATEGY_VERSION = "v2"
    try:
        # Regime bearish (v1 would block), 15-min ST bullish (v2's OR admits it), 5-min crossed_above.
        _install(_regime(is_bullish=False), _supertrend(is_above=True, prev_is_above=False),
                  _supertrend(is_above=True, prev_is_above=True))
        result = asyncio.run(ste._evaluate_entry_signal("TESTSTOCK"))
        assert result == "BULLISH", f"expected BULLISH via the 15-min-ST-alone OR branch, got {result!r}"
        print("2. v2 admits BULLISH when only the 15-min Supertrend agrees (EMA-regime disagrees): PASSED")
    finally:
        sc.ENTRY_STRATEGY_VERSION = real_version
        ste.signals.get_regime_state = _REAL_GET_REGIME_STATE
        ste.signals.get_supertrend_state = _REAL_GET_SUPERTREND_STATE


def test_3_v2_admits_bullish_via_ema_regime_alone():
    real_version = sc.ENTRY_STRATEGY_VERSION
    sc.ENTRY_STRATEGY_VERSION = "v2"
    try:
        # Regime bullish (agrees), 15-min ST bearish (would block alone), 5-min crossed_above.
        _install(_regime(is_bullish=True), _supertrend(is_above=True, prev_is_above=False),
                  _supertrend(is_above=False, prev_is_above=False))
        result = asyncio.run(ste._evaluate_entry_signal("TESTSTOCK"))
        assert result == "BULLISH", f"expected BULLISH via the EMA-regime-alone OR branch, got {result!r}"
        print("3. v2 admits BULLISH when only the EMA-regime agrees (15-min Supertrend disagrees): PASSED")
    finally:
        sc.ENTRY_STRATEGY_VERSION = real_version
        ste.signals.get_regime_state = _REAL_GET_REGIME_STATE
        ste.signals.get_supertrend_state = _REAL_GET_SUPERTREND_STATE


def test_4_v2_mirrors_bearish_case():
    real_version = sc.ENTRY_STRATEGY_VERSION
    sc.ENTRY_STRATEGY_VERSION = "v2"
    try:
        # Regime bullish (would block BEARISH alone), 15-min ST bearish (OR admits it), 5-min crossed_below.
        _install(_regime(is_bullish=True), _supertrend(is_above=False, prev_is_above=True),
                  _supertrend(is_above=False, prev_is_above=False))
        result = asyncio.run(ste._evaluate_entry_signal("TESTSTOCK"))
        assert result == "BEARISH", f"expected BEARISH via the 15-min-ST-alone OR branch, got {result!r}"
        print("4. v2's BEARISH mirror admits the entry when only the 15-min Supertrend agrees: PASSED")
    finally:
        sc.ENTRY_STRATEGY_VERSION = real_version
        ste.signals.get_regime_state = _REAL_GET_REGIME_STATE
        ste.signals.get_supertrend_state = _REAL_GET_SUPERTREND_STATE


def test_5_v2_rejects_when_both_filters_disagree_with_the_crossover():
    real_version = sc.ENTRY_STRATEGY_VERSION
    sc.ENTRY_STRATEGY_VERSION = "v2"
    try:
        # Regime bearish AND 15-min ST bearish, but the 5-min crossed ABOVE
        # (a real crossover event) - the OR must not invent a BULLISH signal
        # neither higher-timeframe filter actually supports.
        _install(_regime(is_bullish=False), _supertrend(is_above=True, prev_is_above=False),
                  _supertrend(is_above=False, prev_is_above=False))
        result = asyncio.run(ste._evaluate_entry_signal("TESTSTOCK"))
        assert result is None, f"expected no signal when both filters disagree with the crossover, got {result!r}"
        print("5. v2 correctly rejects a crossover neither higher-timeframe filter supports: PASSED")
    finally:
        sc.ENTRY_STRATEGY_VERSION = real_version
        ste.signals.get_regime_state = _REAL_GET_REGIME_STATE
        ste.signals.get_supertrend_state = _REAL_GET_SUPERTREND_STATE


if __name__ == "__main__":
    test_1_v1_ignores_15min_supertrend_entirely()
    test_2_v2_admits_bullish_via_15min_supertrend_alone()
    test_3_v2_admits_bullish_via_ema_regime_alone()
    test_4_v2_mirrors_bearish_case()
    test_5_v2_rejects_when_both_filters_disagree_with_the_crossover()
    print("\nAll tests passed.")
