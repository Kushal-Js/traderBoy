"""
Tests for Swing/watchlist.py's simplified file sync - specifically the
backward-compatible handling of the old `,YYYY-MM-DD` line suffix, found
live 12 Sep 2026: the real droplet's data/watchlist file still has every
line in that old format (e.g. "ASHOKLEY,2026-09-01") from the pre-
rewrite design, and the naive `line.strip().upper()` this rewrite
originally shipped with would have imported the literal string
"ASHOKLEY,2026-09-01" as a symbol - caught by the Stage 3/4 dry-run
before any code went live.

HOW TO RUN:
    uv run python tests/test_swing_v2_watchlist.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from Swing.watchlist import WatchlistStore


def test_1_old_date_suffix_lines_are_stripped_to_just_the_symbol():
    store = WatchlistStore()
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "watchlist"
        f.write_text("ASHOKLEY,2026-09-01\nNMDC,2026-09-01\n# a comment\n\nBAJAJ-AUTO,2026-09-01\n")
        import Swing.watchlist as wl_module
        real_file = wl_module.WATCHLIST_FILE
        wl_module.WATCHLIST_FILE = f
        try:
            added = asyncio.run(store.sync_from_file())
            assert set(added) == {"ASHOKLEY", "NMDC", "BAJAJ-AUTO"}, added
            assert "ASHOKLEY,2026-09-01" not in added, "the date suffix must never end up baked into the symbol"
            print("1. Old ',YYYY-MM-DD' suffixed lines are correctly stripped to just the symbol: PASSED")
        finally:
            wl_module.WATCHLIST_FILE = real_file


def test_2_plain_lines_without_a_suffix_still_work():
    store = WatchlistStore()
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "watchlist"
        f.write_text("RELIANCE\nTCS\n")
        import Swing.watchlist as wl_module
        real_file = wl_module.WATCHLIST_FILE
        wl_module.WATCHLIST_FILE = f
        try:
            added = asyncio.run(store.sync_from_file())
            assert set(added) == {"RELIANCE", "TCS"}, added
            print("2. Plain lines with no comma suffix still work exactly as before: PASSED")
        finally:
            wl_module.WATCHLIST_FILE = real_file


def test_3_missing_file_fails_open():
    store = WatchlistStore()
    import Swing.watchlist as wl_module
    real_file = wl_module.WATCHLIST_FILE
    wl_module.WATCHLIST_FILE = Path("/tmp/definitely_does_not_exist_swing_watchlist_test")
    try:
        added = asyncio.run(store.sync_from_file())
        assert added == []
        print("3. A missing watchlist file fails open (empty list, no exception): PASSED")
    finally:
        wl_module.WATCHLIST_FILE = real_file


def test_4_replace_symbols_wipes_and_replaces_not_adds():
    async def run():
        store = WatchlistStore()
        await store.add_symbols(["OLDSTOCK1", "OLDSTOCK2"])
        result = await store.replace_symbols(["ADANIPORTS", "coalindia", " copper "])
        assert result == ["ADANIPORTS", "COALINDIA", "COPPER"], result
        current = await store.symbols()
        assert set(current) == {"ADANIPORTS", "COALINDIA", "COPPER"}, current
        assert "OLDSTOCK1" not in current and "OLDSTOCK2" not in current, \
            "replace must be a full wipe, not additive like add_symbols"
    asyncio.run(run())
    print("4. replace_symbols wipes the existing watchlist entirely (not additive), dedupes/uppercases: PASSED")


def test_5_persist_to_file_writes_back_and_backs_up_the_old_file():
    async def run():
        store = WatchlistStore()
        await store.replace_symbols(["ADANIPORTS", "COPPER"])
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "watchlist"
            f.write_text("OLDSTOCK1\nOLDSTOCK2\n")
            import Swing.watchlist as wl_module
            real_file = wl_module.WATCHLIST_FILE
            wl_module.WATCHLIST_FILE = f
            try:
                await store.persist_to_file()
                new_content = f.read_text()
                assert new_content == "ADANIPORTS\nCOPPER\n", repr(new_content)
                backups = list(Path(d).glob("watchlist.bak.*"))
                assert len(backups) == 1, "the pre-existing file must be backed up before being overwritten"
                assert backups[0].read_text() == "OLDSTOCK1\nOLDSTOCK2\n", \
                    "the backup must hold the OLD content, not the new one"
            finally:
                wl_module.WATCHLIST_FILE = real_file
    asyncio.run(run())
    print("5. persist_to_file overwrites the file with the current in-memory watchlist, backing up "
          "the old content first: PASSED")


def main():
    print("=== Swing v2 watchlist file-sync test suite ===\n")
    test_1_old_date_suffix_lines_are_stripped_to_just_the_symbol()
    test_2_plain_lines_without_a_suffix_still_work()
    test_3_missing_file_fails_open()
    test_4_replace_symbols_wipes_and_replaces_not_adds()
    test_5_persist_to_file_writes_back_and_backs_up_the_old_file()
    print("\nALL SWING V2 WATCHLIST CHECKS PASSED")


if __name__ == "__main__":
    main()
