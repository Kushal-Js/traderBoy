"""
Paper-trading engine for the Swing strategy - PAPER ONLY.

User request 1 Sep 2026: "Let's enable paper trading for tomorrow for
'Swing' package and keep track of trades, history and profit loss also
like real trades in files and we will evaluate them later tomorrow."

SAFETY INVARIANT: this module must NEVER call `dhan_wrapper.place_market_order`
or `dhan_wrapper.client.order_placement` - the only two real order-
placement entry points reachable through dhan_wrapper. Every Dhan call
here is READ-ONLY: `get_futures_contract`, `get_atm_option` (both just
RESOLVE a contract - trading_symbol/security_id/lot_size - they don't
place an order), `get_option_ltp` (a plain REST LTP fetch, generic
across instrument types despite its option-flavored name - confirmed by
reading Options/dhan_client.py directly: it's a bare `get_ltp_data(names=
[trading_symbol])` call keyed only by trading_symbol, with no option-
specific validation, and Options/dhan_client.py's own get_day_change_pct
already reuses it for plain equity symbols), and (added 1 Sep 2026)
`get_margin_required`/`get_fund_limits` - Dhan's own `/margincalculator`
and `/fundlimit` endpoints, which only ever QUOTE a figure, never place
or reserve anything. A "SWING PAPER ENTRY"/"SWING PAPER EXIT" log line
and an on-disk trade record are the only side effects of a paper trade.

Real margin/funds logging (user request 1 Sep 2026: "make sure we would
also be logging real margin and funds required during paper trading so
that we can do analysis also") - at entry, fetches each leg's real,
Dhan-quoted standalone margin requirement plus the account's fund
snapshot at that moment, purely for later offline analysis (e.g. "would
this basket actually have been affordable, and by how much"). Never
influences whether a paper trade is entered/exited or how its P&L is
computed - a fetch failure here logs a warning and leaves that figure
`None`, it never blocks or invalidates the trade itself. See
get_margin_required's own docstring (Options/dhan_client.py) for why this
is a worst-case, no-hedge-discount figure (Dhan's margin_calculator has
no concept of "combo" margin), and PaperBasket's own field comments for
why the combined total is only ever reported when BOTH legs' figures are
real (never a partial, silently-understated sum).

Deliberately reuses trading_engine.py's REAL entry/exit signal functions
(_evaluate_watchlist_entry_signal/_evaluate_basket_exit_signal, which in
turn use _is_price_confirmed_above_prev_close/_fetch_supertrend_state)
rather than a separate/simplified stand-in - the whole point of paper
trading this is to see how the EXACT signal that would eventually place
real orders actually performs, not to test a different approximation of
it. This module needed NO changes at all when that price gate was
relaxed on 1 Sep 2026 (gap-up -> "at/above prev close") - it calls the
signal functions generically, never the gate itself directly.

Runs as its own poll loop (paper_poll_loop, started from swing_main.py's
lifespan alongside the real monitor_loop) gated by its OWN, independent
flag - config.PAPER_TRADING_ENABLED - completely separate from
config.STRATEGY_ENABLED (the real-money switch, which stays False; see
config.py's own docstring). Both loops can run at once with no real
duplication of REST cost: entry evaluation in the real monitor_loop is
itself gated behind STRATEGY_ENABLED (off for now), and even if both were
ever on together, trading_engine.py's own _supertrend_state_cache/
_price_confirmation_cache are module-level and shared, so a cache hit
from one loop serves the other - only the first caller within a refresh
window ever
pays the REST cost.

Persists via the SAME trade_history.py dated-file convention every other
paper-trading engine in this codebase already uses (see
CopperOptions/paper_engine.py's own PaperTradeStore for the identical
pattern) - its OWN log name (SWING_PAPER_LOG_NAME), a completely separate
file from the REAL trade logs (`real_trades`/`position_opened`) that
basket_store.py's own add_basket/close_basket write to, so a paper trade
can never be mistaken for - or accidentally aggregated with - a real one
when reading history back.

Paper baskets are tracked in their own PaperBasketStore, entirely
separate from the real basket_store.py - no capacity cap the way real
baskets have (config.MAX_LIVE_BASKETS exists to cap REAL FINANCIAL RISK,
which doesn't apply to a simulated trade; the watchlist itself is small
enough - see data/watchlist - that this isn't a meaningful concern
anyway). A symbol already holding a live PAPER basket is simply skipped
on the next entry-signal check (no duplicate paper position on the same
stock), same dedup spirit as the real store's reserve_symbol without
needing the same locking rigor (nothing here is racing another strategy
for real capital).

One completed trade = one JSON record covering BOTH legs (not two
separate leg records the way real trades are logged) - a basket's
futures+PE entry/exit and the pnl for each leg plus the combined total,
all in a single row. Chosen deliberately for tomorrow's stated use case
("we will evaluate them later") - a per-basket row is the natural unit to
eyeball for "did this trade work", where two half-rows per trade are not.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from trade_history import append_jsonl, read_all_jsonl

from . import config, trading_engine
from .dhan_client import dhan_wrapper
from .watchlist import watchlist_store

logger = logging.getLogger("swing_paper_engine")

IST = ZoneInfo(config.MARKET_TZ)


def _now_ist() -> datetime:
    return datetime.now(IST)


@dataclass
class PaperLeg:
    instrument_type: str            # "FUT" | "PE"
    trading_symbol: str
    quantity: int
    lot_size: int
    entry_price: float
    # Real margin Dhan's own /margincalculator would have blocked for
    # THIS leg alone, at entry - added 1 Sep 2026 (user request: "logging
    # real margin and funds required during paper trading so that we can
    # do analysis also"). None if the fetch failed - never a guessed/
    # defaulted 0 (see get_margin_required's own docstring for why a
    # silent 0 would be actively misleading here).
    margin_required: Optional[float] = None
    exit_price: Optional[float] = None


@dataclass
class PaperBasket:
    underlying_symbol: str
    futures_leg: PaperLeg
    option_leg: PaperLeg
    opened_at: datetime = field(default_factory=_now_ist)
    # Naive sum of both legs' own standalone margin_required - the
    # "practical rule" from the trading-skills repo's own
    # basket-order-feasibility.md: budget for the worst case (no combo/
    # SPAN-hedge discount assumed), since Dhan's margin_calculator has no
    # way to ask "what would the COMBINED margin be." None unless BOTH
    # legs' own margin_required were fetched successfully - a partial sum
    # would understate the real requirement without saying so.
    total_margin_required: Optional[float] = None
    # The account's full get_fund_limits() snapshot taken at entry time -
    # so a later analysis can check "was this basket actually affordable
    # right then," not just look at the requirement in isolation. Kept as
    # the raw dict (Dhan's own field names/casing) rather than picking
    # out one key - see get_fund_limits' own docstring.
    account_funds_snapshot: Optional[dict] = None


SWING_PAPER_LOG_NAME = "swing_paper_trades"


class PaperBasketStore:
    """See this module's own docstring for the persistence/record-shape
    rationale. Guarded by a lock for basic consistency with the rest of
    this codebase's style, even though nothing here races another
    strategy for real capital the way the real basket_store does."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.live_baskets: Dict[str, PaperBasket] = {}
        self.completed: List[dict] = read_all_jsonl(SWING_PAPER_LOG_NAME)

    async def has_open(self, symbol: str) -> bool:
        async with self._lock:
            return symbol in self.live_baskets

    async def add_basket(self, basket: PaperBasket) -> None:
        async with self._lock:
            self.live_baskets[basket.underlying_symbol] = basket

    async def symbols_with_open_baskets(self) -> List[str]:
        async with self._lock:
            return list(self.live_baskets.keys())

    async def close_basket(
        self, symbol: str, futures_exit_price: float, option_exit_price: float, reason: str,
    ) -> Optional[dict]:
        async with self._lock:
            basket = self.live_baskets.pop(symbol, None)
            if basket is None:
                return None
            closed_at = _now_ist()
            futures_pnl = (futures_exit_price - basket.futures_leg.entry_price) * basket.futures_leg.quantity
            option_pnl = (option_exit_price - basket.option_leg.entry_price) * basket.option_leg.quantity
            trade = {
                "underlying_symbol": symbol,
                "opened_at": basket.opened_at.isoformat(),
                "closed_at": closed_at.isoformat(),
                "exit_reason": reason,
                "futures_trading_symbol": basket.futures_leg.trading_symbol,
                "futures_quantity": basket.futures_leg.quantity,
                "futures_entry_price": basket.futures_leg.entry_price,
                "futures_exit_price": futures_exit_price,
                "futures_pnl": futures_pnl,
                "futures_margin_required": basket.futures_leg.margin_required,
                "option_trading_symbol": basket.option_leg.trading_symbol,
                "option_quantity": basket.option_leg.quantity,
                "option_entry_price": basket.option_leg.entry_price,
                "option_exit_price": option_exit_price,
                "option_pnl": option_pnl,
                "option_margin_required": basket.option_leg.margin_required,
                "total_pnl": futures_pnl + option_pnl,
                # See PaperBasket's own field comments - both None unless
                # the underlying REST calls succeeded at entry time; never
                # guessed/defaulted.
                "total_margin_required": basket.total_margin_required,
                "account_funds_snapshot_at_entry": basket.account_funds_snapshot,
            }
            self.completed.append(trade)
            append_jsonl(SWING_PAPER_LOG_NAME, trade)
            logger.info(
                "%s: SWING PAPER EXIT (no real order placed) reason=%s futures_pnl=%.2f "
                "option_pnl=%.2f total_pnl=%.2f",
                symbol, reason, futures_pnl, option_pnl, trade["total_pnl"],
            )
            return trade

    async def snapshot(self, limit: int = 50) -> dict:
        async with self._lock:
            recent = list(reversed(self.completed))[:limit]
            total_pnl = sum(t["total_pnl"] for t in self.completed)
            wins = sum(1 for t in self.completed if t["total_pnl"] > 0)
            return {
                "paper_trading_enabled": config.PAPER_TRADING_ENABLED,
                "live_paper_baskets": [self._basket_dict(b) for b in self.live_baskets.values()],
                "total_completed_paper_trades": len(self.completed),
                "pnl_total": total_pnl,
                "win_rate": (wins / len(self.completed)) if self.completed else None,
                "recent_trades": recent,
            }

    @staticmethod
    def _basket_dict(b: PaperBasket) -> dict:
        return {
            "underlying_symbol": b.underlying_symbol,
            "opened_at": b.opened_at.isoformat() if b.opened_at else None,
            "futures_leg": vars(b.futures_leg),
            "option_leg": vars(b.option_leg),
            "total_margin_required": b.total_margin_required,
            "account_funds_snapshot_at_entry": b.account_funds_snapshot,
        }


