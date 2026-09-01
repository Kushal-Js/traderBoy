"""
Tests for Swing's stale-age watchlist prune (added 1 Sep 2026, user
request, verbatim): "Add one more pruning logic for watchlist update
that those stocks that were added 10 days earlier to be removed unless
they are again fed in using chartink scan results."

A SECOND, independent daily watchlist prune alongside the daily TREND-
based one (test_swing_daily_watchlist_prune.py) - this one is purely
AGE-based: any watchlist symbol whose own `last_confirmed_at` (see
Swing/watchlist.WatchlistEntry) is config.WATCHLIST_STALE_AGE_DAYS (10
by default) or more calendar days old gets removed. Applies to EVERY
watchlist symbol uniformly (webhook-added, hand-edited-file-added, or
Chartink-scan-added alike) - the ONE thing that resets a symbol's own
clock is the daily Chartink scan pull re-returning it (see
test_swing_chartink_scan.py's own test 2 for that half of the
mechanism - `confirm_chartink_symbols` resetting `last_confirmed_at`).
This file covers the REMOVAL side.

Covers, against the REAL production functions (not reimplemented):
  1. `WatchlistStore.stale_symbols` - the exact age boundary (>= N days
     old is stale, one day under is not stale), against real
     WatchlistEntry objects with a manually-backdated `last_confirmed_at`
     (no mocked clock needed - the store's own date-diff math is what's
     under test).
  2. `_stale_watchlist_age_prune_tick`'s own once-per-day gating: nothing
     before 09:15 IST, the first eligible tick actually prunes, a LATER
     tick the same day is a no-op, a fresh trading day resets it - same
     pattern as the trend-based prune's own gating.
  3. A mixed watchlist pruned correctly: a genuinely stale symbol (10+
     days since last confirmed) removed; a symbol just under the
     threshold (9 days) left alone; and - the crux of "unless they are
     again fed in using chartink scan results" - a symbol originally
     added 15 days ago but RECONFIRMED by the Chartink scan just today
     survives, because its own clock is `last_confirmed_at`, not
     `added_at`. Each real removal durably logs a
     `WATCHLIST_STALE_AGE_PRUNED` event with the right detail.
  4. `config.WATCHLIST_STALE_AGE_PRUNE_ENABLED = False` disables the
     whole feature - even a symbol 100 days stale survives untouched.

HOW TO RUN:
    uv run python tests/test_swing_stale_age_prune.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_stale_age_prune_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Swing.trading_engine as ste
import Swing.watchlist as swl

IST = ZoneInfo("Asia/Kolkata")


def _backdate(store: swl.WatchlistStore, symbol: str, days_ago: int) -> None:
    """Test helper - directly rewrites a symbol's own last_confirmed_at
    (and added_at, so both stay consistent) to simulate it having sat on
    the watchlist for `days_ago` calendar days without being touched.
    Only ever pokes at the store's own real WatchlistEntry objects -
    nothing about stale_symbols()'s own logic is reimplemented here."""
    backdated = datetime.now() - timedelta(days=days_ago)
    entry = store._symbols[symbol]
    entry.added_at = backdated
    entry.last_confirmed_at = backdated


async def test_1_stale_symbols_exact_age_boundary():
    store = swl.WatchlistStore()
    await store.add_symbols(["EXACTLY10", "NINEDAYS", "FRESH"])
    _backdate(store, "EXACTLY10", days_ago=10)
    _backdate(store, "NINEDAYS", days_ago=9)
    # FRESH stays at its real just-added timestamp (0 days old).

    stale = await store.stale_symbols(max_age_days=10)
    stale_symbols = {sym for sym, _ in stale}
    assert stale_symbols == {"EXACTLY10"}, \
        f"expected exactly EXACTLY10 (>= 10 days) to be stale, NINEDAYS (9 days) and FRESH (0 days) " \
        f"to survive, got {stale_symbols}"

    print("1. WatchlistStore.stale_symbols correctly applies the '>= N calendar days old' boundary - "
          "a symbol exactly at the threshold is stale, one day under is not: PASSED")


async def test_2_daily_once_gating():
    store = swl.WatchlistStore()
    ste.watchlist_store = store
    real_enabled = ste.config.WATCHLIST_STALE_AGE_PRUNE_ENABLED
    ste.config.WATCHLIST_STALE_AGE_PRUNE_ENABLED = True
    real_now = ste._now_ist
    real_last_prune = ste._last_stale_age_prune_date
    ste._last_stale_age_prune_date = None

    await store.add_symbols(["OLDONE"])
    _backdate(store, "OLDONE", days_ago=30)
    try:
        # Before 09:15 IST - must do nothing.
        ste._now_ist = lambda: datetime(2026, 9, 5, 9, 0, tzinfo=IST)
        await ste._stale_watchlist_age_prune_tick()
        assert ste._last_stale_age_prune_date is None
        assert "OLDONE" in await store.symbols(), "must not prune before market open"

        # At/after 09:15 the SAME day - the first call actually runs.
        ste._now_ist = lambda: datetime(2026, 9, 5, 9, 15, tzinfo=IST)
        await ste._stale_watchlist_age_prune_tick()
        assert ste._last_stale_age_prune_date == date(2026, 9, 5)
        assert "OLDONE" not in await store.symbols()

        # A LATER tick, same day - no-op (re-add OLDONE and re-backdate
        # it; if the gate weren't date-based it would get pruned again
        # immediately, which would still pass by coincidence - so instead
        # assert the gate variable itself doesn't move and a FRESH stale
        # symbol added after the first run survives until tomorrow).
        await store.add_symbols(["ADDEDLATE"])
        _backdate(store, "ADDEDLATE", days_ago=30)
        ste._now_ist = lambda: datetime(2026, 9, 5, 15, 0, tzinfo=IST)
        await ste._stale_watchlist_age_prune_tick()
        assert ste._last_stale_age_prune_date == date(2026, 9, 5)
        assert "ADDEDLATE" in await store.symbols(), \
            "a later tick the same day must be a no-op, even for a symbol that's already stale"

        # A NEW trading day resets the gate, allowed to prune again -
        # ADDEDLATE (already stale) is now removed.
        ste._now_ist = lambda: datetime(2026, 9, 6, 9, 20, tzinfo=IST)
        await ste._stale_watchlist_age_prune_tick()
        assert ste._last_stale_age_prune_date == date(2026, 9, 6)
        assert "ADDEDLATE" not in await store.symbols()

        print("2. _stale_watchlist_age_prune_tick runs exactly ONCE per trading day at/after 09:15 "
              "IST - does nothing before market open, runs on the first eligible tick, stays a "
              "no-op for every later tick the same day (even for an already-stale symbol), and "
              "resets cleanly on a new trading day: PASSED")
    finally:
        ste._now_ist = real_now
        ste._last_stale_age_prune_date = real_last_prune
        ste.config.WATCHLIST_STALE_AGE_PRUNE_ENABLED = real_enabled


