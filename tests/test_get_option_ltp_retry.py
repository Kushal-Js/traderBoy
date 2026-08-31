"""
Tests for get_option_ltp()'s retry wrapper - user request 31 Aug 2026,
found via a lag audit of a live trading day: this REST LTP fallback (used
by _get_ltp() whenever the WebSocket cache is stale/missing, on the exit-
monitoring critical path) had no retry at all, unlike its sibling REST
calls (get_atm_option/get_day_change_pct/get_open_fno_positions), which
all got this exact fix earlier for the identical failure mode. That day
produced 46 unretried "Could not fetch LTP" failures spread across nearly
every held position - each one silently skipped that position's exit-
check for a single ~2s monitor tick before self-healing on the next one.

Covers, against the REAL production functions (not reimplemented):
  1. get_option_ltp recovers from a transient failure (empty/missing LTP
     in the response) and returns the correct value on retry - mirrors
     the exact test pattern used for get_open_fno_positions's own retry
     fix (NOTES.md bug #51/entry #51).
  2. get_option_ltp still raises (not silently swallowed) after
     exhausting all retries against a persistent failure.
  3. The retry actually backs off (real ~1.5s delay observed), not a
     tight busy-loop - confirms this is the same _retry() helper, not a
     custom no-op wrapper.

HOW TO RUN:
    uv run python tests/test_get_option_ltp_retry.py
"""
import os
import sys
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.dhan_client as dc


def test_1_get_option_ltp_retries_on_transient_failure_and_recovers():
    wrapper = dc.DhanWrapper.__new__(dc.DhanWrapper)
    calls = {"n": 0}

    def flaky_get_ltp_data(names):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulates the documented failure mode this bug produced
            # live: Dhan's response missing the requested symbol's entry
            # entirely (get_option_ltp's own check: `if ltp is None`).
            return {}
        return {names[0]: 42.5}

    wrapper._client = types.SimpleNamespace(get_ltp_data=flaky_get_ltp_data)

    start = time.monotonic()
    result = wrapper.get_option_ltp("TESTSTOCK 25 SEP 100 CALL")
    elapsed = time.monotonic() - start

    assert result == 42.5, f"expected the recovered LTP value, got {result}"
    assert calls["n"] == 2, f"expected exactly 2 calls (1 fail + 1 success), got {calls['n']}"
    assert elapsed >= 1.4, f"expected the ~1.5s retry backoff to have elapsed, only saw {elapsed:.2f}s"
    print(f"1. get_option_ltp retries a transient empty-response failure and recovers "
          f"(2 calls, {elapsed:.2f}s elapsed for the backoff): PASSED")


def test_2_get_option_ltp_still_raises_after_exhausting_retries():
    wrapper = dc.DhanWrapper.__new__(dc.DhanWrapper)
    calls = {"n": 0}

    def always_fails(names):
        calls["n"] += 1
        return {}

    wrapper._client = types.SimpleNamespace(get_ltp_data=always_fails)

    try:
        wrapper.get_option_ltp("TESTSTOCK 25 SEP 100 CALL")
        assert False, "expected a ValueError after exhausting retries"
    except ValueError as e:
        assert "No LTP returned" in str(e), str(e)
        assert calls["n"] == 3, f"expected 3 total attempts (1 initial + 2 retries), got {calls['n']}"
        print("2. get_option_ltp still raises after exhausting all 3 attempts against a "
              "persistent failure (not silently swallowed): PASSED")


def test_3_retry_uses_a_real_backoff_not_a_tight_loop():
    """Confirms this goes through the shared _retry() helper (with its
    real ~1.5s sleep between attempts), not a custom immediate-retry
    wrapper that would mask the exact same rate-limit problem _retry
    exists to work around."""
    wrapper = dc.DhanWrapper.__new__(dc.DhanWrapper)
    call_times = []

    def flaky_get_ltp_data(names):
        call_times.append(time.monotonic())
        if len(call_times) < 2:
            return {}
        return {names[0]: 10.0}

    wrapper._client = types.SimpleNamespace(get_ltp_data=flaky_get_ltp_data)
    wrapper.get_option_ltp("TESTSTOCK 25 SEP 100 CALL")

    gap = call_times[1] - call_times[0]
    assert gap >= 1.4, f"expected a real ~1.5s backoff between attempts, only saw {gap:.2f}s"
    print(f"3. Retry backoff between attempts is a real ~1.5s delay ({gap:.2f}s observed), "
          "confirming this uses the shared _retry() helper: PASSED")


def main():
    print("=== get_option_ltp retry-wrapper test suite ===\n")
    test_1_get_option_ltp_retries_on_transient_failure_and_recovers()
    test_2_get_option_ltp_still_raises_after_exhausting_retries()
    test_3_retry_uses_a_real_backoff_not_a_tight_loop()
    print("\nALL get_option_ltp RETRY CHECKS PASSED")


if __name__ == "__main__":
    main()
