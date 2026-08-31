"""
Cross-strategy entry registry - closes a real cross-package race window,
user request 31 Aug 2026 (identified while explaining Luxury's own design:
"would there be any race conditions if all 3 webhooks... received
triggers for same stock at same time?").

Options, Futures, and Luxury each have their own independent
PositionStore (separate asyncio.Lock, separate reserved_symbols dict) and
all three share ONE real Dhan broker account. Each package's own
has_open_position_for_underlying() check (a broker-side REST read) is the
only thing standing between them and entering the same stock 3 times over
- but it's a check-then-act (TOCTOU) gap: it only reflects an order once
that order has actually been placed AND settled at the broker. If alerts
for the same underlying land on two or three of these packages close
enough together (all inside that settlement window, roughly tens of ms to
a couple seconds), all three could see "nothing open yet" and all
independently place a real order for the same stock - each within its own
capacity cap, but collectively multiplying real exposure on that one
underlying by however many strategies raced for it.

This module adds ONE shared, in-memory, atomically-locked "who's
currently entering this underlying" registry, keyed per-symbol - checked
and claimed BEFORE any package's own reserve_symbol/broker-check/order-
placement sequence, and held for that sequence's ENTIRE duration (not
just around the broker check). That's what actually closes the gap: by
the time a second strategy's own has_open_position_for_underlying() check
runs, the first strategy's order has already been placed and (in
practice) settled at the broker, because the two were never allowed to
interleave on that same symbol at all.

Deliberately per-symbol, not a single global lock - claiming RELIANCE for
Options never blocks Futures/Luxury from claiming TCS at the exact same
instant; only two attempts on the SAME underlying ever contend, and even
then `try_claim` returns immediately (no waiting) rather than blocking
the loser until the winner finishes. Pure in-memory dict operations under
one asyncio.Lock - no I/O, no network call - costing low single-digit
microseconds against a code path already dominated by real REST round-
trips (has_open_position_for_underlying, place_market_order,
wait_for_order_result each cost tens of ms to several seconds), so this
adds no measurable latency.

Only the three REAL-money strategies (Options, Futures, Luxury)
participate - K01/CopperOptions/IndexScalping never place real orders, so
they're out of scope for this specific race.

Pure in-memory, same tradeoff as every PositionStore in this codebase -
resets on restart. That's fine here: this only ever needs to protect a
single in-flight entry attempt (seconds), not persistent state. It does
NOT replace has_open_position_for_underlying() - that remains the
correct, restart-durable defense against a position opened by a manual
trade, a stale reservation surviving a crashed process, or a run that
predates this registry entirely; this closes the specific gap BETWEEN
strategies concurrently racing for the same stock IN THIS SAME PROCESS.
"""
from __future__ import annotations

import asyncio

_lock = asyncio.Lock()
_claimed: dict[str, str] = {}  # underlying_symbol -> strategy name currently entering it


async def try_claim(underlying_symbol: str, strategy: str) -> bool:
    """Atomically claims underlying_symbol for `strategy` if no OTHER
    strategy currently holds it. Returns True (and records the claim)
    immediately if free or already held by this same strategy (re-
    claiming your own claim is a harmless no-op, not an error - keeps
    callers simple, no need to check "do I already own this"). Returns
    False immediately (never blocks/waits) if a different strategy holds
    it - the loser backs off right away rather than queuing behind the
    winner's whole entry attempt."""
    async with _lock:
        holder = _claimed.get(underlying_symbol)
        if holder is not None and holder != strategy:
            return False
        _claimed[underlying_symbol] = strategy
        return True


async def release_claim(underlying_symbol: str, strategy: str) -> None:
    """Releases a claim once an entry attempt is fully resolved (entered,
    rejected, skipped, or errored) - callers wrap their whole attempt in
    try/finally so this always runs. Safe to call even if this strategy
    never actually held the claim, or another strategy has since claimed
    it (only clears the entry if it still matches `strategy` - never
    clears a claim you don't own)."""
    async with _lock:
        if _claimed.get(underlying_symbol) == strategy:
            del _claimed[underlying_symbol]


def snapshot() -> dict[str, str]:
    """Read-only view of every underlying currently claimed and by which
    strategy - for observability (GET /entry-claims) and tests. Not
    lock-protected (a plain dict read/copy is atomic enough for a status
    snapshot; the caller isn't making a decision based on it)."""
    return dict(_claimed)
