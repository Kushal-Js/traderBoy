"""
In-memory, thread/async-safe state for the Futures strategy's trading day -
entirely independent from Options/position_store.py's own instance (own
capacity, own dedup, own orders). A near-verbatim copy of that file's
logic - see its own docstring/comments for the full rationale behind each
piece; this file exists separately (rather than importing Options' class
with different config) so the two strategies' live-money state can never
cross-contaminate, matching the "each strategy owns its own package"
principle used throughout this codebase.

Tracks:
  - `live_positions`: currently open legs (capped per option type - see
    config.MAX_LIVE_POSITIONS_CE / MAX_LIVE_POSITIONS_PE)
  - `reserved_symbols`: underlying_symbol -> option_type for every
    underlying currently blocked from a new entry
  - `orders_today`: every order placed today, keyed by order_id

NOTE: intentionally in-memory, same tradeoff as Options/position_store.py.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import config
from trade_history import record_closed_trade

logger = logging.getLogger("futures_position_store")

# Placeholder for Position.pending_exit_order_id while an exit attempt is
# claimed (via try_start_exit) but doesn't have a real order_id yet.
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
    lot_size: Optional[int] = None
    option_type: Optional[str] = None
    placed_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
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
    # under - see Options/position_store.py's Position for the live
    # incident (bug #23) that made this matter.
    product_type: str
    opened_at: datetime = field(default_factory=datetime.now)
    status: str = "OPEN"           # OPEN | CLOSED
    exit_reason: Optional[str] = None
    exit_price: Optional[float] = None
    closed_at: Optional[datetime] = None
    reconciled: bool = False
    pending_exit_order_id: Optional[str] = None
    pending_exit_reason: Optional[str] = None
    exit_failure_count: int = 0
    next_exit_retry_at: Optional[datetime] = None
    supertrend_entry_candle_start: Optional[datetime] = None

    @property
    def current_trailing_sl(self) -> float:
        """See Options/position_store.py's Position.current_trailing_sl for
        the full explanation - identical logic, reading this package's own
        config values."""
        floor = self.hard_stop_loss
        if config.ENABLE_TRAILING_SL:
            floor = max(floor, self.highest_price * (1 - config.TRAILING_SL_PCT))
        if config.ENABLE_DYNAMIC_SL and self.entry_price:
            step_pct = (
                config.DYNAMIC_SL_STEP_PCT_CE if self.option_type == "CE"
                else config.DYNAMIC_SL_STEP_PCT_PE
            )
            pct_up = (self.highest_price - self.entry_price) / self.entry_price
            steps = int(pct_up // step_pct) if pct_up > 0 else 0
            if steps > 0:
                dynamic_sl_pct = config.STOP_LOSS_PCT - steps * config.DYNAMIC_SL_INCREASE_PCT
                floor = max(floor, self.entry_price * (1 - dynamic_sl_pct))
        return floor


def _cap_for(option_type: str) -> int:
    return config.MAX_LIVE_POSITIONS_CE if option_type == "CE" else config.MAX_LIVE_POSITIONS_PE


class PositionStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.live_positions: Dict[str, Position] = {}
        self.reserved_symbols: Dict[str, str] = {}
        self.closed_positions_today: List[Position] = []
        self.orders_today: Dict[str, OrderRecord] = {}
        self._trading_day: date = date.today()

    async def maybe_reset_for_new_day(self) -> None:
        """See Options/position_store.py's identical function - same
        ENABLE_SQUARE_OFF-gated rationale. Doubly important here since this
        package doesn't run broker reconciliation at startup at all (see
        trading_engine.py's module docstring) - an in-memory clear of a
        real overnight position here would have NO recovery path, ever,
        not even after a future restart."""
        async with self._lock:
            today = date.today()
            if today != self._trading_day:
                logger.info("New trading day detected (%s) - resetting daily state.", today)
                if config.ENABLE_SQUARE_OFF:
                    self.live_positions.clear()
                    self.reserved_symbols.clear()
                elif self.live_positions:
                    logger.info(
                        "ENABLE_SQUARE_OFF=false - carrying %d live position(s) over the day boundary: %s",
                        len(self.live_positions), list(self.live_positions.keys()),
                    )
                self.closed_positions_today.clear()
                self.orders_today.clear()
                self._trading_day = today

    async def reserve_symbol(self, underlying_symbol: str, option_type: str) -> bool:
        """See Options/position_store.py's reserve_symbol for the full
        race-condition rationale - identical logic here."""
        async with self._lock:
            if underlying_symbol in self.reserved_symbols or underlying_symbol in self.live_positions:
                return False
            current = sum(1 for ot in self.reserved_symbols.values() if ot == option_type)
            if current >= _cap_for(option_type):
                return False
            self.reserved_symbols[underlying_symbol] = option_type
            return True

    async def release_symbol(self, underlying_symbol: str) -> None:
        async with self._lock:
            if underlying_symbol not in self.live_positions:
                self.reserved_symbols.pop(underlying_symbol, None)

    async def remaining_capacity(self, option_type: str) -> int:
        async with self._lock:
            current = sum(1 for ot in self.reserved_symbols.values() if ot == option_type)
            return max(0, _cap_for(option_type) - current)

    async def add_position(self, pos: Position) -> None:
        async with self._lock:
            self.live_positions[pos.underlying_symbol] = pos
            self.reserved_symbols[pos.underlying_symbol] = pos.option_type
            logger.info(
                "Position OPENED: %s (%s) entry=%.2f target=%.2f sl=%.2f qty=%s",
                pos.underlying_symbol, pos.option_trading_symbol,
                pos.entry_price, pos.target_price, pos.hard_stop_loss, pos.quantity,
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
        async with self._lock:
            order = self.orders_today.get(order_id)
            if order:
                order.owned_by_placer = False

    async def try_start_exit(self, underlying_symbol: str) -> bool:
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
            record_closed_trade("Futures", pos)
            self.reserved_symbols.pop(underlying_symbol, None)
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
                "reserved_symbols_by_type": dict(self.reserved_symbols),
                "closed_positions_today": [vars(p) for p in self.closed_positions_today],
                "orders_today": [vars(o) for o in self.orders_today.values()],
            }


position_store = PositionStore()
