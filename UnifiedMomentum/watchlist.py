"""
Unified Momentum's OWN watchlist (1 Oct 2026): data/unified_momentum_watchlist,
chosen by the HYBRID selection (stock_selection.py - ATH top 40 among liquid
stocks, ranked by how the pullback-call rules have done on each over the last
20 sessions; the backtest of both engines used this list), refreshed by the
Friday 00:00 IST scheduler (weekly_watchlist_refresh.py). Bollinger and Swing
keep their ATH lists (data/bollinger_watchlist, data/watchlist).

The file is re-read in full on every read (a few ms, runs in an executor),
so a replaced list - including REMOVED stocks - takes effect within one
monitor tick, no restart. If the file is missing or empty, Unified Momentum
falls back to its own newest backup (data/unified_momentum_watchlist.bak.*
- every rewrite keeps one, so that is last week's list), then to Super
Bollinger's list (the same HYBRID selection, while that strategy exists),
then to the Bollinger watchlist, and says so loudly, instead of silently
trading nothing. Nothing here imports Super Bollinger.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from Bollinger.watchlist import watchlist_store as bollinger_watchlist

logger = logging.getLogger("unified_momentum_watchlist")

WATCHLIST_FILE = Path("data/unified_momentum_watchlist")
HYBRID_FALLBACK_FILE = Path("data/super_bollinger_watchlist")
_warned = {"fallback": False}


def _latest_backup() -> Optional[Path]:
    """Newest data/unified_momentum_watchlist.bak.<YYYYmmdd_HHMMSS> (the names sort in time order)."""
    backups = sorted(WATCHLIST_FILE.parent.glob(f"{WATCHLIST_FILE.name}.bak.*"))
    return backups[-1] if backups else None


def _read_sync(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        sym = line.strip().split(",", 1)[0].strip().upper()
        if sym and not sym.startswith("#") and sym not in out:
            out.append(sym)
    return out


async def symbols() -> tuple[list[str], str]:
    """(symbols, source) - source is "unified_momentum_watchlist", "<its newest backup> (fallback)",
    "super_bollinger_watchlist (fallback)" or "bollinger_watchlist (fallback)"."""
    loop = asyncio.get_running_loop()
    try:
        syms = await loop.run_in_executor(None, _read_sync, WATCHLIST_FILE)
    except Exception:  # noqa: BLE001
        logger.exception("Could not read %s", WATCHLIST_FILE)
        syms = []
    if syms:
        _warned["fallback"] = False
        return syms, "unified_momentum_watchlist"
    for fallback in (_latest_backup(), HYBRID_FALLBACK_FILE):
        if fallback is None:
            continue
        try:
            syms = await loop.run_in_executor(None, _read_sync, fallback)
        except Exception:  # noqa: BLE001
            logger.exception("Could not read %s", fallback)
            syms = []
        if syms:
            if not _warned["fallback"]:
                logger.warning("%s missing or empty - Unified Momentum is using %s instead", WATCHLIST_FILE, fallback)
                _warned["fallback"] = True
            return syms, f"{fallback.name} (fallback)"
    if not _warned["fallback"]:
        logger.warning("%s missing or empty and no backup - Unified Momentum is using the Bollinger (ATH) watchlist",
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
    logger.info("Unified Momentum watchlist replaced (%d): %s", len(clean), clean)
    return clean
