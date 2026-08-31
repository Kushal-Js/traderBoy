"""
Core strategy logic for the Futures package - PLACEHOLDER: currently buys
ATM CE options via the identical mechanics as Options/trading_engine.py
(that file's docstring/comments cover the full rationale behind each
piece; this is a near-verbatim copy, not reimplemented from scratch, so
the two strategies can't silently drift apart in how they rank/enter/exit
while both still do the same thing). Adapted only where it must differ:

  - reconcile_broker_positions() (added 31 Aug 2026, user request) now
    DOES run here, unlike originally - but filtered through
    trade_history.attribute_open_broker_position first, not a blind
    import: Dhan's get_open_fno_positions() returns every open FNO
    position in the account with no notion of which strategy placed it -
    the same call Options' own reconciliation uses would otherwise
    re-import Options' own live positions into this package's separate
    position_store too, and both strategies could then try to
    independently manage (and exit) the same real position. Our own
    persistent opened/closed-position history (trade_history.py) is what
    actually distinguishes ownership now, since Dhan's data never can -
    see that module's own docstring and NOTES.md's updated design-
    decision entry for the full mechanism and its real limitation (a
    position that predates this logging, or was placed manually, is still
    safely skipped rather than guessed at).
  - CE only - no prefer_highest=False/PE path is wired up in futures_main.py,
    since only one bullish webhook was requested for this package.

Flow:
  1. rank_and_pick_top_stocks() - from the Chartink alert's stock list,
     pick the top-N by today's %change (highest first).
  2. enter_positions_for_stocks() - for each qualifying stock (not already
     traded today, capacity available), find the ATM option and place a
     BUY MARKET order (AMO outside market hours).
  3. monitor_loop() - background asyncio loop, polls every
     MONITOR_INTERVAL_SECONDS, and exits a leg when target / stop-loss /
     trailing stop-loss / Supertrend is hit, or force-squares-off
     everything at SQUARE_OFF_TIME. Also re-syncs any order still queued
     as AMO.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from trade_history import attribute_open_broker_position

from . import config
from .dhan_client import OrderStatus, dhan_wrapper
from .position_store import EXIT_CLAIMED, OrderRecord, Position, position_store

logger = logging.getLogger("futures_trading_engine")


async def reconcile_broker_positions() -> list[Position]:
    """Near-verbatim copy of Options/trading_engine.py's own function -
    see this module's own docstring above for why this package now runs
    it too (31 Aug 2026, previously skipped entirely) and see that
    function's docstring for the full attribute_open_broker_position
    filtering rationale, identical here except "Futures" is the strategy
    this filters FOR (Options' own positions get skipped here, mirror-
    image of that file's own filter)."""
    loop = asyncio.get_running_loop()
    broker_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)

    positions: list[Position] = []
    for bp in broker_positions:
        avg_price = bp["avg_price"]
        if not avg_price:
            logger.warning(
                "Skipping reconciliation for %s - broker reported no average price.",
                bp["trading_symbol"],
            )
            continue

        owner = await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"])
        if owner != "Futures":
            logger.warning(
                "Skipping reconciliation for %s - attributed to %s (not Futures) by our own "
                "opened-position history. Real broker position is unaffected; this process just "
                "won't manage it. If this is wrong (e.g. a manually-placed position, or one that "
                "predates this logging), it needs manual handling.",
                bp["trading_symbol"], owner or "no strategy (no record found)",
            )
            continue

        positions.append(Position(
            underlying_symbol=bp["underlying_symbol"],
            option_trading_symbol=bp["trading_symbol"],
            option_type=bp["option_type"] or config.OPTION_TYPE,
            quantity=bp["quantity"],
            lot_size=bp["lot_size"],
            entry_price=avg_price,
            highest_price=avg_price,
            target_price=avg_price * (1 + config.TARGET_PCT),
            hard_stop_loss=avg_price * (1 - config.STOP_LOSS_PCT),
            order_id="",
            # Same reasoning as Options' own reconciliation - the broker's
            # positions API reports a human-readable product label, not
            # the code order_placement() needs; this package only ever
            # trades config.OPTIONS_PRODUCT itself, so that's always
            # correct here too. See NOTES.md bug #23.
            product_type=config.OPTIONS_PRODUCT,
            reconciled=True,
        ))

    for pos in positions:
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, pos.option_trading_symbol)

    return positions

