"""
In-memory, async-safe state for Swing v2 (complete rewrite, 12 Sep 2026 -
see Swing/config.py's own module docstring for the full context).

Replaces the old design's Leg/Basket/BasketStore/SequentialPositionStore/
BasketHedgePosition/BasketHedgeStore split with ONE Position dataclass and
ONE SwingPositionStore, modeled directly on Options/position_store.py's
proven reserve/release/lock pattern - collapsed to a single capacity
counter (config.MAX_CONCURRENT_TRADES) since this strategy's regime is
mutually exclusive per stock (never simultaneously both a long and a
short candidate), unlike Options' CE/PE split.

Direction-aware math (a position here can be LONG or SHORT, unlike
Options/Futures/Luxury which are always long) is handled by a handful of
pure, independently-testable module-level functions rather than scattered
`if instrument_side == "SHORT"` branches throughout trading_engine.py -
see the block below the Position dataclass.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import config
from trade_history import fire_and_forget, record_closed_trade, record_opened_position

logger = logging.getLogger("swing_position_store")

# Same sentinel/semantics as Options/position_store.py's own EXIT_CLAIMED -
# see try_start_exit's docstring below.
EXIT_CLAIMED = "CLAIMED"


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
    placed_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    owned_by_placer: bool = True


@dataclass
class Position:
    underlying_symbol: str
    trading_symbol: str            # the real contract/scrip actually traded
    basket_type: str               # "FUTURES" | "OPTIONS" | "EQUITY" - snapshot of config.BASKET_TYPE at entry
    regime: str                    # "BULLISH" | "BEARISH" | "UNKNOWN" (UNKNOWN only for a broker-reconciled position)
    instrument_side: str           # "LONG" | "SHORT" - the actual broker-side direction; every direction-sensitive expression reads THIS, never basket_type/regime directly
    exchange_segment: str          # "NSE_FNO" | "NSE_EQ" - drives every broker call's routing (get_broker_net_quantity, order placement)
    product_type: str              # MARGIN | CNC - must match whatever the position was actually opened under, same reasoning as every other package's Position.product_type
    quantity: int
    lot_size: Optional[int]        # None for equity
    entry_price: float
    best_price: float              # replaces "highest_price" - the most FAVORABLE price seen: highest for LONG, lowest for SHORT (see unrealized_pnl_rs/target_price_for below)
    target_price: float
    hard_stop_loss: float
    order_id: str
    resolved_option_type: Optional[str] = None  # real "CE"/"PE" when basket_type=="OPTIONS", else None - see the option_type property below
    opened_at: datetime = field(default_factory=datetime.now)
    status: str = "OPEN"
    exit_reason: Optional[str] = None
    exit_price: Optional[float] = None
    closed_at: Optional[datetime] = None
    reconciled: bool = False
    pending_exit_order_id: Optional[str] = None
    pending_exit_reason: Optional[str] = None
    exit_failure_count: int = 0
    next_exit_retry_at: Optional[datetime] = None
    # Start of the 5-min Supertrend candle this position was entered on -
    # a Supertrend-reversal exit is only honored once the cached signal
    # has moved past this (see trading_engine._evaluate_exit_signal), so
    # the very breakout candle that triggered entry can't immediately
    # "reverse" it. Identical reasoning to Options/position_store.py's
    # own supertrend_entry_candle_start.
    supertrend_entry_candle_start: Optional[datetime] = None
    # Real broker-side SELL/BUY STOP-LOSS LIMIT (SL-L) order resting at
    # Dhan for this exact position - None if config.BROKER_STOP_LOSS_
    # ENABLED is off or placement failed. See trading_engine.py's
    # enter_position_for_stock (placement) and _exit_position (the
    # cancel-and-reconcile sequence ported from Options/Futures/Luxury).
    stop_loss_order_id: Optional[str] = None

    # ---- read-only aliases so the SHARED trade_history.py module (which
    # reads pos.option_trading_symbol / pos.option_type for every
    # strategy) works unchanged against this differently-shaped Position,
    # without widening trade_history.py itself for one caller. ----
    @property
    def option_trading_symbol(self) -> str:
        return self.trading_symbol

    @property
    def option_type(self) -> str:
        """The real CE/PE for an OPTIONS basket-type position; for
        FUTURES/EQUITY (which have no CE/PE concept) this is just the
        basket_type string, so a real_trades.log row is still
        self-describing rather than blank."""
        return self.resolved_option_type or self.basket_type


# --------------------------------------------------------------------------- #
# Direction-aware math - pure, module-level, independently unit-testable.
# Every expression in trading_engine.py that depends on LONG vs SHORT goes
# through exactly one of these rather than a scattered if/else, so there is
# exactly one place to get (and verify) the SHORT-side sign right.
# --------------------------------------------------------------------------- #
def resolve_instrument_side(basket_type: str, regime: str) -> Optional[str]:
    """basket_type + regime -> "LONG"/"SHORT", or None if this combination
    should never be entered at all. The one place this lookup table lives:
      FUTURES + BULLISH -> LONG (buy futures)          FUTURES + BEARISH -> SHORT (sell futures to open)
      OPTIONS + BULLISH -> LONG (buy ATM CE)            OPTIONS + BEARISH -> LONG  (buy ATM PE - a PE position is itself always entered LONG)
      EQUITY  + BULLISH -> LONG (buy N shares)          EQUITY  + BEARISH -> None  (skip - no legal naked overnight equity short in India, confirmed with user 12 Sep 2026)
    """
    basket_type = basket_type.upper()
    regime = regime.upper()
    if basket_type == "EQUITY" and regime == "BEARISH":
        return None
    if basket_type == "FUTURES" and regime == "BEARISH":
        return "SHORT"
    return "LONG"  # every other combination (including OPTIONS+BEARISH, which is a LONG PE) is LONG


def resolved_option_type_for(basket_type: str, regime: str) -> Optional[str]:
    """CE for an OPTIONS+BULLISH entry, PE for OPTIONS+BEARISH, None otherwise."""
    if basket_type.upper() != "OPTIONS":
        return None
    return "CE" if regime.upper() == "BULLISH" else "PE"


def entry_transaction_type(side: str) -> str:
    return "BUY" if side == "LONG" else "SELL"


def exit_transaction_type(side: str) -> str:
    """The transaction type an EXIT order (and any resting broker-side SL)
    for this position actually has - the opposite of the entry. Scanning
    get_pending_order_id with the wrong side finds nothing for a SHORT
    position, since its resting order is a BUY, not a SELL."""
    return "SELL" if side == "LONG" else "BUY"


def unrealized_pnl_rs(side: str, entry_price: float, ltp: float, quantity: int) -> float:
    if side == "LONG":
        return (ltp - entry_price) * quantity
    return (entry_price - ltp) * quantity


def is_more_favorable(side: str, candidate_price: float, current_best: float) -> bool:
    """Whether candidate_price improves on current_best (used to update
    Position.best_price on every tick) - higher is better for LONG, lower
    is better for SHORT."""
    return candidate_price > current_best if side == "LONG" else candidate_price < current_best


def target_price_for(side: str, entry_price: float, target_pct: float) -> float:
    return entry_price * (1 + target_pct) if side == "LONG" else entry_price * (1 - target_pct)


def hard_stop_for(side: str, entry_price: float, stop_pct: float) -> float:
    return entry_price * (1 - stop_pct) if side == "LONG" else entry_price * (1 + stop_pct)


def price_past_target(side: str, ltp: float, target_price: float) -> bool:
    return ltp >= target_price if side == "LONG" else ltp <= target_price


def price_past_hard_stop(side: str, ltp: float, hard_stop: float) -> bool:
    return ltp <= hard_stop if side == "LONG" else ltp >= hard_stop


def giveback_floor(side: str, best_price: float, giveback_pct: float) -> float:
    """The price level a retrace from best_price must cross to arm
    PROFIT_PROTECTION_HIT - below best_price for LONG (price falling back
    down), above best_price for SHORT (price rising back up)."""
    return best_price * (1 - giveback_pct) if side == "LONG" else best_price * (1 + giveback_pct)


def price_past_giveback_floor(side: str, ltp: float, floor: float) -> bool:
    return ltp < floor if side == "LONG" else ltp > floor


def broker_stop_trigger_and_limit(
    side: str, fill_price: float, quantity: int, cap_rs: float, gap_multiple: float,
) -> Tuple[float, float]:
    """The broker-side SL-L order's (trigger_price, limit_price) - direct
    port of Options/trading_engine.py's own formula for LONG; the SHORT
    case is its mirror image, not just a sign flip on the same line, so
    it's worth spelling out why: a SHORT position's protective order is a
    BUY (to close), and a BUY stop-loss must sit ABOVE the current price
    (triggering when price rises against the position) with its limit
    ABOVE the trigger (the worst/highest price still acceptable) - exactly
    inverted from a LONG's protective SELL, whose stop sits BELOW price
    with its limit BELOW the trigger. Getting this sign wrong produces a
    stop that can never fire (or fires backwards) - see this module's own
    test file for the case that catches it."""
    per_unit_cap = cap_rs / quantity
    gap = cap_rs * gap_multiple / quantity
    if side == "LONG":
        trigger = fill_price - per_unit_cap
        limit = trigger - gap
    else:
        trigger = fill_price + per_unit_cap
        limit = trigger + gap
    return trigger, limit


# --------------------------------------------------------------------------- #
def _cap_reached(reserved_count: int) -> bool:
    return reserved_count >= config.MAX_CONCURRENT_TRADES


class SwingPositionStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.live_positions: Dict[str, Position] = {}   # keyed by underlying_symbol
        self.reserved_symbols: set[str] = set()
        self.closed_positions_today: List[Position] = []
        self.orders_today: Dict[str, OrderRecord] = {}
        self._trading_day: date = date.today()

    async def maybe_reset_for_new_day(self) -> None:
        """Unlike Options/position_store.py's version, this NEVER clears
        live_positions/reserved_symbols on a day boundary - Swing
        positions are meant to carry across days by design (no EOD/Friday
        square-off anywhere in this package). Clearing here would silently
        orphan real money from all future exit monitoring with no
        recovery path until the next process restart (reconcile_broker_
        positions only runs at startup) - see Options/position_store.py's
        own maybe_reset_for_new_day docstring for the identical reasoning
        it applies when ENABLE_SQUARE_OFF is False."""
        async with self._lock:
            today = date.today()
            if today != self._trading_day:
                logger.info(
                    "New trading day detected (%s) - carrying %d live position(s) over "
                    "the day boundary: %s (Swing never clears these).",
                    today, len(self.live_positions), sorted(self.live_positions),
                )
                self.closed_positions_today.clear()
                self.orders_today.clear()
                self._trading_day = today

    async def reserve_symbol(self, underlying_symbol: str) -> bool:
        """Atomic check-and-claim, one shared MAX_CONCURRENT_TRADES counter
        (no CE/PE-style split - see this module's own docstring for why).
        Gated on reserved_symbols (a superset of live_positions, claimed
        the instant reservation happens, not just once a fill lands) -
        identical reasoning to Options/position_store.py's reserve_symbol,
        which this is modeled on directly."""
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

    async def remaining_capacity(self) -> int:
        async with self._lock:
            return max(0, config.MAX_CONCURRENT_TRADES - len(self.reserved_symbols))

    async def add_position(self, pos: Position) -> None:
        async with self._lock:
            self.live_positions[pos.underlying_symbol] = pos
            self.reserved_symbols.add(pos.underlying_symbol)
            fire_and_forget(record_opened_position("Swing", pos))
            logger.info(
                "Position OPENED: %s (%s, %s %s) entry=%.2f target=%.2f sl=%.2f qty=%s",
                pos.underlying_symbol, pos.trading_symbol, pos.basket_type, pos.instrument_side,
                pos.entry_price, pos.target_price, pos.hard_stop_loss, pos.quantity,
            )

    async def reconcile_from_broker(self, positions: List[Position]) -> None:
        async with self._lock:
            for pos in positions:
                if pos.underlying_symbol in self.live_positions:
                    continue
                self.live_positions[pos.underlying_symbol] = pos
                self.reserved_symbols.add(pos.underlying_symbol)
                logger.info(
                    "Reconciled existing broker position: %s (%s, %s %s) qty=%s entry_price=%.2f",
                    pos.underlying_symbol, pos.trading_symbol, pos.basket_type, pos.instrument_side,
                    pos.quantity, pos.entry_price,
                )

    async def update_best_price(self, underlying_symbol: str, current_price: float) -> None:
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if pos and is_more_favorable(pos.instrument_side, current_price, pos.best_price):
                pos.best_price = current_price

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

    async def release_order_ownership(self, order_id: str) -> None:
        async with self._lock:
            order = self.orders_today.get(order_id)
            if order:
                order.owned_by_placer = False

    async def try_start_exit(self, underlying_symbol: str) -> bool:
        """Same atomic-claim semantics as Options/position_store.py's own
        try_start_exit - see its docstring. Every code path after a
        successful claim must release it via set_pending_exit_order() or
        record_exit_failure()."""
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
            fire_and_forget(record_closed_trade("Swing", pos))
            self.reserved_symbols.discard(underlying_symbol)
            pnl = unrealized_pnl_rs(pos.instrument_side, pos.entry_price, exit_price, pos.quantity)
            logger.info(
                "Position CLOSED: %s (%s, %s %s) reason=%s exit=%.2f pnl=%.2f",
                pos.underlying_symbol, pos.trading_symbol, pos.basket_type, pos.instrument_side,
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


position_store = SwingPositionStore()
