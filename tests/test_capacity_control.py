"""
Tests for capacity_control.py (added 26 Sep 2026, user request: "max
concurrent trade capacity is a configurable item which should be able to
change without a deployment by simply updating it using an endpoint").
Exercises the REAL module functions directly - no reimplementation - only
ever touching a tempfile copy of OVERRIDE_FILE and resetting the module's
own in-memory state around each test, so this never reads/writes the real
data/capacity_overrides.json.

HOW TO RUN:
    uv run python tests/test_capacity_control.py
"""
import sys
import tempfile
import asyncio
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import capacity_control  # noqa: E402
from Swing import config as swing_config  # noqa: E402
from Bollinger import config as bollinger_config  # noqa: E402


class _Isolated:
    """Context manager: points OVERRIDE_FILE at a throwaway tempfile and
    resets capacity_control's in-memory override state on both entry and
    exit, so tests never see each other's state and never touch the real
    data/capacity_overrides.json."""

    def __enter__(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._file_patch = mock.patch.object(
            capacity_control, "OVERRIDE_FILE", Path(self._tmpdir.name) / "capacity_overrides.json"
        )
        self._file_patch.start()
        capacity_control._overrides.clear()
        capacity_control._overrides_loaded = False
        return self

    def __exit__(self, *exc):
        self._file_patch.stop()
        self._tmpdir.cleanup()
        capacity_control._overrides.clear()
        capacity_control._overrides_loaded = False


def test_1_falls_back_to_env_default_when_no_override_set():
    with _Isolated():
        with mock.patch.object(swing_config, "MAX_CONCURRENT_TRADES", 5):
            assert capacity_control.get_max_concurrent_trades("Swing") == 5
            assert capacity_control.capacity_source("Swing") == "env_default"
        with mock.patch.object(swing_config, "MAX_CONCURRENT_TRADES", 7):
            # Read fresh each call, not cached - a live .env-driven config
            # change (pre-existing behavior) must still be visible.
            assert capacity_control.get_max_concurrent_trades("Swing") == 7
    print("1. Falls back to live config.MAX_CONCURRENT_TRADES when no override is set: PASSED")


def test_2_runtime_override_takes_precedence_over_env_default():
    with _Isolated():
        with mock.patch.object(swing_config, "MAX_CONCURRENT_TRADES", 5):
            asyncio.run(capacity_control.set_max_concurrent_trades("Swing", 3))
            assert capacity_control.get_max_concurrent_trades("Swing") == 3
            assert capacity_control.capacity_source("Swing") == "runtime_override"
    print("2. Runtime override takes precedence over the .env default: PASSED")


def test_3_override_persists_across_a_simulated_restart():
    """A restart re-imports this module fresh - simulated here by
    resetting the in-memory dict/loaded-flag WITHOUT touching the
    (tempfile) OVERRIDE_FILE on disk, then confirming the value comes
    back from that file."""
    with _Isolated():
        asyncio.run(capacity_control.set_max_concurrent_trades("Swing", 2))
        assert capacity_control.OVERRIDE_FILE.exists(), "override must be persisted to disk"

        capacity_control._overrides.clear()
        capacity_control._overrides_loaded = False

        assert capacity_control.get_max_concurrent_trades("Swing") == 2, \
            "override must survive a restart by being re-read from OVERRIDE_FILE"
        assert capacity_control.capacity_source("Swing") == "runtime_override"
    print("3. Override survives a simulated restart (re-read from disk): PASSED")


def test_4_negative_value_rejected():
    with _Isolated():
        try:
            asyncio.run(capacity_control.set_max_concurrent_trades("Swing", -1))
            raise AssertionError("expected ValueError for a negative capacity")
        except ValueError:
            pass
    print("4. A negative max-concurrent-trades value is rejected: PASSED")


def test_5_swing_and_bollinger_overrides_are_independent():
    with _Isolated():
        with mock.patch.object(swing_config, "MAX_CONCURRENT_TRADES", 5), \
             mock.patch.object(bollinger_config, "MAX_CONCURRENT_TRADES", 5):
            asyncio.run(capacity_control.set_max_concurrent_trades("Swing", 1))
            assert capacity_control.get_max_concurrent_trades("Swing") == 1
            assert capacity_control.get_max_concurrent_trades("Bollinger") == 5, \
                "setting Swing's override must not affect Bollinger's own value"
            assert capacity_control.capacity_source("Bollinger") == "env_default"
    print("5. Swing and Bollinger overrides are independent: PASSED")


def test_6_zero_is_a_valid_capacity_meaning_no_new_entries():
    with _Isolated():
        asyncio.run(capacity_control.set_max_concurrent_trades("Bollinger", 0))
        assert capacity_control.get_max_concurrent_trades("Bollinger") == 0
    print("6. Zero is accepted as a valid (halt-new-entries) capacity: PASSED")


if __name__ == "__main__":
    test_1_falls_back_to_env_default_when_no_override_set()
    test_2_runtime_override_takes_precedence_over_env_default()
    test_3_override_persists_across_a_simulated_restart()
    test_4_negative_value_rejected()
    test_5_swing_and_bollinger_overrides_are_independent()
    test_6_zero_is_a_valid_capacity_meaning_no_new_entries()
    print("\nAll tests passed.")
