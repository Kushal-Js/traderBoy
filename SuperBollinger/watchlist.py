"""
Super Bollinger's OWN watchlist (30 Sep 2026): data/super_bollinger_watchlist,
chosen by the HYBRID selection (stock_selection.py - ATH top 40, ranked by
how these rules have been doing on each stock lately), refreshed by the
Friday 00:00 IST scheduler (weekly_watchlist_refresh.py). Bollinger and
Swing keep their ATH lists (data/bollinger_watchlist, data/watchlist).

The file is re-read in full on every read (a few ms, runs in an executor),
so a replaced list - including REMOVED stocks - takes effect within one
monitor tick, no restart. If the file is missing or empty, Super Bollinger
falls back to the Bollinger watchlist and says so loudly, instead of
silently trading nothing.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from Bollinger.watchlist import watchlist_store as bollinger_watchlist

logger = logging.getLogger("super_bollinger_watchlist")

WATCHLIST_FILE = Path("data/super_bollinger_watchlist")
_warned = {"fallback": False}


def _read_sync() -> list[str]:
    if not WATCHLIST_FILE.exists():
        return []
    out = []
    for line in WATCHLIST_FILE.read_text().splitlines():
        sym = line.strip().split(",", 1)[0].strip().upper()
        if sym and not sym.startswith("#") and sym not in out:
            out.append(sym)
    return out


async def symbols() -> tuple[list[str], str]:
    """(symbols, source) - source is "super_bollinger_watchlist" or
    "bollinger_watchlist (fallback)"."""
    try:
        syms = await asyncio.get_running_loop().run_in_executor(None, _read_sync)
    except Exception:  # noqa: BLE001
        logger.exception("Could not read %s", WATCHLIST_FILE)
        syms = []
    if syms:
        _warned["fallback"] = False
        return syms, "super_bollinger_watchlist"
    if not _warned["fallback"]:
        logger.warning("%s missing or empty - Super Bollinger is using the Bollinger (ATH) watchlist instead",
                       WATCHLIST_FILE)
        _warned["fallback"] = True
    await bollinger_watchlist.sync_from_file()
    return await bollinger_watchlist.symbols(), "bollinger_watchlist (fallback)"


def replace(symbols_: list[str]) -> list[str]:
    """Writes a new list (backup of the old one first). Blocking - call from
    an executor."""
    clean = []
    for s in symbols_:
        s = s.strip().upper()
        if s and s not in clean:
            clean.append(s)
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    if WATCHLIST_FILE.exists():
        WATCHLIST_FILE.with_name(f"{WATCHLIST_FILE.name}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}").write_text(
            WATCHLIST_FILE.read_text())
    tmp = WATCHLIST_FILE.with_suffix(".tmp")
    tmp.write_text("".join(f"{s}\n" for s in clean))
    tmp.replace(WATCHLIST_FILE)
    logger.info("Super Bollinger watchlist replaced (%d): %s", len(clean), clean)
    return clean
