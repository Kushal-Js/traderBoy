"""
Core strategy logic for the Swing package - user request 31 Aug 2026.

  - enter_basket_for_stock(): the all-or-nothing basket placement - buys
    1 lot of the underlying's nearest-expiry FUTURES contract, then 1 lot
    of its ATM PE option, as two independent real orders (Dhan has no
    native basket-order API - see the separate trading-skills repo's
    `basket-order-feasibility.md` for the full investigation this design
    is based on). "All-or-nothing" ("do both of these or neither" - the
    user's own framing) is enforced at the APPLICATION level via a
    compensating rollback, since no broker call gives real atomicity
    across two different instruments:
      - futures leg fails -> the PE leg is never attempted at all
        ("neither").
      - futures leg succeeds but the PE leg then fails -> the futures leg
        is immediately SOLD to unwind it ("neither", via a best-effort
        compensating transaction - the standard "saga" pattern for
        exactly this situation).
    A failed unwind is logged as an ERROR, loudly - it means a real,
    un-hedged futures position may still be open and needs manual
    handling; this code never pretends otherwise.
  - reconcile_broker_positions(): pairs up any FUTSTK + OPTSTK broker
    positions attributed to "Swing" (via
    trade_history.attribute_open_broker_position - same mechanism
    Options/Futures/Luxury already use to avoid double-tracking each
    other's real positions) back into a Basket at startup. Especially
    important here since Swing baskets are meant to carry across
    restarts/days by design (no EOD square-off, unlike every other
    package) - a restart must never lose track of a real, still-open
    basket. A leg found with no matching counterpart (one side attributed
    to Swing, the other missing) is logged as a clear warning and left
    alone rather than guessed at - same never-guess philosophy
    attribute_open_broker_position itself follows.
  - Entry/exit signal (added 31 Aug 2026, user request; ENTRY's price gate
    RELAXED from a strict gap-up to a broader "at/above yesterday's
    close" check on 1 Sep 2026) - a price-confirmation gate plus a
    dual-timeframe Supertrend crossover on the underlying's own STOCK
    price:
      ENTRY: today's price is confirmed at or above yesterday's close -
      checked via `_is_price_confirmed_above_prev_close()`, true as soon
      as EITHER today's open >= yesterday's close (checked once, at
      market open) OR the current price has, at any point since, reached
      or crossed above yesterday's close (checked on later ticks until
      it fires - see that function's own docstring for the exact
      caching/latching behavior) - AND the 5-min close crosses ABOVE the
      5-min Supertrend, AND the 1-min close is above (or has itself just
      crossed above) the 1-min Supertrend - the two Supertrend conditions
      each read on their own most recently fully-closed candle (user's
      own wording, 1 Sep 2026: "an explicit gap up is not mandatory,
      however when market opens stock price should rise or be greater or
      equal than yesterday's stock close price or the entry condition
      becomes active when current price cross above yesterday close
      price... Rest other conditions should remain as it is").
      EXIT: the 5-min close crosses BELOW the 5-min Supertrend ("5 min
      close price cross below super trend" - unchanged since first
      defined).
      Both legs of a basket are always entered/exited together (the
      all-or-nothing entry above; `_exit_basket()` below) - this signal
      change only touches WHEN a basket is triggered, never WHICH legs
      move, so that guarantee is untouched by this update (re-confirmed
      by the user 1 Sep 2026: "Entry and Exit for both legs of same
      trade at the same time is to be honored").
    `_fetch_supertrend_state()` is a SELF-CONTAINED dual-timeframe
    crossover detector, deliberately NOT built on top of
    Options/dhan_client.py's own single-timeframe Supertrend cache/
    refresh mechanism (`refresh_supertrend_signal`/
    `get_cached_supertrend_bearish`) - that cache is keyed by underlying
    only (one timeframe's state per symbol) and is already live,
    real-money exit protection for Options/Futures/Luxury; extending it
    to carry a second timeframe risked that shared, already-relied-upon
    path for no good reason. This file's own cache
    (`_supertrend_state_cache`) is entirely independent - reuses the same
    PURE `_compute_supertrend` function and the same
    `intraday_minute_data` REST call shape, just parameterized by
    interval and keeping the last TWO closed candles (not just one) so
    an actual crossover (a state CHANGE between consecutive candles) can
    be detected, not merely a current side. `_is_price_confirmed_above_prev_close()`
    similarly reuses `Options/dhan_client.py`'s `get_today_open_and_prev_close()`
    (the same OHLC quote `get_day_change_pct()` already fetches) for the
    once-per-day open check, and the already-generic `get_option_ltp()`
    for the cheaper intraday-crossover check on later ticks.
    `_evaluate_watchlist_entry_signal()`/`_evaluate_basket_exit_signal()`
    apply the rules above; `monitor_loop()` calls them every tick
    once `config.STRATEGY_ENABLED` is on, entering/exiting exactly as
    described - a successful auto-entry also removes the symbol from the
    watchlist (no reason to keep evaluating a stock for entry once it has
    a live basket).

SEQUENTIAL mode (added 1 Sep 2026, user request - see config.py's own
STRATEGY_MODE docstring for why both this and the basket design above
coexist rather than one replacing the other): "now it won't be a basket
order but 2 different orders running sequentially." A symbol holds AT
MOST ONE leg at a time (see position_store.SequentialPositionStore),
looping between futures and a PE hedge:

    NONE --(entry signal)--------------------------> FUTURES
    FUTURES --(exit signal: 5-min crossed below ST)-> PE
    PE --(entry signal re-fires)---------------------> FUTURES   [loop]
    PE --(unrealized loss > config.PE_MAX_LOSS_RS)---> NONE (watching)

The entry/exit SIGNAL itself (_evaluate_watchlist_entry_signal/
_evaluate_basket_exit_signal, both below) is IDENTICAL between the two
modes - only what happens once a signal fires differs. Two points were
genuinely ambiguous in the user's own wording ("Exit this PE option
contract once loss become more than 2k or entry condition are met
again, then buy future contract again at market price") and were
confirmed explicitly via AskUserQuestion before building this:
  1. A PE loss-cap exit returns the symbol to plain WATCHING (does NOT
     blindly re-buy futures) - only the entry-condition-refire path
     does that, since it's the only one with an actual fresh, confirmed
     signal backing the re-entry.
  2. Paper trading (paper_engine.py) mirrors whichever mode
     (config.STRATEGY_MODE) is currently active, rather than staying
     pinned to basket mode - so paper results always reflect what real
     trading would do if turned on right now.
_enter_futures_for_stock/_swap_futures_to_pe/_exit_pe_to_watching/
_swap_pe_to_futures implement the four transitions above; each failure
path is handled "fail safe to flat" (see each function's own docstring) -
a leg that can't be closed is left exactly as-is for the next tick to
retry, but once a leg IS closed, a failure to open the NEXT leg leaves
the symbol with zero real exposure (capacity released) rather than
stuck in a half-transitioned state, since a real, un-hedged futures
position surviving unintentionally would be the actually dangerous
outcome, not a missed re-entry.

Deliberately does NOT participate in cross_strategy_registry.py (the
shared per-symbol lock Options/Futures/Luxury use to stop two of THEM
racing for the same underlying) - user decision 1 Sep 2026: Swing is an
"independent strategy" and gets its own separate live-basket tracking
instead of sharing that registry. (It briefly did participate, from this
package's own creation on 31 Aug 2026 until this date - see git history/
NOTES.md entry #66 if you're wondering why an older comment or test
mentions it.)

basket_store.py's own `reserve_symbol()`/`release_symbol()` (an atomic
check-and-set under BasketStore's own asyncio.Lock, entirely separate
from Options/Futures/Luxury's own PositionStore instances) already IS
that separate claim mechanism - it fully protects against a SWING-vs-
SWING double entry on the same symbol (e.g. the watchlist-driven
monitor_loop and a manual webhook call racing each other), which is all
this package's own entry path ever needed. What it deliberately no
longer protects against: Swing and Options (or Futures/Luxury)
independently entering the SAME underlying stock at the same instant -
since 1 Sep 2026 that's an accepted possibility, not a bug, given Swing
trades a completely different instrument combination (futures + PE
hedge, not a bare CE/PE) with its own capital pool.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from trade_history import append_jsonl, attribute_open_broker_position

from . import config
from .dhan_client import OrderStatus, _compute_supertrend, _retry, dhan_wrapper
from .position_store import (
    Basket,
    BasketHedgePosition,
    Leg,
    basket_hedge_store,
    basket_store,
    sequential_store,
)
from .watchlist import watchlist_store

logger = logging.getLogger("swing_trading_engine")

IST = ZoneInfo(config.MARKET_TZ)


def _now_ist() -> datetime:
    return datetime.now(IST)


def _gen_tag(prefix: str, symbol: str) -> str:
    """See Options/trading_engine.py's identical helper - same DH-905
    special-character rationale (GVT&D)."""
    safe_symbol = re.sub(r"[^A-Za-z0-9]", "", symbol)
    suffix = "".join(random.choices(string.digits, k=6))
    return f"{prefix}-{safe_symbol[:6]}-{suffix}"[:25]


SWING_EVENTS_LOG_NAME = "swing_events"


async def _record_swing_event(event: str, symbol: str, detail: dict) -> None:
    """Durable, queryable event log for Swing specifically (added 1 Sep
    2026, user request: "log everything under history folder for Swing
    package also," made right after Swing's real trading went live) -
    closes the same "only in journald, limited retention, not queryable"
    gap `record_webhook_alert` already closed for incoming alerts (see
    its own docstring in trade_history.py). `position_opened`/
    `real_trades` already capture the bare entry/exit price per LEG; this
    captures the higher-level STATE TRANSITION and its own reasoning
    (which basket/sequential action fired, and why) in one row per
    event, for both basket mode (BASKET_ENTERED/BASKET_EXITED) and
    sequential mode (SEQUENTIAL_ENTERED_FUTURES/_SWAPPED_TO_PE/
    _SWAPPED_TO_FUTURES/_EXITED_TO_WATCHING).

    Fire-and-forget-style (awaited here, but the write itself runs in
    the executor thread pool, never the event loop, and is wrapped so it
    can never raise into a real trading action) - same "logging must
    never risk breaking the actual action" discipline as every other
    trade_history write in this codebase. Called AFTER the real
    action/store update has already succeeded, never before, so a
    logging failure here can only ever mean a missing observability
    record, not a missed or duplicated trade."""
    record = {
        "event": event,
        "underlying_symbol": symbol,
        "strategy_mode": config.STRATEGY_MODE,
        "logged_at": _now_ist().isoformat(),
        **detail,
    }
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, append_jsonl, SWING_EVENTS_LOG_NAME, record)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not append Swing event record (%s, %s) - the action itself is unaffected, "
            "this is logging-only.", event, symbol,
        )


# --------------------------------------------------------------------------- #
# Order placement helper - shared by both legs and by the unwind/exit SELLs
# --------------------------------------------------------------------------- #
async def _place_leg(
    trading_symbol: str, quantity: int, transaction_type: str, product_type: str,
    tag_prefix: str, symbol: str,
) -> dict:
    """Places ONE real order and waits for its result. Never raises - the
    caller decides what a failure means for the basket as a whole (abort
    with no PE leg attempted, unwind an already-filled futures leg,
    etc.)."""
    loop = asyncio.get_running_loop()
    tag = _gen_tag(tag_prefix, symbol)
    try:
        order_resp = await loop.run_in_executor(
            None, dhan_wrapper.place_market_order, trading_symbol, quantity, transaction_type, tag, product_type,
        )
        order_id = order_resp["order_id"]
        is_amo = order_resp["is_amo"]
        result = await loop.run_in_executor(None, dhan_wrapper.wait_for_order_result, order_id, is_amo)
        ok = result.status not in OrderStatus.REJECTED_STATUSES and result.status != OrderStatus.CANCELLED
        return {
            "ok": ok, "order_id": order_id, "status": result.status, "remark": result.remark,
            "fill_price": result.fill_price, "is_queued_amo": result.is_queued_amo,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s: %s order failed for %s", symbol, transaction_type, trading_symbol)
        return {"ok": False, "error": str(exc)}


async def _unwind_futures_leg(symbol: str, trading_symbol: str, quantity: int) -> dict:
    """Best-effort compensating SELL to close a futures leg that filled
    but must not survive alone (the PE hedge leg failed, or couldn't even
    be looked up). Never raises - logs loudly on failure since the caller
    has already decided the basket entry failed either way; this is
    cleanup, not something that can un-fail the outer operation. A failed
    unwind leaves a REAL, un-hedged futures position the user must handle
    manually - always logged as an ERROR, never silently swallowed."""
    result = await _place_leg(trading_symbol, quantity, "SELL", config.FUTURES_PRODUCT, "Unwind", symbol)
    if result["ok"]:
        logger.info("%s: futures leg unwound successfully (SELL %s filled)", symbol, trading_symbol)
    else:
        logger.error(
            "%s: COULD NOT UNWIND the futures leg (%s) after the PE leg failed - a REAL, "
            "un-hedged futures position may still be open at the broker. Manual intervention "
            "required. Detail: %s", symbol, trading_symbol, result,
        )
    return result


# --------------------------------------------------------------------------- #
# Basket entry - the all-or-nothing guarantee
# --------------------------------------------------------------------------- #
async def enter_basket_for_stock(symbol: str) -> dict:
    """The all-or-nothing basket entry - see this module's own docstring
    for the compensating-rollback design ("do both of these or
    neither")."""
    if not config.STRATEGY_ENABLED:
        return {"symbol": symbol, "status": "ignored", "reason": "strategy_disabled"}

    loop = asyncio.get_running_loop()

    # Swing's OWN dedup/claim (see this module's own docstring for why
    # this deliberately does NOT also go through cross_strategy_registry -
    # basket_store.reserve_symbol() is an atomic check-and-set under its
    # own lock, entirely separate from Options/Futures/Luxury's own
    # stores, and that's all this package's own entry path needs).
    if not await basket_store.reserve_symbol(symbol):
        logger.info("%s: skipped - already open/in-flight, or no basket capacity", symbol)
        return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}

    try:
        # Belt-and-suspenders: confirm the broker doesn't already show
        # an open FNO position for this underlying (a manual trade, a
        # position from before this logging, or state this process
        # hasn't reconciled yet) - our own reservation above only
        # guards duplicates within this process's in-memory state.
        already_open = await loop.run_in_executor(
            None, dhan_wrapper.has_open_position_for_underlying, symbol
        )
        if already_open:
            logger.warning("%s: skipped - broker already shows an open FNO position for it", symbol)
            await basket_store.release_symbol(symbol)
            return {"symbol": symbol, "status": "skipped", "reason": "already_open_at_broker"}

        # --- Leg 1: futures contract ---
        try:
            fut = await loop.run_in_executor(None, dhan_wrapper.get_futures_contract, symbol)
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: could not resolve futures contract - basket entry aborted", symbol)
            await basket_store.release_symbol(symbol)
            return {"symbol": symbol, "status": "error", "reason": f"futures_lookup_failed: {exc}"}

        fut_qty = fut.lot_size * config.QUANTITY_LOTS
        futures_result = await _place_leg(
            fut.trading_symbol, fut_qty, "BUY", config.FUTURES_PRODUCT, config.ORDER_TAG_PREFIX, symbol,
        )
        if not futures_result["ok"]:
            logger.warning(
                "%s: futures leg failed (%s) - basket entry aborted, PE leg never attempted (neither)",
                symbol, futures_result.get("remark") or futures_result.get("error"),
            )
            await basket_store.release_symbol(symbol)
            return {
                "symbol": symbol, "status": "rejected", "reason": "futures_leg_failed",
                "detail": futures_result,
            }

        futures_fill_price = futures_result.get("fill_price") or 0.0

        # --- Leg 2: ATM PE option (the hedge) ---
        try:
            atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, "PE")
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: could not resolve ATM PE option - unwinding the futures leg", symbol)
            await _unwind_futures_leg(symbol, fut.trading_symbol, fut_qty)
            await basket_store.release_symbol(symbol)
            return {
                "symbol": symbol, "status": "error",
                "reason": f"pe_lookup_failed: {exc} - futures leg unwound",
            }

        option_qty = atm.lot_size * config.QUANTITY_LOTS
        option_result = await _place_leg(
            atm.trading_symbol, option_qty, "BUY", config.OPTIONS_PRODUCT, config.ORDER_TAG_PREFIX, symbol,
        )
        if not option_result["ok"]:
            logger.warning(
                "%s: PE leg failed (%s) - unwinding the already-filled futures leg (neither)",
                symbol, option_result.get("remark") or option_result.get("error"),
            )
            await _unwind_futures_leg(symbol, fut.trading_symbol, fut_qty)
            await basket_store.release_symbol(symbol)
            return {
                "symbol": symbol, "status": "rejected", "reason": "pe_leg_failed_futures_unwound",
                "detail": option_result,
            }

        option_fill_price = option_result.get("fill_price") or 0.0

        futures_leg = Leg(
            underlying_symbol=symbol, option_trading_symbol=fut.trading_symbol, option_type="FUT",
            quantity=fut_qty, lot_size=fut.lot_size, entry_price=futures_fill_price,
            order_id=futures_result["order_id"], product_type=config.FUTURES_PRODUCT,
            security_id=fut.security_id,
        )
        option_leg = Leg(
            underlying_symbol=symbol, option_trading_symbol=atm.trading_symbol, option_type="PE",
            quantity=option_qty, lot_size=atm.lot_size, entry_price=option_fill_price,
            order_id=option_result["order_id"], product_type=config.OPTIONS_PRODUCT,
            security_id=atm.security_id,
        )
        basket = Basket(underlying_symbol=symbol, futures_leg=futures_leg, option_leg=option_leg)
        await basket_store.add_basket(basket)

        logger.info(
            "%s: basket ENTERED - futures %s@%.2f, PE %s@%.2f",
            symbol, fut.trading_symbol, futures_fill_price, atm.trading_symbol, option_fill_price,
        )
        await _record_swing_event("BASKET_ENTERED", symbol, {
            "futures_trading_symbol": fut.trading_symbol, "futures_entry_price": futures_fill_price,
            "futures_quantity": fut_qty,
            "option_trading_symbol": atm.trading_symbol, "option_entry_price": option_fill_price,
            "option_quantity": option_qty,
        })
        return {
            "symbol": symbol, "status": "entered",
            "futures_leg": {"trading_symbol": fut.trading_symbol, "entry_price": futures_fill_price, "quantity": fut_qty},
            "option_leg": {"trading_symbol": atm.trading_symbol, "entry_price": option_fill_price, "quantity": option_qty},
        }
    except Exception as exc:  # noqa: BLE001
        await basket_store.release_symbol(symbol)
        logger.exception("%s: unexpected error entering basket", symbol)
        return {"symbol": symbol, "status": "error", "reason": str(exc)}


