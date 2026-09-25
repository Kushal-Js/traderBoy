"""
Live-reloadable per-symbol MCX config for Swing v2 (added 25 Sep 2026 -
fixes a real restart-required gap). Before this, Swing/config.py's
MCX_OPTIONS_ONLY_SYMBOLS and MCX_PNL_MULTIPLIERS were plain env-var-driven
constants, computed once at process import time from SWING_MCX_OPTIONS_
ONLY_SYMBOLS/SWING_MCX_PNL_MULTIPLIER_<SYMBOL> - adding a new MCX symbol
needed an .env edit AND a full bot restart before it could ever place a
real order. Same file-backed, additive, fails-open design as
Swing/watchlist.py (already fixed the same way, 25 Sep 2026, for the plain
watchlist itself) - `data/mcx_config`, re-read every monitor_loop tick, an
edit takes effect within one tick, no restart needed.

MCX MEMBERSHIP ITSELF (is this symbol even an MCX commodity at all) is
deliberately NOT part of this file - that question is answered live,
config-free, via Options.dhan_client.dhan_wrapper.is_mcx_commodity, which
checks Dhan's own real instrument master (cached per underlying) and can
never go stale or need an entry added here just to be recognized. This
registry only carries the two things that genuinely can't be safely
auto-derived:

- options_only: whether this MCX symbol should ALWAYS trade options,
  completely independent of the global BASKET_TYPE (user request 12 Sep
  2026, re: COPPER - "whatever is the BASKET_TYPE, it should not impact
  COPPER as it only has to trade in options" - explicitly corrected the
  same day to NOT be a blanket rule for every MCX symbol: "this doesn't
  apply to all instruments under MCX but only for COPPER"). A symbol with
  no entry (or options_only=false) just follows the global BASKET_TYPE,
  same as any NSE symbol.

- pnl_multiplier: the REAL per-lot economic quantity (e.g. 2500 for
  Copper's 2500kg lot), used ONLY for P&L/rupee-threshold math, NEVER for
  the real order's own `quantity` parameter. Dhan's own instrument master
  reports SEM_LOT_UNITS=1 for every single MCX row regardless of the
  commodity - this is genuinely not derivable from the instrument master
  and must be verified per-symbol (margin_calculator at quantity=1 should
  price out to that commodity's real-world one-lot margin - see the old
  MCX_PNL_MULTIPLIERS docstring, preserved in git history, for the full
  reasoning). A symbol with NO configured multiplier returns None here -
  entry code must treat that as "skip, don't guess" (see
  Swing/trading_engine.py's enter_position_for_stock), never silently fall
  back to another commodity's real-world quantity the way the old env-var
  dict's `"2500"` default used to for any unconfigured MCX symbol.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("swing_mcx_registry")

MCX_CONFIG_FILE = Path("data/mcx_config")


@dataclass
class MCXSymbolConfig:
    options_only: bool
    pnl_multiplier: Optional[int]


def _read_lines_sync() -> list[str]:
    """Mirrors Swing/watchlist.py's own _read_lines_sync - see that
    function's docstring for why this stays a plain open().readlines()
    dispatched through run_in_executor rather than read_text().splitlines()."""
    with open(MCX_CONFIG_FILE) as f:
        return f.readlines()


def _parse_line(line: str) -> Optional[tuple[str, MCXSymbolConfig]]:
    """One line: "SYMBOL,options_only(true/false),pnl_multiplier(int or
    blank)". Blank lines and #-comments ignored, same convention as
    watchlist.py. A malformed line is logged and skipped rather than
    raising - this is called from sync_from_file every 5s tick, one bad
    line must never break the whole sync."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = [p.strip() for p in stripped.split(",")]
    if len(parts) < 2:
        logger.warning("Malformed line in %s (need at least SYMBOL,options_only): %r - skipping", MCX_CONFIG_FILE, line)
        return None
    symbol = parts[0].upper()
    if not symbol:
        return None
    options_only = parts[1].strip().lower() == "true"
    pnl_multiplier: Optional[int] = None
    if len(parts) >= 3 and parts[2].strip():
        try:
            pnl_multiplier = int(parts[2].strip())
        except ValueError:
            logger.warning("%s: non-integer pnl_multiplier %r in %s - leaving unconfigured (None)",
                            symbol, parts[2], MCX_CONFIG_FILE)
    return symbol, MCXSymbolConfig(options_only=options_only, pnl_multiplier=pnl_multiplier)


class MCXRegistryStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: Dict[str, MCXSymbolConfig] = {}

    async def set_symbol(self, symbol: str, options_only: bool, pnl_multiplier: Optional[int]) -> None:
        async with self._lock:
            self._entries[symbol.strip().upper()] = MCXSymbolConfig(options_only=options_only, pnl_multiplier=pnl_multiplier)

    async def options_only(self, symbol: str) -> bool:
        async with self._lock:
            entry = self._entries.get(symbol.strip().upper())
            return entry.options_only if entry else False

    async def pnl_multiplier(self, symbol: str) -> Optional[int]:
        async with self._lock:
            entry = self._entries.get(symbol.strip().upper())
            return entry.pnl_multiplier if entry else None

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                sym: {"options_only": e.options_only, "pnl_multiplier": e.pnl_multiplier}
                for sym, e in self._entries.items()
            }

    async def persist_to_file(self) -> None:
        """Overwrites MCX_CONFIG_FILE with the CURRENT in-memory registry -
        same backup-first discipline as WatchlistStore.persist_to_file."""
        MCX_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if MCX_CONFIG_FILE.exists():
            backup_path = MCX_CONFIG_FILE.with_name(
                f"{MCX_CONFIG_FILE.name}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            try:
                backup_path.write_text(MCX_CONFIG_FILE.read_text())
            except Exception:  # noqa: BLE001
                logger.exception("Could not back up %s before overwriting it - proceeding anyway", MCX_CONFIG_FILE)
        async with self._lock:
            entries = dict(self._entries)
        lines = [
            f"{sym},{str(e.options_only).lower()},{e.pnl_multiplier if e.pnl_multiplier is not None else ''}\n"
            for sym, e in entries.items()
        ]
        MCX_CONFIG_FILE.write_text("".join(lines))
        logger.info("Persisted %d MCX symbol config(s) to %s: %s", len(entries), MCX_CONFIG_FILE, list(entries.keys()))

    async def sync_from_file(self) -> list[str]:
        """Reads MCX_CONFIG_FILE (if it exists) and upserts every entry
        found there. Fails open - a missing/unreadable/blank file just
        means "nothing to sync this time," never an error that could
        interrupt the monitor loop this is called from every tick. Unlike
        WatchlistStore.sync_from_file (additive-only, never overwrites an
        existing in-memory symbol), this UPSERTS - the file is the single
        source of truth for a given symbol's config, so editing an
        existing line (e.g. correcting a multiplier) takes effect on the
        next tick too, not just a brand new symbol."""
        loop = asyncio.get_running_loop()
        try:
            lines = await loop.run_in_executor(None, _read_lines_sync)
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001
            logger.exception("Could not read %s - skipping this sync", MCX_CONFIG_FILE)
            return []

        parsed = [_parse_line(line) for line in lines]
        updates = {sym: cfg for sym, cfg in (p for p in parsed if p is not None)}
        if not updates:
            return []
        async with self._lock:
            changed = [sym for sym, cfg in updates.items() if self._entries.get(sym) != cfg]
            self._entries.update(updates)
        if changed:
            logger.info("Synced %d MCX symbol config(s) from %s: %s", len(changed), MCX_CONFIG_FILE, changed)
        return changed


mcx_registry = MCXRegistryStore()
