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

Deliberately just a set of symbols with an added_at timestamp - no
ranking, no scoring, no scan metadata; those are exactly the kind of
thing the user's own future "business logic" would want to define, not
something to guess at now.

The in-memory store itself is still pure in-memory (resets on restart,
same tradeoff as every store in this codebase) - what's persistent is
the FILE, which gets re-synced into a fresh in-memory store on the very
next startup/tick, so nothing is actually lost across a restart the way
it would be for the webhook-only symbols before this file existed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("swing_watchlist")

WATCHLIST_FILE = Path("data/watchlist")


class WatchlistStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._symbols: Dict[str, datetime] = {}  # symbol -> added_at

    async def add_symbols(self, symbols: List[str]) -> List[str]:
        """Adds any symbols not already present. Returns only the ones
        actually newly added, for the webhook's own response - lets the
        caller see at a glance which of their requested symbols were
        already on the list."""
        async with self._lock:
            added = []
            for sym in symbols:
                if sym not in self._symbols:
                    self._symbols[sym] = datetime.now()
                    added.append(sym)
            return added

    async def remove_symbol(self, symbol: str) -> bool:
        async with self._lock:
            return self._symbols.pop(symbol, None) is not None

    async def symbols(self) -> List[str]:
        async with self._lock:
            return list(self._symbols.keys())

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "count": len(self._symbols),
                "watchlist": [
                    {"symbol": sym, "added_at": ts.isoformat()}
                    for sym, ts in self._symbols.items()
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
        own docstring for the identical negligible-cost reasoning)."""
        try:
            with open(WATCHLIST_FILE) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001
            logger.exception("Could not read %s - skipping this sync", WATCHLIST_FILE)
            return []

        symbols = [
            line.strip().upper() for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
        if not symbols:
            return []
        added = await self.add_symbols(symbols)
        if added:
            logger.info("Synced %d new symbol(s) from %s: %s", len(added), WATCHLIST_FILE, added)
        return added


watchlist_store = WatchlistStore()
