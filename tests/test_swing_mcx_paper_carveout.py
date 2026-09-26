"""
Tests for Swing/trading_engine.py's `_should_paper_trade` (added 26 Sep
2026, user request: "Enable paper trading for SWING V3 (disable real
trading) by flag but only allow MCX trades to be through for SWING (allow
real trading for MCX entries only, have a separate flag for them and keep
it disabled for paper trading for MCX only). Test and deploy this
change.") - the highest-risk part of this change: a wrong routing
decision here silently sends a real-money order to the paper engine (lost
profit, no real position) or a paper candidate to the real engine (an
unintended real order). Exercises the REAL `_should_paper_trade` function
directly - no reimplementation - mocking only `dhan_wrapper.is_mcx_
commodity` (the network/instrument-master boundary) and the relevant
config flags, same convention as tests/test_bollinger_mcx_index_dispatch.py.

HOW TO RUN:
    uv run python tests/test_swing_mcx_paper_carveout.py
"""
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Swing.trading_engine as trading_engine  # noqa: E402
from Swing import config  # noqa: E402


def _flags(**overrides):
    """Patches the two config flags `_should_paper_trade` reads directly
    (INDEX_PAPER_MODE_ENABLED, MCX_PAPER_MODE_ENABLED), defaulting both to
    off. The GLOBAL flag (config.PAPER_MODE_ENABLED) is deliberately NOT
    patched here - `_should_paper_trade` never reads it directly, only via
    `paper_mode_control.is_paper_mode_enabled("Swing")`, which each test
    mocks explicitly instead (that function's own .env-fallback vs
    runtime-override logic is out of scope for these tests)."""
    defaults = {
        "INDEX_PAPER_MODE_ENABLED": False,
        "MCX_PAPER_MODE_ENABLED": False,
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


def test_1_mcx_stays_real_when_global_paper_mode_is_on_and_mcx_flag_is_off():
    """The core scenario this change exists for: global Swing paper mode
    ON, MCX's own flag OFF (the user's explicit deployed configuration) -
    an MCX candidate must still route to REAL trading, completely ignoring
    the global flag."""
    patchers = _apply(_flags(MCX_PAPER_MODE_ENABLED=False))
    try:
        with mock.patch.object(trading_engine.paper_mode_control, "is_paper_mode_enabled", return_value=True), \
             mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=True) as is_mcx:
            assert trading_engine._should_paper_trade("COPPER") is False, \
                "MCX must trade REAL even while the global Swing paper-mode flag is on"
        is_mcx.assert_called_once_with("COPPER")
    finally:
        _stop(patchers)
    print("1. MCX stays REAL when global paper mode is on and MCX_PAPER_MODE_ENABLED is off: PASSED")


def test_2_equity_goes_to_paper_when_global_flag_is_on():
    """Same deployed configuration as test 1, but for a plain NSE-equity
    symbol - it MUST go to paper, since the global flag is exactly what's
    supposed to catch it."""
    patchers = _apply(_flags(MCX_PAPER_MODE_ENABLED=False))
    try:
        with mock.patch.object(trading_engine.paper_mode_control, "is_paper_mode_enabled", return_value=True), \
             mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=False):
            assert trading_engine._should_paper_trade("ASHOKLEY") is True, \
                "a plain NSE-equity symbol must go to paper when the global Swing paper-mode flag is on"
    finally:
        _stop(patchers)
    print("2. NSE-equity symbol (ASHOKLEY) goes to PAPER when global paper mode is on: PASSED")


def test_3_mcx_goes_to_paper_only_when_its_own_flag_is_explicitly_on():
    """MCX_PAPER_MODE_ENABLED=True must send MCX to paper EVEN IF the
    global flag is off - the two flags are independent in both
    directions, not just one."""
    patchers = _apply(_flags(MCX_PAPER_MODE_ENABLED=True))
    try:
        with mock.patch.object(trading_engine.paper_mode_control, "is_paper_mode_enabled", return_value=False), \
             mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=True):
            assert trading_engine._should_paper_trade("NATURALGAS") is True, \
                "MCX_PAPER_MODE_ENABLED alone must be enough to paper-trade MCX, independent of the global flag"
    finally:
        _stop(patchers)
    print("3. MCX goes to PAPER when only its own MCX_PAPER_MODE_ENABLED flag is on: PASSED")


def test_4_everything_real_when_all_flags_off():
    """Baseline sanity check - MCX, index, and plain equity all trade real
    when every paper-mode flag is off."""
    patchers = _apply(_flags())
    try:
        with mock.patch.object(trading_engine.paper_mode_control, "is_paper_mode_enabled", return_value=False), \
             mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", side_effect=lambda s: s == "COPPER"):
            assert trading_engine._should_paper_trade("COPPER") is False
            assert trading_engine._should_paper_trade("NIFTY") is False
            assert trading_engine._should_paper_trade("ASHOKLEY") is False
    finally:
        _stop(patchers)
    print("4. Every symbol type trades REAL when all paper-mode flags are off: PASSED")


def test_5_index_flag_unaffected_by_mcx_carveout():
    """Regression guard: the pre-existing INDEX_PAPER_MODE_ENABLED
    behavior (ORed with the global flag, index-only) must be completely
    unchanged by this MCX carve-out - an index symbol never even reaches
    the MCX branch."""
    patchers = _apply(_flags(INDEX_PAPER_MODE_ENABLED=True, MCX_PAPER_MODE_ENABLED=False))
    try:
        with mock.patch.object(trading_engine.paper_mode_control, "is_paper_mode_enabled", return_value=False), \
             mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=False) as is_mcx:
            assert trading_engine._should_paper_trade("NIFTY") is True, \
                "INDEX_PAPER_MODE_ENABLED alone must still paper-trade NIFTY, unchanged by this session's MCX change"
            assert trading_engine._should_paper_trade("ASHOKLEY") is False, \
                "a non-index equity must be unaffected by INDEX_PAPER_MODE_ENABLED"
        assert is_mcx.call_count == 2, "both symbols must still be checked against is_mcx_commodity first"
    finally:
        _stop(patchers)
    print("5. INDEX_PAPER_MODE_ENABLED behavior is unchanged by the MCX carve-out: PASSED")


if __name__ == "__main__":
    test_1_mcx_stays_real_when_global_paper_mode_is_on_and_mcx_flag_is_off()
    test_2_equity_goes_to_paper_when_global_flag_is_on()
    test_3_mcx_goes_to_paper_only_when_its_own_flag_is_explicitly_on()
    test_4_everything_real_when_all_flags_off()
    test_5_index_flag_unaffected_by_mcx_carveout()
    print("\nAll tests passed.")
