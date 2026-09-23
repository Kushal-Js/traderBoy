"""
Tests for breakout_signal.py's scan-executor concurrency fix (added 23
Sep 2026, user request after a live audit found ALL breakout-signal
evaluation - WS-walk AND REST-fallback, across Options/Luxury/Futures
AND the UniverseDispatcher - serialized through a single-worker thread
pool, `_SCAN_EXECUTOR`. A slow/rate-limited REST call in one strategy's
evaluation could transiently stall every OTHER strategy's otherwise-
instant WS-walk evaluation behind it on that one thread.

_SCAN_EXECUTOR's worker count is now configurable (BREAKOUT_SCAN_
EXECUTOR_WORKERS, default 3, up from a hardcoded 1), and _daily_cache
(previously safe only because a single worker could never race itself)
is now guarded by a dedicated lock around its own get/set, since real
concurrent access is possible for the first time.

Covers:
  1. The executor is no longer single-threaded (>1 worker) - the actual
     fix, not just its plumbing.
  2. THE real-world scenario this exists for: a slow call submitted to
     the shared executor no longer blocks an unrelated fast one queued
     alongside it - proven by timing, not just "no exception".
  3. _daily_cache stays consistent under genuine concurrent access from
     multiple real threads (many symbols, real races, a slow fake REST
     call to widen the window) - every symbol ends with a well-formed
     cache entry, nothing torn or corrupted.
  4. A same-symbol concurrent-miss race (the one case the lock
     deliberately does NOT fully prevent) still converges on a correct,
     usable cached value - the accepted "benign duplicate fetch,
     self-heals" behavior actually behaves that way, not silently wrong.

HOW TO RUN:
    uv run python tests/test_breakout_signal_scan_executor_concurrency.py
"""
import os
import sys
import threading
import time
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")

import breakout_signal as bs  # noqa: E402


def test_1_executor_is_no_longer_single_threaded():
    workers = bs._SCAN_EXECUTOR._max_workers
    assert workers >= 2, (
        f"_SCAN_EXECUTOR still has only {workers} worker(s) - the whole point of this fix is that "
        f"one slow evaluation must not be able to block every other strategy's evaluation"
    )
    print(f"1. _SCAN_EXECUTOR now has {workers} workers (was hardcoded to 1): PASSED")


def test_2_a_slow_submission_no_longer_blocks_a_fast_one():
    """THE real-world scenario: one strategy's REST-fallback evaluation
    is slow (rate-limited/network-bound); a DIFFERENT strategy's
    WS-walk evaluation for an unrelated symbol is submitted around the
    same time and must complete quickly regardless, not wait behind it."""
    def slow_call():
        time.sleep(1.5)
        return "slow-done"

    fast_done_at = []

    def fast_call():
        fast_done_at.append(time.monotonic())
        return "fast-done"

    start = time.monotonic()
    slow_future = bs._SCAN_EXECUTOR.submit(slow_call)
    time.sleep(0.05)  # let the slow one actually start occupying a worker first
    fast_future = bs._SCAN_EXECUTOR.submit(fast_call)

    assert fast_future.result(timeout=5) == "fast-done"
    fast_elapsed = fast_done_at[0] - start
    assert fast_elapsed < 1.0, (
        f"the fast submission took {fast_elapsed:.2f}s to run - it queued behind the slow one instead "
        f"of running concurrently, meaning the single-worker bottleneck this fix targets is still there"
    )
    assert slow_future.result(timeout=5) == "slow-done"
    print(f"2. A fast submission completed in {fast_elapsed:.2f}s while a slow one was still running "
          f"(no longer queued behind it): PASSED")