paper_basket_store = PaperBasketStore()


# --------------------------------------------------------------------------- #
# Simulated entry/exit - no real orders, ever (see module docstring)
# --------------------------------------------------------------------------- #
async def _enter_paper_basket(symbol: str) -> None:
    """Simulates a basket entry at current LTP for both legs. If EITHER
    leg's contract lookup or LTP fetch fails, the whole attempt is simply
    abandoned with nothing recorded - unlike the real all-or-nothing
    entry, there's no compensating rollback to do here, since nothing was
    ever actually "entered" (persisted) until BOTH legs have priced
    successfully.

    Margin (per leg, via get_margin_required) and the account's fund
    snapshot (via get_fund_limits) are fetched separately, AFTER both
    legs are already priced - added 1 Sep 2026 (user request: "logging
    real margin and funds required during paper trading so that we can
    do analysis also"). Deliberately best-effort: a failure here logs a
    warning and leaves that figure `None` rather than aborting the whole
    paper entry - unlike price, margin/funds data is for offline analysis
    only, not something the simulated P&L depends on, so it must never
    block or invalidate an otherwise-good paper trade."""
    loop = asyncio.get_running_loop()
    try:
        fut = await loop.run_in_executor(None, dhan_wrapper.get_futures_contract, symbol)
        fut_ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, fut.trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception("%s: SWING PAPER entry skipped - could not resolve/price the futures leg", symbol)
        return

    try:
        atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, "PE")
        option_ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, atm.trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: SWING PAPER entry skipped - could not resolve/price the PE leg "
            "(nothing was recorded for either leg)", symbol,
        )
        return

    fut_qty = fut.lot_size * config.QUANTITY_LOTS
    option_qty = atm.lot_size * config.QUANTITY_LOTS

    futures_margin: Optional[float] = None
    try:
        futures_margin_data = await loop.run_in_executor(
            None, dhan_wrapper.get_margin_required,
            fut.security_id, "NSE_FNO", "BUY", fut_qty, config.FUTURES_PRODUCT, fut_ltp,
        )
        futures_margin = futures_margin_data.get("totalMargin")
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: could not fetch margin required for the futures leg - the paper trade still "
            "proceeds, this figure will read as null for analysis", symbol,
        )

    option_margin: Optional[float] = None
    try:
        option_margin_data = await loop.run_in_executor(
            None, dhan_wrapper.get_margin_required,
            atm.security_id, "NSE_FNO", "BUY", option_qty, config.OPTIONS_PRODUCT, option_ltp,
        )
        option_margin = option_margin_data.get("totalMargin")
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: could not fetch margin required for the PE leg - the paper trade still "
            "proceeds, this figure will read as null for analysis", symbol,
        )

    account_funds_snapshot: Optional[dict] = None
    try:
        account_funds_snapshot = await loop.run_in_executor(None, dhan_wrapper.get_fund_limits)
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: could not fetch the account's fund limits at entry - the paper trade still "
            "proceeds, this snapshot will read as null for analysis", symbol,
        )

    # A partial sum (only one leg's margin known) would UNDERSTATE the
    # real requirement without saying so - only sum when both are real.
    total_margin_required = (
        futures_margin + option_margin if futures_margin is not None and option_margin is not None else None
    )

    basket = PaperBasket(
        underlying_symbol=symbol,
        futures_leg=PaperLeg("FUT", fut.trading_symbol, fut_qty, fut.lot_size, fut_ltp, margin_required=futures_margin),
        option_leg=PaperLeg("PE", atm.trading_symbol, option_qty, atm.lot_size, option_ltp, margin_required=option_margin),
        total_margin_required=total_margin_required,
        account_funds_snapshot=account_funds_snapshot,
    )
    await paper_basket_store.add_basket(basket)
    logger.info(
        "%s: SWING PAPER ENTRY (no real order placed) - futures %s@%.2f (margin=%s), "
        "PE %s@%.2f (margin=%s), total_margin_required=%s",
        symbol, fut.trading_symbol, fut_ltp, futures_margin,
        atm.trading_symbol, option_ltp, option_margin, total_margin_required,
    )


