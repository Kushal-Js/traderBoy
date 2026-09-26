"""
Plain, manually-curated watchlist for the Bollinger strategy - direct
clone of Swing/watchlist.py's design (see that module's own docstring for
the full rationale). The user edits `data/bollinger_watchlist` directly on
the server (one symbol per line, blank lines and `#`-prefixed comments
ignored) - a gitignored, server-only runtime data file. Re-read every
monitor_loop tick, so an edit takes effect within one tick, no restart
needed.

Starts EMPTY on a fresh deploy (see Bollinger/config.py's own module
docstring - paper mode is off from day one, so the user must explicitly
opt symbols in before any real order can fire).

The in-memory store is pure in-memory (resets on restart) - what's
persistent is the FILE, which re-syncs into a fresh in-memory store on
the very next startup/tick.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("bollinger_watchlist")

WATCHLIST_FILE = Path("data/bollinger_watchlist")


def _read_lines_sync() -> list[str]:
    with open(WATCHLIST_FILE) as f:
        return f.readlines()


class WatchlistStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._symbols: Dict[str, None] = {}  # insertion-ordered set (dict preserves order; value unused)

    async def add_symbols(self, symbols: List[str]) -> List[str]:
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
        `symbols`. Does NOT touch any already-open position for a removed
        symbol - only stops new entries on it. Caller is responsible for
        also calling persist_to_file() if this replacement should survive
        a restart."""
        async with self._lock:
            self._symbols = {}
            for sym in symbols:
                sym = sym.strip().upper()
                if sym:
                    self._symbols[sym] = None
            return list(self._symbols.keys())

    async def persist_to_file(self) -> None:
        """Overwrites WATCHLIST_FILE with the CURRENT in-memory watchlist,
        one symbol per line. Backs up the existing file first."""
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
        file (the default, fresh-deploy state), an unreadable file, or a
        blank file all just mean "nothing to add this time," never an
        error that could interrupt the monitor loop this is called from
        every tick. Only the part before the first comma on a line is
        treated as the symbol (same convention as Swing/watchlist.py, in
        case a line ever carries a trailing annotation)."""
        loop = asyncio.get_running_loop()
        try:
            lines = await loop.run_in_executor(None, _read_lines_sync)
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
