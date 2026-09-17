"""
Shared pytest fixtures for this test suite.

Real bug, found via a flaky "daily_reentry_cap_reached" failure that kept
shifting to a different specific test across sessions (confirmed twice on
2026-09-17 alone - see trading-skills' incident write-ups from that date):
every test file below sets `trade_history.HISTORY_DIR` to its own scratch
tempdir at MODULE IMPORT time (see this directory's own README.md, "it
points trade_history.HISTORY_DIR at a scratch temp directory"), so each
file's tests don't touch the real history/ folder or leak into another
file's. That works when a file is run standalone (`python tests/test_X.py`),
but `pytest tests/` imports every test MODULE during collection, before
running any test - so whichever file happens to be collected/imported LAST
silently "wins" that one shared global assignment for the rest of the run.
Every OTHER file's tests then read/write daily-entry counts
(count_opened_today, attribute_open_broker_position - both keyed off
trade_history.HISTORY_DIR) from that ONE shared tempdir instead of an
isolated one. Symbols reused across many files (TCS, RELIANCE, WIPRO,
HDFCBANK, etc.) then accumulate cross-file entry counts, occasionally
tripping MAX_DAILY_ENTRIES_PER_SYMBOL in a test that never itself placed
enough entries to earn that cap on its own.

Fix: an autouse, MODULE-scoped fixture that runs once before the first
test in each test file and points trade_history.HISTORY_DIR at a fresh,
empty tempdir for the rest of that file's own tests - overriding whatever
an EARLIER-collected module's import-time assignment left behind,
restoring the original value and deleting the tempdir once the whole
module is done. Module scope (not function scope) matters: several files
(e.g. test_daily_reentry_cap.py's own test_2 -> test_3) deliberately rely
on state persisting ACROSS tests within the same file, to simulate "a
restart's counter survives on disk" - a function-scoped fixture that wipes
HISTORY_DIR before every single test would break that legitimate,
intentional within-file continuity while fixing the cross-file leak. Module
scope preserves it: every test in ONE file still shares one HISTORY_DIR (as
each file's own name intended), it just genuinely stops being clobbered by
whichever OTHER file pytest happened to import last.

This does not change how any file behaves when run standalone (that path
never goes through pytest/conftest.py at all) - only pytest-driven runs
gain real per-module isolation.
"""
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import trade_history


@pytest.fixture(autouse=True, scope="module")
def isolated_trade_history_dir():
    original = trade_history.HISTORY_DIR
    scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_pytest_history_"))
    trade_history.HISTORY_DIR = scratch_dir
    try:
        yield
    finally:
        trade_history.HISTORY_DIR = original
        shutil.rmtree(scratch_dir, ignore_errors=True)