# --------------------------------------------------------------------------- #
# Basket exit
# --------------------------------------------------------------------------- #
async def _exit_basket(symbol: str, basket: Basket, reason: str) -> None:
    """Closes BOTH legs of an already-open basket. Unlike entry, exit
    doesn't need the same all-or-nothing rollback in the same sense - a
    basket that's already live just needs both legs closed; if one SELL
    fails, the other leg is still attempted rather than aborting, since a
    stuck leg here costs nothing structurally the way a failed ENTRY
    would (see enter_basket_for_stock). Every failure is still logged
    loudly so a stuck leg is visible, never silently lost."""
    futures_exit = await _place_leg(
        basket.futures_leg.option_trading_symbol, basket.futures_leg.quantity, "SELL",
        basket.futures_leg.product_type, "Ext", symbol,
    )
    option_exit = await _place_leg(
        basket.option_leg.option_trading_symbol, basket.option_leg.quantity, "SELL",
        basket.option_leg.product_type, "Ext", symbol,
    )
    futures_exit_price = futures_exit.get("fill_price") or basket.futures_leg.entry_price
    option_exit_price = option_exit.get("fill_price") or basket.option_leg.entry_price

    if not futures_exit["ok"]:
        logger.error(
            "%s: futures leg SELL failed during basket exit (%s) - may still be open, check manually",
            symbol, futures_exit.get("remark") or futures_exit.get("error"),
        )
    if not option_exit["ok"]:
        logger.error(
            "%s: PE leg SELL failed during basket exit (%s) - may still be open, check manually",
            symbol, option_exit.get("remark") or option_exit.get("error"),
        )

    await basket_store.close_basket(symbol, futures_exit_price, option_exit_price, reason)
    await _record_swing_event("BASKET_EXITED", symbol, {
        "exit_reason": reason,
        "futures_exit_price": futures_exit_price, "option_exit_price": option_exit_price,
        "futures_sell_ok": futures_exit["ok"], "option_sell_ok": option_exit["ok"],
    })


