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
  7. A line's optional `,YYYY-MM-DD` suffix (added 2 Sep 2026) correctly
     backdates that symbol's own added_at/last_confirmed_at, a plain
     symbol still gets "now", and an unparseable date falls back to
     "now" for that one symbol rather than aborting the sync.
  8-11. A real bug found live 7 Sep 2026: sync_from_file()'s own per-tick
     re-add was silently undoing the trend/stale-age prunes' own
     removals for any symbol still listed in the file (confirmed live -
     UNOMINDA/ATHERENERG/GAIL were pruned at 09:15 IST but still showed
     up on GET /swing/watchlist moments later). remove_symbol(...,
     suppress_resync_today=True) - what both prunes now call - survives
     a subsequent sync_from_file() the same day (8); an UNSUPPRESSED
     removal (the OTHER remove_symbol call sites, "just entered a real
     position") still resyncs normally, unchanged (9); the suppression
     is scoped to the same calendar day only, not permanent (10); and
     the REAL production _daily_watchlist_prune_tick's own removal
     survives a REAL subsequent sync_from_file() call, full end-to-end
     (11).

HOW TO RUN:
    uv run python tests/test_swing_watchlist_file.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
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
    originals included; each line gained a `,YYYY-MM-DD` curation date
    2 Sep 2026, see test_7 below) - reads whatever's actually there right
    now rather than hardcoding either exact set, so this test doesn't go
    stale the next time the watchlist is edited. Only the symbol part of
    each line (before any comma) is compared here - the optional date
    suffix is test_7's own concern."""
    real_file = swl.WATCHLIST_FILE
    swl.WATCHLIST_FILE = REPO_ROOT / "data" / "watchlist"
    try:
        assert swl.WATCHLIST_FILE.exists(), f"expected {swl.WATCHLIST_FILE} to exist"
        expected = {
            line.strip().partition(",")[0].strip().upper()
            for line in swl.WATCHLIST_FILE.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        assert expected, "the real watchlist file must not be empty"
        store = swl.WatchlistStore()
        added = await store.sync_from_file()
        assert set(added) == expected, (sorted(added), sorted(expected))
        print(f"6. The real data/watchlist file round-trips correctly ({len(added)} symbols): PASSED")
    finally:
        swl.WATCHLIST_FILE = real_file


async def test_7_optional_per_line_date_sets_added_at():
    """Added 2 Sep 2026, user request: backdate the then-current
    watchlist to 1 Sep 2026 "so they can later be pruned if required".
    A line's optional `,YYYY-MM-DD` suffix becomes that symbol's own
    added_at/last_confirmed_at instead of "now" - a plain symbol (no
    date) still gets "now", unaffected; an unparseable date is logged
    and swallowed, falling back to "now" for that ONE symbol rather than
    aborting the whole sync."""
    store = swl.WatchlistStore()
    restore = _use_scratch_watchlist_file()
    try:
        swl.WATCHLIST_FILE.write_text(
            "RELIANCE,2026-09-01\nTCS\nSBIN,not-a-real-date\n"
        )
        before = datetime.now()
        added = await store.sync_from_file()
        after = datetime.now()
        assert set(added) == {"RELIANCE", "TCS", "SBIN"}

        snap = {e["symbol"]: e for e in (await store.snapshot())["watchlist"]}
        assert snap["RELIANCE"]["added_at"].startswith("2026-09-01T00:00:00")
        assert snap["RELIANCE"]["last_confirmed_at"].startswith("2026-09-01T00:00:00")

        for sym in ("TCS", "SBIN"):
            added_at = datetime.fromisoformat(snap[sym]["added_at"])
            assert before <= added_at <= after, \
                f"{sym} has no valid date suffix - must fall back to 'now', got {added_at}"

        print("7. A line's optional ',YYYY-MM-DD' suffix correctly becomes that symbol's own "
              "added_at/last_confirmed_at, a plain symbol still gets 'now', and an unparseable "
              "date falls back to 'now' rather than aborting the sync: PASSED")
    finally:
        restore()


async def test_8_suppressed_removal_survives_a_resync():
    """Reproduces the real live bug found 7 Sep 2026: UNOMINDA/
    ATHERENERG/GAIL were pruned for a genuine trend break at 09:15 IST,
    but GET /swing/watchlist still showed all three moments later,
    because sync_from_file() was silently re-adding them from
    data/watchlist on the very next tick. remove_symbol(...,
    suppress_resync_today=True) - what the trend/stale-age prunes now
    both call - must survive a subsequent sync_from_file() the same
    day."""
    store = swl.WatchlistStore()
    restore = _use_scratch_watchlist_file()
    try:
        swl.WATCHLIST_FILE.write_text("UNOMINDA,2026-09-01\n")
        await store.sync_from_file()
        assert "UNOMINDA" in await store.symbols()

        removed = await store.remove_symbol("UNOMINDA", suppress_resync_today=True)
        assert removed is True
        assert "UNOMINDA" not in await store.symbols()

        # The exact bug: data/watchlist STILL lists UNOMINDA (removing a
        # line from the file was never part of this mechanism - see the
        # module's own docstring) - a naive re-sync would silently bring
        # it right back.
        again = await store.sync_from_file()
        assert "UNOMINDA" not in again, \
            f"a suppressed removal must NOT be undone by the very next sync, got {again}"
        assert "UNOMINDA" not in await store.symbols(), \
            "UNOMINDA must still be absent from the live watchlist after the resync"
        print("8. A prune's own removal (suppress_resync_today=True) correctly survives a "
              "subsequent sync_from_file() the same day - the exact live bug this fixes: PASSED")
    finally:
        restore()


async def test_9_unsuppressed_removal_still_resyncs_normally():
    """The OTHER remove_symbol call sites in this codebase (a symbol
    taken off the watchlist because it just entered a real position)
    deliberately do NOT pass suppress_resync_today - that removal SHOULD
    become watchable again as soon as it's re-synced. Confirms the
    default (suppress_resync_today=False) keeps the pre-fix behavior
    completely unchanged - this fix is additive, not a behavior change
    for every other caller."""
    store = swl.WatchlistStore()
    restore = _use_scratch_watchlist_file()
    try:
        swl.WATCHLIST_FILE.write_text("RELIANCE\n")
        await store.sync_from_file()
        await store.remove_symbol("RELIANCE")  # default: suppress_resync_today=False
        assert "RELIANCE" not in await store.symbols()

        again = await store.sync_from_file()
        assert "RELIANCE" in again, \
            "an UNSUPPRESSED removal must still resync normally - this fix must not change that path"
        print("9. An unsuppressed removal (the default) still resyncs normally on the next tick - "
              "this fix doesn't change behavior for the 'just entered a position' removal path: PASSED")
    finally:
        restore()


async def test_10_suppression_resets_on_a_new_day():
    """A symbol pruned today must become eligible for re-sync again
    tomorrow - the suppression is a same-day-only guard, not permanent
    (a permanent block would need its own explicit un-prune mechanism,
    which isn't what was asked for or built here)."""
    store = swl.WatchlistStore()
    restore = _use_scratch_watchlist_file()
    try:
        swl.WATCHLIST_FILE.write_text("MAHABANK,2026-09-01\n")
        await store.sync_from_file()
        await store.remove_symbol("MAHABANK", suppress_resync_today=True)
        assert "MAHABANK" not in (await store.sync_from_file())

        # Simulate the next calendar day by backdating the store's own
        # bookkeeping directly (the same field remove_symbol/sync_from_file
        # themselves read) rather than reaching for a real datetime mock.
        store._pruned_today_date = store._pruned_today_date - timedelta(days=1)

        again = await store.sync_from_file()
        assert "MAHABANK" in again, "the suppression must NOT still apply on a new calendar day"
        print("10. The suppression is scoped to the SAME calendar day only - a new day makes the "
              "symbol eligible for re-sync again: PASSED")
    finally:
        restore()


async def test_11_real_daily_prune_tick_removal_survives_a_real_resync():
    """Full real integration - the actual production
    _daily_watchlist_prune_tick (imported from Swing.trading_engine)
    against a real trend-broken symbol, followed by a real
    sync_from_file() call, reproducing the exact live scenario end to
    end rather than only testing the store's own two methods in
    isolation."""
    import Swing.trading_engine as ste

    real_watchlist_store = ste.watchlist_store
    real_last_prune_date = ste._last_watchlist_prune_date
    real_enabled = ste.config.WATCHLIST_DAILY_PRUNE_ENABLED
    real_now_ist = ste._now_ist
    real_evaluate = ste._evaluate_daily_trend_break

    store = swl.WatchlistStore()
    ste.watchlist_store = store
    ste._last_watchlist_prune_date = None
    ste.config.WATCHLIST_DAILY_PRUNE_ENABLED = True
    ste._now_ist = lambda: datetime.now().replace(hour=9, minute=20)  # past the 09:15 gate
    ste._evaluate_daily_trend_break = lambda symbol: (
        "DAILY_EMA12_CROSSED_BELOW" if symbol == "GAIL" else None
    )
    restore = _use_scratch_watchlist_file()
    try:
        swl.WATCHLIST_FILE.write_text("GAIL,2026-09-01\nRELIANCE,2026-09-01\n")
        await store.sync_from_file()
        assert set(await store.symbols()) == {"GAIL", "RELIANCE"}

        await ste._daily_watchlist_prune_tick()
        assert "GAIL" not in await store.symbols(), "the real prune tick must have removed GAIL"

        # The exact live bug: a later real sync_from_file() call (as
        # monitor_loop makes every tick) must NOT bring GAIL back, even
        # though data/watchlist still lists it.
        await store.sync_from_file()
        assert "GAIL" not in await store.symbols(), \
            "GAIL must still be absent after a real sync_from_file() call - this is the exact live bug"
        assert "RELIANCE" in await store.symbols(), "an unaffected symbol must be untouched throughout"

        print("11. The REAL production _daily_watchlist_prune_tick's own removal survives a REAL "
              "subsequent sync_from_file() call, full end-to-end - the exact live scenario: PASSED")
    finally:
        restore()
        ste.watchlist_store = real_watchlist_store
        ste._last_watchlist_prune_date = real_last_prune_date
        ste.config.WATCHLIST_DAILY_PRUNE_ENABLED = real_enabled
        ste._now_ist = real_now_ist
        ste._evaluate_daily_trend_break = real_evaluate


async def main():
    print("=== Swing file-backed watchlist test suite ===\n")
    await test_1_sync_adds_all_symbols_uppercased()
    await test_2_blank_lines_and_comments_ignored()
    await test_3_no_duplicate_on_repeated_sync()
    await test_4_missing_file_fails_open()
    await test_5_hot_edit_picked_up_without_restart()
    await test_6_real_seed_file_round_trips()
    await test_7_optional_per_line_date_sets_added_at()
    await test_8_suppressed_removal_survives_a_resync()
    await test_9_unsuppressed_removal_still_resyncs_normally()
    await test_10_suppression_resets_on_a_new_day()
    await test_11_real_daily_prune_tick_removal_survives_a_real_resync()
    print("\nALL SWING WATCHLIST FILE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