IST = ZoneInfo(config.MARKET_TZ)


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_hhmm_today(hhmm: str) -> datetime:
    now = _now_ist()
    hour, minute = map(int, hhmm.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _todays_square_off_time() -> Optional[str]:
    """See Options/trading_engine.py's identical function - this package's
    own config.ENABLE_SQUARE_OFF/ENABLE_FRIDAY_SQUARE_OFF/FRIDAY_SQUARE_OFF_TIME."""
    if config.ENABLE_SQUARE_OFF:
        return config.SQUARE_OFF_TIME
    if config.ENABLE_FRIDAY_SQUARE_OFF and _now_ist().weekday() == 4:
        return config.FRIDAY_SQUARE_OFF_TIME
    return None


def is_past_square_off_time() -> bool:
    """See Options/trading_engine.py's is_past_square_off_time (bug #25) -
    identical rationale, this package's own SQUARE_OFF_TIME/ENABLE_SQUARE_OFF/
    Friday carve-out."""
    cutoff = _todays_square_off_time()
    if cutoff is None:
        return False
    return _now_ist() >= _parse_hhmm_today(cutoff)


def is_past_allowed_trading_time() -> bool:
    """See Options/trading_engine.py's identical function - same rationale,
    this package's own config.ENABLE_TRADING_TIME_LIMIT / ALLOWED_TRADING_TIME."""
    if not config.ENABLE_TRADING_TIME_LIMIT:
        return False
    return _now_ist() >= _parse_hhmm_today(config.ALLOWED_TRADING_TIME)


def _gen_tag(prefix: str, symbol: str) -> str:
    """Dhan's correlationId rejects special characters - see Options'
    equivalent for the live incident (GVT&D, DH-905) that made this
    necessary."""
    safe_symbol = re.sub(r"[^A-Za-z0-9]", "", symbol)
    suffix = "".join(random.choices(string.digits, k=6))
    return f"{prefix}-{safe_symbol[:6]}-{suffix}"[:25]


# --------------------------------------------------------------------------- #
# Step 1: rank stocks from the webhook payload by today's % change
# --------------------------------------------------------------------------- #
def rank_and_pick_top_stocks(
    stock_symbols: list[str], top_n: int = config.TOP_N_STOCKS, prefer_highest: bool = True
) -> list[tuple[str, float]]:
    """See Options/trading_engine.py's rank_and_pick_top_stocks - identical
    logic, reusing the shared dhan_wrapper's get_day_change_pct, including
    the config.SELECT_BOTTOM_N_STOCKS bottom-N/top-N selection toggle."""
    scored: list[tuple[str, float]] = []
    for i, symbol in enumerate(stock_symbols):
        if i > 0:
            time.sleep(0.35)
        try:
            pct = dhan_wrapper.get_day_change_pct(symbol)
            scored.append((symbol, pct))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping %s - could not fetch day change: %s", symbol, exc)

    scored.sort(key=lambda t: t[1], reverse=prefer_highest)
    if config.SELECT_BOTTOM_N_STOCKS:
        return scored[-top_n:] if top_n > 0 else []
    return scored[:top_n]


# --------------------------------------------------------------------------- #
# Step 2: enter positions
# --------------------------------------------------------------------------- #
async def _process_one_entry(symbol: str, option_type: str) -> dict:
    """See Options/trading_engine.py's identical function - factored out
    31 Aug 2026 (user request/resilience audit) so ranked stocks' entries
    run concurrently via asyncio.gather instead of sequentially. Safe as-is
    for the same reasons: reserve_symbol() was already atomically locked,
    and every exception path here was already caught locally into a result
    dict rather than left to propagate."""
    loop = asyncio.get_running_loop()

    if not await position_store.reserve_symbol(symbol, option_type):
        logger.info("%s: skipped - already open/in-flight, or no capacity", symbol)
        return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}

    try:
        already_open = await loop.run_in_executor(
            None, dhan_wrapper.has_open_position_for_underlying, symbol
        )
        if already_open:
            logger.warning("%s: skipped - broker already shows an open FNO position for it", symbol)
            await position_store.release_symbol(symbol)
            return {"symbol": symbol, "status": "skipped", "reason": "already_open_at_broker"}

        entry_result = await _enter_single_position(symbol, option_type)
        if entry_result.get("status") not in ("entered", "amo_placed", "pending_confirmation"):
            await position_store.release_symbol(symbol)
        return entry_result
    except Exception as exc:  # noqa: BLE001
        await position_store.release_symbol(symbol)
        logger.exception("Failed to enter position for %s", symbol)
        return {"symbol": symbol, "status": "error", "reason": str(exc)}


