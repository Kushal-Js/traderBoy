"""
In-memory, thread/async-safe state for the trading day.

Tracks:
  - `live_positions`: currently open option legs (max MAX_LIVE_POSITIONS)
  - `traded_symbols_today`: underlyings we've already entered today, so a
    repeat Chartink alert for the same stock is ignored even after the
    position has been closed.
  - `orders_today`: every order placed today (both entry BUY and exit SELL
    legs), keyed by order_id, with Dhan's own order_status (e.g.
    "REJECTED", "TRADED" - see dhan_client.OrderStatus).

NOTE: This is intentionally in-memory. If you need the bot to survive a
process restart mid-day, swap this for a small SQLite/Redis-backed store —
the public interface (PositionStore) would stay the same.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import config

logger = logging.getLogger("position_store")


@dataclass
class OrderRecord:
    order_id: str
    underlying_symbol: str
    trading_symbol: str
    transaction_type: str          # BUY | SELL
    quantity: int
    status: str                    # Dhan's order_status, e.g. "TRADED", "REJECTED"
    remark: str = ""
    is_amo: bool = False           # placed outside market hours (afterMarketOrder=True)
    # Set for BUY entry orders only, so a queued AMO order can be promoted
    # to a live Position later (once filled) without re-deriving these.
    lot_size: Optional[int] = None
    option_type: Optional[str] = None
    placed_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class Position:
    underlying_symbol: str
    option_trading_symbol: str
    option_type: str
    quantity: int
    lot_size: int
    entry_price: float
    highest_price: float           # for trailing SL, tracked from entry
    target_price: float
    hard_stop_loss: float
    order_id: str
    opened_at: datetime = field(default_factory=datetime.now)
    status: str = "OPEN"           # OPEN | CLOSED
    exit_reason: Optional[str] = None
    exit_price: Optional[float] = None
    closed_at: Optional[datetime] = None
    # True if this Position was recovered from Dhan's own portfolio at
    # startup (e.g. left open by a previous run) rather than entered by us
    # this run - target/stop-loss are computed off the broker's reported
    # average price, not an actual fill we observed.
    reconciled: bool = False
    # order_id of an exit (SELL) order that's been placed but hasn't
    # reached a terminal status yet (e.g. queued as AMO) - while set, the
    # monitor loop must not place another exit order for this position.
    pending_exit_order_id: Optional[str] = None
    pending_exit_reason: Optional[str] = None
    # Consecutive exit-order placement failures (e.g. the account's IP isn't
    # whitelisted with the broker) and when to next retry - confirmed live
    # that without backoff, a persistent failure gets hammered every
    # monitor tick (5s) indefinitely.
    exit_failure_count: int = 0
    next_exit_retry_at: Optional[datetime] = None

    @property
    def current_trailing_sl(self) -> float:
        """1% trailing stop measured off the highest price seen so far,
        never below the hard stop loss."""
        trail = self.highest_price * (1 - config.TRAILING_SL_PCT)
        return max(trail, self.hard_stop_loss)


class PositionStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.live_positions: Dict[str, Position] = {}   # keyed by underlying_symbol
        self.traded_symbols_today: set[str] = set()
        self.closed_positions_today: List[Position] = []
        self.orders_today: Dict[str, OrderRecord] = {}  # keyed by order_id
        self._trading_day: date = date.today()

    async def maybe_reset_for_new_day(self) -> None:
        async with self._lock:
            today = date.today()
            if today != self._trading_day:
                logger.info("New trading day detected (%s) - resetting state.", today)
                self.live_positions.clear()
                self.traded_symbols_today.clear()
                self.closed_positions_today.clear()
                self.orders_today.clear()
                self._trading_day = today

    async def reserve_symbol(self, underlying_symbol: str) -> bool:
        """Atomically checks dedup + capacity and claims the symbol in one
        locked step, so two near-simultaneous calls (e.g. a duplicate
        Chartink webhook delivery) can't both pass the check and both go on
        to place an order for the same underlying. Returns True if the
        caller now owns the reservation and should proceed to enter;
        False means someone/something already has this symbol (or there's
        no capacity) and the caller must NOT place an order.

        Callers that end up not entering (order rejected, exception, etc.)
        must call release_symbol() so a later, non-duplicate alert can still
        retry the same symbol today."""
        async with self._lock:
            if underlying_symbol in self.traded_symbols_today or underlying_symbol in self.live_positions:
                return False
            if len(self.live_positions) >= config.MAX_LIVE_POSITIONS:
                return False
            self.traded_symbols_today.add(underlying_symbol)
            return True

    async def release_symbol(self, underlying_symbol: str) -> None:
        """Undoes reserve_symbol() when entry didn't end up happening."""
        async with self._lock:
            if underlying_symbol not in self.live_positions:
                self.traded_symbols_today.discard(underlying_symbol)

    async def remaining_capacity(self) -> int:
        async with self._lock:
            return max(0, config.MAX_LIVE_POSITIONS - len(self.live_positions))

    async def add_position(self, pos: Position) -> None:
        async with self._lock:
            self.live_positions[pos.underlying_symbol] = pos
            self.traded_symbols_today.add(pos.underlying_symbol)
            logger.info(
                "Position OPENED: %s (%s) entry=%.2f target=%.2f sl=%.2f qty=%s",
                pos.underlying_symbol, pos.option_trading_symbol,
                pos.entry_price, pos.target_price, pos.hard_stop_loss, pos.quantity,
            )

    async def reconcile_from_broker(self, positions: List[Position]) -> None:
        """Imports positions already open at Dhan (e.g. left over from a
        previous run) into live_positions/traded_symbols_today, so we don't
        blindly place a duplicate entry for an underlying we already hold."""
        async with self._lock:
            for pos in positions:
                if pos.underlying_symbol in self.live_positions:
                    continue
                self.live_positions[pos.underlying_symbol] = pos
                self.traded_symbols_today.add(pos.underlying_symbol)
                logger.info(
                    "Reconciled existing broker position: %s (%s) qty=%s avg_price=%.2f",
                    pos.underlying_symbol, pos.option_trading_symbol, pos.quantity, pos.entry_price,
                )

    async def update_highest_price(self, underlying_symbol: str, current_price: float) -> None:
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if pos and current_price > pos.highest_price:
                pos.highest_price = current_price

    async def record_order(self, order: OrderRecord) -> None:
        async with self._lock:
            self.orders_today[order.order_id] = order
            logger.info(
                "Order PLACED: %s %s %s x%s status=%s%s",
                order.transaction_type, order.trading_symbol, order.order_id,
                order.quantity, order.status, " (AMO)" if order.is_amo else "",
            )

    async def update_order_status(self, order_id: str, status: str, remark: str = "") -> None:
        async with self._lock:
            order = self.orders_today.get(order_id)
            if order is None:
                return
            order.status = status
            order.remark = remark or order.remark
            order.updated_at = datetime.now()
            logger.info("Order STATUS: %s (%s) -> %s", order_id, order.trading_symbol, status)

    async def record_exit_failure(self, underlying_symbol: str) -> None:
        """Backs off the next retry after an exit order failed to even get
        placed (as opposed to being placed and then REJECTED - that path
        already retries every tick via _exit_position's normal flow).
        5s, 10s, 20s, 40s, 80s, 160s, capped at 5 minutes."""
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if not pos:
                return
            pos.exit_failure_count += 1
            backoff = min(5 * (2 ** pos.exit_failure_count), 300)
            pos.next_exit_retry_at = datetime.now() + timedelta(seconds=backoff)
            logger.warning(
                "%s: exit order placement failed (%d consecutive) - next retry in %ds",
                underlying_symbol, pos.exit_failure_count, backoff,
            )

    async def clear_exit_failure(self, underlying_symbol: str) -> None:
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if pos:
                pos.exit_failure_count = 0
                pos.next_exit_retry_at = None

    async def set_pending_exit_order(
        self, underlying_symbol: str, order_id: Optional[str], reason: Optional[str] = None
    ) -> None:
        """Marks (or clears, passing None) an outstanding exit order for a
        live position, so the monitor loop doesn't place a second exit order
        for it while the first one (e.g. a queued AMO SELL) is still
        unresolved."""
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if pos:
                pos.pending_exit_order_id = order_id
                pos.pending_exit_reason = reason

    async def close_position(self, underlying_symbol: str, exit_price: float, reason: str) -> Optional[Position]:
        async with self._lock:
            pos = self.live_positions.pop(underlying_symbol, None)
            if pos is None:
                return None
            pos.status = "CLOSED"
            pos.exit_reason = reason
            pos.exit_price = exit_price
            pos.closed_at = datetime.now()
            self.closed_positions_today.append(pos)
            logger.info(
                "Position CLOSED: %s (%s) reason=%s exit=%.2f pnl_pct=%.2f%%",
                pos.underlying_symbol, pos.option_trading_symbol, reason, exit_price,
                (exit_price - pos.entry_price) / pos.entry_price * 100,
            )
            return pos

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "live_positions": [vars(p) | {
                    "current_trailing_sl": p.current_trailing_sl
                } for p in self.live_positions.values()],
                "traded_symbols_today": sorted(self.traded_symbols_today),
                "closed_positions_today": [vars(p) for p in self.closed_positions_today],
                "orders_today": [vars(o) for o in self.orders_today.values()],
            }


position_store = PositionStore()
