"""
Tests for the "SwingIndex" pseudo-strategy in paper_mode_control.py
(added 28 Sep 2026, user request: "Make this Index trading paper mode
toggle also the one which won't require any restart in future"). Before
this, NIFTY/BANKNIFTY's own paper-vs-real state lived in a plain
Swing/config.py flag (INDEX_PAPER_MODE_ENABLED, then briefly
INDEX_REAL_CARVEOUT_ENABLED) - both read once at process startup, so
flipping either needed a redeploy+restart. Folding index into
paper_mode_control gives it the exact same runtime-toggle + .env-sync
guarantee Options/Luxury/Swing/Bollinger already have.

Exercises the REAL paper_mode_control functions end-to-end (no mocking
of set_paper_mode/is_paper_mode_enabled themselves - only the .env file
and Swing.config's own INDEX_PAPER_MODE_ENABLED default are patched),
plus the REAL Swing/trading_engine._should_paper_trade routing decision
for NIFTY - same "exercise the real function" convention as
test_swing_mcx_paper_carveout.py.

HOW TO RUN:
    uv run python tests/test_swing_index_runtime_paper_mode.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import paper_mode_control as pmc  # noqa: E402
import Swing.trading_engine as trading_engine  # noqa: E402
from Swing import config  # noqa: E402

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_index_runtime_test_"))


def _reset(env_text: str = "") -> Path:
    pmc._overrides.clear()
    pmc._overrides_loaded = True
    env_path = scratch_dir / f"env_{id(env_text)}"
    env_path.write_text(env_text)
    pmc.ENV_FILE = env_path
    pmc.OVERRIDE_FILE = scratch_dir / f"overrides_{id(env_text)}.json"
    return env_path


def test_1_swingindex_is_a_registered_strategy():
    assert "SwingIndex" in pmc.STRATEGIES
    assert pmc._ENV_VAR_NAMES["SwingIndex"] == "SWING_INDEX_PAPER_MODE_ENABLED"
    print("1. 'SwingIndex' is registered in paper_mode_control.STRATEGIES with the right env var: PASSED")


def test_2_env_default_reads_swing_config_index_flag():
    with mock.patch.object(config, "INDEX_PAPER_MODE_ENABLED", True):
        assert pmc._env_default("SwingIndex") is True
    with mock.patch.object(config, "INDEX_PAPER_MODE_ENABLED", False):
        assert pmc._env_default("SwingIndex") is False
    print("2. SwingIndex's .env default correctly mirrors Swing.config.INDEX_PAPER_MODE_ENABLED: PASSED")


async def test_3_set_paper_mode_toggles_and_syncs_env_without_restart():
    """The core promise: flip it once, is_paper_mode_enabled reflects it
    immediately (no restart), and .env's own line is rewritten so a
    FUTURE restart also comes up correct."""
    env_path = _reset("SWING_INDEX_PAPER_MODE_ENABLED=false\n")
    with mock.patch.object(config, "INDEX_PAPER_MODE_ENABLED", False):
        assert pmc.is_paper_mode_enabled("SwingIndex") is False, "starts real, matching .env default"
        await pmc.set_paper_mode("SwingIndex", True)
        assert pmc.is_paper_mode_enabled("SwingIndex") is True, \
            "must flip immediately in this process - no restart needed"
    text = env_path.read_text()
    assert "SWING_INDEX_PAPER_MODE_ENABLED=true\n" in text, text
    assert pmc.paper_mode_source("SwingIndex") == "runtime_override"
    print("3. set_paper_mode('SwingIndex', ...) flips instantly and syncs .env for a future restart: PASSED")


def test_4_index_symbol_routes_through_swingindex_not_swing():
    """The real _should_paper_trade, with only the network/instrument-
    master boundary (is_mcx_commodity) and paper_mode_control's own state
    involved - no reimplementation. Swing's global flag is real (true),
    SwingIndex is set real (false-paper i.e. override False) - NIFTY must
    follow SwingIndex, not the global flag."""
    _reset("")
    pmc._overrides["Swing"] = True          # global Swing: paper
    pmc._overrides["SwingIndex"] = False    # SwingIndex: real
    try:
        with mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=False):
            assert trading_engine._should_paper_trade("NIFTY") is False, \
                "NIFTY must trade REAL (SwingIndex's own state), ignoring the global Swing flag being paper"
            assert trading_engine._should_paper_trade("BANKNIFTY") is False, \
                "BANKNIFTY must trade REAL (SwingIndex's own state), ignoring the global Swing flag being paper"
            assert trading_engine._should_paper_trade("ASHOKLEY") is True, \
                "a non-index equity must still follow the global Swing flag (paper), unaffected by SwingIndex"
    finally:
        pmc._overrides.clear()
    print("4. NIFTY/BANKNIFTY route through SwingIndex's own state, independent of the global Swing flag: PASSED")


async def main():
    print("=== SwingIndex runtime paper-mode test suite ===\n")
    test_1_swingindex_is_a_registered_strategy()
    test_2_env_default_reads_swing_config_index_flag()
    await test_3_set_paper_mode_toggles_and_syncs_env_without_restart()
    test_4_index_symbol_routes_through_swingindex_not_swing()
    print("\nALL SWINGINDEX RUNTIME PAPER-MODE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
