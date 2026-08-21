"""
In-memory, thread/async-safe state for the trading day.

Tracks:
  - `live_positions`: currently open option legs (max MAX_LIVE_POSITIONS)
  - `reserved_symbols`: underlyings currently blocked from a new entry -
    either an entry attempt is actively in flight for it right now, an AMO
    BUY is queued awaiting fill confirmation, or a position is open. Once a
    position closes (or a pending entry ends up rejected), the symbol is
    freed up again - a repeat Chartink alert for the same stock later the
    same day is allowed as long as nothing is currently open/pending for it.
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

# Placeholder for Position.pending_exit_order_id while an exit attempt is
# claimed (via try_start_exit) but doesn't have a real order_id yet -
# callers reading pending_exit_order_id as if it were a real order_id (e.g.
# to poll its status) must check for and skip this sentinel.
EXIT_CLAIMED = "CLAIMED"


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
    # True while whoever placed this BUY order (_enter_single_position) is
    # still actively resolving it inline. _sync_pending_orders() must never
    # touch an order while this is True - confirmed live that without this,
    # a monitor_loop tick landing mid-entry can race the placer to promote
    # the same order to a Position twice (the second call silently resets
    # highest_price back to entry, erasing any trailing-stop progress).
    # Only released (see release_order_ownership) when the placer
    # determines the order is genuinely queued as AMO and needs the async
    # follow-up - never for a normal fill/rejection, which the placer
    # always resolves to completion itself.
    owned_by_placer: bool = True


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
    # Must match whatever product type the position was actually opened
    # under (Dhan's MIS/MARGIN/CNC/...) - confirmed live that a SELL placed
    # with a mismatched product type isn't recognized as squaring off the
    # existing position, and instead gets RMS-rejected as a fresh naked
    # short requiring full margin ("insufficient funds").
    product_type: str
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
        self.reserved_symbols: set[str] = set()
        self.closed_positions_today: List[Position] = []
        self.orders_today: Dict[str, OrderRecord] = {}  # keyed by order_id
        self._trading_day: date = date.today()

    async def maybe_reset_for_new_day(self) -> None:
        async with self._lock:
            today = date.today()
            if today != self._trading_day:
                logger.info("New trading day detected (%s) - resetting state.", today)
                self.live_positions.clear()
                self.reserved_symbols.clear()
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
            if underlying_symbol in self.reserved_symbols or underlying_symbol in self.live_positions:
                return False
            if len(self.live_positions) >= config.MAX_LIVE_POSITIONS:
                return False
            self.reserved_symbols.add(underlying_symbol)
            return True

    async def release_symbol(self, underlying_symbol: str) -> None:
        """Undoes reserve_symbol() when entry didn't end up happening."""
        async with self._lock:
            if underlying_symbol not in self.live_positions:
                self.reserved_symbols.discard(underlying_symbol)

    async def remaining_capacity(self) -> int:
        async with self._lock:
            return max(0, config.MAX_LIVE_POSITIONS - len(self.live_positions))

    async def add_position(self, pos: Position) -> None:
        async with self._lock:
            self.live_positions[pos.underlying_symbol] = pos
            self.reserved_symbols.add(pos.underlying_symbol)
            logger.info(
                "Position OPENED: %s (%s) entry=%.2f target=%.2f sl=%.2f qty=%s",
                pos.underlying_symbol, pos.option_trading_symbol,
                pos.entry_price, pos.target_price, pos.hard_stop_loss, pos.quantity,
            )

    async def reconcile_from_broker(self, positions: List[Position]) -> None:
        """Imports positions already open at Dhan (e.g. left over from a
        previous run) into live_positions/reserved_symbols, so we don't
        blindly place a duplicate entry for an underlying we already hold."""
        async with self._lock:
            for pos in positions:
                if pos.underlying_symbol in self.live_positions:
                    continue
                self.live_positions[pos.underlying_symbol] = pos
                self.reserved_symbols.add(pos.underlying_symbol)
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

    async def release_order_ownership(self, order_id: str) -> None:
        """Signals that whoever placed this BUY order is done actively
        resolving it inline and it's now genuinely queued as AMO -
        _sync_pending_orders() may only pick up an order after this."""
        async with self._lock:
            order = self.orders_today.get(order_id)
            if order:
                order.owned_by_placer = False

    async def try_start_exit(self, underlying_symbol: str) -> bool:
        """Atomically checks (not already pending/claimed, not on cooldown)
        and claims the right to place an exit order for this position, in
        one locked step. Now that exit checks can fire from two places -
        the poll loop AND event-driven WebSocket ticks - without this,
        two near-simultaneous triggers could both pass a check-then-act
        race and both place a real SELL order for the same position.

        Returns True if the caller now owns the exit attempt and must
        proceed to place the order; False means someone else already has
        it (or it's on cooldown) and the caller must NOT place an order.
        Every code path after a successful claim must release it: either
        set_pending_exit_order() with the real order_id (on a successful
        placement) or None (on an exchange-side rejection), or
        record_exit_failure() (if placement itself failed) - all three
        clear/replace the placeholder this sets."""
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if not pos:
                return False
            if pos.pending_exit_order_id:
                return False
            if pos.next_exit_retry_at and datetime.now() < pos.next_exit_retry_at:
                return False
            pos.pending_exit_order_id = EXIT_CLAIMED
            return True

    async def record_exit_failure(self, underlying_symbol: str) -> None:
        """Backs off the next retry after an exit order failed to even get
        placed (as opposed to being placed and then REJECTED - that path
        already retries every tick via _exit_position's normal flow).
        5s, 10s, 20s, 40s, 80s, 160s, capped at 5 minutes. Also releases
        the try_start_exit() claim, since no real order_id resulted."""
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if not pos:
                return
            pos.pending_exit_order_id = None
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
            # Frees the symbol up for a fresh entry on a later alert today -
            # reserved_symbols only blocks while something is genuinely
            # open/in-flight for it, not for the rest of the day.
            self.reserved_symbols.discard(underlying_symbol)
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
                "reserved_symbols": sorted(self.reserved_symbols),
                "closed_positions_today": [vars(p) for p in self.closed_positions_today],
                "orders_today": [vars(o) for o in self.orders_today.values()],
            }


position_store = PositionStore()