async def _square_off_all(reason: str) -> None:
    """Manual kill-switch (POST /swing/square-off-now) - mode-aware
    (added 1 Sep 2026, extended to 3-way 1 Sep 2026): closes every live
    BASKET in basket mode, every live LEG in sequential mode, or every
    live basket_hedge POSITION (whatever state it's currently in - a
    BASKET's both/one leg(s), or a standalone PE hedge) in basket_hedge
    mode - returning each symbol straight to plain watching (does NOT
    continue into a hedge from a BASKET-state position, and does NOT
    re-enter after a PE_HEDGE-state one - a manual kill-switch means "get
    me flat now", not "keep managing this symbol")."""
    if config.STRATEGY_MODE == "basket":
        baskets = dict(basket_store.live_baskets)
        if not baskets:
            return
        logger.info("Swing square-off triggered (%s) for %d open basket(s)", reason, len(baskets))
        for symbol, basket in baskets.items():
            await _exit_basket(symbol, basket, reason)
    elif config.STRATEGY_MODE == "basket_hedge":
        positions = dict(basket_hedge_store.live_positions)
        if not positions:
            return
        logger.info("Swing square-off triggered (%s) for %d open basket_hedge position(s)", reason, len(positions))
        for symbol, position in positions.items():
            exit_results = {}
            all_sold = True
            for leg in position.legs:
                result = await _place_leg(leg.option_trading_symbol, leg.quantity, "SELL", leg.product_type, "Ext", symbol)
                if not result["ok"]:
                    logger.error(
                        "%s: %s leg SELL failed during manual basket_hedge square-off (%s) - "
                        "leg left open, check manually", symbol, leg.option_type,
                        result.get("remark") or result.get("error"),
                    )
                    all_sold = False
                    continue
                exit_results[leg.option_trading_symbol] = result.get("fill_price") or leg.entry_price
            if not all_sold:
                continue
            # close_current_legs_for_hedge_swap closes EVERY current leg
            # (1 or 2, whatever this position holds) with its own real
            # exit price - unlike exit_to_watching, which only closes
            # legs[0] and is meant for the single-leg PE_HEDGE state. It
            # doesn't release capacity on its own (normally the next step
            # is buying a PE hedge), so release_symbol() is called right
            # after - a manual square-off means "get flat," not "swap."
            await basket_hedge_store.close_current_legs_for_hedge_swap(symbol, exit_results, reason)
            await basket_hedge_store.release_symbol(symbol)
            await _record_swing_event("BASKET_HEDGE_SQUARED_OFF", symbol, {
                "exit_reason": reason, "state_at_square_off": position.state, "exit_prices": exit_results,
            })
    else:
        legs = dict(sequential_store.live_legs)
        if not legs:
            return
        logger.info("Swing square-off triggered (%s) for %d open sequential leg(s)", reason, len(legs))
        for symbol, leg in legs.items():
            result = await _place_leg(leg.option_trading_symbol, leg.quantity, "SELL", leg.product_type, "Ext", symbol)
            if not result["ok"]:
                logger.error(
                    "%s: leg SELL failed during manual square-off (%s) - may still be open, check manually",
                    symbol, result.get("remark") or result.get("error"),
                )
                continue
            exit_price = result.get("fill_price") or leg.entry_price
            await sequential_store.exit_to_watching(symbol, exit_price, reason)
            await _record_swing_event("SEQUENTIAL_EXITED_TO_WATCHING", symbol, {
                "exit_reason": reason, "leg_type": leg.option_type,
                "exit_price": exit_price, "entry_price": leg.entry_price,
            })


# --------------------------------------------------------------------------- #
# SEQUENTIAL mode - see this module's own docstring for the full state-
# machine diagram (user request 1 Sep 2026)
# --------------------------------------------------------------------------- #
async def _enter_futures_for_stock(symbol: str) -> dict:
    """NONE -> FUTURES. Mirrors enter_basket_for_stock's own futures leg
    exactly (same contract lookup, same order placement), but there's no
    second leg here and therefore no all-or-nothing rollback needed - a
    failed futures BUY just means the symbol stays in NONE, released for
    a later attempt."""
    if not config.STRATEGY_ENABLED:
        return {"symbol": symbol, "status": "ignored", "reason": "strategy_disabled"}

    loop = asyncio.get_running_loop()
    if not await sequential_store.try_enter(symbol):
        logger.info("%s: skipped - already open/in-flight, or no sequential capacity", symbol)
        return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}

    try:
        already_open = await loop.run_in_executor(
            None, dhan_wrapper.has_open_position_for_underlying, symbol
        )
        if already_open:
            logger.warning("%s: skipped - broker already shows an open FNO position for it", symbol)
            await sequential_store.release_symbol(symbol)
            return {"symbol": symbol, "status": "skipped", "reason": "already_open_at_broker"}

        try:
            fut = await loop.run_in_executor(None, dhan_wrapper.get_futures_contract, symbol)
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: could not resolve futures contract - entry aborted", symbol)
            await sequential_store.release_symbol(symbol)
            return {"symbol": symbol, "status": "error", "reason": f"futures_lookup_failed: {exc}"}

        fut_qty = fut.lot_size * config.QUANTITY_LOTS
        result = await _place_leg(
            fut.trading_symbol, fut_qty, "BUY", config.FUTURES_PRODUCT, config.ORDER_TAG_PREFIX, symbol,
        )
        if not result["ok"]:
            logger.warning("%s: futures entry failed (%s)", symbol, result.get("remark") or result.get("error"))
            await sequential_store.release_symbol(symbol)
            return {"symbol": symbol, "status": "rejected", "reason": "futures_leg_failed", "detail": result}

        fill_price = result.get("fill_price") or 0.0
        leg = Leg(
            underlying_symbol=symbol, option_trading_symbol=fut.trading_symbol, option_type="FUT",
            quantity=fut_qty, lot_size=fut.lot_size, entry_price=fill_price,
            order_id=result["order_id"], product_type=config.FUTURES_PRODUCT, security_id=fut.security_id,
        )
        await sequential_store.set_leg(leg)
        logger.info("%s: sequential ENTRY - futures %s@%.2f", symbol, fut.trading_symbol, fill_price)
        await _record_swing_event("SEQUENTIAL_ENTERED_FUTURES", symbol, {
            "trading_symbol": fut.trading_symbol, "entry_price": fill_price, "quantity": fut_qty,
        })
        return {
            "symbol": symbol, "status": "entered", "leg": "FUT",
            "trading_symbol": fut.trading_symbol, "entry_price": fill_price, "quantity": fut_qty,
        }
    except Exception as exc:  # noqa: BLE001
        await sequential_store.release_symbol(symbol)
        logger.exception("%s: unexpected error entering futures (sequential)", symbol)
        return {"symbol": symbol, "status": "error", "reason": str(exc)}


