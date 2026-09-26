"""
Core Bollinger strategy logic - structurally mirrors Swing/trading_engine.py
(same monitor-loop/reconciliation/exit-order-management shape), simplified
throughout for this package's narrower v1 scope: NSE EQUITY underlyings
only, always a single-leg LONG CE or LONG PE (never futures/equity/short,
never MCX), no profit target (MAX_LOSS_HIT -> STOP_LOSS_HIT ->
TRAILING_STOP_HIT only - see Bollinger/position_store.py's own Position
docstring), no Friday/index square-off (out of v1 scope).

Every exit/order-sync mechanic below (the stale-order-cancel-and-broker-
quantity-reconcile sequence in _exit_position, the broker-side SL-L
already-filled check, the LTP-staleness forced exit) is a direct,
deliberately UNMODIFIED port of Swing/trading_engine.py's own (itself a
port of Options/trading_engine.py's incident-hardened design) - see those
files' own extensive comments for the real incidents that shaped each
piece. Reusing this exact machinery rather than reimplementing it is a
deliberate choice: this is real money with paper mode off from day one,
and this exact sequence has already been through months of live incident
hardening in this codebase.

Entry signal source is entirely different from Swing's - see
Bollinger/signals.py's own module docstring for the Bollinger-ribbon +
Vortex + pullback-pending-order state machine this reads instead of
Swing's regime/Supertrend.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string
from datetime import datetime
from typing import Optional

import fund_allocation
import paper_mode_control
from trade_history import append_jsonl, attribute_open_broker_position

from . import config, signals
from .position_store import (
    EXIT_CLAIMED, OrderRecord, Position, position_store,
)
from Swing.position_store import (
    broker_stop_trigger_and_limit, hard_stop_for, unrealized_pnl_rs,
)
from Options.dhan_client import IST, OrderResult, OrderStatus, dhan_wrapper

logger = logging.getLogger("bollinger_trading_engine")

_ltp_failure_since: dict[tuple[str, datetime], datetime] = {}

# Fairness rotation for the per-tick watchlist scan - same idiom as
# Swing/trading_engine.py's own _watchlist_scan_turn (see that module's
# docstring for the real VEDL incident this prevents).
_watchlist_scan_turn: dict[str, int] = {"i": 0}

_LTP_FETCH_TIMEOUT_SECONDS = 10.0
_ORDER_STATUS_TIMEOUT_SECONDS = 10.0
_ORDER_RESULT_TIMEOUT_SECONDS = 30.0

BOLLINGER_EVENTS_LOG_NAME = "bollinger_events"


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_hhmm_today(hhmm: str) -> datetime:
    now = _now_ist()
    hour, minute = map(int, hhmm.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _gen_tag(prefix: str, symbol: str) -> str:
    safe_symbol = re.sub(r"[^A-Za-z0-9]", "", symbol)
    suffix = "".join(random.choices(string.digits, k=6))
    return f"{prefix}-{safe_symbol[:6]}-{suffix}"[:25]


async def _record_bollinger_event(event: str, symbol: str, detail: dict) -> None:
    """Durable, queryable event log - history/<date>_bollinger_events.log.
    Direct port of Swing's own _record_swing_event."""
    record = {"event": event, "underlying_symbol": symbol, "logged_at": _now_ist().isoformat(), **detail}
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, append_jsonl, BOLLINGER_EVENTS_LOG_NAME, record)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not append Bollinger event record (%s, %s) - the action itself is unaffected, "
            "this is logging-only.", event, symbol,
        )


# --------------------------------------------------------------------------- #
# Signal evaluation
# --------------------------------------------------------------------------- #
async def _evaluate_entry_signal(symbol: str) -> Optional[tuple[str, float, float, float]]:
    """None unless the pending-order state machine actually FIRED on the
    newest confirmed bar this cycle - see Bollinger/signals.py's own
    module docstring for why a stale historical fire is never actionable.
    Returns (side, trigger_price, stop_price, last_close) on a real fire."""
    state = await signals.get_signal_state(symbol)
    if state is None or state.fired is None:
        return None
    return state.fired, state.fired_trigger_price, state.fired_stop_price, state.last_close


