"""
In-memory, thread/async-safe state for the trading day.

Tracks:
  - `live_positions`: currently open option legs (max MAX_LIVE_POSITIONS)
  - `traded_symbols_today`: underlyings we've already entered today, so a
    repeat Chartink alert for the same stock is ignored even after the
    position has been closed.
  - `orders_today`: every order placed today (both entry BUY and exit SELL
    legs), keyed by groww_order_id, with Groww's own order_status (e.g.
    "REJECTED", "COMPLETED" - see groww_client.OrderStatus).

NOTE: This is intentionally in-memory. If you need the bot to survive a
process restart mid-day, swap this for a small SQLite/Redis-backed store —
the public interface (PositionStore) would stay the same.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

import config

logger = logging.getLogger("position_store")


@dataclass
class OrderRecord:
    groww_order_id: str
    order_reference_id: str
    underlying_symbol: str
    trading_symbol: str
    transaction_type: str          # BUY | SELL
    quantity: int
    status: str                    # Groww's order_status, e.g. "COMPLETED", "REJECTED"
    remark: str = ""
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
    groww_order_id: str
    order_reference_id: str
    opened_at: datetime = field(default_factory=datetime.now)
    status: str = "OPEN"           # OPEN | CLOSED
    exit_reason: Optional[str] = None
    exit_price: Optional[float] = None
    closed_at: Optional[datetime] = None

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
        self.orders_today: Dict[str, OrderRecord] = {}  # keyed by groww_order_id
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

    async def has_capacity(self) -> bool:
        async with self._lock:
            return len(self.live_positions) < config.MAX_LIVE_POSITIONS

    async def is_already_traded(self, underlying_symbol: str) -> bool:
        async with self._lock:
            return (
                underlying_symbol in self.traded_symbols_today
                or underlying_symbol in self.live_positions
            )

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

    async def update_highest_price(self, underlying_symbol: str, current_price: float) -> None:
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if pos and current_price > pos.highest_price:
                pos.highest_price = current_price

    async def record_order(self, order: OrderRecord) -> None:
        async with self._lock:
            self.orders_today[order.groww_order_id] = order
            logger.info(
                "Order PLACED: %s %s %s x%s status=%s",
                order.transaction_type, order.trading_symbol, order.groww_order_id,
                order.quantity, order.status,
            )

    async def update_order_status(self, groww_order_id: str, status: str, remark: str = "") -> None:
        async with self._lock:
            order = self.orders_today.get(groww_order_id)
            if order is None:
                return
            order.status = status
            order.remark = remark or order.remark
            order.updated_at = datetime.now()
            logger.info("Order STATUS: %s (%s) -> %s", groww_order_id, order.trading_symbol, status)

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
