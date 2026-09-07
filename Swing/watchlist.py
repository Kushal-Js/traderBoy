"""
In-memory watchlist for the Swing strategy - stocks added either via
POST /chartink/webhook-swing-watchlist, or (added 31 Aug 2026, user
request) by hand-editing a plain-text file on the server,
`data/watchlist` (one stock symbol per line, blank lines and `#`-prefixed
comments ignored) - a gitignored, server-only runtime data file, same
convention as `choppy/choppy_stocks.json`/`history/` elsewhere in this
codebase. Edit the file directly on the server to add stocks; it's
re-read every monitor_loop tick (see trading_engine.py), so a change
takes effect within one tick, no restart needed - the same hot-reload UX
choppy_stocks.py already established. It only ever ADDS symbols found in
the file that aren't already being watched - removing a line from the
file does NOT remove that symbol from the live watchlist (use
remove_symbol()/a future removal webhook for that) - this keeps the
file's semantics simple and matches the webhook's own add-only behavior;
nothing here silently drops something that might still matter.

Fixed 7 Sep 2026 (real bug found live): since this per-tick re-add and
the trend/stale-age prunes' own removals were fighting each other for
any symbol still listed in the file, a prune's own removal used to get
silently undone by the very next tick - see remove_symbol()'s own
suppress_resync_today docstring for the full mechanism. sync_from_file()
now skips re-adding anything pruned earlier the same day.

A line may optionally carry `,YYYY-MM-DD` after the symbol (e.g.
`AUROPHARMA,2026-09-01`) - the date this symbol was ACTUALLY curated,
used as its `added_at`/`last_confirmed_at` instead of "now" (added 2 Sep
2026, user request: backdate the then-current watchlist to 1 Sep 2026
"so they can later be pruned if required"). This matters because the
in-memory store itself resets on every restart (see below) - without a
persisted date, every restart would silently re-stamp the file's
symbols with THAT restart's own timestamp, perpetually postponing the
stale-age prune's own 10-day clock and defeating the whole point of the
feature. A line with no date (plain `SYMBOL`) still works exactly as
before, stamped with "now" - this is a purely additive, backward-
compatible format change.

Each symbol carries two timestamps (added 1 Sep 2026, for the new
stale-age prune - see trading_engine.py's own
_stale_watchlist_age_prune_tick): `added_at` (when it FIRST joined the
watchlist, permanent/historical) and `last_confirmed_at` (defaults to
added_at; reset to "now" ONLY by the daily Chartink scan pull
re-returning this exact symbol - see confirm_chartink_symbols() below).
Beyond that, deliberately just a set of symbols - no ranking, no
scoring; those are exactly the kind of thing the user's own future
"business logic" would want to define, not something to guess at now.

The in-memory store itself is still pure in-memory (resets on restart,
same tradeoff as every store in this codebase) - what's persistent is
the FILE, which gets re-synced into a fresh in-memory store on the very
next startup/tick, so nothing is actually lost across a restart the way
it would be for the webhook-only symbols before this file existed.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("swing_watchlist")

WATCHLIST_FILE = Path("data/watchlist")


@dataclass
class WatchlistEntry:
    added_at: datetime
    last_confirmed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.last_confirmed_at is None:
            self.last_confirmed_at = self.added_at


class WatchlistStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._symbols: Dict[str, WatchlistEntry] = {}
        # symbol -> pruned TODAY for an editorial reason (trend break /
        # stale age) - see remove_symbol()'s own suppress_resync_today
        # docstring and sync_from_file()'s own use of this set for the
        # bug this fixes (added 7 Sep 2026). Resets to empty the moment
        # the calendar date rolls over past _pruned_today_date.
        self._pruned_today: Set[str] = set()
        self._pruned_today_date: Optional[date] = None

    async def add_symbols(
        self, symbols: List[str], added_at_override: Optional[Dict[str, datetime]] = None
    ) -> List[str]:
        """Adds any symbols not already present. Returns only the ones
        actually newly added, for the webhook's own response - lets the
        caller see at a glance which of their requested symbols were
        already on the list. An already-present symbol is left
        completely untouched here, including its own last_confirmed_at -
        this does NOT reset the stale-age prune's own clock (see
        confirm_chartink_symbols() below for the one thing that does);
        every existing caller (the webhook, the file sync) keeps this
        exact same behavior unchanged.

        `added_at_override` (added 2 Sep 2026, used only by
        sync_from_file() below) lets a caller stamp specific NEWLY-added
        symbols with a real historical date instead of "now" - every
        other/existing caller omits it and gets the original behavior."""
        async with self._lock:
            added = []
            now = datetime.now()
            for sym in symbols:
                if sym not in self._symbols:
                    ts = (added_at_override or {}).get(sym, now)
                    self._symbols[sym] = WatchlistEntry(added_at=ts)
                    added.append(sym)
            return added

    async def confirm_chartink_symbols(self, symbols: List[str]) -> Tuple[List[str], List[str]]:
        """Called ONLY by the daily Chartink scan pull (trading_engine.
        _run_chartink_watchlist_scan) - user request 1 Sep 2026: "those
        stocks that were added 10 days earlier to be removed unless they
        are again fed in using chartink scan results." For each symbol
        the scan just returned: a genuinely NEW one is added (same as
        add_symbols); an ALREADY-present one has its own
        last_confirmed_at reset to now instead - the ONE thing in this
        codebase that resets the stale-age prune's clock for a symbol,
        regardless of which mechanism originally added it. Returns
        (newly_added, reconfirmed) for the caller's own logging."""
        async with self._lock:
            now = datetime.now()
            newly_added, reconfirmed = [], []
            for sym in symbols:
                existing = self._symbols.get(sym)
                if existing is None:
                    self._symbols[sym] = WatchlistEntry(added_at=now)
                    newly_added.append(sym)
                else:
                    existing.last_confirmed_at = now
                    reconfirmed.append(sym)
            return newly_added, reconfirmed

    async def remove_symbol(self, symbol: str, suppress_resync_today: bool = False) -> bool:
        """suppress_resync_today (added 7 Sep 2026, fixing a real bug
        found live) - when True, ALSO marks `symbol` as pruned for the
        rest of today so sync_from_file() won't silently re-add it from
        data/watchlist on the very next monitor tick, undoing this
        removal within seconds. Before this fix, the trend-based and
        stale-age prunes' own removals were being immediately
        overwritten this way for any symbol still listed in the file -
        confirmed live 7 Sep 2026: UNOMINDA/ATHERENERG/GAIL were pruned
        for a genuine daily-EMA12 trend break at 09:15 IST, but `GET
        /swing/watchlist` still showed all three moments later, and the
        prune itself only evaluates once per calendar day (date-gated),
        so once silently undone this way a symbol survived the ENTIRE
        rest of the day, every day, for as long as it stayed in the
        file - since 1 Sep 2026 when both prunes were first built.

        Deliberately left False (the default) for the OTHER kind of
        removal in this codebase - a symbol taken off the watchlist
        because it just entered a real position
        (trading_engine.py's own basket/basket_hedge entry paths). That
        removal SHOULD become re-watchable again as soon as it's
        re-synced (once its position eventually exits) - a genuine
        duplicate entry attempt is already independently prevented by
        basket_store's/position_store's own reserve_symbol dedup, so
        there's no bug to fix on that path and no reason to suppress it
        there."""
        async with self._lock:
            removed = self._symbols.pop(symbol, None) is not None
            if removed and suppress_resync_today:
                today = datetime.now().date()
                if self._pruned_today_date != today:
                    self._pruned_today = set()
                    self._pruned_today_date = today
                self._pruned_today.add(symbol)
            return removed

    async def symbols(self) -> List[str]:
        async with self._lock:
            return list(self._symbols.keys())

    async def stale_symbols(self, max_age_days: int) -> List[Tuple[str, datetime]]:
        """Read-only: returns (symbol, last_confirmed_at) for every
        symbol whose own last_confirmed_at is max_age_days or more
        CALENDAR days old (a plain date-to-date difference, matching the
        user's own everyday phrasing "10 days earlier" - not a strict
        24h-multiple timedelta, so a symbol confirmed at 23:59 and
        checked at 00:01 the next calendar day already counts as "1 day
        old"). The actual removal decision/action lives in
        trading_engine's own prune tick, not here - keeps this store a
        pure data structure, same convention as every other store in
        this codebase."""
        async with self._lock:
            today = datetime.now().date()
            return [
                (sym, entry.last_confirmed_at) for sym, entry in self._symbols.items()
                if (today - entry.last_confirmed_at.date()).days >= max_age_days
            ]

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "count": len(self._symbols),
                "watchlist": [
                    {
                        "symbol": sym, "added_at": entry.added_at.isoformat(),
                        "last_confirmed_at": entry.last_confirmed_at.isoformat(),
                    }
                    for sym, entry in self._symbols.items()
                ],
            }

    async def sync_from_file(self) -> List[str]:
        """Reads WATCHLIST_FILE (if it exists) and adds any symbols found
        there that aren't already being watched. Fails open, same
        philosophy as choppy_stocks.py's is_choppy() - a missing file, an
        unreadable file, or a blank file all just mean "nothing to add
        this time", never an error that could interrupt the monitor loop
        the caller runs this from. Cheap enough to call every tick (a
        small, OS-page-cached local file read - see choppy_stocks.py's
        own docstring for the identical negligible-cost reasoning).

        Each line may optionally carry `,YYYY-MM-DD` after the symbol -
        see the module docstring. An unparseable date is logged and
        ignored (falls back to "now" for that one symbol, rather than
        aborting the whole sync).

        Skips re-adding any symbol pruned earlier TODAY via remove_symbol
        (..., suppress_resync_today=True) - see that method's own
        docstring for the real live bug this fixes (added 7 Sep 2026):
        without this, a symbol still listed in the file would get
        silently re-added on the very next tick after an editorial
        prune, undoing it within seconds. It becomes eligible for
        re-sync again the moment the calendar date rolls over (or
        sooner, via the Chartink scan's own confirm_chartink_symbols -
        a genuinely different, deliberate re-confirmation signal, not
        this passive per-tick resync, so that path is untouched)."""
        try:
            with open(WATCHLIST_FILE) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001
            logger.exception("Could not read %s - skipping this sync", WATCHLIST_FILE)
            return []

        async with self._lock:
            today = datetime.now().date()
            pruned_today = set(self._pruned_today) if self._pruned_today_date == today else set()

        symbols: List[str] = []
        added_at_override: Dict[str, datetime] = {}
        skipped_pruned: List[str] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            sym_part, _, date_part = line.partition(",")
            sym = sym_part.strip().upper()
            if sym in pruned_today:
                skipped_pruned.append(sym)
                continue
            date_part = date_part.strip()
            if date_part:
                try:
                    added_at_override[sym] = datetime.strptime(date_part, "%Y-%m-%d")
                except ValueError:
                    logger.warning(
                        "Could not parse date %r for %s in %s - using today's date instead",
                        date_part, sym, WATCHLIST_FILE,
                    )
            symbols.append(sym)
        if skipped_pruned:
            logger.info(
                "Not re-adding %s from %s - pruned earlier today, eligible again tomorrow "
                "(or sooner if the Chartink scan re-confirms it)", skipped_pruned, WATCHLIST_FILE,
            )
        if not symbols:
            return []
        added = await self.add_symbols(symbols, added_at_override=added_at_override)
        if added:
            logger.info("Synced %d new symbol(s) from %s: %s", len(added), WATCHLIST_FILE, added)
        return added


watchlist_store = WatchlistStore()
