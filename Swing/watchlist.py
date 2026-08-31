"""
Simple in-memory watchlist for the Swing strategy - stocks added via
POST /chartink/webhook-swing-watchlist, continuously polled by
trading_engine.monitor_loop() once entry-condition logic is defined (see
config.py's own module docstring for what's deferred). Deliberately just
a set of symbols with an added_at timestamp - no ranking, no scoring, no
scan metadata; those are exactly the kind of thing the user's own future
"business logic" would want to define, not something to guess at now.

Pure in-memory, same tradeoff as every store in this codebase - resets on
restart. A watchlist entry is disposable, cheap-to-rebuild bookkeeping
(unlike a live basket, which reconciles from the broker on restart) - not
persisting it isn't a real gap the way losing track of a real position
would be.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Dict, List


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


watchlist_store = WatchlistStore()