def _exit_reason_for(position: Position, ltp: float) -> Optional[str]:
    """Pure function - position_store.update_trailing must be called
    (under its own lock) BEFORE this, to keep best_price/trailing_armed/
    trailing_stop_price consistent with each other - see that method's
    own docstring for why this can't be two separate unlocked steps.
    Always LONG (every position this package ever opens)."""
    loss_rs = -unrealized_pnl_rs("LONG", position.entry_price, ltp, position.pnl_multiplier)
    if loss_rs >= config.MAX_LOSS_PROTECTION_RS:
        return "MAX_LOSS_HIT"
    active_stop = position.trailing_stop_price if position.trailing_armed else position.hard_stop_loss
    if ltp <= active_stop:
        return "TRAILING_STOP_HIT" if position.trailing_armed else "STOP_LOSS_HIT"
    return None


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
async def enter_position_for_stock(symbol: str, entry_signal: str, trigger_price: float,
                                    stop_price: float, last_close: float) -> dict:
    if not config.STRATEGY_ENABLED:
        return {"symbol": symbol, "status": "ignored", "reason": "strategy_disabled"}

    if not await position_store.reserve_symbol(symbol):
        return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}

    loop = asyncio.get_running_loop()
    try:
        option_type = "CE" if entry_signal == "BULLISH" else "PE"
        try:
            atm = await loop.run_in_executor(None, dhan_wrapper.get_liquid_atm_option, symbol, option_type)
            if atm is None:
                logger.info("%s: skipped - no liquid, actively-traded %s contract found nearby", symbol, option_type)
                return {"symbol": symbol, "status": "skipped", "reason": "no_liquid_contract_available"}
            if atm.expiry_date == _now_ist().date():
                logger.info("%s: skipped - %s expires today and no later expiry is available yet",
                            symbol, atm.trading_symbol)
                return {"symbol": symbol, "status": "skipped_expiry_day", "option_trading_symbol": atm.trading_symbol}
            trading_symbol, security_id, lot_size = atm.trading_symbol, atm.security_id, atm.lot_size
            quantity = lot_size * config.QUANTITY_LOTS
            exchange_segment, product_type = "NSE_FNO", config.OPTIONS_PRODUCT
            pnl_multiplier = quantity
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not resolve the OPTIONS instrument for entry", symbol)
            return {"symbol": symbol, "status": "error", "reason": "instrument_resolution_failed"}

        tag = _gen_tag(config.ORDER_TAG_PREFIX, symbol)

        if config.FUNDS_CHECK_ENABLED:
            try:
                price = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, trading_symbol)
                sufficient = await fund_allocation.has_sufficient_bucket_funds(
                    config.FUND_BUCKET, symbol,
                    [(security_id, product_type, quantity, price, exchange_segment)],
                    buffer_rs=config.FUNDS_CHECK_BUFFER_RS,
                )
            except Exception:  # noqa: BLE001
                logger.exception("%s: could not price the leg for the funds check - proceeding optimistically", symbol)
                sufficient = True
            if not sufficient:
                await _record_bollinger_event("ENTRY_SKIPPED_INSUFFICIENT_FUNDS", symbol, {})
                return {"symbol": symbol, "status": "skipped", "reason": "insufficient_funds", "trading_symbol": trading_symbol}

        transaction_type = "BUY"  # always LONG - a PE entry is itself a BUY, same convention as every OPTIONS package here

        # Duplicate-real-order guard - see Swing/trading_engine.py's own
        # identical guard for the real COALINDIA incident this prevents.
        try:
            existing_order_id = await loop.run_in_executor(
                None, dhan_wrapper.get_pending_order_id, trading_symbol, transaction_type, "NSE",
            )
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not check for an already-resting entry order - proceeding anyway", symbol)
            existing_order_id = None
        if existing_order_id:
            logger.warning(
                "%s: a %s order %s is already resting/pending at the broker for %s - NOT placing a duplicate.",
                symbol, transaction_type, existing_order_id, trading_symbol,
            )
            return {"symbol": symbol, "status": "already_pending", "order_id": existing_order_id,
                    "trading_symbol": trading_symbol}

        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, trading_symbol)
        order_resp = await loop.run_in_executor(
            None, dhan_wrapper.place_market_order, trading_symbol, quantity, transaction_type, tag, product_type,
        )
        order_id, is_amo = order_resp["order_id"], order_resp["is_amo"]
        await position_store.record_order(OrderRecord(
            order_id=order_id, underlying_symbol=symbol, trading_symbol=trading_symbol,
            transaction_type=transaction_type, quantity=quantity, status=OrderStatus.TRANSIT,
            is_amo=is_amo, lot_size=lot_size,
        ))

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, dhan_wrapper.wait_for_order_result, order_id, is_amo),
                timeout=_ORDER_RESULT_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not confirm entry order %s's fill status within %.0fs - treating as a failed entry",
                symbol, order_id, _ORDER_RESULT_TIMEOUT_SECONDS,
            )
            result = OrderResult(order_id=order_id, status=OrderStatus.TRANSIT, remark="order_confirmation_timeout",
                                  fill_price=0.0, filled_quantity=0, is_amo=is_amo)
        await position_store.update_order_status(order_id, result.status, result.remark)

        # Literal TRADED-only fill discipline - no AMO-promotion path for
        # entries, same rule as Swing's own (the real MAHABANK phantom-exit
        # incident this guards against).
        if result.status != OrderStatus.TRADED:
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, trading_symbol)
            logger.warning("%s: entry order %s did not reach TRADED (status=%s remark=%s) - treating as a failed entry",
                            symbol, order_id, result.status, result.remark)
            return {"symbol": symbol, "status": "failed", "order_status": result.status, "trading_symbol": trading_symbol}

        fill_price = result.fill_price or await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, trading_symbol)

        # Dynamic, per-trade stop percentage from the actual swing distance
        # this entry fired against - see Bollinger/config.py's own module
        # docstring and backtest_bollinger_vortex_9symbols_30day.py's
        # INTERPRETATION #5. Floored at config.MIN_STOP_PCT (the backtest's
        # own floor - a genuine, already-disclosed characteristic of this
        # exact parameter set, not a bug to work around here).
        stop_distance_underlying = abs(trigger_price - stop_price)
        stop_pct = stop_distance_underlying / last_close if last_close else config.MIN_STOP_PCT
        stop_pct = max(stop_pct, config.MIN_STOP_PCT)
        hard_stop_loss = hard_stop_for("LONG", fill_price, stop_pct)
        trailing_stop_dist = fill_price * stop_pct * config.TRAILING_STOP_FRACTION
        trailing_step = trailing_stop_dist * config.TRAILING_STEP_FRACTION

        stop_loss_order_id = None
        if config.BROKER_STOP_LOSS_ENABLED:
            trigger, limit = broker_stop_trigger_and_limit(
                "LONG", fill_price, pnl_multiplier, config.MAX_LOSS_PROTECTION_RS,
                config.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE, hard_stop_pct=stop_pct,
            )
            try:
                stop_tag = _gen_tag("SL", symbol)
                stop_resp = await loop.run_in_executor(
                    None, dhan_wrapper.place_stop_loss_limit_order, trading_symbol, quantity, "SELL",
                    trigger, limit, stop_tag, product_type,
                )
                stop_loss_order_id = stop_resp["order_id"]
                logger.info("%s: broker-side SELL STOP-LOSS LIMIT order %s placed for %s, trigger=%.2f limit=%.2f",
                            symbol, stop_loss_order_id, trading_symbol, trigger, limit)
                await position_store.record_order(OrderRecord(
                    order_id=stop_loss_order_id, underlying_symbol=symbol, trading_symbol=trading_symbol,
                    transaction_type="SELL", quantity=quantity, status="PENDING", is_amo=False,
                ))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "%s: could not place the broker-side stop-loss order for %s - proceeding without it, "
                    "the poll/tick-driven MAX_LOSS_HIT/STOP_LOSS_HIT check still protects this position",
                    symbol, trading_symbol,
                )

        position = Position(
            underlying_symbol=symbol, trading_symbol=trading_symbol, resolved_option_type=option_type,
            instrument_side="LONG", exchange_segment=exchange_segment, product_type=product_type,
            quantity=quantity, lot_size=lot_size, entry_price=fill_price, best_price=fill_price,
            stop_pct=stop_pct, hard_stop_loss=hard_stop_loss,
            trailing_stop_dist=trailing_stop_dist, trailing_step=trailing_step,
            pnl_multiplier=pnl_multiplier, order_id=order_id,
            entry_candle_start=None, stop_loss_order_id=stop_loss_order_id,
        )
        await position_store.add_position(position)
        await _record_bollinger_event("POSITION_OPENED", symbol, {
            "entry_signal": entry_signal, "trading_symbol": trading_symbol, "entry_price": fill_price,
            "quantity": quantity, "stop_pct": stop_pct, "trigger_price": trigger_price, "stop_price": stop_price,
        })
        return {"symbol": symbol, "status": "entered", "trading_symbol": trading_symbol, "entry_price": fill_price}
    except Exception:  # noqa: BLE001
        logger.exception("%s: unexpected error entering position", symbol)
        return {"symbol": symbol, "status": "error"}
    finally:
        if symbol not in position_store.live_positions:
            await position_store.record_failed_entry(symbol)
            await position_store.release_symbol(symbol)


