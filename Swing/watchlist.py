"""
Plain, manually-curated watchlist for Swing v2 (simplified 12 Sep 2026 -
user request: "No watchlist pruning logic... required as of now"). The
user edits `data/watchlist` directly on the server (one symbol per line,
blank lines and `#`-prefixed comments ignored) - a gitignored, server-only
runtime data file, same convention as `choppy/choppy_stocks.json`/
`history/` elsewhere in this codebase. Re-read every monitor_loop tick,
so an edit takes effect within one tick, no restart needed.

This REPLACES the old design's Chartink-scan integration and its two
daily prunes (trend-break and stale-age), and the `suppress_resync_today`
machinery that existed ONLY to stop those prunes and this per-tick file
resync from fighting each other. With no pruning at all, there's nothing
left to fight - the file is simply the source of truth, re-read every
tick, no special-casing needed. The old `,YYYY-MM-DD` backdate suffix
support is also dropped along with it (it only ever mattered for the
stale-age prune's own clock).

The in-memory store is pure in-memory (resets on restart, same tradeoff
as every store in this codebase) - what's persistent is the FILE, which
re-syncs into a fresh in-memory store on the very next startup/tick.
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
        self._symbols: Dict[str, None] = {}  # insertion-ordered set (dict preserves order; value unused)

    async def add_symbols(self, symbols: List[str]) -> List[str]:
        """Adds any symbols not already present. Returns only the ones
        actually newly added."""
        async with self._lock:
            added = []
            for sym in symbols:
                sym = sym.strip().upper()
                if sym and sym not in self._symbols:
                    self._symbols[sym] = None
                    added.append(sym)
            return added

    async def remove_symbol(self, symbol: str) -> bool:
        async with self._lock:
            existed = symbol.strip().upper() in self._symbols
            self._symbols.pop(symbol.strip().upper(), None)
            return existed

    async def replace_symbols(self, symbols: List[str]) -> List[str]:
        """Wipes the ENTIRE current watchlist and replaces it with exactly
        `symbols` (added 12 Sep 2026, user request: "replace existing
        watchlist contents... replace entire content of watchlist file any
        time"). Unlike add_symbols/remove_symbol, this is NOT additive -
        any symbol not in the new list stops being watched immediately
        (any of ITS live positions are unaffected - see monitor_loop's own
        handling of a position whose symbol has left the watchlist; this
        only stops NEW entries on the removed symbol, it never touches an
        already-open position). Deduplicates and uppercases, preserving
        the order given. Caller is responsible for also calling
        persist_to_file() if this replacement should survive a restart -
        kept as two steps rather than one so an in-memory-only preview is
        possible, though every real caller today does both together."""
        async with self._lock:
            self._symbols = {}
            for sym in symbols:
                sym = sym.strip().upper()
                if sym:
                    self._symbols[sym] = None
            return list(self._symbols.keys())

    async def persist_to_file(self) -> None:
        """Overwrites WATCHLIST_FILE with the CURRENT in-memory watchlist,
        one symbol per line - the write-back counterpart to sync_from_file
        (added 12 Sep 2026 for the new /swing/watchlist/replace endpoint).
        Without this, a replace only lives in memory: sync_from_file only
        ever ADDS, so any symbol still sitting in the old on-disk file
        would silently reappear on the next restart, quietly undoing the
        replace. Backs up the existing file first (same discipline as
        every other watchlist-file edit this session) - never a hard
        overwrite with no way back."""
        WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        if WATCHLIST_FILE.exists():
            backup_path = WATCHLIST_FILE.with_name(
                f"{WATCHLIST_FILE.name}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            try:
                backup_path.write_text(WATCHLIST_FILE.read_text())
            except Exception:  # noqa: BLE001
                logger.exception("Could not back up %s before overwriting it - proceeding anyway", WATCHLIST_FILE)
        symbols = await self.symbols()
        WATCHLIST_FILE.write_text("".join(f"{sym}\n" for sym in symbols))
        logger.info("Persisted %d symbol(s) to %s: %s", len(symbols), WATCHLIST_FILE, symbols)

    async def symbols(self) -> List[str]:
        async with self._lock:
            return list(self._symbols.keys())

    async def snapshot(self) -> dict:
        async with self._lock:
            return {"count": len(self._symbols), "watchlist": list(self._symbols.keys())}

    async def sync_from_file(self) -> List[str]:
        """Reads WATCHLIST_FILE (if it exists) and adds any symbols found
        there that aren't already being watched. Fails open - a missing
        file, an unreadable file, or a blank file all just mean "nothing
        to add this time," never an error that could interrupt the
        monitor loop this is called from every tick.

        A line may still carry the old `,YYYY-MM-DD` backdate suffix from
        the pre-rewrite design (confirmed live 12 Sep 2026 - the real
        data/watchlist file on the droplet still has it on every line,
        e.g. "ASHOKLEY,2026-09-01") - that date only ever mattered for the
        old stale-age prune, which this rewrite removed, but the FILE
        itself wasn't rewritten. Only the part before the first comma is
        ever treated as the symbol; anything after it (the old date, or
        nothing) is silently ignored rather than corrupting the symbol
        with a literal ",2026-09-01" tail."""
        try:
            with open(WATCHLIST_FILE) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001
            logger.exception("Could not read %s - skipping this sync", WATCHLIST_FILE)
            return []

        symbols = [
            line.strip().split(",", 1)[0].strip().upper() for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
        symbols = [s for s in symbols if s]
        if not symbols:
            return []
        added = await self.add_symbols(symbols)
        if added:
            logger.info("Synced %d new symbol(s) from %s: %s", len(added), WATCHLIST_FILE, added)
        return added


watchlist_store = WatchlistStore()
