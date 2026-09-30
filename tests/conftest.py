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

OFFLINE INSTRUMENT MASTER (added 1 Oct 2026): a test module that defines
OFFLINE_INSTRUMENTS = {"equities": [...], "mcx": [...]} gets
tests/offline_instruments.install() for the duration of that module (its own
main() does the same when run standalone) - see that helper's docstring.

CANDLE FILES (added 1 Oct 2026): Swing/candle_feed.py and
underlying_candle_feed.py persist every completed bar under their own
HISTORY_DIR (default: the real history/ folder). The two candle-feed test
files redirect it only in their standalone main(), so every pytest run wrote
TESTSYM / STRESSTEST / RESTARTSYM / COPPER_OLD_CONTRACT bar files into the
real history/ - and read the real ones back (test_5 found 756 bars instead of
1). isolated_candle_history_dirs points both at scratch dirs per module.

DHAN WRAPPER SINGLETON (added 1 Oct 2026): some files replace the module
attribute Options.dhan_client.dhan_wrapper with a fake (e.g.
test_swing_candle_feed's _install_fake_dhan_wrapper) and never put the real
one back - fine as a standalone script, but under pytest every LATER file
then patched methods onto the fake while the code under test still used the
real singleton (~40 Swing failures, "'_FakeDhanWrapper' object has no
attribute ..."). restore_dhan_wrapper_singleton puts the original back after
each module.

ASYNC TESTS (added 1 Oct 2026): most older files are standalone scripts whose
`async def test_*` functions their own `main()` runs with asyncio. Under
pytest (no pytest-asyncio installed) every one of them was reported as a
failure - "async def functions are not natively supported" - about 176 of the
suite's ~198 "failures", which hid the real ones. pytest_pyfunc_call below
runs each coroutine test in its own event loop (asyncio.run), with its
fixtures, exactly like the scripts do.
"""
import asyncio
import inspect
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


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run `async def test_*` functions (see the module docstring)."""
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        args = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(pyfuncitem.obj(**args))
        return True
    return None


@pytest.fixture(autouse=True, scope="module")
def offline_instrument_master(request):
    """See OFFLINE INSTRUMENT MASTER in the module docstring."""
    spec = getattr(request.module, "OFFLINE_INSTRUMENTS", None)
    if spec is None:
        yield
        return
    import offline_instruments
    from Options.dhan_client import dhan_wrapper
    restore = offline_instruments.install(dhan_wrapper, **spec)
    try:
        yield
    finally:
        restore()


@pytest.fixture(autouse=True, scope="module")
def module_config_overrides(request):
    """A test module's TEST_CONFIG_OVERRIDES (see tests/config_overrides.py), applied for that module only."""
    overrides = getattr(request.module, "TEST_CONFIG_OVERRIDES", None)
    if not overrides:
        yield
        return
    import config_overrides
    restore = config_overrides.apply_config_overrides(overrides)
    try:
        yield
    finally:
        restore()


@pytest.fixture(autouse=True, scope="module")
def isolated_candle_history_dirs():
    """See CANDLE FILES in the module docstring."""
    import underlying_candle_feed
    from Swing import candle_feed
    saved = (candle_feed.HISTORY_DIR, underlying_candle_feed.HISTORY_DIR)
    scratch = Path(tempfile.mkdtemp(prefix="dhanboy_pytest_candles_"))
    candle_feed.HISTORY_DIR, underlying_candle_feed.HISTORY_DIR = scratch / "swing", scratch / "underlying"
    try:
        yield
    finally:
        candle_feed.HISTORY_DIR, underlying_candle_feed.HISTORY_DIR = saved
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture(autouse=True, scope="module")
def restore_dhan_wrapper_singleton():
    """See DHAN WRAPPER SINGLETON in the module docstring."""
    import Options.dhan_client as dc
    original = dc.dhan_wrapper
    try:
        yield
    finally:
        dc.dhan_wrapper = original