# --------------------------------------------------------------------------- #
# Exit - direct port of Swing/trading_engine.py's own proven sequence
# --------------------------------------------------------------------------- #
async def _check_broker_stop_already_filled(symbol: str, position: Position) -> bool:
    if not config.BROKER_STOP_LOSS_ENABLED or not position.stop_loss_order_id:
        return False
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, dhan_wrapper.check_if_order_filled, position.stop_loss_order_id),
            timeout=_ORDER_STATUS_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not check broker stop-loss order %s status - falling through to "
                          "the normal poll/tick-driven check this tick", symbol, position.stop_loss_order_id)
        return False
    if result is None:
        return False
    if result.status == OrderStatus.TRADED:
        final_exit_price = result.fill_price or position.hard_stop_loss
        logger.info("%s: broker-side stop-loss order %s ALREADY FILLED - closing at the real fill price %.2f",
                    symbol, position.stop_loss_order_id, final_exit_price)
        await position_store.close_position(symbol, final_exit_price, "STOP_LOSS_HIT")
        await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)
        return True
    logger.warning("%s: broker-side stop-loss order %s ended as %s without firing - this position now relies "
                    "solely on the regular poll/tick-driven check", symbol, position.stop_loss_order_id, result.status)
    await position_store.clear_stop_loss_order_id(symbol)
    return False