def test_3_daily_cache_stays_consistent_under_real_concurrent_access():
    saved_cache = dict(bs._daily_cache)
    saved_fetch = bs._fetch_daily_sync
    bs._daily_cache.clear()
    fetch_calls = []
    fetch_lock = threading.Lock()

    def fake_fetch_daily_sync(symbol, lookback_days):
        with fetch_lock:
            fetch_calls.append(symbol)
        time.sleep(0.05)  # widen the race window so concurrent workers genuinely overlap
        return {"close": [100.0, 101.0], "volume": [1000.0, 1000.0]}

    bs._fetch_daily_sync = fake_fetch_daily_sync
    try:
        fake_cfg = type("Cfg", (), {"BREAKOUT_DAILY_LOOKBACK_DAYS": 100})()
        symbols = [f"SYM{i}" for i in range(20)]
        errors = []

        def worker(sym):
            try:
                for _ in range(3):  # each symbol fetched multiple times across threads - forces real races
                    daily, _did_fetch = bs._fetch_daily_cached(sym, fake_cfg)
                    assert daily.get("close") == [100.0, 101.0], f"{sym}: corrupted/incomplete cache entry: {daily}"
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(sym,)) for sym in symbols for _ in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)
            assert not th.is_alive(), "a worker thread failed to finish - possible deadlock on _daily_cache_lock"

        assert not errors, f"concurrent _fetch_daily_cached access raised: {errors}"
        for sym in symbols:
            cached = bs._daily_cache.get(sym)
            assert cached is not None, f"{sym}: never got cached despite being fetched"
            cached_date, cached_data = cached
            assert cached_date == bs._today()
            assert cached_data.get("close") == [100.0, 101.0], f"{sym}: final cached value is corrupted: {cached_data}"
        print(f"3. _daily_cache stayed fully consistent across {len(threads)} concurrent threads / "
              f"{len(symbols)} symbols, no corruption, no deadlock: PASSED")
    finally:
        bs._fetch_daily_sync = saved_fetch
        bs._daily_cache.clear()
        bs._daily_cache.update(saved_cache)


def test_4_same_symbol_concurrent_miss_race_still_converges_correctly():
    """The one race the lock deliberately does NOT fully prevent (see
    _daily_cache_lock's own docstring): two threads both missing the
    cache for the SAME symbol at the same instant may both fetch - this
    must still be benign, not corrupt the cache or leave it stuck empty."""
    saved_cache = dict(bs._daily_cache)
    saved_fetch = bs._fetch_daily_sync
    bs._daily_cache.clear()
    call_count = [0]

    def fake_fetch_daily_sync(symbol, lookback_days):
        call_count[0] += 1
        time.sleep(0.1)
        return {"close": [50.0, 51.0], "volume": [500.0, 500.0]}

    bs._fetch_daily_sync = fake_fetch_daily_sync
    try:
        fake_cfg = type("Cfg", (), {"BREAKOUT_DAILY_LOOKBACK_DAYS": 100})()
        results = []

        def worker():
            results.append(bs._fetch_daily_cached("SAMESYM", fake_cfg))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5)

        assert call_count[0] >= 1, "the cache-miss race must still result in at least one real fetch"
        for daily, _did_fetch in results:
            assert daily.get("close") == [50.0, 51.0], f"a racing caller got a corrupted/wrong result: {daily}"
        final = bs._daily_cache.get("SAMESYM")
        assert final is not None and final[1].get("close") == [50.0, 51.0]
        print(f"4. Same-symbol concurrent-miss race ({call_count[0]} duplicate fetch(es), benign) "
              f"still converges on a correct final cached value: PASSED")
    finally:
        bs._fetch_daily_sync = saved_fetch
        bs._daily_cache.clear()
        bs._daily_cache.update(saved_cache)


def main():
    print("=== breakout_signal.py scan-executor concurrency fix test suite ===\n")
    test_1_executor_is_no_longer_single_threaded()
    test_2_a_slow_submission_no_longer_blocks_a_fast_one()
    test_3_daily_cache_stays_consistent_under_real_concurrent_access()
    test_4_same_symbol_concurrent_miss_race_still_converges_correctly()
    print("\nALL SCAN-EXECUTOR CONCURRENCY TESTS PASSED")


if __name__ == "__main__":
    main()