async def enter_positions_for_stocks(
    ranked_stocks: list[tuple[str, float]], option_type: str = config.OPTION_TYPE
) -> list[dict]:
    """See Options/trading_engine.py's enter_positions_for_stocks - identical
    logic (now concurrent via asyncio.gather, not sequential), this
    package's own position_store."""
    return await asyncio.gather(*[
        _process_one_entry(symbol, option_type) for symbol, _pct_change in ranked_stocks
    ])


async def _enter_single_position(symbol: str, option_type: str = config.OPTION_TYPE) -> dict:
    loop = asyncio.get_running_loop()

    atm = await loop.run_in_executor(
        None, dhan_wrapper.get_atm_option, symbol, option_type
    )

    if atm.expiry_date == _now_ist().date():
        # See Options/trading_engine.py's identical guard (NOTES.md bug #28)
        # for the full rationale - get_atm_option() already rolls forward
        # to next month's contract on expiry day; reaching here means even
        # that rolled-forward contract still expires today.
        logger.info(
            "%s: skipped - %s expires today and no later expiry is available yet",
            symbol, atm.trading_symbol,
        )
        return {
            "symbol": symbol,
            "status": "skipped_expiry_day",
            "option_trading_symbol": atm.trading_symbol,
            "expiry_date": str(atm.expiry_date),
        }

    quantity = atm.lot_size * config.QUANTITY_LOTS
    tag = _gen_tag(config.ORDER_TAG_PREFIX, symbol)

    await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, atm.trading_symbol)

    order_resp = await loop.run_in_executor(
        None, dhan_wrapper.place_market_order, atm.trading_symbol, quantity, "BUY", tag,
    )
    order_id = order_resp["order_id"]
    is_amo = order_resp["is_amo"]

    await position_store.record_order(OrderRecord(
        order_id=order_id,
        underlying_symbol=symbol,
        trading_symbol=atm.trading_symbol,
        transaction_type="BUY",
        quantity=quantity,
        status=OrderStatus.TRANSIT,
        is_amo=is_amo,
        lot_size=atm.lot_size,
        option_type=atm.option_type,
    ))

    result = await loop.run_in_executor(
        None, dhan_wrapper.wait_for_order_result, order_id, is_amo
    )
    await position_store.update_order_status(order_id, result.status, result.remark)

    if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
        await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, atm.trading_symbol)
        logger.warning(
            "BUY order %s for %s rejected: status=%s remark=%s",
            order_id, symbol, result.status, result.remark,
        )
        return {
            "symbol": symbol,
            "status": "rejected",
            "order_status": result.status,
            "remark": result.remark,
            "option_trading_symbol": atm.trading_symbol,
            "order_id": order_id,
        }

    if result.is_queued_amo:
        await position_store.release_order_ownership(order_id)
        logger.info(
            "BUY order %s for %s queued as AMO - will confirm fill next session.",
            order_id, symbol,
        )
        return {
            "symbol": symbol,
            "status": "amo_placed",
            "order_status": result.status,
            "option_trading_symbol": atm.trading_symbol,
            "quantity": quantity,
            "order_id": order_id,
        }

    if result.status not in OrderStatus.TERMINAL_STATUSES:
        # See Options/trading_engine.py's _enter_single_position (bug #22) -
        # defer to _sync_pending_orders instead of guessing a fill price.
        await position_store.release_order_ownership(order_id)
        logger.warning(
            "BUY order %s for %s still %s after poll budget - deferring to "
            "background sync instead of guessing a fill price.",
            order_id, symbol, result.status,
        )
        return {
            "symbol": symbol,
            "status": "pending_confirmation",
            "order_status": result.status,
            "option_trading_symbol": atm.trading_symbol,
            "quantity": quantity,
            "order_id": order_id,
        }

    fill_price = result.fill_price
    if not fill_price:
        fill_price = await loop.run_in_executor(
            None, dhan_wrapper.get_option_ltp, atm.trading_symbol
        )

    entry_candle_start = await _capture_supertrend_entry_candle(loop, symbol)

    position = Position(
        underlying_symbol=symbol,
        option_trading_symbol=atm.trading_symbol,
        option_type=atm.option_type,
        quantity=quantity,
        lot_size=atm.lot_size,
        entry_price=fill_price,
        highest_price=fill_price,
        target_price=fill_price * (1 + config.TARGET_PCT),
        hard_stop_loss=fill_price * (1 - config.STOP_LOSS_PCT),
        order_id=order_id,
        product_type=config.OPTIONS_PRODUCT,
        supertrend_entry_candle_start=entry_candle_start,
    )
    await position_store.add_position(position)

    logger.info(
        "BUY order %s FILLED for %s (%s): qty=%s entry_price=%s target=%.2f sl=%.2f",
        order_id, symbol, atm.trading_symbol, quantity, fill_price,
        position.target_price, position.hard_stop_loss,
    )

    return {
        "symbol": symbol,
        "status": "entered",
        "order_status": result.status,
        "option_trading_symbol": atm.trading_symbol,
        "quantity": quantity,
        "entry_price": fill_price,
        "order_id": order_id,
    }