async def _swap_futures_to_pe(symbol: str, futures_leg: Leg) -> None:
    """FUTURES -> PE. Sells the futures leg, then buys the ATM PE hedge.
    If the futures SELL itself fails, the leg is left exactly as-is (no
    state change) - the next monitor tick re-detects the same exit
    condition and retries, the same simple retry-via-next-tick approach
    _exit_basket's own SELL failures already accept. If the SELL
    succeeds but the PE BUY then fails, the symbol is left FLAT
    (capacity released) rather than stuck - the safest failure state (no
    real exposure) - and the next entry signal picks the symbol up fresh."""
    loop = asyncio.get_running_loop()
    futures_exit = await _place_leg(
        futures_leg.option_trading_symbol, futures_leg.quantity, "SELL",
        futures_leg.product_type, "Ext", symbol,
    )
    if not futures_exit["ok"]:
        logger.error(
            "%s: futures leg SELL failed during sequential exit (%s) - leg left open, "
            "will retry on the next tick", symbol, futures_exit.get("remark") or futures_exit.get("error"),
        )
        return

    futures_exit_price = futures_exit.get("fill_price") or futures_leg.entry_price
    await sequential_store.close_leg_for_swap(symbol, futures_exit_price, "SUPERTREND_5MIN_EXIT")

    try:
        atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, "PE")
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: futures leg sold but could not resolve the ATM PE hedge - symbol left FLAT "
            "(no real exposure), will re-enter fresh on the next entry signal", symbol,
        )
        await sequential_store.release_symbol(symbol)
        await _record_swing_event("SEQUENTIAL_LEFT_FLAT", symbol, {
            "reason": "pe_lookup_failed_after_futures_sold", "futures_exit_price": futures_exit_price,
        })
        return

    option_qty = atm.lot_size * config.QUANTITY_LOTS
    option_result = await _place_leg(
        atm.trading_symbol, option_qty, "BUY", config.OPTIONS_PRODUCT, config.ORDER_TAG_PREFIX, symbol,
    )
    if not option_result["ok"]:
        logger.error(
            "%s: futures leg sold but the PE hedge BUY failed (%s) - symbol left FLAT "
            "(no real exposure), will re-enter fresh on the next entry signal",
            symbol, option_result.get("remark") or option_result.get("error"),
        )
        await sequential_store.release_symbol(symbol)
        await _record_swing_event("SEQUENTIAL_LEFT_FLAT", symbol, {
            "reason": "pe_buy_failed_after_futures_sold", "futures_exit_price": futures_exit_price,
        })
        return

    option_fill_price = option_result.get("fill_price") or 0.0
    pe_leg = Leg(
        underlying_symbol=symbol, option_trading_symbol=atm.trading_symbol, option_type="PE",
        quantity=option_qty, lot_size=atm.lot_size, entry_price=option_fill_price,
        order_id=option_result["order_id"], product_type=config.OPTIONS_PRODUCT, security_id=atm.security_id,
    )
    await sequential_store.set_leg(pe_leg)
    logger.info("%s: sequential SWAP futures->PE - PE %s@%.2f", symbol, atm.trading_symbol, option_fill_price)
    await _record_swing_event("SEQUENTIAL_SWAPPED_TO_PE", symbol, {
        "exit_reason": "SUPERTREND_5MIN_EXIT", "futures_exit_price": futures_exit_price,
        "pe_trading_symbol": atm.trading_symbol, "pe_entry_price": option_fill_price, "pe_quantity": option_qty,
    })


async def _exit_pe_to_watching(symbol: str, pe_leg: Leg, reason: str) -> None:
    """PE -> NONE (the loss-cap exit). Sells the PE and releases the
    symbol's capacity - returns to plain watching, does NOT re-buy
    futures (user confirmed via AskUserQuestion 1 Sep 2026 - see this
    module's own docstring)."""
    option_exit = await _place_leg(
        pe_leg.option_trading_symbol, pe_leg.quantity, "SELL", pe_leg.product_type, "Ext", symbol,
    )
    if not option_exit["ok"]:
        logger.error(
            "%s: PE leg SELL failed during sequential loss-cap exit (%s) - leg left open, "
            "will retry on the next tick", symbol, option_exit.get("remark") or option_exit.get("error"),
        )
        return
    option_exit_price = option_exit.get("fill_price") or pe_leg.entry_price
    await sequential_store.exit_to_watching(symbol, option_exit_price, reason)
    await _record_swing_event("SEQUENTIAL_EXITED_TO_WATCHING", symbol, {
        "exit_reason": reason, "pe_exit_price": option_exit_price, "pe_entry_price": pe_leg.entry_price,
    })


async def _swap_pe_to_futures(symbol: str, pe_leg: Leg) -> None:
    """PE -> FUTURES (the "keep this loop going" transition - the entry
    condition has re-fired). Sells the PE, buys futures again at market.
    Same fail-safe-to-flat handling as _swap_futures_to_pe's own PE-buy
    failure path, mirrored here for the futures-buy failure."""
    loop = asyncio.get_running_loop()
    option_exit = await _place_leg(
        pe_leg.option_trading_symbol, pe_leg.quantity, "SELL", pe_leg.product_type, "Ext", symbol,
    )
    if not option_exit["ok"]:
        logger.error(
            "%s: PE leg SELL failed while trying to swap back to futures (%s) - leg left open, "
            "will retry on the next tick", symbol, option_exit.get("remark") or option_exit.get("error"),
        )
        return

    option_exit_price = option_exit.get("fill_price") or pe_leg.entry_price
    await sequential_store.close_leg_for_swap(symbol, option_exit_price, "ENTRY_SIGNAL_REFIRED")

    try:
        fut = await loop.run_in_executor(None, dhan_wrapper.get_futures_contract, symbol)
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: PE leg sold but could not resolve the futures contract - symbol left FLAT "
            "(no real exposure), will re-enter fresh on the next entry signal", symbol,
        )
        await sequential_store.release_symbol(symbol)
        await _record_swing_event("SEQUENTIAL_LEFT_FLAT", symbol, {
            "reason": "futures_lookup_failed_after_pe_sold", "pe_exit_price": option_exit_price,
        })
        return

    fut_qty = fut.lot_size * config.QUANTITY_LOTS
    futures_result = await _place_leg(
        fut.trading_symbol, fut_qty, "BUY", config.FUTURES_PRODUCT, config.ORDER_TAG_PREFIX, symbol,
    )
    if not futures_result["ok"]:
        logger.error(
            "%s: PE leg sold but the futures re-entry BUY failed (%s) - symbol left FLAT "
            "(no real exposure), will re-enter fresh on the next entry signal",
            symbol, futures_result.get("remark") or futures_result.get("error"),
        )
        await sequential_store.release_symbol(symbol)
        await _record_swing_event("SEQUENTIAL_LEFT_FLAT", symbol, {
            "reason": "futures_buy_failed_after_pe_sold", "pe_exit_price": option_exit_price,
        })
        return

    fill_price = futures_result.get("fill_price") or 0.0
    fut_leg = Leg(
        underlying_symbol=symbol, option_trading_symbol=fut.trading_symbol, option_type="FUT",
        quantity=fut_qty, lot_size=fut.lot_size, entry_price=fill_price,
        order_id=futures_result["order_id"], product_type=config.FUTURES_PRODUCT, security_id=fut.security_id,
    )
    await sequential_store.set_leg(fut_leg)
    logger.info(
        "%s: sequential SWAP PE->futures (loop continues) - futures %s@%.2f",
        symbol, fut.trading_symbol, fill_price,
    )
    await _record_swing_event("SEQUENTIAL_SWAPPED_TO_FUTURES", symbol, {
        "exit_reason": "ENTRY_SIGNAL_REFIRED", "pe_exit_price": option_exit_price,
        "futures_trading_symbol": fut.trading_symbol, "futures_entry_price": fill_price, "futures_quantity": fut_qty,
    })


async def _evaluate_pe_exit_signal(symbol: str, pe_leg: Leg) -> Optional[str]:
    """PE loss-cap check (user's own wording: "Exit this PE option
    contract once loss become more than 2k") - unrealized, mark-to-market
    against the current LTP, the same style as every other rupee-loss-cap
    elsewhere in this codebase (e.g. Options/trading_engine.py's own
    current_max_loss_per_trade_rs check)."""
    loop = asyncio.get_running_loop()
    try:
        ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, pe_leg.option_trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch PE LTP for the loss-cap check", symbol)
        return None
    loss_rs = (pe_leg.entry_price - ltp) * pe_leg.quantity
    if loss_rs > config.PE_MAX_LOSS_RS:
        logger.info("%s: PE loss-cap HIT - unrealized loss %.2f > cap %.2f", symbol, loss_rs, config.PE_MAX_LOSS_RS)
        return "PE_MAX_LOSS_HIT"
    return None


