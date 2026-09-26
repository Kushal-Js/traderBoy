"""
In-memory, async-safe state for the Bollinger strategy - structurally
cloned from Swing/position_store.py's proven reserve/release/lock pattern
(see that module's own docstring), which is itself modeled on
Options/position_store.py's. One capacity counter
(config.MAX_CONCURRENT_TRADES), independent of every other package's own.

Position is NOT a copy of Swing's - Bollinger has no profit target and no
LONG/SHORT direction split (every entry is a LONG CE or LONG PE, same as
every OPTIONS position in this repo), but it DOES have swing-distance-
derived dynamic stop/trailing fields Swing's own Position doesn't need.
The pure, direction-aware math (unrealized_pnl_rs, hard_stop_for,
price_past_hard_stop, resolve_instrument_side, broker_stop_trigger_and_
limit, entry_transaction_type, exit_transaction_type) is reused DIRECTLY
from Swing.position_store rather than duplicated - those functions are
pure/stateless with zero Swing-specific coupling (confirmed: this repo's
own backtest_bollinger_vortex_9symbols_30day.py already imports them the
same way).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import reversal_filters
from . import config
from Options.dhan_client import IST
from trade_history import fire_and_forget, record_closed_trade, record_opened_position

logger = logging.getLogger("bollinger_position_store")

EXIT_CLAIMED = "CLAIMED"


def _now_ist() -> datetime:
    return datetime.now(IST)


@dataclass
class OrderRecord:
    order_id: str
    underlying_symbol: str
    trading_symbol: str
    transaction_type: str          # BUY | SELL
    quantity: int
    status: str
    remark: str = ""
    is_amo: bool = False
    lot_size: Optional[int] = None
    placed_at: datetime = field(default_factory=_now_ist)
    updated_at: datetime = field(default_factory=_now_ist)
    owned_by_placer: bool = True


@dataclass
class Position:
    underlying_symbol: str
    trading_symbol: str
    resolved_option_type: str      # "CE" | "PE"
    instrument_side: str           # always "LONG" - a PE position is itself entered LONG, same convention as every OPTIONS entry in this repo
    exchange_segment: str          # always "NSE_FNO" - v1 scope is NSE equity options only, see config.py's own docstring
    product_type: str
    quantity: int
    lot_size: Optional[int]
    entry_price: float
    best_price: float
    # The dynamic, PER-TRADE stop percentage computed at entry from the
    # actual swing distance (fired_order.trigger_price vs its own
    # stop_price) - NOT a fixed config constant like Swing's HARD_STOP_
    # LOSS_PCT. See Bollinger/trading_engine.py's enter_position_for_stock.
    stop_pct: float
    hard_stop_loss: float          # entry_price * (1 - stop_pct), via Swing.position_store.hard_stop_for
    trailing_stop_dist: float
    trailing_step: float
    # Deliberately NO default (see Swing/position_store.py's own Position.
    # pnl_multiplier docstring for the exact reasoning) - always identical
    # to `quantity` for every position this package ever opens (NSE
    # options, no MCX), but every construction site must still pass it
    # explicitly so a future call site can never silently default to 0
    # and make every rupee-threshold exit check permanently no-op.
    pnl_multiplier: int
    trailing_armed: bool = False
    trailing_stop_price: Optional[float] = None
    order_id: str = ""
    opened_at: datetime = field(default_factory=_now_ist)
    status: str = "OPEN"
    exit_reason: Optional[str] = None
    exit_price: Optional[float] = None
    closed_at: Optional[datetime] = None
    reconciled: bool = False
    pending_exit_order_id: Optional[str] = None
    pending_exit_reason: Optional[str] = None
    exit_failure_count: int = 0
    next_exit_retry_at: Optional[datetime] = None
    # Start of the signal-interval candle this position's ENTRY trigger
    # fired on - mirrors Swing's own supertrend_entry_candle_start (same
    # "the very candle that triggered entry can't immediately reverse it"
    # guard, though Bollinger has no reversal-exit signal today; kept for
    # parity/observability and in case one is added later).
    entry_candle_start: Optional[datetime] = None
    stop_loss_order_id: Optional[str] = None

    @property
    def option_trading_symbol(self) -> str:
        return self.trading_symbol

    @property
    def option_type(self) -> str:
        return self.resolved_option_type


def _cap_reached(reserved_count: int) -> bool:
    return reserved_count >= config.MAX_CONCURRENT_TRADES


class BollingerPositionStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.live_positions: Dict[str, Position] = {}   # keyed by underlying_symbol
        self.reserved_symbols: set[str] = set()
        self.closed_positions_today: List[Position] = []
        self.orders_today: Dict[str, OrderRecord] = {}
        self._trading_day: date = date.today()
        self._last_failed_entry_at: Dict[str, float] = {}

    async def maybe_reset_for_new_day(self) -> None:
        """Same "never clear live_positions on a day boundary" rule as
        Swing's own store - Bollinger has no EOD/Friday square-off either,
        clearing here would silently orphan real money from all future
        exit monitoring until the next restart."""
        async with self._lock:
            today = date.today()
            if today != self._trading_day:
                logger.info(
                    "New trading day detected (%s) - carrying %d live Bollinger position(s) over "
                    "the day boundary: %s.", today, len(self.live_positions), sorted(self.live_positions),
                )
                self.closed_positions_today.clear()
                self.orders_today.clear()
                self._trading_day = today

    async def reserve_symbol(self, underlying_symbol: str) -> bool:
        async with self._lock:
            if underlying_symbol in self.reserved_symbols or underlying_symbol in self.live_positions:
                return False
            if _cap_reached(len(self.reserved_symbols)):
                return False
            self.reserved_symbols.add(underlying_symbol)
            return True

    async def release_symbol(self, underlying_symbol: str) -> None:
        async with self._lock:
            if underlying_symbol not in self.live_positions:
                self.reserved_symbols.discard(underlying_symbol)

    async def record_failed_entry(self, underlying_symbol: str) -> None:
        async with self._lock:
            self._last_failed_entry_at[underlying_symbol] = time.monotonic()

    async def is_in_entry_cooldown(self, underlying_symbol: str) -> bool:
        async with self._lock:
            last = self._last_failed_entry_at.get(underlying_symbol)
            return last is not None and (time.monotonic() - last) < config.ENTRY_RETRY_COOLDOWN_SECONDS

    async def remaining_capacity(self) -> int:
        async with self._lock:
            return max(0, config.MAX_CONCURRENT_TRADES - len(self.reserved_symbols))

    async def add_position(self, pos: Position) -> None:
        async with self._lock:
            self.live_positions[pos.underlying_symbol] = pos
            self.reserved_symbols.add(pos.underlying_symbol)
            fire_and_forget(record_opened_position("Bollinger", pos))
            fire_and_forget(reversal_filters.evaluate_and_log(
                "Bollinger", pos.underlying_symbol, pos.resolved_option_type, pos.entry_price, pos.order_id,
            ))
            logger.info(
                "Position OPENED: %s (%s, LONG %s) entry=%.2f stop=%.2f stop_pct=%.3f%% qty=%s",
                pos.underlying_symbol, pos.trading_symbol, pos.resolved_option_type,
                pos.entry_price, pos.hard_stop_loss, pos.stop_pct * 100, pos.quantity,
            )

    async def reconcile_from_broker(self, positions: List[Position]) -> None:
        async with self._lock:
            for pos in positions:
                if pos.underlying_symbol in self.live_positions:
                    continue
                self.live_positions[pos.underlying_symbol] = pos
                self.reserved_symbols.add(pos.underlying_symbol)
                logger.info(
                    "Reconciled existing broker position: %s (%s, LONG %s) qty=%s entry_price=%.2f",
                    pos.underlying_symbol, pos.trading_symbol, pos.resolved_option_type,
                    pos.quantity, pos.entry_price,
                )

    async def update_best_price(self, underlying_symbol: str, current_price: float) -> None:
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if pos and current_price > pos.best_price:
                pos.best_price = current_price

    async def update_trailing(self, underlying_symbol: str, current_price: float) -> None:
        """Atomically updates best_price AND the trailing-arm/ratchet state
        together, under the same lock - both on_price_tick's fast WS path
        and _check_one_position's poll path can call this concurrently for
        the same position, so best_price and trailing_stop_price must
        never be updated as two separate unlocked steps (a race there
        could arm/ratchet the trailing stop off a stale best_price).
        Always LONG (every position this package opens) - favorable move
        is simply current best_price minus entry_price. Trails ratchet
        UP only (never loosens), armed once price has moved favorably
        past trailing_stop_dist, then tightened in trailing_step
        increments - same ratchet semantics as
        backtest_bollinger_vortex_9symbols_30day.py's own per-tick block."""
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if not pos:
                return
            if current_price > pos.best_price:
                pos.best_price = current_price
            favorable_move = pos.best_price - pos.entry_price
            if not pos.trailing_armed and favorable_move >= pos.trailing_stop_dist:
                pos.trailing_armed = True
                pos.trailing_stop_price = pos.best_price - pos.trailing_stop_dist
            elif pos.trailing_armed:
                candidate = pos.best_price - pos.trailing_stop_dist
                if candidate - pos.trailing_stop_price >= pos.trailing_step:
                    pos.trailing_stop_price = candidate

    async def clear_stop_loss_order_id(self, underlying_symbol: str) -> None:
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if pos:
                pos.stop_loss_order_id = None

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
            order.updated_at = _now_ist()

    async def try_start_exit(self, underlying_symbol: str) -> bool:
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if not pos:
                return False
            if pos.pending_exit_order_id:
                return False
            if pos.next_exit_retry_at and _now_ist() < pos.next_exit_retry_at:
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
            pos.next_exit_retry_at = _now_ist() + timedelta(seconds=backoff)
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
            pos.closed_at = _now_ist()
            self.closed_positions_today.append(pos)
            fire_and_forget(record_closed_trade("Bollinger", pos))
            self.reserved_symbols.discard(underlying_symbol)
            pnl = (exit_price - pos.entry_price) * pos.pnl_multiplier  # always LONG - see Position.instrument_side
            logger.info(
                "Position CLOSED: %s (%s, LONG %s) reason=%s exit=%.2f pnl=%.2f",
                pos.underlying_symbol, pos.trading_symbol, pos.resolved_option_type,
                reason, exit_price, pnl,
            )
            return pos

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "live_positions": [vars(p) | {
                    "option_trading_symbol": p.option_trading_symbol,
                    "option_type": p.option_type,
                } for p in self.live_positions.values()],
                "reserved_symbols": sorted(self.reserved_symbols),
                "closed_positions_today": [vars(p) for p in self.closed_positions_today],
                "orders_today": [vars(o) for o in self.orders_today.values()],
            }


position_store = BollingerPositionStore()
