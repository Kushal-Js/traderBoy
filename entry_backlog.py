"""
Generic per-strategy entry-signal backlog with freshness-priority
dispatch, shared by Swing and Bollinger (added 26 Sep 2026, user request):
"if more than five signals are generated at a single point of time, then
only five should be placed... after a slot gets free, whoever was the
signal which couldn't get placed, including any new signals generated at
that point, should be placed - but the freshness of the signal should
always take precedence... a signal which is more recently/freshly
generated would be placed first as compared to a signal which got
generated some time prior."

Before this module existed, both Swing/trading_engine.py and
Bollinger/trading_engine.py's own _monitor_tick built a `candidates` list
purely from THIS tick's signal scan and stopped entering the moment
capacity ran out - any candidate past that point was silently dropped
forever (never retried), since most entry signals here are edge-triggered
(fire only on the tick a crossover/confirmation actually happens) rather
than a persistent level check. This module makes an unplaced signal
survive across ticks instead of being lost, while still respecting
capacity every tick.

Design: NOT a FIFO queue - a freshness-priority pool, by explicit user
request. A fresher signal always jumps ahead of an older one still
waiting for a slot, even if that means an old signal never gets placed
while fresh ones keep arriving (an accepted tradeoff, not a bug - the
user was explicit that freshness always wins).

Keyed by symbol (dict, not a list) - at most one pending entry per symbol
at a time. A fresh signal for a symbol that already has a pending entry
REPLACES it and resets its freshness timestamp, the same "replaces,
doesn't duplicate" semantic used everywhere else in this codebase for a
given symbol's trading state.

Not persisted across a restart or carried across a trading-day boundary
by design - a pending signal is inherently a "the moment right now" read
of a setup; carrying it across a restart (stale process state) or
overnight (stale market state - unlike a real open position, which
correctly does carry) would mean acting on a setup no longer necessarily
true. Each strategy's own trading_engine.py clears its own backlog
instance at its own day-boundary reset.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class _Entry:
    generated_at: float  # time.monotonic() - elapsed-time comparisons only, immune to wall-clock jumps
    payload: object


class EntryBacklog:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pending: dict[str, _Entry] = {}

    async def upsert_fresh(self, symbol: str, payload: object) -> None:
        """Add or replace this symbol's pending entry with a brand-new
        freshness timestamp. Call this only for a signal your own per-tick
        scan just generated THIS tick - never to put back a signal that
        simply lost out to fresher ones this tick (that must keep its
        original timestamp, see reinsert_unplaced)."""
        async with self._lock:
            self._pending[symbol] = _Entry(generated_at=time.monotonic(), payload=payload)

    async def pop_all_freshest_first(self) -> list[tuple[str, float, object]]:
        """Removes and returns EVERY pending entry as (symbol,
        generated_at, payload), most recently generated first. The caller
        owns re-inserting (via reinsert_unplaced) whichever ones it
        couldn't actually place this tick."""
        async with self._lock:
            ordered = sorted(self._pending.items(), key=lambda kv: kv[1].generated_at, reverse=True)
            self._pending.clear()
            return [(symbol, entry.generated_at, entry.payload) for symbol, entry in ordered]

    async def reinsert_unplaced(self, symbol: str, generated_at: float, payload: object) -> None:
        """Puts back a signal that lost out to fresher ones this tick -
        preserves its ORIGINAL generated_at so it doesn't unfairly jump
        the freshness ranking next tick just for having been considered
        once already. If a genuinely newer signal for this same symbol
        has already been upserted in the meantime, that newer one wins
        and this reinsert is a no-op."""
        async with self._lock:
            existing = self._pending.get(symbol)
            if existing is not None and existing.generated_at >= generated_at:
                return
            self._pending[symbol] = _Entry(generated_at=generated_at, payload=payload)

    async def clear(self) -> None:
        async with self._lock:
            self._pending.clear()

    async def snapshot(self) -> list[dict]:
        """Read-only view for monitoring (GET /swing/entry-backlog etc.) -
        does not consume anything. Freshest first, same order dispatch
        would use."""
        async with self._lock:
            now = time.monotonic()
            ordered = sorted(self._pending.items(), key=lambda kv: kv[1].generated_at, reverse=True)
            return [{"symbol": symbol, "age_seconds": round(now - entry.generated_at, 1)} for symbol, entry in ordered]


async def dispatch(
    backlog: EntryBacklog,
    fresh_candidates: list[tuple[str, object]],
    *,
    is_paper_trade: Callable[[str], bool],
    remaining_capacity: Callable[[], Awaitable[int]],
    place_paper: Callable[[str, object], Awaitable[None]],
    place_real: Callable[[str, object], Awaitable[None]],
) -> None:
    """The one dispatch algorithm both Swing and Bollinger's own
    _monitor_tick call from their respective entry-scan tail, so the
    freshness-priority/capacity-gating logic has a single implementation
    to get right for both strategies (see this module's own docstring for
    the full design/rationale).

    Every tick:
      1. This tick's genuinely NEW signals (fresh_candidates) are merged
         into the backlog with a fresh timestamp - a new signal for a
         symbol already pending replaces/refreshes it.
      2. EVERY pending entry (this tick's new ones + anything still
         waiting from before) is popped out, freshest first.
      3. Walk that freshest-first list: a paper-mode symbol is dispatched
         immediately and never competes for real capacity (matching the
         pre-existing "paper-mode replaces real trading" semantic - it
         does not consume a real-capacity slot at all). A real-mode
         symbol is placed only while capacity remains; the instant
         capacity hits zero, every remaining symbol in the list
         (freshest-first, so exactly the least-fresh ones still waiting)
         is put back in the backlog UNCHANGED (original timestamp
         preserved) to compete again next tick - still ranked behind
         whatever even-fresher signal shows up by then, by design.
    """
    for symbol, payload in fresh_candidates:
        await backlog.upsert_fresh(symbol, payload)

    pending = await backlog.pop_all_freshest_first()
    capacity_exhausted = False
    for symbol, generated_at, payload in pending:
        if is_paper_trade(symbol):
            await place_paper(symbol, payload)
            continue
        if not capacity_exhausted and await remaining_capacity() > 0:
            await place_real(symbol, payload)
            continue
        capacity_exhausted = True
        await backlog.reinsert_unplaced(symbol, generated_at, payload)
