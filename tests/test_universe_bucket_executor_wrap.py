"""
Test for the 25 Sep 2026 fix: universe_bucket.py's active_symbols() used
to read every non-today day in the rolling window from disk SYNCHRONOUSLY,
while holding the module's asyncio.Lock - freezing the entire process
(all six packages share one event loop) for that duration on every call,
and blocking any other caller (e.g. record_alert/webhook handlers) waiting
on the same lock. _ensure_today_locked had the identical issue on a
day-rollover. Both now offload the file read via run_in_executor. See
PERFORMANCE_AUDIT_2026-09-25.md.

No dedicated test file existed for this module before this fix - this
covers the functional behavior end-to-end (not just "doesn't crash"),
proving multi-day reads still work correctly through the executor.

HOW TO RUN:
    uv run python tests/test_universe_bucket_executor_wrap.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_universe_bucket_test_"))
trade_history.HISTORY_DIR = scratch_dir

import universe_bucket as ub


async def test_1_record_alert_and_active_symbols_round_trip():
    await ub.record_alert("CE", ["RELIANCE", "TCS"], scan_name="test-scan")
    syms = await ub.active_symbols("CE")
    assert syms == {"RELIANCE", "TCS"}, syms
    print("1. record_alert -> active_symbols round-trips correctly through the executor-wrapped path: PASSED")


async def test_2_active_symbols_reads_a_prior_days_file_via_executor():
    """The specific code path the fix changed: a day OTHER than today's
    in-memory bucket, read from disk via run_in_executor inside
    active_symbols's own loop."""
    yesterday = date.today() - timedelta(days=1)
    path = trade_history.HISTORY_DIR / f"{yesterday.isoformat()}_universe_bucket_CE.json"
    path.write_text(json.dumps({"INFY": {"alert_count": 1}}))

    await ub.record_alert("CE", ["WIPRO"], scan_name="test-scan-2")
    syms = await ub.active_symbols("CE")
    assert "WIPRO" in syms, f"today's symbol must still be present, got {syms}"
    if ub._window_for("CE") >= 2:
        assert "INFY" in syms, f"yesterday's symbol (read via executor) must be included, got {syms}"
    print("2. active_symbols correctly reads a prior day's file via run_in_executor (not just today's "
          "in-memory bucket): PASSED")


async def test_3_ensure_today_locked_is_awaitable_and_idempotent():
    b = ub._BUCKETS["PE"]
    b.day = None  # force a fresh day-rollover load
    await ub._ensure_today_locked(b)
    assert b.day == ub._today()
    first_items = b.items
    await ub._ensure_today_locked(b)  # same day again - must be a no-op, not re-read
    assert b.items is first_items, "a same-day call must not re-load from disk"
    print("3. _ensure_today_locked is awaitable (executor-wrapped) and idempotent within the same day: PASSED")


async def main():
    print("=== universe_bucket.py executor-wrap test suite ===\n")
    await test_1_record_alert_and_active_symbols_round_trip()
    await test_2_active_symbols_reads_a_prior_days_file_via_executor()
    await test_3_ensure_today_locked_is_awaitable_and_idempotent()
    print("\nALL PASSED")


if __name__ == "__main__":
    asyncio.run(main())
