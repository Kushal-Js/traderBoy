"""
Tests for Options' volume-floor entry gate (promoted from shadow-mode
logging to a real live gate, 16 Sep 2026 - user request: "Turn the volume
floor into a live gate for Options first... make it as flag enabled
change... keep this turned on before deployment to live").

Coverage:
  1. A thin-volume signal is BLOCKED when config.VOLUME_FLOOR_GATE_ENABLED
     is True - _process_one_entry returns status="skipped",
     reason="volume_floor_gate", and places ZERO real orders (verified
     via the actual mocked place_market_order call count, not just the
     returned status field).
  2. The SAME thin-volume signal is allowed through when the flag is
     False - proves this is a genuine, independently-flippable flag, not
     baked unconditionally into the entry path.
  3. A healthy volume ratio is not blocked even with the flag on.
  4. reversal_filters.check_volume_floor_sync itself fails OPEN
     (passes=True) when dhan_wrapper._client is None - the same real
     incident this module's own docstring documents (must never trigger
     a live Dhan login as a side effect of a diagnostic/gate check).

HOW TO RUN:
    uv run python tests/test_options_volume_floor_gate.py
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
import tempfile
scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_options_volume_floor_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.config as ocfg
import Options.dhan_client as odc
import Options.position_store as ops
import Options.trading_engine as ote
import reversal_filters
from Options.dhan_client import dhan_wrapper

sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_daily_reentry_cap import install_all_dhan_mocks  # noqa: E402 - reuses the proven full-mock harness


def _set():
    store = ops.PositionStore()
    ote.position_store = store
    ocfg.MAX_LIVE_POSITIONS_CE = 5
    ocfg.MAX_LIVE_POSITIONS_PE = 5
    ocfg.LOSS_REPEAT_BLOCK_ENABLED = False
    ocfg.ENABLE_RSI_LOSS_REENTRY_BLOCK = False


async def test_1_thin_volume_blocked_when_gate_enabled():
    _set()
    ocfg.VOLUME_FLOOR_GATE_ENABLED = True
    restore_dhan, placed_orders = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_volume_floor", new=AsyncMock(return_value=(False, 0.35))):
        try:
            result = await ote._process_one_entry("RELIANCE", "CE")
            assert result["status"] == "skipped" and result["reason"] == "volume_floor_gate", result
            assert result["vol_ratio"] == 0.35
            assert len(placed_orders) == 0, "no real order should have been placed"
            assert "RELIANCE" not in ote.position_store.reserved_symbols
            print("1. A thin-volume signal (0.35x) is BLOCKED when VOLUME_FLOOR_GATE_ENABLED=True - "
                  "zero real orders placed: PASSED")
        finally:
            restore_dhan()


async def test_2_same_thin_volume_allowed_when_gate_disabled():
    _set()
    ocfg.VOLUME_FLOOR_GATE_ENABLED = False
    restore_dhan, placed_orders = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_volume_floor", new=AsyncMock(return_value=(False, 0.35))):
        try:
            result = await ote._process_one_entry("RELIANCE", "CE")
            assert result["status"] == "entered", result
            assert len(placed_orders) == 1
            print("2. The SAME thin-volume signal (0.35x) is allowed through when "
                  "VOLUME_FLOOR_GATE_ENABLED=False - confirms this is a real, flippable flag: PASSED")
        finally:
            restore_dhan()


async def test_3_healthy_volume_not_blocked():
    _set()
    ocfg.VOLUME_FLOOR_GATE_ENABLED = True
    restore_dhan, placed_orders = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_volume_floor", new=AsyncMock(return_value=(True, 2.4))):
        try:
            result = await ote._process_one_entry("RELIANCE", "CE")
            assert result["status"] == "entered", result
            assert len(placed_orders) == 1
            print("3. A healthy volume ratio (2.4x) is NOT blocked even with the gate enabled: PASSED")
        finally:
            restore_dhan()


def test_4_check_volume_floor_fails_open_when_not_authenticated():
    original = dhan_wrapper._client
    dhan_wrapper._client = None
    with mock.patch.object(dhan_wrapper, "authenticate") as fake_auth:
        try:
            passes, vol_ratio = reversal_filters.check_volume_floor_sync("RELIANCE", 1.2)
            assert passes is True and vol_ratio is None
            fake_auth.assert_not_called()
            print("4. check_volume_floor_sync fails OPEN (passes=True) when dhan_wrapper._client is None, "
                  "and never triggers a real login as a side effect: PASSED")
        finally:
            dhan_wrapper._client = original


async def main():
    print("=== Options volume-floor live gate test suite ===\n")
    await test_1_thin_volume_blocked_when_gate_enabled()
    await test_2_same_thin_volume_allowed_when_gate_disabled()
    await test_3_healthy_volume_not_blocked()
    test_4_check_volume_floor_fails_open_when_not_authenticated()
    print("\nALL Options volume-floor gate tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