async def _exit_position(symbol: str, position: Position, exit_price: float, reason: str) -> None:
    """Caller MUST have already claimed via position_store.try_start_exit.
    Direct, deliberately unmodified port of Swing/trading_engine.py's own
    _exit_position, simplified: always a SELL (every position here is
    LONG), always NSE_FNO."""
    loop = asyncio.get_running_loop()
    net_qty_fn = dhan_wrapper.get_broker_net_quantity

    try:
        stale_order_id = await loop.run_in_executor(
            None, dhan_wrapper.get_pending_order_id, position.trading_symbol, "SELL", "NSE",
        )
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not check for an already-outstanding SELL order before placing a new "
                          "one - proceeding anyway", symbol)
        stale_order_id = None

    if not stale_order_id and config.BROKER_STOP_LOSS_ENABLED and position.stop_loss_order_id:
        stale_order_id = position.stop_loss_order_id

    if stale_order_id:
        logger.warning("%s: found an already-outstanding SELL order %s for %s - cancelling it before "
                        "placing a fresh exit order.", symbol, stale_order_id, position.trading_symbol)
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, dhan_wrapper.cancel_order, stale_order_id),
                timeout=_ORDER_STATUS_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not cancel stale SELL order %s - proceeding with a new order anyway",
                              symbol, stale_order_id)

        try:
            broker_qty = await loop.run_in_executor(None, net_qty_fn, position.trading_symbol, position.exchange_segment)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not reconcile broker quantity after cancelling stale order %s - "
                              "proceeding with the stored quantity (%d)", symbol, stale_order_id, position.quantity)
            broker_qty = None
        if broker_qty is not None and broker_qty != position.quantity:
            if broker_qty == 0:
                logger.warning("%s: broker shows this position already FLAT after cancelling stale order %s - "
                                "reconciling as closed using that order's own real fill price.", symbol, stale_order_id)
                try:
                    stale_result = await asyncio.wait_for(
                        loop.run_in_executor(None, dhan_wrapper.refresh_order_status, stale_order_id),
                        timeout=_ORDER_STATUS_TIMEOUT_SECONDS,
                    )
                    final_exit_price = stale_result.fill_price or exit_price
                except Exception:  # noqa: BLE001
                    logger.exception("%s: could not fetch stale order %s's own fill price - using %.2f instead",
                                      symbol, stale_order_id, exit_price)
                    final_exit_price = exit_price
                await position_store.close_position(symbol, final_exit_price, reason)
                await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)
                return
            logger.warning("%s: broker shows only %d qty left (stored position says %d) after cancelling stale "
                            "order %s - a PARTIAL fill happened. Exiting only the real remaining %d qty.",
                            symbol, broker_qty, position.quantity, stale_order_id, broker_qty)
            position.pnl_multiplier = round(position.pnl_multiplier * broker_qty / position.quantity)
            position.quantity = broker_qty

    if position.exit_failure_count >= 1:
        try:
            broker_qty = await loop.run_in_executor(None, net_qty_fn, position.trading_symbol, position.exchange_segment)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not reconcile broker position before retrying exit (attempt %d) - "
                              "proceeding with the retry anyway", symbol, position.exit_failure_count)
            broker_qty = None
        if broker_qty == 0:
            logger.warning("%s: broker shows this position already flat after %d exit failure(s) - reconciling "
                            "locally as closed instead of retrying.", symbol, position.exit_failure_count)
            await position_store.close_position(symbol, exit_price or position.best_price, "RECONCILED_ALREADY_FLAT")
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)
            return

    tag = _gen_tag("Ext", symbol)
    try:
        order_resp = await loop.run_in_executor(
            None, dhan_wrapper.place_market_order, position.trading_symbol, position.quantity, "SELL",
            tag, position.product_type,
        )
    except Exception:  # noqa: BLE001
        logger.exception("SELL exit order failed for %s (%s) - backing off before retrying", symbol, position.trading_symbol)
        await position_store.record_exit_failure(symbol)
        return

    try:
        order_id, is_amo = order_resp["order_id"], order_resp["is_amo"]
        await position_store.record_order(OrderRecord(
            order_id=order_id, underlying_symbol=symbol, trading_symbol=position.trading_symbol,
            transaction_type="SELL", quantity=position.quantity, status=OrderStatus.TRANSIT, is_amo=is_amo,
        ))
        await position_store.set_pending_exit_order(symbol, order_id, reason)

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, dhan_wrapper.wait_for_order_result, order_id, is_amo),
                timeout=_ORDER_RESULT_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not confirm SELL exit order %s's fill status within %.0fs - treating as still pending",
                symbol, order_id, _ORDER_RESULT_TIMEOUT_SECONDS,
            )
            result = OrderResult(order_id=order_id, status=OrderStatus.TRANSIT, remark="order_confirmation_timeout",
                                  fill_price=0.0, filled_quantity=0, is_amo=is_amo)
        await position_store.update_order_status(order_id, result.status, result.remark)

        if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
            logger.warning("SELL exit order %s for %s rejected: status=%s remark=%s - backing off before retrying",
                            order_id, symbol, result.status, result.remark)
            await position_store.set_pending_exit_order(symbol, None)
            await position_store.record_exit_failure(symbol)
            return

        await position_store.clear_exit_failure(symbol)

        if result.is_queued_amo:
            logger.info("SELL exit order %s for %s queued as AMO - will confirm fill next session.", order_id, symbol)
            return

        if result.status not in OrderStatus.TERMINAL_STATUSES:
            logger.warning("SELL exit order %s for %s still %s after the poll budget - deferring to background sync.",
                            order_id, symbol, result.status)
            return

        final_exit_price = result.fill_price or exit_price
        await position_store.close_position(symbol, final_exit_price, reason)
        await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)
        pnl = unrealized_pnl_rs("LONG", position.entry_price, final_exit_price, position.pnl_multiplier)
        logger.info("SELL exit order %s FILLED for %s (%s): reason=%s entry=%s exit=%s qty=%s pnl=%.2f",
                    order_id, symbol, position.trading_symbol, reason,
                    position.entry_price, final_exit_price, position.quantity, pnl)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error resolving SELL exit order for %s (%s) - backing off before retrying",
                          symbol, position.trading_symbol)
        await position_store.record_exit_failure(symbol)