# --------------------------------------------------------------------------- #
# Step 3: monitoring / exits
# --------------------------------------------------------------------------- #
async def _exit_position(symbol: str, position: Position, exit_price: float, reason: str) -> None:
    """See Options/trading_engine.py's _exit_position - identical logic,
    including the broker-reconciliation check after 2+ consecutive exit
    failures and the stale-pending-order cancel-before-retry check."""
    loop = asyncio.get_running_loop()

    try:
        stale_order_id = await loop.run_in_executor(
            None, dhan_wrapper.get_pending_order_id, position.option_trading_symbol, "SELL"
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: could not check for an already-outstanding SELL order before placing a new one - "
            "proceeding anyway", symbol,
        )
        stale_order_id = None
    if stale_order_id:
        logger.warning(
            "%s: found an already-outstanding SELL order %s for %s (likely surviving a restart) - "
            "cancelling it before placing a fresh exit order.",
            symbol, stale_order_id, position.option_trading_symbol,
        )
        try:
            await loop.run_in_executor(None, dhan_wrapper.cancel_order, stale_order_id)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not cancel stale SELL order %s - proceeding with a new order anyway",
                              symbol, stale_order_id)

    if position.exit_failure_count >= 2:
        try:
            broker_qty = await loop.run_in_executor(
                None, dhan_wrapper.get_broker_net_quantity, position.option_trading_symbol
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not reconcile broker position before retrying exit (attempt %d) - "
                "proceeding with the retry anyway", symbol, position.exit_failure_count,
            )
            broker_qty = None
        if broker_qty == 0:
            logger.warning(
                "%s: broker shows this position already flat after %d consecutive exit failures - "
                "reconciling locally as closed instead of retrying.",
                symbol, position.exit_failure_count,
            )
            mark_price = exit_price or position.highest_price
            await position_store.close_position(symbol, mark_price, "RECONCILED_ALREADY_FLAT")
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol)
            return

    tag = _gen_tag("Ext", symbol)
    try:
        order_resp = await loop.run_in_executor(
            None, dhan_wrapper.place_market_order,
            position.option_trading_symbol, position.quantity, "SELL", tag, position.product_type,
        )
    except Exception:  # noqa: BLE001
        logger.exception("SELL order failed for %s (%s) - backing off before retrying",
                          symbol, position.option_trading_symbol)
        await position_store.record_exit_failure(symbol)
        return

    try:
        order_id = order_resp["order_id"]
        is_amo = order_resp["is_amo"]
        await position_store.record_order(OrderRecord(
            order_id=order_id,
            underlying_symbol=symbol,
            trading_symbol=position.option_trading_symbol,
            transaction_type="SELL",
            quantity=position.quantity,
            status=OrderStatus.TRANSIT,
            is_amo=is_amo,
        ))
        await position_store.set_pending_exit_order(symbol, order_id, reason)

        result = await loop.run_in_executor(
            None, dhan_wrapper.wait_for_order_result, order_id, is_amo
        )
        await position_store.update_order_status(order_id, result.status, result.remark)

        if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
            logger.warning(
                "SELL order %s for %s rejected: status=%s remark=%s - backing off before retrying",
                order_id, symbol, result.status, result.remark,
            )
            await position_store.set_pending_exit_order(symbol, None)
            await position_store.record_exit_failure(symbol)
            return

        await position_store.clear_exit_failure(symbol)

        if result.is_queued_amo:
            logger.info(
                "SELL order %s for %s queued as AMO - will confirm fill next session.",
                order_id, symbol,
            )
            return

        final_exit_price = result.fill_price or exit_price
        await position_store.close_position(symbol, final_exit_price, reason)
        await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol)
        pnl = (final_exit_price - position.entry_price) * position.quantity
        logger.info(
            "SELL order %s FILLED for %s (%s): reason=%s entry=%s exit=%s qty=%s pnl=%.2f",
            order_id, symbol, position.option_trading_symbol, reason,
            position.entry_price, final_exit_price, position.quantity, pnl,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error resolving SELL order for %s (%s) - backing off before retrying",
                          symbol, position.option_trading_symbol)
        await position_store.record_exit_failure(symbol)


