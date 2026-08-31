"""
In-memory, async-safe state for the Swing strategy - tracks BASKETS (a
futures leg + a PE option leg on the same underlying, entered together
under an all-or-nothing guarantee and meant to be exited together - see
trading_engine.py's own module docstring for the full design) and is
entirely independent from Options/Futures/Luxury's own position_store.py
files - own capacity, own dedup, own state. See watchlist.py for the
separate, simpler watchlist store.

NOTE: intentionally in-memory, same tradeoff as every other
package's own position_store.py in this codebase - resets on restart.
Unlike those, though, Swing baskets are meant to carry across restarts
BY DESIGN (no daily square-off) - trading_engine.reconcile_broker_positions()
is what recovers a still-open basket after a restart, into this same
in-memory store, from the broker's own reported positions.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Set

from . import config
from trade_history import fire_and_forget, record_closed_trade, record_opened_position

logger = logging.getLogger("swing_position_store")


@dataclass
class Leg:
    """One side of a basket - either the futures contract or the PE
    option. Tracked separately (own order_id/entry_price/exit_price) so a
    partial fill/partial exit is always visible, even though the two legs
    are meant to move together.

    Field names deliberately match Options/Futures/Luxury's own Position
    dataclass shape (`option_trading_symbol`, `option_type`, etc.) even
    though a futures leg isn't really an "option" - confirmed by reading
    trade_history.py directly that record_opened_position/
    record_closed_trade/attribute_open_broker_position only ever read
    these exact attribute names generically, nothing assumes "option"
    beyond the field name - so a Leg can be passed to all three completely
    unchanged, no adapter needed. `option_type` is "FUT" for the futures
    leg, "PE" for the option leg - a value neither Options/Futures/
    Luxury's own CE/PE pair ever uses, so a Swing leg's own record in
    /trade-history or /webhook-alerts is always visually distinguishable
    from theirs."""
    underlying_symbol: str
    option_trading_symbol: str      # the leg's own trading_symbol (futures OR option format)
    option_type: str                # "FUT" | "PE"
    quantity: int
    lot_size: int
    entry_price: float
    order_id: str
    product_type: str
    security_id: str = ""
    status: str = "OPEN"            # OPEN | CLOSED
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    opened_at: datetime = field(default_factory=datetime.now)
    closed_at: Optional[datetime] = None
    reconciled: bool = False


@dataclass
class Basket:
    """A futures leg + a PE option leg on the same underlying, entered
    together (all-or-nothing - see trading_engine.enter_basket_for_stock)
    and meant to be exited together (see trading_engine._exit_basket)."""
    underlying_symbol: str
    futures_leg: Leg
    option_leg: Leg
    opened_at: datetime = field(default_factory=datetime.now)
    status: str = "OPEN"            # OPEN | CLOSED
    exit_reason: Optional[str] = None
    closed_at: Optional[datetime] = None


class BasketStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.live_baskets: Dict[str, Basket] = {}
        self.reserved_symbols: Set[str] = set()
        self.closed_baskets_today: List[Basket] = []
        self._trading_day: date = date.today()

    async def maybe_reset_for_new_day(self) -> None:
        """Unlike every other package's own version of this, Swing does
        NOT clear live_baskets on a day change - baskets are explicitly
        meant to carry across multiple days (see config.py's own
        docstring for why there's no SQUARE_OFF_TIME here at all). Only
        the day-scoped closed_baskets_today log resets."""
        async with self._lock:
            today = date.today()
            if today != self._trading_day:
                logger.info(
                    "New trading day detected (%s) - resetting only the daily closed-baskets "
                    "log (live_baskets carries over by design, see config.py).", today,
                )
                self.closed_baskets_today.clear()
                self._trading_day = today

    async def reserve_symbol(self, underlying_symbol: str) -> bool:
        """See Options/position_store.py's reserve_symbol for the full
        race-condition rationale - identical logic here, just keyed by
        underlying only (a basket has no separate option_type dimension
        the way a CE/PE position does)."""
        async with self._lock:
            if underlying_symbol in self.reserved_symbols or underlying_symbol in self.live_baskets:
                return False
            if len(self.reserved_symbols) >= config.MAX_LIVE_BASKETS:
                return False
            self.reserved_symbols.add(underlying_symbol)
            return True

    async def release_symbol(self, underlying_symbol: str) -> None:
        async with self._lock:
            if underlying_symbol not in self.live_baskets:
                self.reserved_symbols.discard(underlying_symbol)

    async def remaining_capacity(self) -> int:
        async with self._lock:
            return max(0, config.MAX_LIVE_BASKETS - len(self.reserved_symbols))

    async def add_basket(self, basket: Basket) -> None:
        async with self._lock:
            self.live_baskets[basket.underlying_symbol] = basket
            self.reserved_symbols.add(basket.underlying_symbol)
            # Fire-and-forget - see Options/position_store.py's identical
            # comment: this is what lets a future restart correctly
            # attribute these exact broker positions back to Swing during
            # reconciliation. Both legs recorded separately.
            fire_and_forget(record_opened_position("Swing", basket.futures_leg))
            fire_and_forget(record_opened_position("Swing", basket.option_leg))
            logger.info(
                "Basket OPENED: %s futures=%s@%.2f option=%s@%.2f",
                basket.underlying_symbol,
                basket.futures_leg.option_trading_symbol, basket.futures_leg.entry_price,
                basket.option_leg.option_trading_symbol, basket.option_leg.entry_price,
            )

    async def reconcile_from_broker(self, baskets: List[Basket]) -> None:
        """Mirrors Options/Futures/Luxury's identical method - imports
        baskets already open at Dhan (already paired/filtered to Swing's
        own by trading_engine.reconcile_broker_positions) into
        live_baskets/reserved_symbols."""
        async with self._lock:
            for basket in baskets:
                if basket.underlying_symbol in self.live_baskets:
                    continue
                self.live_baskets[basket.underlying_symbol] = basket
                self.reserved_symbols.add(basket.underlying_symbol)
                logger.info(
                    "Reconciled existing basket: %s futures=%s (qty=%s) option=%s (qty=%s)",
                    basket.underlying_symbol,
                    basket.futures_leg.option_trading_symbol, basket.futures_leg.quantity,
                    basket.option_leg.option_trading_symbol, basket.option_leg.quantity,
                )

    async def close_basket(
        self, underlying_symbol: str, futures_exit_price: float, option_exit_price: float, reason: str,
    ) -> Optional[Basket]:
        async with self._lock:
            basket = self.live_baskets.pop(underlying_symbol, None)
            if basket is None:
                return None
            closed_at = datetime.now()
            basket.status = "CLOSED"
            basket.exit_reason = reason
            basket.closed_at = closed_at

            basket.futures_leg.status = "CLOSED"
            basket.futures_leg.exit_price = futures_exit_price
            basket.futures_leg.exit_reason = reason
            basket.futures_leg.closed_at = closed_at

            basket.option_leg.status = "CLOSED"
            basket.option_leg.exit_price = option_exit_price
            basket.option_leg.exit_reason = reason
            basket.option_leg.closed_at = closed_at

            self.closed_baskets_today.append(basket)
            # Fire-and-forget - see Options/position_store.py's identical
            # comment / trade_history.py's record_closed_trade docstring:
            # must not be awaited while _lock is held.
            fire_and_forget(record_closed_trade("Swing", basket.futures_leg))
            fire_and_forget(record_closed_trade("Swing", basket.option_leg))
            self.reserved_symbols.discard(underlying_symbol)
            logger.info(
                "Basket CLOSED: %s reason=%s futures_exit=%.2f option_exit=%.2f",
                underlying_symbol, reason, futures_exit_price, option_exit_price,
            )
            return basket

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "live_baskets": [self._basket_dict(b) for b in self.live_baskets.values()],
                "reserved_symbols": sorted(self.reserved_symbols),
                "closed_baskets_today": [self._basket_dict(b) for b in self.closed_baskets_today],
            }

    @staticmethod
    def _basket_dict(b: Basket) -> dict:
        return {
            "underlying_symbol": b.underlying_symbol,
            "status": b.status,
            "exit_reason": b.exit_reason,
            "opened_at": b.opened_at.isoformat() if b.opened_at else None,
            "closed_at": b.closed_at.isoformat() if b.closed_at else None,
            "futures_leg": vars(b.futures_leg) | {
                "opened_at": b.futures_leg.opened_at.isoformat() if b.futures_leg.opened_at else None,
                "closed_at": b.futures_leg.closed_at.isoformat() if b.futures_leg.closed_at else None,
            },
            "option_leg": vars(b.option_leg) | {
                "opened_at": b.option_leg.opened_at.isoformat() if b.option_leg.opened_at else None,
                "closed_at": b.option_leg.closed_at.isoformat() if b.option_leg.closed_at else None,
            },
        }


basket_store = BasketStore()