# --------------------------------------------------------------------------- #
# BASKET_HEDGE mode - see config.py's own STRATEGY_MODE docstring for the
# full explanation (user request 1 Sep 2026: "enabling basket buy
# strategy but with a caveat"). Entry is IDENTICAL to plain basket mode
# (all-or-nothing futures+PE); only what happens on EXIT differs - sell
# the basket, buy a single standalone PE hedge, hold it until any of 3
# conditions, then back to plain watching.
# --------------------------------------------------------------------------- #
async def _enter_basket_hedge_for_stock(symbol: str) -> dict:
    """NONE -> BASKET. Same all-or-nothing rollback as enter_basket_for_stock
    (futures leg fails -> PE never attempted; PE fails after futures fills
    -> futures unwound) - the only difference from that function is which
    store records the result."""
    if not config.STRATEGY_ENABLED:
        return {"symbol": symbol, "status": "ignored", "reason": "strategy_disabled"}

    loop = asyncio.get_running_loop()
    if not await basket_hedge_store.try_enter(symbol):
        logger.info("%s: skipped - already open/in-flight, or no basket_hedge capacity", symbol)
        return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}

    try:
        already_open = await loop.run_in_executor(
            None, dhan_wrapper.has_open_position_for_underlying, symbol
        )
        if already_open:
            logger.warning("%s: skipped - broker already shows an open FNO position for it", symbol)
            await basket_hedge_store.release_symbol(symbol)
            return {"symbol": symbol, "status": "skipped", "reason": "already_open_at_broker"}

        try:
            fut = await loop.run_in_executor(None, dhan_wrapper.get_futures_contract, symbol)
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: could not resolve futures contract - basket_hedge entry aborted", symbol)
            await basket_hedge_store.release_symbol(symbol)
            return {"symbol": symbol, "status": "error", "reason": f"futures_lookup_failed: {exc}"}

        fut_qty = fut.lot_size * config.QUANTITY_LOTS
        futures_result = await _place_leg(
            fut.trading_symbol, fut_qty, "BUY", config.FUTURES_PRODUCT, config.ORDER_TAG_PREFIX, symbol,
        )
        if not futures_result["ok"]:
            logger.warning(
                "%s: futures leg failed (%s) - basket_hedge entry aborted, PE leg never attempted (neither)",
                symbol, futures_result.get("remark") or futures_result.get("error"),
            )
            await basket_hedge_store.release_symbol(symbol)
            return {
                "symbol": symbol, "status": "rejected", "reason": "futures_leg_failed",
                "detail": futures_result,
            }

        futures_fill_price = futures_result.get("fill_price") or 0.0

        try:
            atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, "PE")
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: could not resolve ATM PE option - unwinding the futures leg", symbol)
            await _unwind_futures_leg(symbol, fut.trading_symbol, fut_qty)
            await basket_hedge_store.release_symbol(symbol)
            return {
                "symbol": symbol, "status": "error",
                "reason": f"pe_lookup_failed: {exc} - futures leg unwound",
            }

        option_qty = atm.lot_size * config.QUANTITY_LOTS
        option_result = await _place_leg(
            atm.trading_symbol, option_qty, "BUY", config.OPTIONS_PRODUCT, config.ORDER_TAG_PREFIX, symbol,
        )
        if not option_result["ok"]:
            logger.warning(
                "%s: PE leg failed (%s) - unwinding the already-filled futures leg (neither)",
                symbol, option_result.get("remark") or option_result.get("error"),
            )
            await _unwind_futures_leg(symbol, fut.trading_symbol, fut_qty)
            await basket_hedge_store.release_symbol(symbol)
            return {
                "symbol": symbol, "status": "rejected", "reason": "pe_leg_failed_futures_unwound",
                "detail": option_result,
            }

        option_fill_price = option_result.get("fill_price") or 0.0

        futures_leg = Leg(
            underlying_symbol=symbol, option_trading_symbol=fut.trading_symbol, option_type="FUT",
            quantity=fut_qty, lot_size=fut.lot_size, entry_price=futures_fill_price,
            order_id=futures_result["order_id"], product_type=config.FUTURES_PRODUCT,
            security_id=fut.security_id,
        )
        option_leg = Leg(
            underlying_symbol=symbol, option_trading_symbol=atm.trading_symbol, option_type="PE",
            quantity=option_qty, lot_size=atm.lot_size, entry_price=option_fill_price,
            order_id=option_result["order_id"], product_type=config.OPTIONS_PRODUCT,
            security_id=atm.security_id,
        )
        await basket_hedge_store.set_basket(symbol, [futures_leg, option_leg])

        logger.info(
            "%s: basket_hedge BASKET ENTERED - futures %s@%.2f, PE %s@%.2f",
            symbol, fut.trading_symbol, futures_fill_price, atm.trading_symbol, option_fill_price,
        )
        await _record_swing_event("BASKET_HEDGE_BASKET_ENTERED", symbol, {
            "futures_trading_symbol": fut.trading_symbol, "futures_entry_price": futures_fill_price,
            "futures_quantity": fut_qty,
            "option_trading_symbol": atm.trading_symbol, "option_entry_price": option_fill_price,
            "option_quantity": option_qty,
        })
        return {
            "symbol": symbol, "status": "entered",
            "futures_leg": {"trading_symbol": fut.trading_symbol, "entry_price": futures_fill_price, "quantity": fut_qty},
            "option_leg": {"trading_symbol": atm.trading_symbol, "entry_price": option_fill_price, "quantity": option_qty},
        }
    except Exception as exc:  # noqa: BLE001
        await basket_hedge_store.release_symbol(symbol)
        logger.exception("%s: unexpected error entering basket_hedge basket", symbol)
        return {"symbol": symbol, "status": "error", "reason": str(exc)}


async def _exit_basket_hedge_to_pe(symbol: str, position: BasketHedgePosition) -> None:
    """BASKET -> PE_HEDGE. Sells every leg CURRENTLY held (1 for the
    grandfathered position, 2 for a normal all-or-nothing entry - see
    BasketHedgePosition's own docstring), then buys ONE ATM PE hedge.
    Same "fail safe to flat" philosophy as sequential mode's own swaps:
    a leg that can't be SOLD is left exactly as-is for the next tick to
    retry (nothing here proceeds to the PE buy until every current leg
    is confirmed sold); once all are sold, a failure to buy the PE hedge
    leaves the symbol FLAT (capacity released) rather than stuck."""
    loop = asyncio.get_running_loop()
    exit_results = {}
    all_sold = True
    for leg in position.legs:
        result = await _place_leg(leg.option_trading_symbol, leg.quantity, "SELL", leg.product_type, "Ext", symbol)
        if not result["ok"]:
            logger.error(
                "%s: %s leg SELL failed during basket_hedge exit (%s) - leg left open, "
                "will retry on the next tick", symbol, leg.option_type,
                result.get("remark") or result.get("error"),
            )
            all_sold = False
            continue
        exit_results[leg.option_trading_symbol] = result.get("fill_price") or leg.entry_price

    if not all_sold:
        return  # at least one leg still open - next tick re-detects the same exit condition and retries

    await basket_hedge_store.close_current_legs_for_hedge_swap(symbol, exit_results, "SUPERTREND_5MIN_EXIT")
    await _record_swing_event("BASKET_HEDGE_BASKET_EXITED", symbol, {
        "exit_reason": "SUPERTREND_5MIN_EXIT", "exit_prices": exit_results,
    })

    try:
        atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, "PE")
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: basket sold but could not resolve the ATM PE hedge - symbol left FLAT "
            "(no real exposure), will re-enter fresh on the next entry signal", symbol,
        )
        await basket_hedge_store.release_symbol(symbol)
        await _record_swing_event("BASKET_HEDGE_LEFT_FLAT", symbol, {"reason": "pe_lookup_failed_after_basket_sold"})
        return

    option_qty = atm.lot_size * config.QUANTITY_LOTS
    option_result = await _place_leg(
        atm.trading_symbol, option_qty, "BUY", config.OPTIONS_PRODUCT, config.ORDER_TAG_PREFIX, symbol,
    )
    if not option_result["ok"]:
        logger.error(
            "%s: basket sold but the PE hedge BUY failed (%s) - symbol left FLAT "
            "(no real exposure), will re-enter fresh on the next entry signal",
            symbol, option_result.get("remark") or option_result.get("error"),
        )
        await basket_hedge_store.release_symbol(symbol)
        await _record_swing_event("BASKET_HEDGE_LEFT_FLAT", symbol, {"reason": "pe_buy_failed_after_basket_sold"})
        return

    option_fill_price = option_result.get("fill_price") or 0.0
    pe_leg = Leg(
        underlying_symbol=symbol, option_trading_symbol=atm.trading_symbol, option_type="PE",
        quantity=option_qty, lot_size=atm.lot_size, entry_price=option_fill_price,
        order_id=option_result["order_id"], product_type=config.OPTIONS_PRODUCT, security_id=atm.security_id,
    )
    await basket_hedge_store.set_pe_hedge(symbol, pe_leg)
    logger.info(
        "%s: basket_hedge SWAP basket->PE hedge - PE %s@%.2f", symbol, atm.trading_symbol, option_fill_price,
    )
    await _record_swing_event("BASKET_HEDGE_PE_HEDGE_ENTERED", symbol, {
        "trading_symbol": atm.trading_symbol, "entry_price": option_fill_price, "quantity": option_qty,
    })


async def _exit_pe_hedge_to_watching(symbol: str, pe_leg: Leg, reason: str) -> None:
    """PE_HEDGE -> NONE. Sells the standalone PE hedge and releases the
    symbol's capacity - back to plain watching for a fresh basket entry.
    None of the 3 PE-hedge exit conditions (loss cap, profit lock, bare
    Supertrend reversal) carries a confirmed fresh BUY signal the way
    sequential mode's own entry-refire path does, so this never re-enters
    anything on its own - same "don't blindly chain" choice made for
    sequential mode's own loss-cap exit."""
    option_exit = await _place_leg(
        pe_leg.option_trading_symbol, pe_leg.quantity, "SELL", pe_leg.product_type, "Ext", symbol,
    )
    if not option_exit["ok"]:
        logger.error(
            "%s: PE hedge SELL failed during basket_hedge exit (%s) - leg left open, "
            "will retry on the next tick", symbol, option_exit.get("remark") or option_exit.get("error"),
        )
        return
    option_exit_price = option_exit.get("fill_price") or pe_leg.entry_price
    await basket_hedge_store.exit_to_watching(symbol, option_exit_price, reason)
    await _record_swing_event("BASKET_HEDGE_PE_HEDGE_EXITED", symbol, {
        "exit_reason": reason, "exit_price": option_exit_price, "entry_price": pe_leg.entry_price,
    })


