"""
Tests for the Swing v2 entry-retry-cooldown fix (added 15 Sep 2026, real
incident): with no cooldown after a failed entry, the very next
MONITOR_INTERVAL_SECONDS (5s) tick immediately re-evaluated the same
still-active signal and retried, placing 4 duplicate real broker orders
for COPPER in under a minute before Dhan's own margin engine finally
started hard-rejecting further attempts. Root cause was compounded by a
second bug (see tests/test_mcx_market_hours.py): dhan_client.is_market_
open() checked only NSE F&O hours, so the MCX order was wrongly tagged
AMO even though MCX was genuinely still open, and Swing's own TRADED-only
fill discipline treats a still-PENDING AMO as a failed entry immediately.

This file covers the cooldown half of the fix in isolation:
  1. A failed entry (non-TRADED fill) marks the symbol in cooldown.
  2. enter_position_for_stock's own finally block calls record_failed_
     entry for every non-entered outcome (mirrors test_swing_v2_entry_
     exit.py's own test_7, just also asserting the cooldown state).
  3. is_in_entry_cooldown correctly expires after config.ENTRY_RETRY_
     COOLDOWN_SECONDS (verified via monkeypatching the config value down
     to something instant rather than sleeping in the test).
  4. _monitor_tick's watchlist scan actually SKIPS a symbol in cooldown -
     the concrete behavior that prevents the hot-retry-storm regression.

HOW TO RUN:
    uv run python tests/test_swing_entry_retry_cooldown.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_entry_cooldown_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
from Options.dhan_client import OrderStatus

import Swing.config as sc
import Swing.trading_engine as ste
from Swing.position_store import SwingPositionStore

from test_swing_v2_entry_exit import install_mocks, _set  # noqa: E402 - reuses the proven entry-flow mock harness


async def test_1_failed_entry_starts_a_cooldown():
    _set("options")
    restore, placed, _ = install_mocks(entry_fill_status=OrderStatus.PENDING)
    try:
        assert not await ste.position_store.is_in_entry_cooldown("RELIANCE")
        result = await ste.enter_position_for_stock("RELIANCE", "BULLISH")
        assert result["status"] == "failed", result
        assert await ste.position_store.is_in_entry_cooldown("RELIANCE"), \
            "a failed entry must start a cooldown for this symbol"
        print("1. A failed (non-TRADED) entry starts an entry-retry cooldown for that symbol: PASSED")
    finally:
        restore()


async def test_2_insufficient_funds_also_starts_a_cooldown():
    """The real incident's actual failure mode - a real broker-side
    rejection (funds/margin), not just a stuck non-TRADED fill - must be
    covered by the same cooldown, or the fix only handles half the
    problem."""
    _set("options")
    restore, placed, _ = install_mocks(funds_sufficient=False)
    try:
        result = await ste.enter_position_for_stock("RELIANCE", "BULLISH")
        assert result["status"] == "skipped" and result["reason"] == "insufficient_funds", result
        assert await ste.position_store.is_in_entry_cooldown("RELIANCE"), \
            "an insufficient-funds skip must also start a cooldown - this is the real incident's actual failure mode"
        print("2. An insufficient-funds skip also starts the cooldown (the real incident's actual failure path): PASSED")
    finally:
        restore()


async def test_3_successful_entry_does_not_start_a_cooldown():
    _set("options")
    restore, placed, _ = install_mocks(entry_fill_status=OrderStatus.TRADED)
    try:
        result = await ste.enter_position_for_stock("RELIANCE", "BULLISH")
        assert result["status"] == "entered", result
        assert not await ste.position_store.is_in_entry_cooldown("RELIANCE"), \
            "a genuinely successful entry has no reason to be in cooldown"
        print("3. A successful entry does NOT start a cooldown: PASSED")
    finally:
        restore()


async def test_4_cooldown_expires_after_the_configured_window():
    _set("options")
    sc.ENTRY_RETRY_COOLDOWN_SECONDS = 0.05
    try:
        await ste.position_store.record_failed_entry("RELIANCE")
        assert await ste.position_store.is_in_entry_cooldown("RELIANCE")
        await asyncio.sleep(0.1)
        assert not await ste.position_store.is_in_entry_cooldown("RELIANCE"), \
            "cooldown must expire once ENTRY_RETRY_COOLDOWN_SECONDS has elapsed"
        print("4. The cooldown expires once config.ENTRY_RETRY_COOLDOWN_SECONDS has elapsed: PASSED")
    finally:
        sc.ENTRY_RETRY_COOLDOWN_SECONDS = 180


async def test_5_monitor_tick_skips_a_symbol_in_cooldown():
    """The actual regression this fix prevents: without it, _monitor_tick's
    watchlist scan would re-evaluate and re-attempt entry for a symbol on
    literally the next tick after a failed attempt."""
    _set("options", max_concurrent=2)
    restore, placed, _ = install_mocks(entry_fill_status=OrderStatus.PENDING)
    try:
        from Swing.watchlist import watchlist_store
        original_symbols = watchlist_store.symbols
        watchlist_store.symbols = lambda: asyncio.sleep(0, result=["RELIANCE"])

        original_evaluate = ste._evaluate_entry_signal
        eval_calls = []

        async def _fake_evaluate(symbol):
            eval_calls.append(symbol)
            return "BULLISH"

        ste._evaluate_entry_signal = _fake_evaluate
        try:
            await ste._monitor_tick()
            assert len(placed) == 1, "first tick should attempt (and fail) the entry once"
            assert await ste.position_store.is_in_entry_cooldown("RELIANCE")

            await ste._monitor_tick()
            assert len(placed) == 1, \
                "second tick must NOT re-attempt entry while RELIANCE is still in cooldown - this is the fix"
            print("5. _monitor_tick's watchlist scan skips a symbol still in its entry-retry cooldown "
                  "(the actual hot-retry-storm regression this fix prevents): PASSED")
        finally:
            ste._evaluate_entry_signal = original_evaluate
            watchlist_store.symbols = original_symbols
    finally:
        restore()


async def main():
    print("=== Swing entry-retry-cooldown fix test suite ===\n")
    await test_1_failed_entry_starts_a_cooldown()
    await test_2_insufficient_funds_also_starts_a_cooldown()
    await test_3_successful_entry_does_not_start_a_cooldown()
    await test_4_cooldown_expires_after_the_configured_window()
    await test_5_monitor_tick_skips_a_symbol_in_cooldown()
    print("\nALL entry-retry-cooldown FIX CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
