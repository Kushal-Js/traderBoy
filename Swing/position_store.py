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

Also tracks SEQUENTIAL positions (added 1 Sep 2026, user request - see
`SequentialPositionStore` below) - the alternate "2 different orders
running sequentially" strategy shape - and BASKET_HEDGE positions (added
1 Sep 2026, user request - see `BasketHedgeStore` below) - basket entry,
but the exit sells the basket and buys a single standalone PE hedge
instead of going flat. All switched via config.STRATEGY_MODE ("basket" |
"sequential" | "basket_hedge") rather than one replacing another, since
"we may need basket strategy again in coming days." All three stores
always exist; only the one matching the current mode is ever written to
by trading_engine.py's monitor_loop (see its own docstring for the full
mode-dispatch).
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


class SequentialPositionStore:
    """State for config.STRATEGY_MODE == "sequential" (user request
    1 Sep 2026: "now it won't be a basket order but 2 different orders
    running sequentially"). Unlike a Basket (always BOTH legs at once),
    a symbol here holds AT MOST ONE leg at a time - `live_legs` maps
    underlying_symbol -> the currently-held Leg, whose own `option_type`
    ("FUT" | "PE") says which instrument that is. No leg at all for a
    symbol means it's in the NONE/watching state.

    Reuses the exact same `Leg` dataclass basket mode uses (unchanged) -
    record_opened_position/record_closed_trade/attribute_open_broker_
    position already read it generically, no new shape needed.

    Capacity is shared conceptually with basket mode's own
    config.MAX_LIVE_BASKETS (a symbol under active sequential management -
    whichever leg it currently holds - occupies one "slot", same as one
    live basket does) rather than inventing a second, redundant cap -
    only one mode's store is ever actually written to at a time (the
    other mode's monitor-loop branch never runs), so there's no risk of
    the two competing for the same numeric budget in practice.

    Reservation lifecycle (see trading_engine.py's own state-machine
    docstring for the full transition diagram):
      - try_enter(): NONE -> FUTURES (the only transition that claims a
        NEW capacity slot).
      - swap_leg(): FUTURES -> PE, or PE -> FUTURES (the "loop" itself -
        does NOT touch the reservation, since the symbol stays under
        active management throughout).
      - exit_to_watching(): PE -> NONE (the PE loss-cap exit, user
        confirmed via AskUserQuestion 1 Sep 2026: returns to watching
        for a fresh entry signal, does NOT blindly re-buy futures) -
        the ONLY transition that RELEASES the slot, freeing it for a
        different symbol (or this same one again later, once its entry
        condition next fires)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.live_legs: Dict[str, Leg] = {}
        self.reserved_symbols: Set[str] = set()
        self.closed_legs_today: List[Leg] = []
        self._trading_day: date = date.today()

    async def maybe_reset_for_new_day(self) -> None:
        """Same choice as BasketStore's own identical method - live_legs
        carries across a day boundary by design (no EOD square-off here
        either); only the day-scoped closed_legs_today log resets."""
        async with self._lock:
            today = date.today()
            if today != self._trading_day:
                logger.info(
                    "New trading day detected (%s) - resetting only the daily closed-legs "
                    "log (live_legs carries over by design, see config.py).", today,
                )
                self.closed_legs_today.clear()
                self._trading_day = today

    async def try_enter(self, underlying_symbol: str) -> bool:
        """NONE -> FUTURES: claims a fresh capacity slot for a symbol not
        currently under active sequential management at all. See
        BasketStore.reserve_symbol's identical race-condition rationale."""
        async with self._lock:
            if underlying_symbol in self.reserved_symbols or underlying_symbol in self.live_legs:
                return False
            if len(self.reserved_symbols) >= config.MAX_LIVE_BASKETS:
                return False
            self.reserved_symbols.add(underlying_symbol)
            return True

    async def release_symbol(self, underlying_symbol: str) -> None:
        """Undoes try_enter() when the futures BUY didn't end up
        happening (order rejected, exception, etc.) - mirrors
        BasketStore.release_symbol."""
        async with self._lock:
            if underlying_symbol not in self.live_legs:
                self.reserved_symbols.discard(underlying_symbol)

    async def remaining_capacity(self) -> int:
        async with self._lock:
            return max(0, config.MAX_LIVE_BASKETS - len(self.reserved_symbols))

    async def set_leg(self, leg: Leg) -> None:
        """Records the leg now held for leg.underlying_symbol - used both
        for the very first FUTURES entry (after try_enter) and for each
        swap within the loop (FUTURES->PE or PE->FUTURES, after the OLD
        leg has already been closed via swap_leg's own close half). Fires
        record_opened_position exactly as BasketStore.add_basket does -
        this is what lets a restart correctly recover an in-progress
        sequential position (see trading_engine.reconcile_sequential_
        positions)."""
        async with self._lock:
            self.live_legs[leg.underlying_symbol] = leg
            self.reserved_symbols.add(leg.underlying_symbol)
            fire_and_forget(record_opened_position("Swing", leg))
            logger.info(
                "Sequential leg OPENED: %s %s %s@%.2f",
                leg.underlying_symbol, leg.option_type, leg.option_trading_symbol, leg.entry_price,
            )

    async def reconcile_leg(self, leg: Leg) -> None:
        """Startup-only: imports a leg already open at Dhan (recovered
        after a restart mid-loop) - mirrors BasketStore.reconcile_from_broker,
        but for a single leg rather than a pair. See trading_engine.
        reconcile_sequential_positions for how a lone Swing-attributed
        broker position is routed here specifically when
        config.STRATEGY_MODE == "sequential" (routed to the basket
        reconciliation's own "unpaired leg" warning instead when the
        mode is "basket" - a lone leg means something different in each
        mode)."""
        async with self._lock:
            if leg.underlying_symbol in self.live_legs:
                return
            self.live_legs[leg.underlying_symbol] = leg
            self.reserved_symbols.add(leg.underlying_symbol)
            logger.info(
                "Reconciled existing sequential leg: %s %s %s (qty=%s avg_price=%.2f)",
                leg.underlying_symbol, leg.option_type, leg.option_trading_symbol,
                leg.quantity, leg.entry_price,
            )

    async def close_leg_for_swap(self, underlying_symbol: str, exit_price: float, reason: str) -> Optional[Leg]:
        """Closes the CURRENTLY held leg as part of a swap (FUTURES->PE
        or PE->FUTURES) - fires record_closed_trade, logs to
        closed_legs_today, but does NOT release the symbol's reservation,
        since it's about to hold the OTHER instrument (still under
        active management). Caller MUST follow this with set_leg() for
        the new leg - if the new leg's own entry then fails, the caller
        is responsible for deciding whether to fall back to
        release_symbol() (see trading_engine.py's own swap functions for
        the "fail safe to flat" choice made there)."""
        async with self._lock:
            leg = self.live_legs.pop(underlying_symbol, None)
            if leg is None:
                return None
            self._close_leg_fields(leg, exit_price, reason)
            self.closed_legs_today.append(leg)
            fire_and_forget(record_closed_trade("Swing", leg))
            logger.info(
                "Sequential leg CLOSED (swap): %s %s reason=%s exit=%.2f",
                underlying_symbol, leg.option_type, reason, exit_price,
            )
            return leg

    async def exit_to_watching(self, underlying_symbol: str, exit_price: float, reason: str) -> Optional[Leg]:
        """PE -> NONE: the loss-cap exit. Closes the leg AND releases the
        symbol's reservation - the only transition that frees capacity -
        so the symbol returns to plain watching (a later fresh entry
        signal re-enters it via try_enter, same as any never-before-seen
        symbol)."""
        async with self._lock:
            leg = self.live_legs.pop(underlying_symbol, None)
            if leg is None:
                return None
            self._close_leg_fields(leg, exit_price, reason)
            self.closed_legs_today.append(leg)
            fire_and_forget(record_closed_trade("Swing", leg))
            self.reserved_symbols.discard(underlying_symbol)
            logger.info(
                "Sequential leg CLOSED (back to watching): %s %s reason=%s exit=%.2f",
                underlying_symbol, leg.option_type, reason, exit_price,
            )
            return leg

    @staticmethod
    def _close_leg_fields(leg: Leg, exit_price: float, reason: str) -> None:
        leg.status = "CLOSED"
        leg.exit_price = exit_price
        leg.exit_reason = reason
        leg.closed_at = datetime.now()

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "live_legs": [self._leg_dict(leg) for leg in self.live_legs.values()],
                "reserved_symbols": sorted(self.reserved_symbols),
                "closed_legs_today": [self._leg_dict(leg) for leg in self.closed_legs_today],
            }

    @staticmethod
    def _leg_dict(leg: Leg) -> dict:
        return vars(leg) | {
            "opened_at": leg.opened_at.isoformat() if leg.opened_at else None,
            "closed_at": leg.closed_at.isoformat() if leg.closed_at else None,
        }