async def _evaluate_pe_hedge_exit_signal(symbol: str, pe_leg: Leg) -> Optional[str]:
    """The PE hedge's own 3-way exit check (user's own numbered list):
      1. Loss exceeds config.PE_MAX_LOSS_RS.
      2. Profit exceeds config.PE_PROFIT_LOCK_RS ("lock profit").
      3. The underlying's 5-min close crosses back ABOVE its own
         Supertrend - checked as a BARE reversal via _fetch_supertrend_state
         directly, deliberately NOT the full _evaluate_watchlist_entry_signal
         (user's own words: "even if buy signal is not yet triggered" -
         the price-confirmation gate and 1-min confirm timeframe are NOT
         required for this specific exit).
    Checked in this order (loss, then profit-lock, then Supertrend) since
    the first two only need one cheap LTP fetch; the Supertrend check is
    tried regardless of an LTP fetch failure (they're independent data
    sources), so one failing never masks the other."""
    loop = asyncio.get_running_loop()
    try:
        ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, pe_leg.option_trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch PE LTP for the basket_hedge loss/profit check", symbol)
        ltp = None

    if ltp is not None:
        loss_rs = (pe_leg.entry_price - ltp) * pe_leg.quantity
        if loss_rs > config.PE_MAX_LOSS_RS:
            logger.info("%s: PE hedge loss-cap HIT - unrealized loss %.2f > cap %.2f", symbol, loss_rs, config.PE_MAX_LOSS_RS)
            return "PE_MAX_LOSS_HIT"
        profit_rs = (ltp - pe_leg.entry_price) * pe_leg.quantity
        if profit_rs > config.PE_PROFIT_LOCK_RS:
            logger.info("%s: PE hedge profit-lock HIT - unrealized profit %.2f > cap %.2f", symbol, profit_rs, config.PE_PROFIT_LOCK_RS)
            return "PE_PROFIT_LOCK_HIT"

    state = await _fetch_supertrend_state(symbol, config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES)
    if state is not None and state.crossed_above:
        logger.info(
            "%s: PE hedge Supertrend reversal HIT - %d-min close %.2f crossed above Supertrend %.2f "
            "(bare reversal, entry signal not required)",
            symbol, config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES, state.close, state.supertrend,
        )
        return "PE_SUPERTREND_REVERSAL_EXIT"
    return None


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
async def reconcile_broker_positions() -> list[Basket]:
    """Pairs up any FUTSTK + OPTSTK broker positions attributed to
    "Swing" back into Baskets at startup - see this module's own
    docstring for why this matters more here than for any other package
    (no EOD square-off, so a basket is meant to survive a restart)."""
    loop = asyncio.get_running_loop()
    broker_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)

    futures_by_underlying: dict[str, dict] = {}
    options_by_underlying: dict[str, dict] = {}
    for bp in broker_positions:
        avg_price = bp["avg_price"]
        if not avg_price:
            logger.warning(
                "Skipping reconciliation for %s - broker reported no average price.",
                bp["trading_symbol"],
            )
            continue

        owner = await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"])
        if owner != "Swing":
            logger.warning(
                "Skipping reconciliation for %s - attributed to %s (not Swing) by our own "
                "opened-position history. Real broker position is unaffected; this process just "
                "won't manage it. If this is wrong (e.g. a manually-placed position, or one that "
                "predates this logging), it needs manual handling.",
                bp["trading_symbol"], owner or "no strategy (no record found)",
            )
            continue

        # FUTSTK rows don't carry a meaningful option_type - distinguish
        # the leg by trading_symbol shape instead (Dhan's own futures
        # SEM_CUSTOM_SYMBOL format always ends "... FUT").
        if bp["trading_symbol"].endswith("FUT"):
            futures_by_underlying[bp["underlying_symbol"]] = bp
        else:
            options_by_underlying[bp["underlying_symbol"]] = bp

    baskets: list[Basket] = []
    for symbol in set(futures_by_underlying) | set(options_by_underlying):
        fut_bp = futures_by_underlying.get(symbol)
        opt_bp = options_by_underlying.get(symbol)
        if fut_bp and opt_bp:
            futures_leg = Leg(
                underlying_symbol=symbol, option_trading_symbol=fut_bp["trading_symbol"], option_type="FUT",
                quantity=fut_bp["quantity"], lot_size=fut_bp["lot_size"], entry_price=fut_bp["avg_price"],
                order_id="", product_type=config.FUTURES_PRODUCT, reconciled=True,
            )
            option_leg = Leg(
                underlying_symbol=symbol, option_trading_symbol=opt_bp["trading_symbol"], option_type="PE",
                quantity=opt_bp["quantity"], lot_size=opt_bp["lot_size"], entry_price=opt_bp["avg_price"],
                order_id="", product_type=config.OPTIONS_PRODUCT, reconciled=True,
            )
            baskets.append(Basket(underlying_symbol=symbol, futures_leg=futures_leg, option_leg=option_leg))
            await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, fut_bp["trading_symbol"])
            await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, opt_bp["trading_symbol"])
        else:
            found, missing = ("futures", "PE option") if fut_bp else ("PE option", "futures")
            logger.warning(
                "%s: found a Swing-attributed %s leg at the broker with NO matching %s leg - "
                "this basket is UNHEDGED/incomplete. Not auto-reconciling a partial basket - "
                "needs manual review.", symbol, found, missing,
            )

    return baskets


async def reconcile_sequential_positions() -> list[Leg]:
    """Recovers a lone FUT or PE broker leg attributed to "Swing" back
    into sequential_store at startup - see position_store.
    SequentialPositionStore.reconcile_leg's own docstring for why a LONE
    leg means something completely different here than it does for
    basket mode above (there, a lone leg is an anomaly needing manual
    review - a symbol should always hold a paired FUT+OPT; here, it's
    the NORMAL, expected shape, since a symbol only ever holds ONE
    instrument at a time under sequential mode). Only called by
    swing_main.py's lifespan when config.STRATEGY_MODE == "sequential" -
    see reconcile_broker_positions() above for the basket-mode
    equivalent, called instead when the mode is "basket"."""
    loop = asyncio.get_running_loop()
    broker_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)

    legs: list[Leg] = []
    for bp in broker_positions:
        avg_price = bp["avg_price"]
        if not avg_price:
            logger.warning(
                "Skipping sequential reconciliation for %s - broker reported no average price.",
                bp["trading_symbol"],
            )
            continue

        owner = await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"])
        if owner != "Swing":
            logger.warning(
                "Skipping sequential reconciliation for %s - attributed to %s (not Swing) by our "
                "own opened-position history. Real broker position is unaffected; this process "
                "just won't manage it.", bp["trading_symbol"], owner or "no strategy (no record found)",
            )
            continue

        option_type = "FUT" if bp["trading_symbol"].endswith("FUT") else "PE"
        leg = Leg(
            underlying_symbol=bp["underlying_symbol"], option_trading_symbol=bp["trading_symbol"],
            option_type=option_type, quantity=bp["quantity"], lot_size=bp["lot_size"],
            entry_price=bp["avg_price"], order_id="",
            product_type=config.FUTURES_PRODUCT if option_type == "FUT" else config.OPTIONS_PRODUCT,
            reconciled=True,
        )
        legs.append(leg)
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, bp["trading_symbol"])

    return legs


async def reconcile_basket_hedge_positions() -> list[BasketHedgePosition]:
    """Recovers any Swing-attributed broker legs into basket_hedge_store
    at startup - MORE PERMISSIVE than plain basket mode's own
    reconcile_broker_positions() (which treats a lone leg as an anomaly
    needing manual review), since basket_hedge mode legitimately has TWO
    different single-leg shapes of its own:
      - a lone FUTSTK leg with NO paired OPTSTK -> BASKET state with just
        that one leg (the "grandfathered" shape - e.g. APLAPOLLO, entered
        under sequential mode before this mode existed, user request:
        "consider the open trade as a basket order for this time as it
        is already live" - also covers a genuine mid-restart interruption
        during a fresh basket_hedge entry between the two legs' own real
        orders).
      - a lone OPTSTK (PE) leg with NO paired FUTSTK -> PE_HEDGE state
        (the NORMAL shape once a basket has already swapped into its
        hedge phase - see _exit_basket_hedge_to_pe).
      - BOTH legs present, same underlying -> BASKET state with both (a
        complete, freshly-entered basket, exactly like plain basket
        mode's own pairing).
    Only called by swing_main.py's lifespan when
    config.STRATEGY_MODE == "basket_hedge"."""
    loop = asyncio.get_running_loop()
    broker_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)

    futures_by_underlying: dict[str, dict] = {}
    options_by_underlying: dict[str, dict] = {}
    for bp in broker_positions:
        avg_price = bp["avg_price"]
        if not avg_price:
            logger.warning(
                "Skipping basket_hedge reconciliation for %s - broker reported no average price.",
                bp["trading_symbol"],
            )
            continue

        owner = await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"])
        if owner != "Swing":
            logger.warning(
                "Skipping basket_hedge reconciliation for %s - attributed to %s (not Swing) by our "
                "own opened-position history. Real broker position is unaffected; this process "
                "just won't manage it.", bp["trading_symbol"], owner or "no strategy (no record found)",
            )
            continue

        if bp["trading_symbol"].endswith("FUT"):
            futures_by_underlying[bp["underlying_symbol"]] = bp
        else:
            options_by_underlying[bp["underlying_symbol"]] = bp

    positions: list[BasketHedgePosition] = []
    for symbol in set(futures_by_underlying) | set(options_by_underlying):
        fut_bp = futures_by_underlying.get(symbol)
        opt_bp = options_by_underlying.get(symbol)
        legs: list[Leg] = []
        if fut_bp:
            legs.append(Leg(
                underlying_symbol=symbol, option_trading_symbol=fut_bp["trading_symbol"], option_type="FUT",
                quantity=fut_bp["quantity"], lot_size=fut_bp["lot_size"], entry_price=fut_bp["avg_price"],
                order_id="", product_type=config.FUTURES_PRODUCT, reconciled=True,
            ))
            await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, fut_bp["trading_symbol"])
        if opt_bp:
            legs.append(Leg(
                underlying_symbol=symbol, option_trading_symbol=opt_bp["trading_symbol"], option_type="PE",
                quantity=opt_bp["quantity"], lot_size=opt_bp["lot_size"], entry_price=opt_bp["avg_price"],
                order_id="", product_type=config.OPTIONS_PRODUCT, reconciled=True,
            ))
            await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, opt_bp["trading_symbol"])

        # BASKET state whenever a futures leg is present (whether alone -
        # the grandfathered/incomplete shape - or paired with its PE);
        # PE_HEDGE state only when it's a lone PE with no futures at all.
        state = "BASKET" if fut_bp else "PE_HEDGE"
        positions.append(BasketHedgePosition(underlying_symbol=symbol, state=state, legs=legs))
        logger.info(
            "%s: reconciling as basket_hedge state=%s legs=%s",
            symbol, state, [l.option_type for l in legs],
        )

    return positions


