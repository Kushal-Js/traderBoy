"""
Core strategy logic for the Swing package - user request 31 Aug 2026.
Entry and exit CONDITION logic is deliberately not built yet (the user
will define it later, see config.py's own module docstring) - what's
built here is the MECHANICS a future signal will plug into:

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
  - monitor_loop(): runs forever, ALWAYS (even when config.STRATEGY_ENABLED
    is False - see config.py's own docstring for why the loop still runs
    rather than not starting at all). Currently a no-op tick over the
    watchlist and live baskets, since there's no entry/exit signal to
    evaluate yet. _evaluate_watchlist_entry_signal() and
    _evaluate_basket_exit_signal() are the two clearly-marked EXTENSION
    POINTS for that future logic - wire it in there without needing to
    touch anything else in this file.

Participates in cross_strategy_registry.py the same way Options/Futures/
Luxury already do (claims the underlying for the full duration of a
basket entry attempt) - Swing places real orders on the same shared Dhan
account, so it's exposed to the exact same cross-strategy race that
registry exists to close.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from trade_history import attribute_open_broker_position
import cross_strategy_registry

from . import config
from .dhan_client import OrderStatus, dhan_wrapper
from .position_store import Basket, Leg, basket_store
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

    # Cross-strategy claim held for this ENTIRE function - Options/
    # Futures/Luxury all place real orders on this same Dhan account, so
    # Swing is exposed to the exact same race cross_strategy_registry
    # exists to close (see its own module docstring).
    if not await cross_strategy_registry.try_claim(symbol, "Swing"):
        logger.info("%s: skipped - another strategy is currently entering it", symbol)
        return {"symbol": symbol, "status": "skipped", "reason": "claimed_by_another_strategy"}

    try:
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
            return {
                "symbol": symbol, "status": "entered",
                "futures_leg": {"trading_symbol": fut.trading_symbol, "entry_price": futures_fill_price, "quantity": fut_qty},
                "option_leg": {"trading_symbol": atm.trading_symbol, "entry_price": option_fill_price, "quantity": option_qty},
            }
        except Exception as exc:  # noqa: BLE001
            await basket_store.release_symbol(symbol)
            logger.exception("%s: unexpected error entering basket", symbol)
            return {"symbol": symbol, "status": "error", "reason": str(exc)}
    finally:
        await cross_strategy_registry.release_claim(symbol, "Swing")


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


async def _square_off_all(reason: str) -> None:
    baskets = dict(basket_store.live_baskets)
    if not baskets:
        return
    logger.info("Swing square-off triggered (%s) for %d open basket(s)", reason, len(baskets))
    for symbol, basket in baskets.items():
        await _exit_basket(symbol, basket, reason)


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


# --------------------------------------------------------------------------- #
# Watchlist / exit signal - EXTENSION POINTS, deliberately no-ops today
# --------------------------------------------------------------------------- #
async def _evaluate_watchlist_entry_signal(symbol: str) -> bool:
    """EXTENSION POINT - entry condition logic goes here once the user
    defines it (see config.py's own module docstring). Always returns
    False today - no signal exists yet, so nothing on the watchlist ever
    auto-enters. The only way a basket opens right now is the explicit
    POST /chartink/webhook-swing-enter."""
    return False


async def _evaluate_basket_exit_signal(symbol: str, basket: Basket) -> Optional[str]:
    """EXTENSION POINT - exit condition logic goes here once the user
    defines it (see config.py's own module docstring). Always returns
    None today - a basket only ever closes via the manual
    POST /swing/square-off-now kill switch until this is filled in."""
    return None


async def monitor_loop() -> None:
    """Runs forever, ALWAYS - see config.py's own docstring for why this
    keeps running even when config.STRATEGY_ENABLED is False (so no
    restart is needed later to pick up the flag flipping), doing nothing
    at all in that case. Currently a no-op tick either way over the
    watchlist and live baskets - _evaluate_watchlist_entry_signal/
    _evaluate_basket_exit_signal above are the extension points for the
    user's own future logic; nothing else in this file needs to change
    once those are filled in."""
    logger.info("Swing monitor loop started. strategy_enabled=%s", config.STRATEGY_ENABLED)
    while True:
        try:
            await basket_store.maybe_reset_for_new_day()
            if config.STRATEGY_ENABLED:
                for symbol in await watchlist_store.symbols():
                    if await _evaluate_watchlist_entry_signal(symbol):
                        await enter_basket_for_stock(symbol)
                for symbol, basket in list(basket_store.live_baskets.items()):
                    reason = await _evaluate_basket_exit_signal(symbol, basket)
                    if reason:
                        await _exit_basket(symbol, basket, reason)
        except Exception:  # noqa: BLE001
            logger.exception("Error in Swing monitor loop tick")
        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)
