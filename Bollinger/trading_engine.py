"""
Core Bollinger strategy logic - structurally mirrors Swing/trading_engine.py
(same monitor-loop/reconciliation/exit-order-management shape), simplified
throughout: always a single-leg LONG CE or LONG PE (never futures/equity/
short), no profit target (MAX_LOSS_HIT -> STOP_LOSS_HIT -> TRAILING_
STOP_HIT only - see Bollinger/position_store.py's own Position docstring).

Underlyings: NSE equity, MCX commodities, and NSE index options (NIFTY/
BANKNIFTY) - MCX/index support and Swing's own market-hours/Friday/index
square-off policy added 26 Sep 2026 (user request), mirroring Swing/
trading_engine.py's identical dispatch/predicates - see config.py's own
module docstring.

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

import entry_backlog
import fund_allocation
import paper_mode_control
from trade_history import append_jsonl, attribute_open_broker_position

from . import config, signals
from .paper_book import paper_book
from .position_store import (
    EXIT_CLAIMED, OrderRecord, Position, position_store,
)
from Swing.position_store import (
    broker_stop_trigger_and_limit, hard_stop_for, unrealized_pnl_rs,
)
from Swing.mcx_registry import mcx_registry
from Swing import candle_feed
from Options.dhan_client import IST, OrderResult, OrderStatus, dhan_wrapper

logger = logging.getLogger("bollinger_trading_engine")

_ltp_failure_since: dict[tuple[str, datetime], datetime] = {}

# Fairness rotation for the per-tick watchlist scan - same idiom as
# Swing/trading_engine.py's own _watchlist_scan_turn (see that module's
# docstring for the real VEDL incident this prevents).
_watchlist_scan_turn: dict[str, int] = {"i": 0}

# Freshness-priority backlog for entry signals that couldn't be placed the
# tick they fired (capacity full) - added 26 Sep 2026, user request. See
# entry_backlog.py's own module docstring for the full design. Bollinger's
# own instance, entirely separate from Swing's - cleared at Bollinger's
# own day-boundary reset, see monitor_loop below.
_entry_backlog = entry_backlog.EntryBacklog()

# Resting-order entry bookkeeping (28 Sep 2026): symbol -> candle_start of
# the closed bar whose pending order we already acted on. A pending order is
# acted on at most ONCE - if the resulting trade stops out a minute later,
# the same (still-displayed) pending order must not open a second trade.
# In-memory only: after a restart the newest pending order is a new one or
# its bar has moved on, so nothing needs persisting.
_resting_consumed: dict[str, datetime] = {}

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


# Market-hours/square-off predicates (26 Sep 2026, user request: "Update
# market hours and timings and Square OFF policies as we have for SWING
# strategy currently") - direct, byte-for-byte port of Swing/trading_
# engine.py's own three predicates. See config.py's own FRIDAY_SQUARE_OFF_
# TIME/MCX_FRIDAY_SQUARE_OFF_TIME/INDEX_DAILY_SQUARE_OFF_TIME docstrings.
def _is_friday_square_off_time() -> bool:
    now = _now_ist()
    return now.weekday() == 4 and now >= _parse_hhmm_today(config.FRIDAY_SQUARE_OFF_TIME)


def _is_mcx_friday_square_off_time() -> bool:
    now = _now_ist()
    return now.weekday() == 4 and now >= _parse_hhmm_today(config.MCX_FRIDAY_SQUARE_OFF_TIME)


def _is_index_square_off_time() -> bool:
    now = _now_ist()
    return now.weekday() < 5 and now >= _parse_hhmm_today(config.INDEX_DAILY_SQUARE_OFF_TIME)


def _is_daily_square_off_time() -> bool:
    """hold_to_close mode only (28 Sep 2026): True from config.DAILY_SQUARE_OFF_
    TIME onward on a weekday - every non-MCX position is closed and no new
    non-MCX entry is taken. Always False in "trailing" mode (positions there
    carry overnight until the Friday square-off, as before)."""
    if config.EXIT_MODE != "hold_to_close":
        return False
    now = _now_ist()
    return now.weekday() < 5 and now >= _parse_hhmm_today(config.DAILY_SQUARE_OFF_TIME)


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
    """Returns (side, trigger_price, stop_price, stop_reference_price) when
    this symbol should be entered right now, else None.

    config.ENTRY_MODE == "resting" (default since 28 Sep 2026): enter the
    moment the live price touches the trigger of the pending order that was
    armed when the previous 5-min bar closed - see signals.
    resting_trigger_hit's docstring for the step-by-step logic. Needs the WS
    tick feed for this symbol to be fresh; if it isn't, no entry (we can't
    see the forming bar, and guessing from a stale price could enter on a
    trigger touch that never happened).

    config.ENTRY_MODE == "bar_close" (original behaviour): enter only after
    the bar that crossed the trigger has closed (`state.fired`)."""
    state = await signals.get_signal_state(symbol)
    if state is None:
        return None
    if config.ENTRY_MODE == "resting":
        forming = (candle_feed.forming_bar(symbol)
                   if candle_feed.is_fresh(symbol, config.WS_STALE_AFTER_SECONDS) else None)
        hit = signals.resting_trigger_hit(state, forming, config.SIGNAL_INTERVAL_MINUTES, _now_ist().date())
        if hit is None or _resting_consumed.get(symbol) == state.candle_start:
            return None
        _resting_consumed[symbol] = state.candle_start
        return _direction_allowed(hit)
    if state.fired is None:
        return None
    return _direction_allowed((state.fired, state.fired_trigger_price, state.fired_stop_price, state.last_close))


def _direction_allowed(entry: tuple[str, float, float, float]) -> Optional[tuple[str, float, float, float]]:
    """config.SIDES == "long" drops BEARISH (buy-PE) entries - see config.SIDES
    for the research behind it."""
    if config.SIDES == "long" and entry[0] != "BULLISH":
        return None
    return entry


def _stop_params(fill_price: float, trigger_price: float, stop_price: float,
                 stop_reference_price: float) -> tuple[float, float, float, float]:
    """Per-trade stop sizing, shared by real and paper entries. Returns
    (stop_pct, hard_stop_loss, trailing_stop_dist, trailing_step), all on
    the OPTION PREMIUM:
      stop_pct  = the underlying's swing distance (trigger -> pullback
                  extreme) as a % of the reference price, floored at
                  config.MIN_STOP_PCT (5% since 28 Sep 2026 - nearly every
                  trade hits the floor, so in practice this IS the stop);
      hard stop = entry premium * (1 - stop_pct);
      trailing  = arms after the premium gains stop_pct/3, then trails that
                  far behind the best premium, moving up in steps of 1/5 of
                  the trailing distance (the video's stated ratios)."""
    distance = abs(trigger_price - stop_price) if stop_price is not None else 0.0
    stop_pct = distance / stop_reference_price if stop_reference_price else config.MIN_STOP_PCT
    stop_pct = max(stop_pct, config.MIN_STOP_PCT)
    trailing_stop_dist = fill_price * stop_pct * config.TRAILING_STOP_FRACTION
    return (stop_pct, hard_stop_for("LONG", fill_price, stop_pct),
            trailing_stop_dist, trailing_stop_dist * config.TRAILING_STEP_FRACTION)


def _broker_stop_pct(stop_pct: float) -> float:
    """Percentage used for the resting broker-side SL-L order. In "trailing"
    mode it's the trade's own stop_pct (unchanged). In "hold_to_close" mode
    the strategy deliberately has no percentage stop, so the broker order is
    only a disaster backstop: 95% of the premium, i.e. the MAX_LOSS rupee cap
    normally decides the trigger, and the 95% floor just guarantees a valid
    positive price (the 25 Sep negative-trigger fix in broker_stop_trigger_
    and_limit takes the tighter of the two)."""
    return 0.95 if config.EXIT_MODE == "hold_to_close" else stop_pct


def premium_too_low(premium: Optional[float], is_mcx: bool) -> bool:
    """The minimum-premium gate (28 Sep 2026, see config.MIN_ATM_PREMIUM_RS
    for why). NSE options only; MCX is exempt. An unknown premium (None)
    also blocks - we never enter without a price."""
    if is_mcx:
        return False
    return premium is None or premium < config.MIN_ATM_PREMIUM_RS


class _SkipEntry(Exception):
    def __init__(self, result: dict):
        super().__init__(result.get("reason") or result.get("status"))
        self.result = result


async def _resolve_option_leg(symbol: str, entry_signal: str) -> dict:
    """Picks the ATM option to buy and its sizing - shared by real and paper
    entries so both always trade the same contract. Raises _SkipEntry with
    the caller's return value when the entry should not happen."""
    loop = asyncio.get_running_loop()
    is_mcx = dhan_wrapper.is_mcx_commodity(symbol)
    option_type = "CE" if entry_signal == "BULLISH" else "PE"
    try:
        # get_liquid_atm_option is already MCX-capable (same call Swing
        # uses for COPPER - see Swing/trading_engine.py's own comment on
        # this) and index-capable (Tradehull's own ATM_Strike_Selection
        # resolves NIFTY/BANKNIFTY natively, same as any NSE underlying -
        # confirmed via Swing's own identical, unbranched call for
        # INDEX_SYMBOLS) - one call for all three underlying types.
        atm = await loop.run_in_executor(None, dhan_wrapper.get_liquid_atm_option, symbol, option_type)
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not resolve the OPTIONS instrument for entry", symbol)
        raise _SkipEntry({"symbol": symbol, "status": "error", "reason": "instrument_resolution_failed"})
    if atm is None:
        logger.info("%s: skipped - no liquid, actively-traded %s contract found nearby", symbol, option_type)
        raise _SkipEntry({"symbol": symbol, "status": "skipped", "reason": "no_liquid_contract_available"})
    if atm.expiry_date == _now_ist().date():
        logger.info("%s: skipped - %s expires today and no later expiry is available yet", symbol, atm.trading_symbol)
        raise _SkipEntry({"symbol": symbol, "status": "skipped_expiry_day", "option_trading_symbol": atm.trading_symbol})
    quantity = atm.lot_size * config.QUANTITY_LOTS
    if is_mcx:
        # NOT quantity - see Swing/position_store.py's own Position.
        # pnl_multiplier docstring (reused here verbatim) for why MCX needs a
        # real, separately-configured rupee-per-point multiplier instead of
        # the tiny lot-count `quantity`. Looked up from the SAME shared
        # Swing.mcx_registry Swing itself uses. A symbol with NO configured
        # multiplier SKIPS rather than guessing.
        raw_multiplier = await mcx_registry.pnl_multiplier(symbol)
        if raw_multiplier is None:
            logger.error(
                "%s: no pnl_multiplier configured in data/mcx_config for this MCX symbol - "
                "skipping entry rather than guessing (would misprice every rupee-threshold "
                "check). Add a line to data/mcx_config, e.g. \"%s,false,<real_per_lot_qty>\".",
                symbol, symbol,
            )
            raise _SkipEntry({"symbol": symbol, "status": "skipped", "reason": "mcx_pnl_multiplier_not_configured"})
        exchange_segment, product_type = "MCX_COMM", config.MCX_PRODUCT
        pnl_multiplier = raw_multiplier * config.QUANTITY_LOTS
    else:
        exchange_segment, product_type = "NSE_FNO", config.OPTIONS_PRODUCT
        pnl_multiplier = quantity
    return {"atm": atm, "is_mcx": is_mcx, "option_type": option_type, "trading_symbol": atm.trading_symbol,
            "security_id": atm.security_id, "lot_size": atm.lot_size, "quantity": quantity,
            "exchange_segment": exchange_segment, "product_type": product_type, "pnl_multiplier": pnl_multiplier}


async def _premium_gate(symbol: str, leg: dict) -> Optional[float]:
    """Fetches the ATM option's live price and applies the minimum-premium
    gate. Returns the price (reused as the funds-check price / paper fill),
    or raises _SkipEntry if the option is too cheap to trade."""
    try:
        price = await dhan_wrapper.get_option_ltp_async(leg["trading_symbol"])
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not price %s for the premium gate", symbol, leg["trading_symbol"])
        price = None
    if premium_too_low(price, leg["is_mcx"]):
        logger.info("%s: skipped - %s premium %s is below the Rs %.2f minimum (too cheap: spread/slippage "
                    "would dominate)", symbol, leg["trading_symbol"], price, config.MIN_ATM_PREMIUM_RS)
        await _record_bollinger_event("ENTRY_SKIPPED_LOW_PREMIUM", symbol, {
            "trading_symbol": leg["trading_symbol"], "premium": price, "minimum": config.MIN_ATM_PREMIUM_RS})
        raise _SkipEntry({"symbol": symbol, "status": "skipped", "reason": "premium_below_minimum",
                          "trading_symbol": leg["trading_symbol"], "premium": price})
    return price


def _exit_reason_for(position: Position, ltp: float) -> Optional[str]:
    """Pure function - position_store.update_trailing must be called
    (under its own lock) BEFORE this, to keep best_price/trailing_armed/
    trailing_stop_price consistent with each other - see that method's
    own docstring for why this can't be two separate unlocked steps.
    Always LONG (every position this package ever opens)."""
    loss_rs = -unrealized_pnl_rs("LONG", position.entry_price, ltp, position.pnl_multiplier)
    if loss_rs >= config.MAX_LOSS_PROTECTION_RS:
        return "MAX_LOSS_HIT"
    if config.EXIT_MODE == "hold_to_close":
        # No percentage or trailing stop in this mode - the position is held
        # until the daily square-off; MAX_LOSS above is the only early exit.
        return None
    active_stop = position.trailing_stop_price if position.trailing_armed else position.hard_stop_loss
    if ltp <= active_stop:
        return "TRAILING_STOP_HIT" if position.trailing_armed else "STOP_LOSS_HIT"
    return None


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
async def enter_position_for_stock(symbol: str, entry_signal: str, trigger_price: float,
                                    stop_price: float, stop_reference_price: float) -> dict:
    """REAL-money entry. stop_reference_price is the underlying price the
    stop % is measured against: the trigger in "resting" mode, the fire
    bar's close in "bar_close" mode (see _evaluate_entry_signal)."""
    if not config.STRATEGY_ENABLED:
        return {"symbol": symbol, "status": "ignored", "reason": "strategy_disabled"}

    if not await position_store.reserve_symbol(symbol):
        return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}

    loop = asyncio.get_running_loop()
    try:
        try:
            leg = await _resolve_option_leg(symbol, entry_signal)
            gate_price = await _premium_gate(symbol, leg)
        except _SkipEntry as skip:
            return skip.result
        option_type, trading_symbol, security_id = leg["option_type"], leg["trading_symbol"], leg["security_id"]
        lot_size, quantity, pnl_multiplier = leg["lot_size"], leg["quantity"], leg["pnl_multiplier"]
        exchange_segment, product_type = leg["exchange_segment"], leg["product_type"]

        tag = _gen_tag(config.ORDER_TAG_PREFIX, symbol)

        if config.FUNDS_CHECK_ENABLED:
            try:
                price = gate_price if gate_price is not None else await dhan_wrapper.get_option_ltp_async(trading_symbol)
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
                None, dhan_wrapper.get_pending_order_id, trading_symbol, transaction_type,
                "MCX" if exchange_segment == "MCX_COMM" else "NSE",
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

        fill_price = result.fill_price or await dhan_wrapper.get_option_ltp_async(trading_symbol)

        stop_pct, hard_stop_loss, trailing_stop_dist, trailing_step = _stop_params(
            fill_price, trigger_price, stop_price, stop_reference_price)

        stop_loss_order_id = None
        if config.BROKER_STOP_LOSS_ENABLED:
            trigger, limit = broker_stop_trigger_and_limit(
                "LONG", fill_price, pnl_multiplier, config.MAX_LOSS_PROTECTION_RS,
                config.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE, hard_stop_pct=_broker_stop_pct(stop_pct),
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
# Paper trading (28 Sep 2026) - see Bollinger/paper_book.py's docstring.
# Same contract choice, same premium gate, same stop sizing and the same exit
# rule as real mode; the only difference is that no order is ever placed.
# --------------------------------------------------------------------------- #
async def _enter_paper(symbol: str, entry_signal: str, trigger_price: float,
                       stop_price: float, stop_reference_price: float) -> dict:
    if symbol in paper_book.positions:
        return {"symbol": symbol, "status": "skipped", "reason": "paper_position_already_open"}
    try:
        leg = await _resolve_option_leg(symbol, entry_signal)
        fill_price = await _premium_gate(symbol, leg)
    except _SkipEntry as skip:
        return skip.result
    if not fill_price:
        return {"symbol": symbol, "status": "skipped", "reason": "no_option_price"}
    stop_pct, hard_stop_loss, trailing_stop_dist, trailing_step = _stop_params(
        fill_price, trigger_price, stop_price, stop_reference_price)
    position = Position(
        underlying_symbol=symbol, trading_symbol=leg["trading_symbol"], resolved_option_type=leg["option_type"],
        instrument_side="LONG", exchange_segment=leg["exchange_segment"], product_type=leg["product_type"],
        quantity=leg["quantity"], lot_size=leg["lot_size"], entry_price=fill_price, best_price=fill_price,
        stop_pct=stop_pct, hard_stop_loss=hard_stop_loss,
        trailing_stop_dist=trailing_stop_dist, trailing_step=trailing_step,
        pnl_multiplier=leg["pnl_multiplier"], order_id="PAPER",
    )
    if not await paper_book.open(position):
        return {"symbol": symbol, "status": "skipped", "reason": "paper_position_already_open"}
    loop = asyncio.get_running_loop()
    try:
        # WS subscription so exits read the cached tick price instead of a
        # REST call every 5 seconds (same as a real position).
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, leg["trading_symbol"])
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not WS-subscribe %s for paper exits - REST fallback will be used",
                         symbol, leg["trading_symbol"])
    await _record_bollinger_event("PAPER_POSITION_OPENED", symbol, {
        "entry_signal": entry_signal, "entry_mode": config.ENTRY_MODE, "trading_symbol": leg["trading_symbol"],
        "entry_price": fill_price, "quantity": leg["quantity"], "stop_pct": stop_pct,
        "trigger_price": trigger_price, "stop_price": stop_price})
    return {"symbol": symbol, "status": "paper_entered", "trading_symbol": leg["trading_symbol"],
            "entry_price": fill_price}


