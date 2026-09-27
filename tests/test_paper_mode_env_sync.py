"""
Tests for paper_mode_control.set_paper_mode's .env-sync fix (27 Sep
2026, real incident: Swing's own .env said paper-mode-on, but an
undated runtime override had it real-trading instead - discovered by
chance days later, not by anything surfacing the disagreement). Covers:

  1. set_paper_mode rewrites the strategy's own .env line to match, and
     drops the runtime override once .env agrees - source becomes
     "env_default", not "runtime_override", after a successful sync.
  2. A strategy whose .env has no existing line for its own var gets one
     appended, not silently dropped.
  3. If .env can't be written, the runtime override is KEPT (so the
     in-process behavior is still correct) and is_paper_mode_enabled
     still reflects the requested value - the fail-safe path.

Uses a scratch .env file (via monkeypatching ENV_FILE), never touches
the real repo .env.

HOW TO RUN:
    uv run python tests/test_paper_mode_env_sync.py
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

import paper_mode_control as pmc

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_paper_mode_env_sync_test_"))


def _reset(env_text: str = "") -> Path:
    pmc._overrides.clear()
    pmc._overrides_loaded = True  # skip _load_overrides' own disk read
    env_path = scratch_dir / f"env_{id(env_text)}"
    env_path.write_text(env_text)
    pmc.ENV_FILE = env_path
    pmc.OVERRIDE_FILE = scratch_dir / f"overrides_{id(env_text)}.json"
    return env_path


async def test_1_env_line_rewritten_and_override_dropped():
    env_path = _reset("SWING_PAPER_MODE_ENABLED=false\nOTHER_VAR=123\n")
    await pmc.set_paper_mode("Swing", True)
    text = env_path.read_text()
    assert "SWING_PAPER_MODE_ENABLED=true\n" in text, text
    assert "OTHER_VAR=123\n" in text, "an unrelated line must survive untouched"
    assert "Swing" not in pmc._overrides, "override must be dropped once .env agrees"
    assert pmc.paper_mode_source("Swing") == "env_default", pmc.paper_mode_source("Swing")
    print("1. set_paper_mode rewrites .env's own line and drops the now-redundant "
          "override - source reports 'env_default': PASSED")


async def test_2_missing_line_gets_appended():
    env_path = _reset("UNRELATED=1\n")
    await pmc.set_paper_mode("Bollinger", True)
    text = env_path.read_text()
    assert "BOLLINGER_PAPER_MODE_ENABLED=true" in text, text
    assert "UNRELATED=1" in text
    print("2. A strategy with no existing .env line gets one appended, not dropped: PASSED")


async def test_3_env_write_failure_keeps_override_as_fallback():
    _reset("")
    pmc.ENV_FILE = scratch_dir / "does_not_exist" / "env"  # parent dir missing -> can't write
    await pmc.set_paper_mode("Luxury", True)
    assert pmc._overrides.get("Luxury") is True, \
        "when .env can't be synced, the override must be kept so runtime behavior stays correct"
    assert pmc.is_paper_mode_enabled("Luxury") is True
    assert pmc.paper_mode_source("Luxury") == "runtime_override"
    print("3. When .env can't be written, the runtime override is kept as a fail-safe - "
          "is_paper_mode_enabled still reflects the requested value: PASSED")


async def main():
    print("=== paper_mode_control .env-sync test suite ===\n")
    await test_1_env_line_rewritten_and_override_dropped()
    await test_2_missing_line_gets_appended()
    await test_3_env_write_failure_keeps_override_as_fallback()
    print("\nALL PAPER-MODE ENV-SYNC CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