# --------------------------------------------------------------------------- #
# Dual-timeframe Supertrend crossover signal (user request 31 Aug 2026)
# --------------------------------------------------------------------------- #
@dataclass
class SupertrendState:
    """The last TWO fully-closed candles' relationship to the Supertrend
    line for one (symbol, timeframe) - enough to detect an actual
    crossover (a state CHANGE), not just a current side. `is_above`/
    `prev_is_above` are None only if there weren't enough candles yet to
    compute a Supertrend value for that bar (see _fetch_supertrend_state)."""
    candle_start: Optional[datetime]
    close: float
    supertrend: float
    is_above: bool
    prev_close: float
    prev_supertrend: float
    prev_is_above: bool

    @property
    def crossed_above(self) -> bool:
        """True only on the candle where price flips from AT-OR-BELOW to
        ABOVE the Supertrend line - a real transition, not merely "is
        above right now" (which stays true for every candle of an
        established uptrend, not just the crossing one)."""
        return (not self.prev_is_above) and self.is_above

    @property
    def crossed_below(self) -> bool:
        """Mirror of crossed_above, for the downside."""
        return self.prev_is_above and not self.is_above


# (fetched_at, SupertrendState) per (symbol, interval_minutes) - entirely
# separate from Options/dhan_client.py's own single-timeframe
# _supertrend_cache (see this module's own docstring for why). Throttled
# by config.SUPERTREND_REFRESH_SECONDS, same rate-limit-avoidance
# rationale as that other cache.
_supertrend_state_cache: dict[tuple[str, int], tuple[datetime, Optional[SupertrendState]]] = {}


def _fetch_supertrend_state_once(symbol: str, interval_minutes: int) -> Optional[SupertrendState]:
    """Blocking (REST calls) - always call via run_in_executor. Fetches
    today's `interval_minutes` candles for `symbol` and computes the
    Supertrend line (via the shared, pure `_compute_supertrend`), keeping
    the last TWO fully-closed bars so a genuine crossover can be told
    apart from an established trend. Returns None if there isn't enough
    data yet (illiquid symbol, very early in the session, etc.) - callers
    treat that as "no signal", never as a false crossover."""
    security_id = dhan_wrapper._equity_security_id(symbol)
    today = _now_ist().strftime("%Y-%m-%d")
    resp = _retry(
        dhan_wrapper.client.Dhan.intraday_minute_data,
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=today, to_date=today, interval=interval_minutes,
    )
    data = resp.get("data") or {}
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    timestamps = data.get("timestamp") or []

    period = config.SUPERTREND_PERIOD
    # Drop a still-forming last candle - only a fully-closed candle's
    # close should ever drive a "crossed" signal (same guard Options/
    # dhan_client.py's own refresh_supertrend_signal uses).
    if timestamps:
        last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
        if _now_ist() < last_candle_start + timedelta(minutes=interval_minutes):
            highs, lows, closes, timestamps = highs[:-1], lows[:-1], closes[:-1], timestamps[:-1]

    # Need period+1 candles for the FIRST computable Supertrend bar, one
    # more on top of that so there's a PREVIOUS bar to compare against for
    # an actual crossover (period+2 total).
    if len(closes) < period + 2:
        return None

    supertrend = _compute_supertrend(highs, lows, closes, period=period, multiplier=config.SUPERTREND_MULTIPLIER)
    if supertrend[-1] is None or supertrend[-2] is None:
        return None

    return SupertrendState(
        candle_start=datetime.fromtimestamp(timestamps[-1], tz=IST) if timestamps else None,
        close=closes[-1], supertrend=supertrend[-1], is_above=closes[-1] > supertrend[-1],
        prev_close=closes[-2], prev_supertrend=supertrend[-2], prev_is_above=closes[-2] > supertrend[-2],
    )


async def _fetch_supertrend_state(symbol: str, interval_minutes: int) -> Optional[SupertrendState]:
    """Cached, throttled wrapper around _fetch_supertrend_state_once - see
    config.SUPERTREND_REFRESH_SECONDS. Swallows and logs any fetch
    failure (transient Dhan REST hiccup, illiquid symbol, etc.) as "no
    signal" rather than raising into the monitor loop - one symbol's data
    problem must never stop every other symbol's own tick from running."""
    key = (symbol, interval_minutes)
    cached = _supertrend_state_cache.get(key)
    if cached and (_now_ist() - cached[0]).total_seconds() < config.SUPERTREND_REFRESH_SECONDS:
        return cached[1]
    loop = asyncio.get_running_loop()
    try:
        state = await loop.run_in_executor(None, _fetch_supertrend_state_once, symbol, interval_minutes)
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch %d-min Supertrend state", symbol, interval_minutes)
        state = None
    _supertrend_state_cache[key] = (_now_ist(), state)
    return state


# (as_of_date, prev_close, confirmed) per symbol - `prev_close` is
# fetched (alongside today's open, in one OHLC call) at most ONCE PER
# SYMBOL PER TRADING DAY, the same as the old gap-up cache this replaced.
# `confirmed` starts as whatever the OPEN-vs-prev_close comparison gave
# and then LATCHES to True permanently the first time it becomes true -
# see _is_price_confirmed_above_prev_close's own docstring for why this
# is a one-way gate ("becomes active"), not a per-tick crossover that can
# flip back off. Self-invalidates on a new day simply because
# `cached[0] == today` stops matching - no separate day-reset call needed.
_price_confirmation_cache: dict[str, tuple[date, float, bool]] = {}


async def _is_price_confirmed_above_prev_close(symbol: str) -> bool:
    """ENTRY price gate (user's own wording, updated 1 Sep 2026 - RELAXED
    from a strict gap-up: "an explicit gap up is not mandatory, however
    when market opens stock price should rise or be greater or equal
    than yesterday's stock close price or the entry condition becomes
    active when current price cross above yesterday close price"). True
    as soon as EITHER:
      - today's open >= yesterday's close (checked once, at the first
        call of the trading day), OR
      - the current price has, at any point since, reached or crossed
        above yesterday's close (checked on every subsequent call until
        it fires).
    Once True for a symbol on a given trading day this LATCHES - it never
    re-checks or reverts to False later that same day even if price
    later pulls back below yesterday's close, since the rule describes a
    gate "becoming active" (a one-way state), not a live crossover that
    can flip back off the way the Supertrend checks can.

    Cheap by construction: the FIRST call of the day is one OHLC fetch
    (today's open + yesterday's close together, via
    get_today_open_and_prev_close) - the same single-call shape the old
    gap-up check used. Every call after that, until confirmed, is just a
    plain LTP fetch (get_option_ltp - already proven generic across
    instrument types elsewhere in this codebase) compared against the
    ALREADY-cached prev_close, no repeated OHLC calls. Once confirmed,
    zero further REST calls for that symbol for the rest of the day."""
    today = _now_ist().date()
    loop = asyncio.get_running_loop()
    cached = _price_confirmation_cache.get(symbol)

    if cached and cached[0] == today:
        _, prev_close, confirmed = cached
        if confirmed:
            return True
        try:
            current_price = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not fetch current price for the price-confirmation check", symbol)
            return False
        confirmed_now = current_price >= prev_close
        if confirmed_now:
            _price_confirmation_cache[symbol] = (today, prev_close, True)
            logger.info(
                "%s: price confirmed at/above yesterday's close intraday - current price %.2f >= "
                "previous close %.2f", symbol, current_price, prev_close,
            )
        return confirmed_now

    try:
        today_open, prev_close = await loop.run_in_executor(
            None, dhan_wrapper.get_today_open_and_prev_close, symbol
        )
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch today's open/previous close for the price-confirmation check", symbol)
        return False

    confirmed = today_open >= prev_close
    _price_confirmation_cache[symbol] = (today, prev_close, confirmed)
    if confirmed:
        logger.info(
            "%s: price confirmed at/above yesterday's close at market open - open %.2f >= "
            "previous close %.2f", symbol, today_open, prev_close,
        )
    return confirmed


