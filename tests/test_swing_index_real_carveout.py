"""
Tests for Swing/trading_engine.py's `_should_paper_trade` INDEX_REAL_
CARVEOUT_ENABLED branch (added 28 Sep 2026, user request: "enable real
trading for NIFTY/BANKNIFTY under SWING now") - mirrors the MCX carve-out
(see test_swing_mcx_paper_carveout.py) applied to INDEX_SYMBOLS instead.
Same highest-risk reasoning as that file: a wrong routing decision here
silently sends a real-money order to the paper engine or a paper
candidate to the real engine. Exercises the REAL `_should_paper_trade`
function directly - no reimplementation.

HOW TO RUN:
    uv run python tests/test_swing_index_real_carveout.py
"""
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Swing.trading_engine as trading_engine  # noqa: E402
from Swing import config  # noqa: E402


def _flags(**overrides):
    defaults = {
        "INDEX_PAPER_MODE_ENABLED": False,
        "MCX_PAPER_MODE_ENABLED": False,
        "INDEX_REAL_CARVEOUT_ENABLED": False,
    }
    defaults.update(overrides)
    patchers = [mock.patch.object(config, name, value) for name, value in defaults.items()]
    return patchers


def _apply(patchers):
    for p in patchers:
        p.start()
    return patchers


def _stop(patchers):
    for p in patchers:
        p.stop()


def test_1_index_goes_real_when_carveout_on_and_global_paper_is_on():
    """The core scenario this change exists for: global Swing paper mode
    ON, INDEX_REAL_CARVEOUT_ENABLED ON, INDEX_PAPER_MODE_ENABLED off (the
    user's requested configuration) - NIFTY/BANKNIFTY must trade REAL,
    completely ignoring the global flag, exactly like MCX already does."""
    patchers = _apply(_flags(INDEX_REAL_CARVEOUT_ENABLED=True, INDEX_PAPER_MODE_ENABLED=False))
    try:
        with mock.patch.object(trading_engine.paper_mode_control, "is_paper_mode_enabled", return_value=True), \
             mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=False) as is_mcx:
            assert trading_engine._should_paper_trade("NIFTY") is False, \
                "NIFTY must trade REAL when the carve-out is on, even while global Swing paper mode is on"
            assert trading_engine._should_paper_trade("BANKNIFTY") is False, \
                "BANKNIFTY must trade REAL when the carve-out is on, even while global Swing paper mode is on"
        assert is_mcx.call_count == 2
    finally:
        _stop(patchers)
    print("1. NIFTY/BANKNIFTY stay REAL when INDEX_REAL_CARVEOUT_ENABLED is on and global paper mode is on: PASSED")


def test_2_equity_unaffected_by_index_carveout():
    """Same configuration as test 1, but for a plain NSE-equity symbol - it
    must still go to paper, since the carve-out only applies to
    INDEX_SYMBOLS and the global flag is exactly what's supposed to catch
    everything else."""
    patchers = _apply(_flags(INDEX_REAL_CARVEOUT_ENABLED=True, INDEX_PAPER_MODE_ENABLED=False))
    try:
        with mock.patch.object(trading_engine.paper_mode_control, "is_paper_mode_enabled", return_value=True), \
             mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=False):
            assert trading_engine._should_paper_trade("ASHOKLEY") is True, \
                "a plain NSE-equity symbol must be unaffected by INDEX_REAL_CARVEOUT_ENABLED"
    finally:
        _stop(patchers)
    print("2. NSE-equity symbol (ASHOKLEY) unaffected by INDEX_REAL_CARVEOUT_ENABLED: PASSED")


def test_3_index_goes_to_paper_when_carveout_on_but_index_paper_flag_also_on():
    """Under the carve-out, INDEX_PAPER_MODE_ENABLED becomes the SOLE
    decision for index symbols - if it's explicitly turned on too, index
    must go to paper even with the carve-out active, mirroring
    MCX_PAPER_MODE_ENABLED=True still paper-trading MCX."""
    patchers = _apply(_flags(INDEX_REAL_CARVEOUT_ENABLED=True, INDEX_PAPER_MODE_ENABLED=True))
    try:
        with mock.patch.object(trading_engine.paper_mode_control, "is_paper_mode_enabled", return_value=False), \
             mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=False):
            assert trading_engine._should_paper_trade("NIFTY") is True, \
                "INDEX_PAPER_MODE_ENABLED=True must still paper-trade NIFTY under the carve-out"
    finally:
        _stop(patchers)
    print("3. Index goes to PAPER under the carve-out when INDEX_PAPER_MODE_ENABLED is also explicitly on: PASSED")


def test_4_mcx_and_existing_behavior_unaffected_by_new_flag_default():
    """Regression guard: with INDEX_REAL_CARVEOUT_ENABLED at its default
    (false), every pre-existing behavior (MCX carve-out, plain ADD-on
    INDEX_PAPER_MODE_ENABLED, global flag) is completely unchanged - this
    is the same set of assertions test_swing_mcx_paper_carveout.py's own
    tests 1/2/5 already make, re-checked here against the new code path."""
    patchers = _apply(_flags(MCX_PAPER_MODE_ENABLED=False, INDEX_PAPER_MODE_ENABLED=True))
    try:
        with mock.patch.object(trading_engine.paper_mode_control, "is_paper_mode_enabled", return_value=True), \
             mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", side_effect=lambda s: s == "COPPER"):
            assert trading_engine._should_paper_trade("COPPER") is False, \
                "MCX must still trade REAL, unaffected by the new index carve-out flag's default"
            assert trading_engine._should_paper_trade("NIFTY") is True, \
                "INDEX_PAPER_MODE_ENABLED's old ADD-on behavior must still paper-trade NIFTY when the new " \
                "carve-out flag is off (its default)"
            assert trading_engine._should_paper_trade("ASHOKLEY") is True, \
                "plain NSE-equity must still paper-trade under the global flag, unaffected by this change"
    finally:
        _stop(patchers)
    print("4. Pre-existing MCX/global/ADD-on-index behavior unchanged by the new flag's default: PASSED")


if __name__ == "__main__":
    test_1_index_goes_real_when_carveout_on_and_global_paper_is_on()
    test_2_equity_unaffected_by_index_carveout()
    test_3_index_goes_to_paper_when_carveout_on_but_index_paper_flag_also_on()
    test_4_mcx_and_existing_behavior_unaffected_by_new_flag_default()
    print("\nAll tests passed.")
