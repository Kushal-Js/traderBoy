"""
Tests for capacity_control.py (added 26 Sep 2026, user request: "max
concurrent trade capacity is a configurable item which should be able to
change without a deployment by simply updating it using an endpoint").
Exercises the REAL module functions directly - no reimplementation - only
ever touching tempfile copies of OVERRIDE_FILE and ENV_FILE and resetting
the module's own in-memory state around each test, so this never reads/
writes the real data/capacity_overrides.json or .env.

Updated 27 Sep 2026 for the .env-sync fix (same fix, same reasoning, as
paper_mode_control.py's own - see that module's docstring, including
why the override is KEPT rather than dropped after a successful sync:
rewriting the .env FILE on disk doesn't change what the already-
imported Swing/Bollinger config module's cached value is for the
CURRENT process, so dropping the override would make an already-
running process silently fall back to whatever .env said at ITS OWN
startup the instant a requested value differs from that - caught by an
earlier version of test_2 below before this ever shipped with that bug
live). Test 7 is new - covers the fail-safe path (.env can't be written
-> override is kept, same as it always was).

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
    """Context manager: points OVERRIDE_FILE and ENV_FILE at throwaway
    tempfiles and resets capacity_control's in-memory override state on
    both entry and exit, so tests never see each other's state and never
    touch the real data/capacity_overrides.json or .env. ENV_FILE is
    seeded with both strategies' own lines already present, matching the
    real .env's shape, so the common "rewrite in place" path is what
    gets exercised (not the "line missing, append" fallback - that has
    its own dedicated test)."""

    def __enter__(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        env_path = Path(self._tmpdir.name) / ".env"
        env_path.write_text("SWING_MAX_CONCURRENT_TRADES=5\nBOLLINGER_MAX_CONCURRENT_TRADES=5\n")
        self._override_patch = mock.patch.object(
            capacity_control, "OVERRIDE_FILE", Path(self._tmpdir.name) / "capacity_overrides.json"
        )
        self._env_patch = mock.patch.object(capacity_control, "ENV_FILE", env_path)
        self._override_patch.start()
        self._env_patch.start()
        self.env_path = env_path
        capacity_control._overrides.clear()
        capacity_control._overrides_loaded = False
        return self

    def __exit__(self, *exc):
        self._override_patch.stop()
        self._env_patch.stop()
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


def test_2_set_value_rewrites_env_and_keeps_override():
    with _Isolated() as iso:
        asyncio.run(capacity_control.set_max_concurrent_trades("Swing", 3))
        # The override must be KEPT so THIS process immediately reads
        # the correct value - .env changing on disk does not affect
        # Swing.config.MAX_CONCURRENT_TRADES, which was already read
        # into memory before this call and stays whatever it was.
        assert capacity_control.get_max_concurrent_trades("Swing") == 3
        assert capacity_control.capacity_source("Swing") == "runtime_override", \
            capacity_control.capacity_source("Swing")
        assert "SWING_MAX_CONCURRENT_TRADES=3" in iso.env_path.read_text()
        assert capacity_control._overrides.get("Swing") == 3
    print("2. set_max_concurrent_trades rewrites .env's own line AND keeps the override - "
          "this process reads the new value immediately: PASSED")


def test_3_value_survives_a_simulated_restart_via_either_mechanism():
    """A restart re-imports this module fresh. Simulated two ways: (a)
    OVERRIDE_FILE still on disk (the normal case) - value comes from
    the override, re-read from disk. (b) override file somehow lost but
    .env was kept in sync regardless - value still comes back correctly
    from .env's own updated default, proving .env sync is a genuine
    second layer of restart-safety, not just cosmetic."""
    with _Isolated() as iso:
        asyncio.run(capacity_control.set_max_concurrent_trades("Swing", 2))

        capacity_control._overrides.clear()
        capacity_control._overrides_loaded = False
        assert capacity_control.get_max_concurrent_trades("Swing") == 2, \
            "(a) override survives a restart via OVERRIDE_FILE, same as before this fix"

        capacity_control._overrides.clear()
        capacity_control._overrides_loaded = True  # pretend OVERRIDE_FILE was lost
        with mock.patch.object(swing_config, "MAX_CONCURRENT_TRADES", 2):
            assert capacity_control.get_max_concurrent_trades("Swing") == 2, \
                "(b) even with no override at all, .env's own updated default is correct"
            assert capacity_control.capacity_source("Swing") == "env_default"
    print("3. Value survives a simulated restart via the override (normal case) AND via "
          ".env alone if the override file were ever lost (the new safety net): PASSED")


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
                "setting Swing's value must not affect Bollinger's own value"
            assert capacity_control.capacity_source("Bollinger") == "env_default"
    print("5. Swing and Bollinger values are independent: PASSED")


def test_6_zero_is_a_valid_capacity_meaning_no_new_entries():
    with _Isolated():
        asyncio.run(capacity_control.set_max_concurrent_trades("Bollinger", 0))
        assert capacity_control.get_max_concurrent_trades("Bollinger") == 0
    print("6. Zero is accepted as a valid (halt-new-entries) capacity: PASSED")


def test_7_env_write_failure_keeps_override_as_fallback():
    """If .env genuinely can't be written, the runtime override must be
    KEPT so the in-process value is still correct - the same fail-safe
    paper_mode_control.py's own .env-sync fix has."""
    with _Isolated() as iso:
        capacity_control.ENV_FILE = iso.env_path.parent / "does_not_exist" / ".env"
        asyncio.run(capacity_control.set_max_concurrent_trades("Swing", 4))
        assert capacity_control._overrides.get("Swing") == 4, \
            "when .env can't be synced, the override must be kept so runtime behavior stays correct"
        assert capacity_control.get_max_concurrent_trades("Swing") == 4
        assert capacity_control.capacity_source("Swing") == "runtime_override"
    print("7. When .env can't be written, the runtime override is kept as a fail-safe: PASSED")


if __name__ == "__main__":
    test_1_falls_back_to_env_default_when_no_override_set()
    test_2_set_value_rewrites_env_and_keeps_override()
    test_3_value_survives_a_simulated_restart_via_either_mechanism()
    test_4_negative_value_rejected()
    test_5_swing_and_bollinger_overrides_are_independent()
    test_6_zero_is_a_valid_capacity_meaning_no_new_entries()
    test_7_env_write_failure_keeps_override_as_fallback()
    print("\nAll tests passed.")