async def _close_paper(symbol: str, exit_price: float, reason: str) -> None:
    pos = paper_book.positions.get(symbol)
    record = await paper_book.close(symbol, exit_price, reason)
    if record is None:
        return
    await _record_bollinger_event("PAPER_POSITION_CLOSED", symbol, record)
    if pos is not None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, dhan_wrapper.unsubscribe_option_price, pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not unsubscribe %s after paper exit", symbol, pos.trading_symbol)


async def _check_paper_position(symbol: str, ltp: float) -> None:
    """Apply one price to one paper position: ratchet the trailing stop, then
    exit if the shared exit rule says so."""
    pos = await paper_book.update(symbol, ltp)
    if pos is None:
        return
    reason = _exit_reason_for(pos, ltp)
    if reason:
        await _close_paper(symbol, ltp, reason)


async def _check_paper_positions(square_off_symbols: Optional[set[str]] = None,
                                 square_off_reason: str = "") -> None:
    """Poll path for every open paper position (the tick path is in
    on_price_tick). Symbols in square_off_symbols are closed outright at the
    current price - Friday / MCX-Friday / index-daily square-off, same
    schedule as real positions."""
    for symbol, pos in list(paper_book.positions.items()):
        try:
            ltp = await _get_ltp(pos)
        except Exception:  # noqa: BLE001
            logger.warning("%s: no price for PAPER position %s this tick - will retry", symbol, pos.trading_symbol)
            continue
        if square_off_symbols is not None and symbol in square_off_symbols:
            await _close_paper(symbol, ltp, square_off_reason)
        else:
            await _check_paper_position(symbol, ltp)


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
    LONG). exchange_segment is NSE_FNO or MCX_COMM depending on the
    underlying (26 Sep 2026, MCX support) - read off the position itself,
    set at entry/reconciliation."""
    loop = asyncio.get_running_loop()
    net_qty_fn = dhan_wrapper.get_broker_net_quantity
    order_exchange = "MCX" if position.exchange_segment == "MCX_COMM" else "NSE"

    try:
        stale_order_id = await loop.run_in_executor(
            None, dhan_wrapper.get_pending_order_id, position.trading_symbol, "SELL", order_exchange,
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
    """WS-cache-then-REST-fallback, direct port of Swing's own _get_ltp -
    trading_symbol-only, no exchange-segment branch needed here (Tradehull
    resolves the right segment internally), same as Swing's identical
    NSE_FNO/MCX_COMM-agnostic usage."""
    loop = asyncio.get_running_loop()
    ltp = await loop.run_in_executor(None, dhan_wrapper.get_cached_option_ltp, position.trading_symbol)
    if ltp is not None:
        return ltp
    async with dhan_wrapper.ltp_rest_fallback_semaphore:
        ltp = await asyncio.wait_for(
            dhan_wrapper.get_option_ltp_async(position.trading_symbol),
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
            paper_symbol = next((sym for sym, pos in paper_book.positions.items()
                                 if pos.trading_symbol == trading_symbol), None)
            if paper_symbol:
                await _check_paper_position(paper_symbol, ltp)
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


async def _square_off_all(reason: str, symbols: Optional[set[str]] = None) -> None:
    """symbols=None (default - the manual kill-switch's own usage) closes
    every open Bollinger position. A non-None set scopes this to just
    those symbols (26 Sep 2026, MCX/index square-off support) - same
    scoping Swing/trading_engine.py's own _square_off_all uses for its
    INDEX_DAILY_SQUARE_OFF_ENABLED/Friday-MCX-split rules."""
    positions = {s: p for s, p in position_store.live_positions.items() if symbols is None or s in symbols}
    if not positions:
        return
    logger.info("Square-off triggered (%s) for %d open Bollinger position(s)%s", reason, len(positions),
                f" (scoped to {sorted(symbols)})" if symbols is not None else "")
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
    # Market-hours/square-off policy (26 Sep 2026, user request - ported
    # from Swing/trading_engine.py's own _monitor_tick, same structure,
    # same predicates, same reasoning - see that module's own extensive
    # comments on each rule for the real incidents that shaped it).
    friday_square_off_now = config.FRIDAY_SQUARE_OFF_ENABLED and _is_friday_square_off_time()
    if friday_square_off_now:
        # Weekly, not daily - non-MCX symbols only, MCX gets its own later
        # square-off below (MCX's Friday session runs well past NSE's
        # close). MCX-ness read straight off each OPEN position's own
        # exchange_segment (ground truth), not a symbol-name list.
        non_mcx_open = {s for s, p in position_store.live_positions.items() if p.exchange_segment != "MCX_COMM"}
        await _square_off_all("FRIDAY_SQUARE_OFF", symbols=non_mcx_open)
        await _check_paper_positions(
            {s for s, p in paper_book.positions.items() if p.exchange_segment != "MCX_COMM"}, "FRIDAY_SQUARE_OFF")

    mcx_friday_square_off_now = config.FRIDAY_SQUARE_OFF_ENABLED and _is_mcx_friday_square_off_time()
    if mcx_friday_square_off_now:
        mcx_open = {s for s, p in position_store.live_positions.items() if p.exchange_segment == "MCX_COMM"}
        await _square_off_all("MCX_FRIDAY_SQUARE_OFF", symbols=mcx_open)
        await _check_paper_positions(
            {s for s, p in paper_book.positions.items() if p.exchange_segment == "MCX_COMM"}, "MCX_FRIDAY_SQUARE_OFF")

    if friday_square_off_now:
        # No point evaluating new entries for the rest of Friday. MCX
        # positions still get their normal exit-check below (not forced
        # flat until MCX_FRIDAY_SQUARE_OFF_TIME fires later) - concurrent
        # (asyncio.gather), same as the normal path below.
        positions = list(position_store.live_positions.items())
        await asyncio.gather(*[_check_one_position(sym, pos) for sym, pos in positions])
        await _check_paper_positions()
        return

    index_square_off_now = config.INDEX_DAILY_SQUARE_OFF_ENABLED and _is_index_square_off_time()
    if index_square_off_now:
        # Scoped to config.INDEX_SYMBOLS (NIFTY/BANKNIFTY) only - does NOT
        # return early: every other Bollinger symbol still gets its normal
        # exit-check/entry-scan this tick.
        await _square_off_all("INDEX_DAILY_SQUARE_OFF", symbols=config.INDEX_SYMBOLS)
        await _check_paper_positions(set(config.INDEX_SYMBOLS), "INDEX_DAILY_SQUARE_OFF")

    daily_square_off_now = _is_daily_square_off_time()
    if daily_square_off_now:
        # hold_to_close mode: close every non-MCX position (real and paper) at
        # DAILY_SQUARE_OFF_TIME. Doesn't return early - MCX positions still get
        # their normal checks below.
        await _square_off_all("DAILY_SQUARE_OFF", symbols={
            s for s, p in position_store.live_positions.items() if p.exchange_segment != "MCX_COMM"})
        await _check_paper_positions(
            {s for s, p in paper_book.positions.items() if p.exchange_segment != "MCX_COMM"}, "DAILY_SQUARE_OFF")

    # Exits first - more urgent than looking for new entries. Concurrent
    # (asyncio.gather), same rationale as Swing's own (PERFORMANCE_AUDIT_
    # 2026-09-25.md finding).
    positions = list(position_store.live_positions.items())
    await asyncio.gather(*[_check_one_position(sym, pos) for sym, pos in positions])
    await _check_paper_positions()

    if not (config.STRATEGY_ENABLED and config.ENTRY_ENABLED):
        return
    if await position_store.remaining_capacity() <= 0:
        return

    from .watchlist import watchlist_store  # local import - avoids a circular import at module load time
    await watchlist_store.sync_from_file()
    # Same live-reload treatment for the shared MCX registry (options_only/
    # pnl_multiplier) as Swing's own monitor tick already does - cheap and
    # idempotent even though Swing (running in the same process) already
    # keeps this fresh; decouples Bollinger from depending on Swing's tick
    # ordering or continued presence.
    await mcx_registry.sync_from_file()
    symbols = await watchlist_store.symbols()
    n = len(symbols)
    if n:
        start = _watchlist_scan_turn["i"] % n
        symbols = symbols[start:] + symbols[:start]
        _watchlist_scan_turn["i"] = (_watchlist_scan_turn["i"] + 1) % n

    candidates: list[tuple[str, tuple[str, float, float, float]]] = []
    for i, symbol in enumerate(symbols):
        if symbol in position_store.reserved_symbols or symbol in paper_book.positions:
            continue
        if await position_store.is_in_entry_cooldown(symbol):
            continue
        if daily_square_off_now and not dhan_wrapper.is_mcx_commodity(symbol):
            continue  # hold_to_close: nothing new after the daily square-off
        if index_square_off_now and symbol in config.INDEX_SYMBOLS:
            # No fresh same-day NIFTY/BANKNIFTY entry once today's index
            # square-off has fired - would defeat the entire "never carry
            # index overnight" point within minutes.
            continue
        if not signals._symbol_market_open(symbol):
            continue
        # Only pace before a symbol likely to actually hit Dhan's REST API
        # this tick - see Swing/trading_engine.py's identical comment and
        # signals.is_symbol_ws_fresh's own docstring for the full
        # rationale (audit finding, 26 Sep 2026).
        if i and not signals.is_symbol_ws_fresh(symbol):
            await asyncio.sleep(config.SYMBOL_PACING_SECONDS)
        try:
            result = await _evaluate_entry_signal(symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not evaluate entry signal", symbol)
            continue
        if result:
            candidates.append((symbol, result))

    # Freshness-priority dispatch (26 Sep 2026, user request) - same
    # algorithm/rationale as Swing's own, see entry_backlog.py's module
    # docstring and Swing/trading_engine.py's own dispatch call site.
    async def _place_paper(symbol: str, payload: tuple[str, float, float, float]) -> None:
        # Paper mode ON: run the full entry on live prices without placing an
        # order (Bollinger/paper_book.py). Any already-open REAL position keeps
        # being managed for real.
        side, trigger_price, stop_price, stop_reference_price = payload
        result = await _enter_paper(symbol, side, trigger_price, stop_price, stop_reference_price)
        if result.get("status") != "paper_entered":
            logger.info("%s: paper entry not taken (%s)", symbol, result.get("reason") or result.get("status"))

    async def _place_real(symbol: str, payload: tuple[str, float, float, float]) -> None:
        side, trigger_price, stop_price, stop_reference_price = payload
        await enter_position_for_stock(symbol, side, trigger_price, stop_price, stop_reference_price)

    await entry_backlog.dispatch(
        _entry_backlog,
        candidates,
        is_paper_trade=lambda _symbol: paper_mode_control.is_paper_mode_enabled("Bollinger"),
        remaining_capacity=position_store.remaining_capacity,
        place_paper=_place_paper,
        place_real=_place_real,
    )


async def monitor_loop() -> None:
    logger.info("Bollinger monitor loop started.")
    while True:
        try:
            if await position_store.maybe_reset_for_new_day():
                # A pending entry signal is a "right now" read of a setup,
                # unlike a position - it should NOT survive overnight (see
                # entry_backlog.py's own module docstring).
                await _entry_backlog.clear()
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
    guessed - see attribute_open_broker_position's own docstring). Scans
    both NSE_FNO (plain equity + index options) and MCX_COMM (26 Sep 2026,
    MCX support) - a Bollinger position is always a LONG option in one of
    these two segments, never equity/futures.

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
    mcx_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_mcx_positions)

    positions: list[Position] = []
    for bp, exchange_segment in (
        [(p, "NSE_FNO") for p in fno_positions] + [(p, "MCX_COMM") for p in mcx_positions]
    ):
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

        # pnl_multiplier: identical to quantity for NSE - looked up from
        # the shared Swing.mcx_registry for a reconciled MCX position
        # instead, same as a fresh entry computes it above. Fails open to
        # `quantity` (a WRONG but non-crashing value) if somehow
        # unconfigured, logging loudly - must never raise, or it'd break
        # reconciliation for every OTHER already-open position too. Same
        # pattern as Swing/trading_engine.py's own identical fallback.
        if exchange_segment == "MCX_COMM":
            raw_multiplier = await mcx_registry.pnl_multiplier(underlying_symbol)
            if raw_multiplier is not None:
                pnl_multiplier = raw_multiplier * config.QUANTITY_LOTS
            else:
                logger.error(
                    "%s: reconciled MCX Bollinger position has no pnl_multiplier configured in "
                    "data/mcx_config - falling back to quantity (%d) as the P&L multiplier, which is "
                    "almost certainly WRONG for a commodity. Add a line to data/mcx_config, e.g. "
                    "\"%s,false,<real_per_lot_qty>\".",
                    underlying_symbol, quantity, underlying_symbol,
                )
                pnl_multiplier = quantity
        else:
            pnl_multiplier = quantity

        stop_loss_order_id = None
        if config.BROKER_STOP_LOSS_ENABLED:
            try:
                stop_loss_order_id = await loop.run_in_executor(
                    None, dhan_wrapper.get_pending_order_id, bp["trading_symbol"], "SELL",
                    "MCX" if exchange_segment == "MCX_COMM" else "NSE",
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
            exchange_segment=exchange_segment,
            product_type=bp.get("product_type") or (config.MCX_PRODUCT if exchange_segment == "MCX_COMM" else config.OPTIONS_PRODUCT),
            quantity=quantity, lot_size=bp.get("lot_size"), entry_price=avg_price, best_price=avg_price,
            stop_pct=stop_pct, hard_stop_loss=hard_stop_loss,
            trailing_stop_dist=trailing_stop_dist, trailing_step=trailing_step,
            pnl_multiplier=pnl_multiplier, order_id="", reconciled=True, stop_loss_order_id=stop_loss_order_id,
        ))

    for pos in positions:
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, pos.trading_symbol)
    return positions