def _exit_on_cooldown(position: Position) -> bool:
    return bool(position.next_exit_retry_at and _now_ist() < position.next_exit_retry_at)


async def _get_ltp(position: Position) -> float:
    """WS-cache-then-REST-fallback, direct port of Swing's own _get_ltp,
    NSE_FNO-only (no MCX branch needed - v1 scope)."""
    loop = asyncio.get_running_loop()
    ltp = await loop.run_in_executor(None, dhan_wrapper.get_cached_option_ltp, position.trading_symbol)
    if ltp is not None:
        return ltp
    async with dhan_wrapper.ltp_rest_fallback_semaphore:
        ltp = await asyncio.wait_for(
            loop.run_in_executor(None, dhan_wrapper.get_option_ltp, position.trading_symbol),
            timeout=_LTP_FETCH_TIMEOUT_SECONDS,
        )
        await loop.run_in_executor(None, dhan_wrapper.note_rest_ltp, position.trading_symbol, ltp)
        return ltp


async def _handle_ltp_staleness(symbol: str, position: Position) -> None:
    """Forces a market exit once the failure has been CONTINUOUS for
    config.LTP_STALE_FORCE_EXIT_MINUTES - same real-incident-driven design
    as Swing's own (ANGELONE, 17 Sep 2026)."""
    key = (symbol, position.opened_at)
    if _now_ist() < _parse_hhmm_today(config.MARKET_OPEN_TIME):
        _ltp_failure_since.pop(key, None)
        return
    failure_start = _ltp_failure_since.setdefault(key, _now_ist())
    stale_minutes = (_now_ist() - failure_start).total_seconds() / 60
    if stale_minutes < config.LTP_STALE_FORCE_EXIT_MINUTES:
        return
    logger.error(
        "LTP STALENESS FORCED EXIT: %s (%s) has had NO live price for %.1f minutes "
        "(>= %s min threshold) - forcing a market exit rather than continuing to hold "
        "an unmonitorable position with no active exit-ladder protection.",
        symbol, position.trading_symbol, stale_minutes, config.LTP_STALE_FORCE_EXIT_MINUTES,
    )
    loop = asyncio.get_running_loop()
    fallback_price = await loop.run_in_executor(None, dhan_wrapper.get_last_historical_close, position.trading_symbol)
    if fallback_price is None:
        fallback_price = position.entry_price
    if await position_store.try_start_exit(symbol):
        await _exit_position(symbol, position, fallback_price, "LTP_STALE_FORCED_EXIT")
    _ltp_failure_since.pop(key, None)