async def _exit_paper_basket(symbol: str, reason: str) -> None:
    """Simulates closing both legs at current LTP. Unlike entry, a
    fetch failure here falls back to the leg's own entry price rather
    than abandoning the exit - a paper basket that's already "open"
    should always close on signal, the same way a real basket's exit
    always attempts both legs regardless of one failing (see
    trading_engine._exit_basket's own comment) - the fallback is flagged
    loudly via the exception log, not silently accepted as accurate."""
    basket = paper_basket_store.live_baskets.get(symbol)
    if basket is None:
        return
    loop = asyncio.get_running_loop()
    try:
        fut_exit_price = await loop.run_in_executor(
            None, dhan_wrapper.get_option_ltp, basket.futures_leg.trading_symbol
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: could not fetch exit LTP for the futures leg - falling back to entry price "
            "so the paper trade still closes (pnl for this leg will read as 0)", symbol,
        )
        fut_exit_price = basket.futures_leg.entry_price
    try:
        option_exit_price = await loop.run_in_executor(
            None, dhan_wrapper.get_option_ltp, basket.option_leg.trading_symbol
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: could not fetch exit LTP for the PE leg - falling back to entry price "
            "so the paper trade still closes (pnl for this leg will read as 0)", symbol,
        )
        option_exit_price = basket.option_leg.entry_price

    await paper_basket_store.close_basket(symbol, fut_exit_price, option_exit_price, reason)