async def _evaluate_watchlist_entry_signal(symbol: str) -> bool:
    """ENTRY rule (user's own wording, updated 1 Sep 2026): "an explicit
    gap up is not mandatory, however when market opens stock price
    should rise or be greater or equal than yesterday's stock close
    price or the entry condition becomes active when current price cross
    above yesterday close price... 5 min close cross above super trend
    with 1 min close greater than or crossed above 1 min super trend."
    The price-confirmation check runs FIRST and short-circuits the (more
    REST-expensive, two-timeframe) Supertrend checks entirely when it
    fails - it's also the cheaper, more cacheable check (see
    _is_price_confirmed_above_prev_close's own docstring: at most one
    OHLC call per symbol per day plus cheap LTP polls until it latches,
    vs the Supertrend checks' own SWING_SUPERTREND_REFRESH_SECONDS-
    throttled but still much more frequent refresh).

    The 1-min half of the Supertrend condition is written as two explicit
    checks (`is_above` OR `crossed_above`) even though `crossed_above`
    already implies `is_above` - kept both so this reads as a direct,
    auditable translation of the stated rule rather than a logically-
    equivalent but less traceable shortcut."""
    if not await _is_price_confirmed_above_prev_close(symbol):
        return False

    entry_tf = await _fetch_supertrend_state(symbol, config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES)
    if entry_tf is None or not entry_tf.crossed_above:
        return False

    confirm_tf = await _fetch_supertrend_state(symbol, config.SUPERTREND_CONFIRM_TIMEFRAME_MINUTES)
    if confirm_tf is None:
        return False
    confirmed = confirm_tf.is_above or confirm_tf.crossed_above

    if confirmed:
        logger.info(
            "%s: ENTRY signal - price confirmed at/above prev close, %d-min close %.2f crossed above Supertrend %.2f, "
            "%d-min close %.2f %s its Supertrend %.2f",
            symbol, config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES, entry_tf.close, entry_tf.supertrend,
            config.SUPERTREND_CONFIRM_TIMEFRAME_MINUTES, confirm_tf.close,
            "crossed above" if confirm_tf.crossed_above else "is above", confirm_tf.supertrend,
        )
    return confirmed


async def _evaluate_basket_exit_signal(symbol: str, basket: Basket) -> Optional[str]:
    """EXIT rule (user's own wording, 31 Aug 2026): "5 min close price
    cross below super trend." Mutually exclusive with the entry rule by
    construction (a single candle can't be both a crossed-above and a
    crossed-below at once), so this can never immediately re-fire on the
    very candle that justified the basket's own entry - no extra
    entry-candle guard needed the way Options/Futures/Luxury's own
    SUPERTREND_EXIT feature requires for its own (differently-shaped)
    check."""
    state = await _fetch_supertrend_state(symbol, config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES)
    if state is None or not state.crossed_below:
        return None
    logger.info(
        "%s: EXIT signal - %d-min close %.2f crossed below Supertrend %.2f",
        symbol, config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES, state.close, state.supertrend,
    )
    return "SUPERTREND_5MIN_EXIT"


async def _basket_monitor_tick() -> None:
    """The ORIGINAL "basket" mode tick body - unchanged behavior, just
    factored out of monitor_loop 1 Sep 2026 when config.STRATEGY_MODE was
    introduced (see monitor_loop's own docstring for the mode dispatch).
    Evaluates the entry signal for every watchlist symbol (removing it
    from the watchlist on a successful auto-entry - no reason to keep
    evaluating a stock once it has a live basket) and the exit signal for
    every live basket. Paced (a small sleep between watchlist symbols)
    the same way rank_and_pick_top_stocks() paces its own sequential Dhan
    calls elsewhere in this codebase."""
    watchlist_symbols = await watchlist_store.symbols()
    for i, symbol in enumerate(watchlist_symbols):
        if i > 0:
            await asyncio.sleep(0.35)
        if await _evaluate_watchlist_entry_signal(symbol):
            result = await enter_basket_for_stock(symbol)
            if result.get("status") == "entered":
                await watchlist_store.remove_symbol(symbol)
    for symbol, basket in list(basket_store.live_baskets.items()):
        reason = await _evaluate_basket_exit_signal(symbol, basket)
        if reason:
            await _exit_basket(symbol, basket, reason)


async def _sequential_monitor_tick() -> None:
    """The "sequential" mode tick body (added 1 Sep 2026) - see this
    module's own SEQUENTIAL mode section for the full state-machine
    diagram. Unlike basket mode, a symbol is NEVER removed from the
    watchlist here - it needs continuous evaluation for as long as it's
    under active sequential management: while holding futures, to watch
    for the exit signal; while holding PE, to watch for the entry signal
    re-firing (the "keep this loop going" transition). Also watches
    every symbol CURRENTLY holding a leg even if it was hand-removed
    from data/watchlist mid-loop - a symbol's own held leg always keeps
    it under management until it naturally returns to NONE (the PE
    loss-cap exit)."""
    watchlist_symbols = await watchlist_store.symbols()
    live_legs = dict(sequential_store.live_legs)
    all_symbols = list(dict.fromkeys(list(watchlist_symbols) + list(live_legs.keys())))

    for i, symbol in enumerate(all_symbols):
        if i > 0:
            await asyncio.sleep(0.35)
        leg = live_legs.get(symbol)
        if leg is None:
            if await _evaluate_watchlist_entry_signal(symbol):
                await _enter_futures_for_stock(symbol)
        elif leg.option_type == "FUT":
            reason = await _evaluate_basket_exit_signal(symbol, None)
            if reason:
                await _swap_futures_to_pe(symbol, leg)
        else:  # "PE"
            if await _evaluate_watchlist_entry_signal(symbol):
                await _swap_pe_to_futures(symbol, leg)
            else:
                pe_reason = await _evaluate_pe_exit_signal(symbol, leg)
                if pe_reason:
                    await _exit_pe_to_watching(symbol, leg, pe_reason)


async def _basket_hedge_monitor_tick() -> None:
    """The "basket_hedge" mode tick body (added 1 Sep 2026) - entry side
    behaves like plain basket mode (a symbol leaves the watchlist once it
    enters a basket); a symbol whose basket has already swapped into its
    PE hedge is NOT on the watchlist any more (basket entry removed it),
    so - same as sequential mode's own tick - every symbol CURRENTLY held
    (BASKET or PE_HEDGE state) is evaluated too, watchlist or not, until
    it naturally returns to plain watching via one of the PE hedge's own
    3 exit conditions."""
    watchlist_symbols = await watchlist_store.symbols()
    live_positions = dict(basket_hedge_store.live_positions)
    all_symbols = list(dict.fromkeys(list(watchlist_symbols) + list(live_positions.keys())))

    for i, symbol in enumerate(all_symbols):
        if i > 0:
            await asyncio.sleep(0.35)
        position = live_positions.get(symbol)
        if position is None:
            if await _evaluate_watchlist_entry_signal(symbol):
                result = await _enter_basket_hedge_for_stock(symbol)
                if result.get("status") == "entered":
                    await watchlist_store.remove_symbol(symbol)
        elif position.state == "BASKET":
            reason = await _evaluate_basket_exit_signal(symbol, None)
            if reason:
                await _exit_basket_hedge_to_pe(symbol, position)
        else:  # "PE_HEDGE"
            pe_leg = position.legs[0]
            reason = await _evaluate_pe_hedge_exit_signal(symbol, pe_leg)
            if reason:
                await _exit_pe_hedge_to_watching(symbol, pe_leg, reason)


async def monitor_loop() -> None:
    """Runs forever, ALWAYS - see config.py's own docstring for why this
    keeps running even when config.STRATEGY_ENABLED is False (so no
    restart is needed later to pick up the flag flipping), doing nothing
    at all in that case. Re-syncs the watchlist from data/watchlist every
    tick regardless of STRATEGY_ENABLED (user request 31 Aug 2026 - a
    hand-edit takes effect within one tick, no restart needed, same
    hot-reload UX choppy_stocks.py already established).

    Dispatches by config.STRATEGY_MODE each tick (added 1 Sep 2026,
    extended to 3-way 1 Sep 2026): "basket" runs _basket_monitor_tick
    (the ORIGINAL, unchanged mechanics), "sequential" runs
    _sequential_monitor_tick (the futures<->PE loop), "basket_hedge" runs
    _basket_hedge_monitor_tick (basket entry, single-PE-hedge exit) - see
    config.py's own STRATEGY_MODE docstring for why all three coexist
    rather than one replacing another. All three stores' own daily reset
    runs every tick regardless of which mode is currently active (cheap,
    keeps a closed-today log from going stale across a mode switch)."""
    logger.info(
        "Swing monitor loop started. strategy_enabled=%s strategy_mode=%s",
        config.STRATEGY_ENABLED, config.STRATEGY_MODE,
    )
    while True:
        try:
            await basket_store.maybe_reset_for_new_day()
            await sequential_store.maybe_reset_for_new_day()
            await basket_hedge_store.maybe_reset_for_new_day()
            await watchlist_store.sync_from_file()
            if config.STRATEGY_ENABLED:
                if config.STRATEGY_MODE == "basket":
                    await _basket_monitor_tick()
                elif config.STRATEGY_MODE == "basket_hedge":
                    await _basket_hedge_monitor_tick()
                else:
                    await _sequential_monitor_tick()
        except Exception:  # noqa: BLE001
            logger.exception("Error in Swing monitor loop tick")
        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)