sequential_store = SequentialPositionStore()


@dataclass
class BasketHedgePosition:
    """One symbol's current state under config.STRATEGY_MODE ==
    "basket_hedge" (user request 1 Sep 2026: "enabling basket buy
    strategy but with a caveat"). `state` is "BASKET" (holding the
    original entry) or "PE_HEDGE" (holding the standalone PE bought
    after the basket's own exit condition fired).

    `legs` is normally [futures_leg, option_leg] in BASKET state (both
    bought together, all-or-nothing, exactly like plain "basket" mode's
    own entry) and [pe_leg] in PE_HEDGE state - EXCEPT for the one real
    position grandfathered in from sequential mode at the exact moment
    this mode went live (APLAPOLLO, futures-only - user's own words:
    "consider the open trade as a basket order for this time as it is
    already live"), which sits in BASKET state with just [futures_leg]
    until it naturally reaches its own exit condition. See
    reconcile_basket_hedge_positions()'s own docstring for why a lone
    leg is accepted here rather than flagged as an anomaly the way plain
    "basket" mode's own reconciliation treats one."""
    underlying_symbol: str
    state: str  # "BASKET" | "PE_HEDGE"
    legs: List[Leg]
    opened_at: datetime = field(default_factory=datetime.now)


class BasketHedgeStore:
    """See BasketHedgePosition's own docstring for the state shape.
    Capacity/dedup follow the exact same pattern as BasketStore/
    SequentialPositionStore above - one symbol occupies one slot for as
    long as it's under active management in EITHER state, released only
    when the PE hedge phase exits back to plain watching."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.live_positions: Dict[str, BasketHedgePosition] = {}
        self.reserved_symbols: Set[str] = set()
        self.closed_today: List[dict] = []
        self._trading_day: date = date.today()

    async def maybe_reset_for_new_day(self) -> None:
        async with self._lock:
            today = date.today()
            if today != self._trading_day:
                logger.info(
                    "New trading day detected (%s) - resetting only the daily closed log "
                    "(live_positions carries over by design, see config.py).", today,
                )
                self.closed_today.clear()
                self._trading_day = today

    async def try_enter(self, underlying_symbol: str) -> bool:
        """NONE -> BASKET: claims a fresh capacity slot for a symbol not
        currently under active management at all."""
        async with self._lock:
            if underlying_symbol in self.reserved_symbols or underlying_symbol in self.live_positions:
                return False
            if len(self.reserved_symbols) >= config.MAX_LIVE_BASKETS:
                return False
            self.reserved_symbols.add(underlying_symbol)
            return True

    async def release_symbol(self, underlying_symbol: str) -> None:
        async with self._lock:
            if underlying_symbol not in self.live_positions:
                self.reserved_symbols.discard(underlying_symbol)

    async def remaining_capacity(self) -> int:
        async with self._lock:
            return max(0, config.MAX_LIVE_BASKETS - len(self.reserved_symbols))

    async def set_basket(self, underlying_symbol: str, legs: List[Leg]) -> None:
        """Records the BASKET state (the initial entry, or a startup
        reconciliation) - fires record_opened_position for every leg
        given (1 for the grandfathered position, 2 for a normal
        all-or-nothing entry)."""
        async with self._lock:
            self.live_positions[underlying_symbol] = BasketHedgePosition(
                underlying_symbol=underlying_symbol, state="BASKET", legs=list(legs),
            )
            self.reserved_symbols.add(underlying_symbol)
            for leg in legs:
                fire_and_forget(record_opened_position("Swing", leg))
            logger.info(
                "BasketHedge BASKET OPENED: %s legs=%s",
                underlying_symbol, [(l.option_type, l.option_trading_symbol, l.entry_price) for l in legs],
            )

    async def reconcile_position(self, position: BasketHedgePosition) -> None:
        """Startup-only: imports a position already open at Dhan
        (recovered after a restart) - mirrors BasketStore/
        SequentialPositionStore's own reconcile methods."""
        async with self._lock:
            if position.underlying_symbol in self.live_positions:
                return
            self.live_positions[position.underlying_symbol] = position
            self.reserved_symbols.add(position.underlying_symbol)
            logger.info(
                "Reconciled existing basket_hedge position: %s state=%s legs=%s",
                position.underlying_symbol, position.state,
                [(l.option_type, l.option_trading_symbol, l.quantity, l.entry_price) for l in position.legs],
            )

    async def close_current_legs_for_hedge_swap(
        self, underlying_symbol: str, exit_prices: Dict[str, float], reason: str,
    ) -> Optional[BasketHedgePosition]:
        """BASKET -> (about to be) PE_HEDGE: closes every leg CURRENTLY
        held (1 or 2, see BasketHedgePosition's own docstring), fires
        record_closed_trade for each, but does NOT release the symbol's
        reservation - it's about to hold the PE hedge instead, still
        under active management. `exit_prices` keyed by each leg's own
        `option_trading_symbol`. Caller MUST follow this with
        set_pe_hedge() for the new leg."""
        async with self._lock:
            position = self.live_positions.pop(underlying_symbol, None)
            if position is None:
                return None
            closed_at = datetime.now()
            for leg in position.legs:
                exit_price = exit_prices.get(leg.option_trading_symbol, leg.entry_price)
                leg.status = "CLOSED"
                leg.exit_price = exit_price
                leg.exit_reason = reason
                leg.closed_at = closed_at
                fire_and_forget(record_closed_trade("Swing", leg))
            self.closed_today.append(self._position_dict(position))
            logger.info(
                "BasketHedge BASKET CLOSED (swap to PE hedge): %s reason=%s", underlying_symbol, reason,
            )
            return position

    async def set_pe_hedge(self, underlying_symbol: str, pe_leg: Leg) -> None:
        async with self._lock:
            self.live_positions[underlying_symbol] = BasketHedgePosition(
                underlying_symbol=underlying_symbol, state="PE_HEDGE", legs=[pe_leg],
            )
            self.reserved_symbols.add(underlying_symbol)
            fire_and_forget(record_opened_position("Swing", pe_leg))
            logger.info(
                "BasketHedge PE_HEDGE OPENED: %s %s@%.2f",
                underlying_symbol, pe_leg.option_trading_symbol, pe_leg.entry_price,
            )

    async def exit_to_watching(self, underlying_symbol: str, exit_price: float, reason: str) -> Optional[Leg]:
        """PE_HEDGE -> NONE: the final exit (any of the 3 conditions).
        Closes the PE leg AND releases the symbol's reservation - back to
        plain watching for a fresh basket entry."""
        async with self._lock:
            position = self.live_positions.pop(underlying_symbol, None)
            if position is None:
                return None
            leg = position.legs[0]
            leg.status = "CLOSED"
            leg.exit_price = exit_price
            leg.exit_reason = reason
            leg.closed_at = datetime.now()
            fire_and_forget(record_closed_trade("Swing", leg))
            self.closed_today.append(self._position_dict(position))
            self.reserved_symbols.discard(underlying_symbol)
            logger.info(
                "BasketHedge PE_HEDGE CLOSED (back to watching): %s reason=%s exit=%.2f",
                underlying_symbol, reason, exit_price,
            )
            return leg

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "live_positions": [self._position_dict(p) for p in self.live_positions.values()],
                "reserved_symbols": sorted(self.reserved_symbols),
                "closed_today": list(self.closed_today),
            }

    @staticmethod
    def _position_dict(position: BasketHedgePosition) -> dict:
        return {
            "underlying_symbol": position.underlying_symbol,
            "state": position.state,
            "opened_at": position.opened_at.isoformat() if position.opened_at else None,
            "legs": [
                vars(leg) | {
                    "opened_at": leg.opened_at.isoformat() if leg.opened_at else None,
                    "closed_at": leg.closed_at.isoformat() if leg.closed_at else None,
                }
                for leg in position.legs
            ],
        }


basket_hedge_store = BasketHedgeStore()