async def paper_poll_loop() -> None:
    """Runs forever, ALWAYS - same "always running, internally gated"
    pattern as trading_engine.monitor_loop() (see config.py), so flipping
    config.PAPER_TRADING_ENABLED later never needs a restart. Reuses the
    SAME watchlist (data/watchlist / the watchlist webhook) the real
    monitor loop reads - deliberately does NOT remove a symbol from that
    shared watchlist on a paper entry (unlike the real loop's auto-entry),
    so paper trading never interferes with what the real loop would later
    see once STRATEGY_ENABLED is flipped on; a symbol already holding an
    open paper basket is simply skipped on the entry-signal check
    instead."""
    logger.info(
        "Swing PAPER-trading poll loop started (PAPER ONLY - no real orders will ever be placed). "
        "paper_trading_enabled=%s", config.PAPER_TRADING_ENABLED,
    )
    while True:
        try:
            if config.PAPER_TRADING_ENABLED:
                watchlist_symbols = await watchlist_store.symbols()
                open_paper_symbols = set(await paper_basket_store.symbols_with_open_baskets())
                for i, symbol in enumerate(watchlist_symbols):
                    if i > 0:
                        await asyncio.sleep(0.35)
                    if symbol in open_paper_symbols:
                        continue
                    if await trading_engine._evaluate_watchlist_entry_signal(symbol):
                        await _enter_paper_basket(symbol)

                for symbol in await paper_basket_store.symbols_with_open_baskets():
                    reason = await trading_engine._evaluate_basket_exit_signal(symbol, None)
                    if reason:
                        await _exit_paper_basket(symbol, reason)
        except Exception:  # noqa: BLE001
            logger.exception("Error in Swing PAPER-trading poll loop tick")
        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)