async def _get_ltp(trading_symbol: str) -> Optional[float]:
    """See Options/trading_engine.py's _get_ltp - identical staleness-driven
    REST fallback + cache re-priming, reusing the same shared dhan_wrapper
    instance/cache (Futures has no LTP cache of its own). The REST fallback
    is gated by dhan_wrapper.ltp_rest_fallback_semaphore - the SAME
    semaphore Options' own _get_ltp uses, since both strategies compete for
    the same real Dhan rate-limit budget."""
    loop = asyncio.get_running_loop()
    ltp = await loop.run_in_executor(None, dhan_wrapper.get_cached_option_ltp, trading_symbol)
    if ltp is not None:
        return ltp
    async with dhan_wrapper.ltp_rest_fallback_semaphore:
        ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, trading_symbol)
        await loop.run_in_executor(None, dhan_wrapper.note_rest_ltp, trading_symbol, ltp)
    return ltp


def _exit_on_cooldown(position: Position) -> bool:
    return bool(position.next_exit_retry_at and datetime.now() < position.next_exit_retry_at)


async def _capture_supertrend_entry_candle(loop, underlying_symbol: str) -> Optional[datetime]:
    """See Options/trading_engine.py's version - identical logic. Note the
    underlying Supertrend computation itself (period/multiplier/warmup) is
    governed by Options/config.py's values, since refresh_supertrend_signal
    lives in the shared dhan_client.py - see Futures/config.py's own
    docstring for why those knobs aren't duplicated here."""
    if not config.ENABLE_SUPERTREND_EXIT:
        return None
    await loop.run_in_executor(None, dhan_wrapper.refresh_supertrend_signal, underlying_symbol)
    return dhan_wrapper.get_cached_supertrend_candle_start(underlying_symbol)


def _supertrend_signal_for(position: Position) -> bool:
    """See Options/trading_engine.py's version - identical logic/rationale."""
    is_bearish = dhan_wrapper.get_cached_supertrend_bearish(position.underlying_symbol)
    if is_bearish is None:
        return False
    against_position = is_bearish if position.option_type == "CE" else (not is_bearish)
    if not against_position:
        return False
    candle_start = dhan_wrapper.get_cached_supertrend_candle_start(position.underlying_symbol)
    entry_candle_start = position.supertrend_entry_candle_start
    if candle_start is None or entry_candle_start is None:
        return True
    return candle_start > entry_candle_start


