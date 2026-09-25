"""
Test for climactic_entry_guard.guard_entry's check-then-set race fix
(audit finding, CODE_AUDIT_2026-09-25.md round 2).

Bug: the `key in _pending` dedup check happened before the function's
only await (_fetch_indicators); two near-simultaneous alerts for the
identical (strategy, symbol, option_type) could both pass the check
before either had registered anything, both fetch indicators
concurrently, and if both landed on a DEFER decision, the second would
silently overwrite the first's PendingEntry - losing the first alert's
own deferred_since/last_checked state with no log of the collision.

Fix: guard_entry now registers a placeholder (_IN_FLIGHT) in _pending
SYNCHRONOUSLY, before the await - so a second concurrent call for the
same key sees it immediately and short-circuits to "skipped" instead of
racing through to the deferred branch too.

HOW TO RUN:
    uv run python tests/test_climactic_entry_guard_race.py
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import climactic_entry_guard as ceg


async def test_1_concurrent_same_key_alerts_only_one_gets_deferred():
    """Two concurrent guard_entry() calls for the SAME (strategy, symbol,
    option_type), both resolving to DEFER - only the first should
    register a real PendingEntry; the second must detect the in-flight
    placeholder and return skipped, never silently overwriting the
    first's pending state."""
    ceg._pending.clear()
    real_fetch = ceg._fetch_indicators
    real_evaluate = ceg.evaluate

    async def slow_fetch(symbol):
        # Forces a real suspension point, so asyncio actually interleaves
        # the two gathered guard_entry() calls right here - without this,
        # the first call would run to completion before the second ever
        # starts, and the race could never be exercised.
        await asyncio.sleep(0.01)
        return {"rsi": 12.0, "er": 0.8}

    ceg._fetch_indicators = slow_fetch
    ceg.evaluate = lambda rsi, er, option_type: ceg.GuardResult(decision="DEFER", rsi=rsi, er=er)

    resolve_calls = []

    async def fake_resolve(symbol, option_type):
        resolve_calls.append((symbol, option_type))
        return {"symbol": symbol, "status": "entered"}

    try:
        r1, r2 = await asyncio.gather(
            ceg.guard_entry("Options", "RELIANCE", "PE", fake_resolve),
            ceg.guard_entry("Options", "RELIANCE", "PE", fake_resolve),
        )
        results = sorted([r1["status"] for r1 in (r1, r2)])
        assert results == ["deferred", "skipped"], \
            f"expected exactly one deferred + one skipped (never two deferred, which would mean the race " \
            f"silently overwrote the first), got {[r1['status'], r2['status']]}"
        skipped = r1 if r1["status"] == "skipped" else r2
        assert skipped["reason"] == "climactic_guard_already_pending", skipped
        assert len(resolve_calls) == 0, "neither call should have entered a real order (both DEFER)"
        assert ("Options", "RELIANCE", "PE") in ceg._pending, "the winning call's PendingEntry must remain"
        assert ceg._pending[("Options", "RELIANCE", "PE")] is not ceg._IN_FLIGHT, \
            "the placeholder must have been replaced by a real PendingEntry, not left dangling"
        print("1. Two concurrent alerts for the identical key: exactly one gets deferred (registers a real "
              "PendingEntry), the other is cleanly skipped as 'already pending' - no silent overwrite: PASSED")
    finally:
        ceg._fetch_indicators = real_fetch
        ceg.evaluate = real_evaluate
        ceg._pending.clear()


async def test_2_placeholder_cleaned_up_on_enter_now_and_on_exception():
    """ENTER_NOW path: the in-flight placeholder must be removed once
    resolved, not left stuck in _pending forever. Same for an exception
    raised mid-resolution."""
    ceg._pending.clear()
    real_fetch = ceg._fetch_indicators
    real_evaluate = ceg.evaluate
    ceg._fetch_indicators = AsyncMock(return_value={"rsi": 50.0, "er": 0.1})
    ceg.evaluate = lambda rsi, er, option_type: ceg.GuardResult(
        decision="ENTER_NOW", resolved_option_type=option_type, rsi=rsi, er=er)

    async def fake_resolve(symbol, option_type):
        return {"symbol": symbol, "status": "entered"}

    try:
        result = await ceg.guard_entry("Futures", "TCS", "CE", fake_resolve)
        assert result["status"] == "entered"
        assert ("Futures", "TCS", "CE") not in ceg._pending, \
            "the placeholder must be cleaned up after an ENTER_NOW resolution, not left stuck forever"
        print("2a. ENTER_NOW path cleans up its placeholder, doesn't leave it stuck: PASSED")

        async def failing_resolve(symbol, option_type):
            raise RuntimeError("simulated failure")

        try:
            await ceg.guard_entry("Futures", "INFY", "CE", failing_resolve)
            assert False, "expected the exception to propagate"
        except RuntimeError:
            pass
        assert ("Futures", "INFY", "CE") not in ceg._pending, \
            "an exception mid-resolution must not leave the placeholder stuck forever either"
        print("2b. An exception mid-resolution also cleans up its placeholder via finally: PASSED")
    finally:
        ceg._fetch_indicators = real_fetch
        ceg.evaluate = real_evaluate
        ceg._pending.clear()


async def test_3_snapshot_and_poll_pending_tolerate_the_placeholder():
    """A concurrent snapshot()/poll_pending() call landing while a key is
    still IN_FLIGHT must not crash - both simply skip it."""
    ceg._pending.clear()
    ceg._pending[("Luxury", "WIPRO", "PE")] = ceg._IN_FLIGHT
    try:
        assert ceg.snapshot("Luxury") == [], "snapshot() must skip an in-flight placeholder, not crash on it"
        await ceg.poll_pending("Luxury")  # must not raise
        print("3. snapshot()/poll_pending() both tolerate an in-flight placeholder without crashing: PASSED")
    finally:
        ceg._pending.clear()


async def main():
    print("=== climactic_entry_guard race-fix test suite ===\n")
    await test_1_concurrent_same_key_alerts_only_one_gets_deferred()
    await test_2_placeholder_cleaned_up_on_enter_now_and_on_exception()
    await test_3_snapshot_and_poll_pending_tolerate_the_placeholder()
    print("\nALL climactic_entry_guard race-fix tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
