"""
Tests for Swing's file-backed watchlist - user request 31 Aug 2026 ("Add
a file named 'watchlist' under folder data in server to keep all stocks
to monitor in it"). WatchlistStore.sync_from_file() reads a plain-text
file (data/watchlist, one symbol per line, blank lines/#-comments
ignored) and adds any new symbols to the in-memory store - re-run every
monitor_loop tick, same hot-reload UX choppy_stocks.py already
established (edit the file, no restart needed).

Covers:
  1. sync_from_file() adds every symbol from a real file, uppercased.
  2. Blank lines and #-prefixed comment lines are ignored.
  3. A symbol already on the watchlist isn't duplicated on a second sync.
  4. A missing file fails open (returns [], no exception) - same
     philosophy as choppy_stocks.py's is_choppy().
  5. Editing the file (simulating a hand-edit while the process keeps
     running) and syncing again picks up the newly-added line, with no
     restart/reload step - proving the hot-reload behavior actually
     works, not just "the file is read once at startup".
  6. The real seed content this request asked for (AUROPHARMA, OFSS,
     TORNTPHARM, VEDL) round-trips correctly through the real
     data/watchlist file this task created.

HOW TO RUN:
    uv run python tests/test_swing_watchlist_file.py
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

import Swing.watchlist as swl

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_watchlist_test_"))


_scratch_counter = 0


def _use_scratch_watchlist_file():
    """Points WATCHLIST_FILE at a fresh scratch path for one test, returns
    a restore() closure. A monotonic counter (not id(object()), which
    CPython can and does reuse - see NOTES.md entry #56's own test-bug
    for why that matters) guarantees no two tests ever collide."""
    global _scratch_counter
    _scratch_counter += 1
    real_file = swl.WATCHLIST_FILE
    swl.WATCHLIST_FILE = scratch_dir / f"watchlist_{_scratch_counter}"

    def restore():
        swl.WATCHLIST_FILE = real_file
    return restore


async def test_1_sync_adds_all_symbols_uppercased():
    store = swl.WatchlistStore()
    restore = _use_scratch_watchlist_file()
    try:
        swl.WATCHLIST_FILE.write_text("reliance\nTCS\nSbin\n")
        added = await store.sync_from_file()
        assert set(added) == {"RELIANCE", "TCS", "SBIN"}, added
        assert set(await store.symbols()) == {"RELIANCE", "TCS", "SBIN"}
        print("1. sync_from_file adds every symbol from the file, uppercased: PASSED")
    finally:
        restore()


async def test_2_blank_lines_and_comments_ignored():
    store = swl.WatchlistStore()
    restore = _use_scratch_watchlist_file()
    try:
        swl.WATCHLIST_FILE.write_text("# my watchlist\nRELIANCE\n\n  \n# TCS (not ready yet)\nSBIN\n")
        added = await store.sync_from_file()
        assert set(added) == {"RELIANCE", "SBIN"}, added
        assert "TCS" not in await store.symbols(), "a commented-out line must not be added"
        print("2. Blank lines and #-prefixed comments are correctly ignored: PASSED")
    finally:
        restore()


async def test_3_no_duplicate_on_repeated_sync():
    store = swl.WatchlistStore()
    restore = _use_scratch_watchlist_file()
    try:
        swl.WATCHLIST_FILE.write_text("RELIANCE\n")
        first = await store.sync_from_file()
        second = await store.sync_from_file()
        assert first == ["RELIANCE"]
        assert second == [], f"a symbol already on the watchlist must not be re-added, got {second}"
        assert await store.symbols() == ["RELIANCE"]
        print("3. A symbol already on the watchlist is never duplicated on a repeated sync: PASSED")
    finally:
        restore()


async def test_4_missing_file_fails_open():
    store = swl.WatchlistStore()
    restore = _use_scratch_watchlist_file()
    try:
        assert not swl.WATCHLIST_FILE.exists()
        added = await store.sync_from_file()
        assert added == [], "a missing file must fail open (nothing added), never raise"
        print("4. A missing data/watchlist file fails open (empty result, no exception): PASSED")
    finally:
        restore()


async def test_5_hot_edit_picked_up_without_restart():
    """Simulates the exact UX the user gets: edit the file while the
    process keeps running, and the very next sync (the same one
    monitor_loop calls every tick) picks up the change."""
    store = swl.WatchlistStore()
    restore = _use_scratch_watchlist_file()
    try:
        swl.WATCHLIST_FILE.write_text("RELIANCE\n")
        await store.sync_from_file()
        assert set(await store.symbols()) == {"RELIANCE"}

        # Simulate a hand-edit landing on disk mid-process - append a new line.
        with open(swl.WATCHLIST_FILE, "a") as f:
            f.write("TCS\n")
        added = await store.sync_from_file()
        assert added == ["TCS"], f"expected the newly-appended line to be picked up, got {added}"
        assert set(await store.symbols()) == {"RELIANCE", "TCS"}
        print("5. A hand-edit to the file (appending a new stock) is picked up on the very next "
              "sync, no restart/reload needed - the same hot-reload UX choppy_stocks.py has: PASSED")
    finally:
        restore()


async def test_6_real_seed_file_round_trips():
    """The actual data/watchlist file on the server - content has been
    replaced since this file was first seeded (31 Aug 2026: AUROPHARMA/
    OFSS/TORNTPHARM/VEDL; replaced 1 Sep 2026 with a 19-stock list, all 4
    originals included) - reads whatever's actually there right now
    rather than hardcoding either exact set, so this test doesn't go
    stale the next time the watchlist is edited."""
    real_file = swl.WATCHLIST_FILE
    swl.WATCHLIST_FILE = REPO_ROOT / "data" / "watchlist"
    try:
        assert swl.WATCHLIST_FILE.exists(), f"expected {swl.WATCHLIST_FILE} to exist"
        expected = {
            line.strip().upper() for line in swl.WATCHLIST_FILE.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        assert expected, "the real watchlist file must not be empty"
        store = swl.WatchlistStore()
        added = await store.sync_from_file()
        assert set(added) == expected, (sorted(added), sorted(expected))
        print(f"6. The real data/watchlist file round-trips correctly ({len(added)} symbols): PASSED")
    finally:
        swl.WATCHLIST_FILE = real_file


async def main():
    print("=== Swing file-backed watchlist test suite ===\n")
    await test_1_sync_adds_all_symbols_uppercased()
    await test_2_blank_lines_and_comments_ignored()
    await test_3_no_duplicate_on_repeated_sync()
    await test_4_missing_file_fails_open()
    await test_5_hot_edit_picked_up_without_restart()
    await test_6_real_seed_file_round_trips()
    print("\nALL SWING WATCHLIST FILE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