def _exit_reason_for(position: Position, ltp: float, supertrend_against_position: bool = False) -> Optional[str]:
    """See Options/trading_engine.py's version - identical logic, including
    the config.MAX_LOSS_PER_TRADE_RS absolute rupee-loss cap checked first
    and the config.PROFIT_PROTECTION_THRESHOLD_RS rupee profit-lock checked
    after TARGET_HIT."""
    loss_rs = (position.entry_price - ltp) * position.quantity
    if loss_rs >= config.MAX_LOSS_PER_TRADE_RS:
        return "MAX_LOSS_HIT"
    if ltp >= position.target_price:
        return "TARGET_HIT"
    peak_profit_rs = (position.highest_price - position.entry_price) * position.quantity
    if peak_profit_rs > config.PROFIT_PROTECTION_THRESHOLD_RS and ltp < position.highest_price:
        return "PROFIT_PROTECTION_HIT"
    trailing_sl = position.current_trailing_sl
    if ltp <= trailing_sl:
        return "TRAILING_SL_HIT" if trailing_sl > position.hard_stop_loss else "STOP_LOSS_HIT"
    if config.ENABLE_SUPERTREND_EXIT and supertrend_against_position:
        return "SUPERTREND_EXIT"
    return None


async def _check_one_position(symbol: str, position: Position) -> None:
    if position.pending_exit_order_id or _exit_on_cooldown(position):
        return

    try:
        ltp = await _get_ltp(position.option_trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception("Could not fetch LTP for %s", position.option_trading_symbol)
        return

    await position_store.update_highest_price(symbol, ltp)

    supertrend_against_position = False
    if config.ENABLE_SUPERTREND_EXIT:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, dhan_wrapper.refresh_supertrend_signal, position.underlying_symbol)
        supertrend_against_position = _supertrend_signal_for(position)

    reason = _exit_reason_for(position, ltp, supertrend_against_position)
    if reason and await position_store.try_start_exit(symbol):
        await _exit_position(symbol, position, ltp, reason)


async def on_price_tick(trading_symbol: str, ltp: float) -> None:
    """See Options/trading_engine.py's version - identical logic, this
    package's own position_store. Wired up in futures_main.py's lifespan
    via dhan_wrapper.add_price_tick_subscriber() - a list, not a single
    slot, specifically so this can coexist with Options' own subscriber
    without either silently overwriting the other (see dhan_client.py's
    _on_price_tick_subscribers docstring)."""
    try:
        match = next(
            ((sym, pos) for sym, pos in position_store.live_positions.items()
             if pos.option_trading_symbol == trading_symbol),
            None,
        )
        if not match:
            return
        symbol, position = match

        if position.pending_exit_order_id or _exit_on_cooldown(position):
            return

        await position_store.update_highest_price(symbol, ltp)
        supertrend_against_position = config.ENABLE_SUPERTREND_EXIT and _supertrend_signal_for(position)

        reason = _exit_reason_for(position, ltp, supertrend_against_position)
        if reason and await position_store.try_start_exit(symbol):
            await _exit_position(symbol, position, ltp, reason)
    except Exception:  # noqa: BLE001
        logger.exception("on_price_tick failed for %s", trading_symbol)


async def _square_off_all(reason: str) -> None:
    positions = dict(position_store.live_positions)
    if not positions:
        return
    logger.info("Square-off triggered (%s) for %d open position(s)", reason, len(positions))
    for symbol, position in positions.items():
        if position.pending_exit_order_id or _exit_on_cooldown(position):
            continue
        try:
            ltp = await _get_ltp(position.option_trading_symbol)
        except Exception:  # noqa: BLE001
            ltp = position.entry_price
        if not await position_store.try_start_exit(symbol):
            continue
        await _exit_position(symbol, position, ltp, reason)


