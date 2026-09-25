"""
Tests for Luxury's volume-floor entry gate (added 25 Sep 2026 - audit
finding 1.2, CODE_AUDIT_2026-09-24.md: Options and Futures both had this
gate since 16/17 Sep 2026 - the filter was validated partly using real
Luxury trade data, per Futures/config.py's own VOLUME_FLOOR_GATE_ENABLED
docstring - but it was never actually wired into Luxury/trading_engine.py
itself. Direct port of tests/test_options_volume_floor_gate.py's own
coverage, adapted to Luxury's mock harness (test_luxury_package.py's
install_all_dhan_mocks).

Coverage:
  1. A thin-volume signal is BLOCKED when config.VOLUME_FLOOR_GATE_ENABLED
     is True - _process_one_entry returns status="skipped",
     reason="volume_floor_gate", and places ZERO real orders.
  2. The SAME thin-volume signal is allowed through when the flag is
     False - proves this is a genuine, independently-flippable flag.
  3. A healthy volume ratio is not blocked even with the flag on.

HOW TO RUN:
    uv run python tests/test_luxury_volume_floor_gate.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_luxury_volume_floor_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Luxury.config as lcfg
import Luxury.position_store as lps
import Luxury.trading_engine as lte
import reversal_filters

sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_luxury_package import install_all_dhan_mocks, _freeze_market_hours  # noqa: E402 - reuses the proven harness

from zoneinfo import ZoneInfo


def _set():
    store = lps.PositionStore()
    lte.position_store = store
    lcfg.MAX_LIVE_POSITIONS_CE = 5
    lcfg.MAX_LIVE_POSITIONS_PE = 5
    lcfg.LOSS_REPEAT_BLOCK_ENABLED = False
    lcfg.LOSS_REENTRY_TREND_CHECK_ENABLED = False


async def test_1_thin_volume_blocked_when_gate_enabled():
    _set()
    lcfg.VOLUME_FLOOR_GATE_ENABLED = True
    restore_time = _freeze_market_hours()
    restore_dhan = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_volume_floor", new=AsyncMock(return_value=(False, 0.35))):
        try:
            result = await lte._process_one_entry("RELIANCE", "CE")
            assert result["status"] == "skipped" and result["reason"] == "volume_floor_gate", result
            assert result["vol_ratio"] == 0.35
            assert "RELIANCE" not in lte.position_store.live_positions
            print("1. A thin-volume signal (0.35x) is BLOCKED when VOLUME_FLOOR_GATE_ENABLED=True - "
                  "zero real positions entered: PASSED")
        finally:
            restore_dhan()
            restore_time()


async def test_2_same_thin_volume_allowed_when_gate_disabled():
    _set()
    lcfg.VOLUME_FLOOR_GATE_ENABLED = False
    restore_time = _freeze_market_hours()
    restore_dhan = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_volume_floor", new=AsyncMock(return_value=(False, 0.35))):
        try:
            result = await lte._process_one_entry("RELIANCE", "CE")
            assert result["status"] == "entered", result
            print("2. The SAME thin-volume signal (0.35x) is allowed through when "
                  "VOLUME_FLOOR_GATE_ENABLED=False - confirms this is a real, flippable flag: PASSED")
        finally:
            restore_dhan()
            restore_time()


async def test_3_healthy_volume_not_blocked():
    _set()
    lcfg.VOLUME_FLOOR_GATE_ENABLED = True
    restore_time = _freeze_market_hours()
    restore_dhan = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_volume_floor", new=AsyncMock(return_value=(True, 2.4))):
        try:
            result = await lte._process_one_entry("RELIANCE", "CE")
            assert result["status"] == "entered", result
            print("3. A healthy volume ratio (2.4x) is NOT blocked even with the gate enabled: PASSED")
        finally:
            restore_dhan()
            restore_time()


async def main():
    print("=== Luxury volume-floor entry gate test suite ===\n")
    await test_1_thin_volume_blocked_when_gate_enabled()
    await test_2_same_thin_volume_allowed_when_gate_disabled()
    await test_3_healthy_volume_not_blocked()
    print("\nALL Luxury volume-floor gate tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