async def _check_one_position(symbol: str, position: Position) -> None:
    if position.pending_exit_order_id or _exit_on_cooldown(position):
        return
    if await _check_broker_stop_already_filled(symbol, position):
        return
    try:
        ltp = await _get_ltp(position)
    except Exception:  # noqa: BLE001
        logger.exception("Could not fetch LTP for %s", position.trading_symbol)
        await _handle_ltp_staleness(symbol, position)
        return

    _ltp_failure_since.pop((symbol, position.opened_at), None)
    await position_store.update_trailing(symbol, ltp)

    reason = _exit_reason_for(position, ltp)
    if reason and await position_store.try_start_exit(symbol):
        await _exit_position(symbol, position, ltp, reason)


async def on_price_tick(trading_symbol: str, ltp: float) -> None:
    """Event-driven fast path, fired on every WebSocket tick - same
    two-speed design as Swing's own on_price_tick."""
    try:
        match = next(
            ((sym, pos) for sym, pos in position_store.live_positions.items() if pos.trading_symbol == trading_symbol),
            None,
        )
        if not match:
            return
        symbol, position = match
        if position.pending_exit_order_id or _exit_on_cooldown(position):
            return
        if await _check_broker_stop_already_filled(symbol, position):
            return
        await position_store.update_trailing(symbol, ltp)
        reason = _exit_reason_for(position, ltp)
        if reason and await position_store.try_start_exit(symbol):
            await _exit_position(symbol, position, ltp, reason)
    except Exception:  # noqa: BLE001
        logger.exception("on_price_tick failed for %s", trading_symbol)