async def _sync_pending_orders() -> None:
    """See Options/trading_engine.py's version - identical logic, operates
    only on orders this package itself placed (position_store.orders_today),
    so it works correctly without broker reconciliation."""
    loop = asyncio.get_running_loop()

    pending_entries = [
        o for o in position_store.orders_today.values()
        if o.transaction_type == "BUY"
        and o.status not in OrderStatus.TERMINAL_STATUSES
        and o.underlying_symbol not in position_store.live_positions
        and not o.owned_by_placer
    ]
    for order in pending_entries:
        try:
            result = await loop.run_in_executor(
                None, dhan_wrapper.refresh_order_status, order.order_id, order.is_amo
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not refresh AMO BUY order %s", order.order_id)
            continue

        await position_store.update_order_status(order.order_id, result.status, result.remark)

        if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
            logger.warning(
                "AMO BUY order %s for %s ended as %s - releasing reservation.",
                order.order_id, order.underlying_symbol, result.status,
            )
            await position_store.release_symbol(order.underlying_symbol)
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, order.trading_symbol)
            continue

        if result.status in OrderStatus.TERMINAL_STATUSES:
            fill_price = result.fill_price
            if not fill_price:
                fill_price = await loop.run_in_executor(
                    None, dhan_wrapper.get_option_ltp, order.trading_symbol
                )
            entry_candle_start = await _capture_supertrend_entry_candle(loop, order.underlying_symbol)
            position = Position(
                underlying_symbol=order.underlying_symbol,
                option_trading_symbol=order.trading_symbol,
                option_type=order.option_type or config.OPTION_TYPE,
                quantity=order.quantity,
                lot_size=order.lot_size or config.LOT_SIZE_FALLBACK,
                entry_price=fill_price,
                highest_price=fill_price,
                target_price=fill_price * (1 + config.TARGET_PCT),
                hard_stop_loss=fill_price * (1 - config.STOP_LOSS_PCT),
                order_id=order.order_id,
                product_type=config.OPTIONS_PRODUCT,
                supertrend_entry_candle_start=entry_candle_start,
            )
            await position_store.add_position(position)
            logger.info(
                "AMO BUY order %s for %s filled - position now live.",
                order.order_id, order.underlying_symbol,
            )

    positions = dict(position_store.live_positions)
    for symbol, position in positions.items():
        if not position.pending_exit_order_id or position.pending_exit_order_id == EXIT_CLAIMED:
            continue
        try:
            result = await loop.run_in_executor(
                None, dhan_wrapper.refresh_order_status, position.pending_exit_order_id, True
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not refresh AMO SELL order %s", position.pending_exit_order_id)
            continue

        await position_store.update_order_status(position.pending_exit_order_id, result.status, result.remark)

        if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
            logger.warning(
                "AMO SELL order %s for %s ended as %s - clearing so the next tick retries the exit.",
                position.pending_exit_order_id, symbol, result.status,
            )
            await position_store.set_pending_exit_order(symbol, None)
            continue

        if result.status in OrderStatus.TERMINAL_STATUSES:
            final_exit_price = result.fill_price or position.highest_price
            await position_store.close_position(symbol, final_exit_price, position.pending_exit_reason or "AMO_EXIT_FILLED")
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol)


async def monitor_loop() -> None:
    """Runs forever; polls open positions and enforces exits + a square-off
    on whichever days _todays_square_off_time() says apply one - see
    Options/trading_engine.py's identical structure and NOTES.md's design-
    decision entry."""
    logger.info("Futures monitor loop started.")
    squared_off_today_for: set = set()

    while True:
        try:
            await position_store.maybe_reset_for_new_day()
            await _sync_pending_orders()

            cutoff = _todays_square_off_time()
            if cutoff is not None:
                now = _now_ist()
                square_off_at = _parse_hhmm_today(cutoff)
                today_key = now.date()

                if now >= square_off_at and today_key not in squared_off_today_for:
                    reason = "EOD_SQUARE_OFF_FRIDAY" if not config.ENABLE_SQUARE_OFF else "EOD_SQUARE_OFF_3_15PM"
                    await _square_off_all(reason)
                    squared_off_today_for.add(today_key)
                elif now < square_off_at:
                    positions = list(position_store.live_positions.items())
                    await asyncio.gather(
                        *[_check_one_position(sym, pos) for sym, pos in positions]
                    )
            elif dhan_wrapper.is_market_open():
                positions = list(position_store.live_positions.items())
                await asyncio.gather(
                    *[_check_one_position(sym, pos) for sym, pos in positions]
                )
        except Exception:  # noqa: BLE001
            logger.exception("Error in Futures monitor loop tick")

        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)