async def test_3_mixed_watchlist_pruned_correctly_including_chartink_reconfirm():
    store = swl.WatchlistStore()
    ste.watchlist_store = store
    real_enabled = ste.config.WATCHLIST_STALE_AGE_PRUNE_ENABLED
    ste.config.WATCHLIST_STALE_AGE_PRUNE_ENABLED = True
    real_now = ste._now_ist
    real_last_prune = ste._last_stale_age_prune_date
    ste._last_stale_age_prune_date = None
    ste._now_ist = lambda: datetime(2026, 9, 7, 9, 15, tzinfo=IST)

    await store.add_symbols(["STALESTOCK", "FRESHSTOCK", "RECONFIRMEDSTOCK"])
    _backdate(store, "STALESTOCK", days_ago=12)
    _backdate(store, "FRESHSTOCK", days_ago=3)
    # RECONFIRMEDSTOCK was originally added 15 days ago (well past the
    # threshold on added_at alone) BUT the Chartink scan re-fed it
    # earlier today - exactly the "unless fed in again" case.
    store._symbols["RECONFIRMEDSTOCK"].added_at = datetime.now() - timedelta(days=15)
    store._symbols["RECONFIRMEDSTOCK"].last_confirmed_at = datetime.now()

    try:
        await ste._stale_watchlist_age_prune_tick()
        remaining = set(await store.symbols())
        assert remaining == {"FRESHSTOCK", "RECONFIRMEDSTOCK"}, remaining

        await asyncio.sleep(0.2)
        relevant_symbols = {"STALESTOCK", "FRESHSTOCK", "RECONFIRMEDSTOCK"}
        events = {e["underlying_symbol"]: e for e in trade_history.read_all_jsonl(ste.SWING_EVENTS_LOG_NAME)
                  if e["event"] == "WATCHLIST_STALE_AGE_PRUNED" and e["underlying_symbol"] in relevant_symbols}
        assert set(events.keys()) == {"STALESTOCK"}, events
        assert events["STALESTOCK"]["max_age_days"] == 10

        print("3. A mixed watchlist is pruned correctly by AGE: a genuinely stale symbol (12 days "
              "since last confirmed) is removed; a fresh one (3 days) survives; and - the crux of "
              "'unless they are again fed in using chartink scan results' - a symbol originally "
              "added 15 days ago but RECONFIRMED by the Chartink scan today survives too, since its "
              "own clock is last_confirmed_at, not added_at. The removal is durably logged via a "
              "WATCHLIST_STALE_AGE_PRUNED event: PASSED")
    finally:
        ste._now_ist = real_now
        ste._last_stale_age_prune_date = real_last_prune
        ste.config.WATCHLIST_STALE_AGE_PRUNE_ENABLED = real_enabled


async def test_4_feature_flag_disables_pruning_entirely():
    store = swl.WatchlistStore()
    ste.watchlist_store = store
    real_enabled = ste.config.WATCHLIST_STALE_AGE_PRUNE_ENABLED
    ste.config.WATCHLIST_STALE_AGE_PRUNE_ENABLED = False
    real_now = ste._now_ist
    real_last_prune = ste._last_stale_age_prune_date
    ste._last_stale_age_prune_date = None
    ste._now_ist = lambda: datetime(2026, 9, 8, 10, 0, tzinfo=IST)

    await store.add_symbols(["ANCIENTSTOCK"])
    _backdate(store, "ANCIENTSTOCK", days_ago=100)
    try:
        await ste._stale_watchlist_age_prune_tick()
        assert "ANCIENTSTOCK" in await store.symbols(), \
            "even a symbol stale by 100 days must survive while the feature flag is off"
        assert ste._last_stale_age_prune_date is None, "must not even mark a run when the flag is off"
        print("4. config.WATCHLIST_STALE_AGE_PRUNE_ENABLED=False disables the feature entirely - "
              "even a symbol stale by 100 days survives untouched: PASSED")
    finally:
        ste._now_ist = real_now
        ste._last_stale_age_prune_date = real_last_prune
        ste.config.WATCHLIST_STALE_AGE_PRUNE_ENABLED = real_enabled


async def main():
    print("=== Swing stale-age watchlist prune test suite ===\n")
    await test_1_stale_symbols_exact_age_boundary()
    await test_2_daily_once_gating()
    await test_3_mixed_watchlist_pruned_correctly_including_chartink_reconfirm()
    await test_4_feature_flag_disables_pruning_entirely()
    print("\nALL SWING STALE-AGE WATCHLIST PRUNE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