async def _square_off_all(reason: str) -> None:
    """Manual kill-switch - closes every open Bollinger position."""
    positions = dict(position_store.live_positions)
    if not positions:
        return
    logger.info("Square-off triggered (%s) for %d open Bollinger position(s)", reason, len(positions))
    for symbol, position in positions.items():
        if position.pending_exit_order_id or _exit_on_cooldown(position):
            continue
        try:
            ltp = await _get_ltp(position)
        except Exception:  # noqa: BLE001
            ltp = position.entry_price
        if not await position_store.try_start_exit(symbol):
            continue
        await _exit_position(symbol, position, ltp, reason)


async def _sync_pending_exit_orders() -> None:
    loop = asyncio.get_running_loop()
    for symbol, position in dict(position_store.live_positions).items():
        if not position.pending_exit_order_id or position.pending_exit_order_id == EXIT_CLAIMED:
            continue
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, dhan_wrapper.refresh_order_status, position.pending_exit_order_id, True),
                timeout=_ORDER_STATUS_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not refresh AMO exit order %s", position.pending_exit_order_id)
            continue
        await position_store.update_order_status(position.pending_exit_order_id, result.status, result.remark)
        if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
            logger.warning("AMO exit order %s for %s ended as %s - clearing so the next tick retries the exit.",
                            position.pending_exit_order_id, symbol, result.status)
            await position_store.set_pending_exit_order(symbol, None)
            continue
        if result.status in OrderStatus.TERMINAL_STATUSES:
            final_exit_price = result.fill_price or position.best_price
            await position_store.close_position(symbol, final_exit_price, position.pending_exit_reason or "AMO_EXIT_FILLED")
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)


# --------------------------------------------------------------------------- #
# Monitor loop
# --------------------------------------------------------------------------- #
async def _monitor_tick() -> None:
    # Exits first - more urgent than looking for new entries. Concurrent
    # (asyncio.gather), same rationale as Swing's own (PERFORMANCE_AUDIT_
    # 2026-09-25.md finding).
    positions = list(position_store.live_positions.items())
    await asyncio.gather(*[_check_one_position(sym, pos) for sym, pos in positions])

    if not (config.STRATEGY_ENABLED and config.ENTRY_ENABLED):
        return
    if await position_store.remaining_capacity() <= 0:
        return

    from .watchlist import watchlist_store  # local import - avoids a circular import at module load time
    await watchlist_store.sync_from_file()
    symbols = await watchlist_store.symbols()
    n = len(symbols)
    if n:
        start = _watchlist_scan_turn["i"] % n
        symbols = symbols[start:] + symbols[:start]
        _watchlist_scan_turn["i"] = (_watchlist_scan_turn["i"] + 1) % n

    candidates: list[tuple[str, str, float, float, float]] = []
    for i, symbol in enumerate(symbols):
        if symbol in position_store.reserved_symbols:
            continue
        if await position_store.is_in_entry_cooldown(symbol):
            continue
        if not signals._symbol_market_open(symbol):
            continue
        if i:
            await asyncio.sleep(config.SYMBOL_PACING_SECONDS)
        try:
            result = await _evaluate_entry_signal(symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not evaluate entry signal", symbol)
            continue
        if result:
            side, trigger_price, stop_price, last_close = result
            candidates.append((symbol, side, trigger_price, stop_price, last_close))

    for symbol, side, trigger_price, stop_price, last_close in candidates:
        if paper_mode_control.is_paper_mode_enabled("Bollinger"):
            # Runtime kill-switch - see paper_mode_control.py's own
            # docstring. No dedicated Bollinger paper engine exists (v1
            # scope) - flipping this on simply stops real entries; any
            # already-open real position keeps being managed for real.
            logger.info("%s: paper mode is ON for Bollinger - skipping real entry (signal=%s)", symbol, side)
            await _record_bollinger_event("ENTRY_SKIPPED_PAPER_MODE", symbol, {"entry_signal": side})
            continue
        if await position_store.remaining_capacity() <= 0:
            break
        await enter_position_for_stock(symbol, side, trigger_price, stop_price, last_close)


async def monitor_loop() -> None:
    logger.info("Bollinger monitor loop started.")
    while True:
        try:
            await position_store.maybe_reset_for_new_day()
            await _sync_pending_exit_orders()
            await _monitor_tick()
        except Exception:  # noqa: BLE001
            logger.exception("Error in Bollinger monitor loop tick")
        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# Startup reconciliation
# --------------------------------------------------------------------------- #
async def reconcile_broker_positions() -> list[Position]:
    """Best-effort import of positions already open at Dhan and attributed
    to "Bollinger" specifically by our own opened-position history (never
    guessed - see attribute_open_broker_position's own docstring). NSE_FNO
    only - v1 scope never opens an MCX/equity position.

    A reconciled position's stop_pct/trailing_armed state cannot be
    recovered (it depended on the exact swing distance at entry, which
    isn't stored anywhere retrievable) - reconciles with a CONSERVATIVE
    FLAT FALLBACK instead: stop_pct=config.MIN_STOP_PCT, trailing not yet
    armed, computed off the broker-reported entry price. Same category of
    tradeoff Swing already accepts for its own reconciled positions
    (loses best_price/trailing memory on restart) - documented explicitly
    here per this package's own architecture plan."""
    loop = asyncio.get_running_loop()
    fno_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)

    positions: list[Position] = []
    for bp in fno_positions:
        avg_price = bp["avg_price"]
        if not avg_price:
            logger.warning("Skipping Bollinger reconciliation for %s - broker reported no average price.",
                            bp["trading_symbol"])
            continue
        if bp["quantity"] <= 0 or not bp.get("option_type"):
            continue  # a Bollinger position is always a LONG option - anything else was never opened by this package
        owner = await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"])
        if owner != "Bollinger":
            continue

        quantity = abs(bp["quantity"])
        underlying_symbol = bp["underlying_symbol"]
        stop_pct = config.MIN_STOP_PCT
        hard_stop_loss = hard_stop_for("LONG", avg_price, stop_pct)
        trailing_stop_dist = avg_price * stop_pct * config.TRAILING_STOP_FRACTION
        trailing_step = trailing_stop_dist * config.TRAILING_STEP_FRACTION

        stop_loss_order_id = None
        if config.BROKER_STOP_LOSS_ENABLED:
            try:
                stop_loss_order_id = await loop.run_in_executor(
                    None, dhan_wrapper.get_pending_order_id, bp["trading_symbol"], "SELL", "NSE",
                )
                if stop_loss_order_id:
                    logger.info(
                        "%s: discovered a pre-existing resting SELL order %s during reconciliation - "
                        "tracking it as this position's own stop-loss order.",
                        bp["trading_symbol"], stop_loss_order_id,
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "%s: could not check for a pre-existing resting stop-loss order during reconciliation - "
                    "proceeding without it", bp["trading_symbol"],
                )

        positions.append(Position(
            underlying_symbol=underlying_symbol, trading_symbol=bp["trading_symbol"],
            resolved_option_type=bp["option_type"], instrument_side="LONG",
            exchange_segment="NSE_FNO", product_type=bp.get("product_type") or config.OPTIONS_PRODUCT,
            quantity=quantity, lot_size=bp.get("lot_size"), entry_price=avg_price, best_price=avg_price,
            stop_pct=stop_pct, hard_stop_loss=hard_stop_loss,
            trailing_stop_dist=trailing_stop_dist, trailing_step=trailing_step,
            pnl_multiplier=quantity, order_id="", reconciled=True, stop_loss_order_id=stop_loss_order_id,
        ))

    for pos in positions:
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, pos.trading_symbol)
    return positions
