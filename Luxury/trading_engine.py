"""
Core strategy logic for the Luxury package - a same-account duplicate of
Options/trading_engine.py (that file's docstring/comments cover the full
rationale behind each piece; this is a near-verbatim copy, not
reimplemented from scratch, so the strategies can't silently drift apart
in how they rank/enter/exit while all doing the same thing). Built off
Futures/trading_engine.py's own copy (itself already proven as "Options
standing on its own"), extended with the second (PE) webhook/leg Futures
doesn't have - both CE (bullish, /chartink/webhook-luxury) and PE
(bearish, /chartink/webhook-luxury-sell) are real here, matching Options'
own two-endpoint design rather than Futures' CE-only one.

  - reconcile_broker_positions() - filtered through
    trade_history.attribute_open_broker_position, not a blind import:
    Dhan's get_open_fno_positions() returns every open FNO position in the
    account with no notion of which strategy placed it - the same call
    Options'/Futures' own reconciliation uses would otherwise re-import
    THEIR live positions into this package's separate position_store too,
    and multiple strategies could then try to independently manage (and
    exit) the same real position. Our own persistent opened/closed-position
    history (trade_history.py) is what actually distinguishes ownership,
    since Dhan's data never can - see that module's own docstring (a
    position that predates this logging, or was placed manually, is still
    safely skipped rather than guessed at).

Does NOT filter against choppy_stocks.py - that feature is scoped to
Options only per the user's own explicit wording when it was requested,
same as Futures doesn't have it either.

Flow:
  1. rank_and_pick_top_stocks() - from the Chartink alert's stock list,
     pick the top-N by today's %change (highest first for the bullish
     endpoint, lowest/most negative first for the bearish one).
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

from trade_history import (
    attribute_open_broker_position, count_opened_today, loss_exit_count_today, minutes_since_last_loss_today,
)
import cross_strategy_registry
import fund_allocation

from . import config
from .dhan_client import OrderStatus, dhan_wrapper
from .position_store import EXIT_CLAIMED, OrderRecord, Position, position_store

logger = logging.getLogger("luxury_trading_engine")


async def reconcile_broker_positions() -> list[Position]:
    """Near-verbatim copy of Options/trading_engine.py's own function - see
    this module's own docstring above for the attribute_open_broker_position
    filtering rationale, identical here except "Luxury" is the strategy
    this filters FOR (Options'/Futures' own positions get skipped here,
    mirror-image of those files' own filters)."""
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
        if owner != "Luxury":
            logger.warning(
                "Skipping reconciliation for %s - attributed to %s (not Luxury) by our own "
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
            # Same reasoning as Options'/Futures' own reconciliation - the
            # broker's positions API reports a human-readable product
            # label, not the code order_placement() needs; this package
            # only ever trades config.OPTIONS_PRODUCT itself, so that's
            # always correct here too. See NOTES.md bug #23.
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


def _is_before_risk_threshold_cutoff() -> bool:
    """See Options/trading_engine.py's identical function - this package's
    own config.RISK_THRESHOLD_CUTOFF_TIME."""
    return _now_ist() < _parse_hhmm_today(config.RISK_THRESHOLD_CUTOFF_TIME)


def current_max_loss_per_trade_rs() -> float:
    """See Options/trading_engine.py's identical function - this package's
    own MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF/_AFTER_CUTOFF pair."""
    if _is_before_risk_threshold_cutoff():
        return config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF
    return config.MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF


def current_profit_protection_threshold_rs() -> float:
    """See Options/trading_engine.py's identical function - this package's
    own PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF/_AFTER_CUTOFF pair."""
    if _is_before_risk_threshold_cutoff():
        return config.PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF
    return config.PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF


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
    """See Options/trading_engine.py's identical function - ranked stocks'
    entries run concurrently via asyncio.gather instead of sequentially.
    Safe as-is: reserve_symbol() is atomically locked, and every exception
    path here is caught locally into a result dict rather than left to
    propagate.

    Also claims `symbol` in cross_strategy_registry for the ENTIRE
    duration of this function (user request 31 Aug 2026) - see
    Options/trading_engine.py's identical function and
    cross_strategy_registry.py's own docstring for why this closes a real
    race window between Options/Futures/Luxury that reserve_symbol()/
    has_open_position_for_underlying() alone can't."""
    loop = asyncio.get_running_loop()

    # Daily re-entry cap - see Options/trading_engine.py's identical check
    # for the full rationale (user request 1 Sep 2026: "only allow entry
    # into same trade max 3 times a day").
    entries_today = await loop.run_in_executor(None, count_opened_today, "Luxury", symbol)
    if entries_today >= config.MAX_DAILY_ENTRIES_PER_SYMBOL:
        logger.info(
            "%s: skipped - already entered %d time(s) today, at the daily cap of %d",
            symbol, entries_today, config.MAX_DAILY_ENTRIES_PER_SYMBOL,
        )
        return {"symbol": symbol, "status": "skipped", "reason": "daily_reentry_cap_reached"}

    # Same-day loss cooldown (added 2 Sep 2026) - see config.LOSS_COOLDOWN_
    # ENABLED's own docstring for the MAHABANK/PHOENIXLTD/GVT&D incidents
    # this was built from. Independent of the daily re-entry cap above -
    # that one is a COUNT limit (at most N times all day); this one is a
    # TIMING limit (not immediately after a loss on this same symbol).
    if config.LOSS_COOLDOWN_ENABLED:
        minutes_since_loss = await loop.run_in_executor(
            None, minutes_since_last_loss_today, "Luxury", symbol, datetime.now()
        )
        if minutes_since_loss is not None and minutes_since_loss < config.LOSS_COOLDOWN_MINUTES:
            logger.info(
                "%s: skipped - stopped out %.1f minute(s) ago today, inside the %s-minute loss cooldown",
                symbol, minutes_since_loss, config.LOSS_COOLDOWN_MINUTES,
            )
            return {"symbol": symbol, "status": "skipped", "reason": "loss_cooldown_active"}

    # Repeat-loss same-day block (added 8 Sep 2026) - see config.LOSS_
    # REPEAT_BLOCK_ENABLED's own docstring for how this differs from both
    # guards above. Unlike the timing-based cooldown just above, this one
    # never expires today once tripped - only a genuine loss-designated
    # exit reason counts (a symbol that's merely had several trades, or
    # even several small losing-by-a-hair TRAILING_SL_HIT exits, is not
    # blocked by this - only real MAX_LOSS_HIT/STOP_LOSS_HIT hits are).
    if config.LOSS_REPEAT_BLOCK_ENABLED:
        loss_count = await loop.run_in_executor(
            None, loss_exit_count_today, "Luxury", symbol, config.LOSS_REPEAT_BLOCK_EXIT_REASONS, datetime.now(),
        )
        if loss_count >= config.LOSS_REPEAT_BLOCK_COUNT:
            logger.info(
                "%s: skipped - already hit a loss-based exit %d time(s) today (limit %d), "
                "blocked for the rest of the day",
                symbol, loss_count, config.LOSS_REPEAT_BLOCK_COUNT,
            )
            return {"symbol": symbol, "status": "skipped", "reason": "loss_repeat_block_active"}

    if not await cross_strategy_registry.try_claim(symbol, "Luxury"):
        logger.info("%s: skipped - another strategy is currently entering it", symbol)
        return {"symbol": symbol, "status": "skipped", "reason": "claimed_by_another_strategy"}

    try:
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
    finally:
        await cross_strategy_registry.release_claim(symbol, "Luxury")


async def enter_positions_for_stocks(
    ranked_stocks: list[tuple[str, float]], option_type: str = config.OPTION_TYPE
) -> list[dict]:
    """See Options/trading_engine.py's enter_positions_for_stocks - identical
    logic (concurrent via asyncio.gather), this package's own position_store."""
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

    # Proactive funds check (added 1 Sep 2026) - see Options/
    # trading_engine.py's identical check and fund_allocation.py's own
    # module docstring for the full 2-bucket design. Checks against the
    # shared SECONDARY bucket (Options/Futures/Luxury together).
    if config.FUNDS_CHECK_ENABLED:
        try:
            option_ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, atm.trading_symbol)
            sufficient = await fund_allocation.has_sufficient_bucket_funds(
                "secondary", symbol, [(atm.security_id, config.OPTIONS_PRODUCT, quantity, option_ltp)],
            )
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not price the leg for the funds check - proceeding optimistically", symbol)
            sufficient = True
        if not sufficient:
            return {
                "symbol": symbol, "status": "skipped", "reason": "insufficient_funds",
                "option_trading_symbol": atm.trading_symbol,
            }

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

    # Broker-side stop-loss order (added 8 Sep 2026, switched from SL-M to
    # SL-L on 9 Sep 2026 - see config.BROKER_STOP_LOSS_ENABLED's own
    # docstring for the full story). Placed here, right after the real
    # fill_price is known, using whichever MAX_LOSS cutoff value is active
    # RIGHT NOW (same "computed once at entry" convention as target_price/
    # hard_stop_loss above). A failure to place this is logged loudly but
    # never blocks the entry itself - stop_loss_order_id simply stays None,
    # and the position is exactly as protected as it always was via the
    # existing poll/tick-driven MAX_LOSS_HIT check.
    stop_loss_order_id = None
    if config.BROKER_STOP_LOSS_ENABLED:
        trigger_price = fill_price - (current_max_loss_per_trade_rs() / quantity)
        limit_price = trigger_price * (1 - config.BROKER_STOP_LOSS_LIMIT_BUFFER_PCT)
        try:
            stop_tag = _gen_tag("SL", symbol)
            stop_resp = await loop.run_in_executor(
                None, dhan_wrapper.place_stop_loss_limit_order,
                atm.trading_symbol, quantity, "SELL", trigger_price, limit_price, stop_tag, config.OPTIONS_PRODUCT,
            )
            stop_loss_order_id = stop_resp["order_id"]
            logger.info(
                "%s: broker-side SELL stop-loss LIMIT order %s placed for %s, trigger=%.2f limit=%.2f",
                symbol, stop_loss_order_id, atm.trading_symbol, trigger_price, limit_price,
            )
            await position_store.record_order(OrderRecord(
                order_id=stop_loss_order_id,
                underlying_symbol=symbol,
                trading_symbol=atm.trading_symbol,
                transaction_type="SELL",
                quantity=quantity,
                status=OrderStatus.PENDING,
                is_amo=False,
                lot_size=atm.lot_size,
                option_type=atm.option_type,
            ))
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not place the broker-side stop-loss order for %s (trigger would have been "
                "%.2f, limit %.2f) - proceeding without it, the existing poll/tick-driven MAX_LOSS_HIT "
                "check still protects this position exactly as before",
                symbol, atm.trading_symbol, trigger_price, limit_price,
            )

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
        stop_loss_order_id=stop_loss_order_id,
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
    failures and the stale-pending-order cancel-before-retry check.

    Broker-quantity reconciliation after a stale-order cancel (added 9
    Sep 2026, alongside the switch to a real SL-L broker stop-loss order
    - see config.BROKER_STOP_LOSS_ENABLED's own docstring): whenever a
    stale resting SELL order is found and cancelled below, position.
    quantity gets RE-DERIVED from the broker's own real net quantity
    before any fresh SELL is placed. Unlike the old SL-M order (which,
    in the one failure mode this codebase ever actually observed, either
    filled fully-instantly or never fired at all - see NOTES.md entry
    #99), a genuine SL-L order can PARTIALLY fill: some quantity sold at
    the limit price, the remainder left resting. If that remainder is
    still outstanding when a DIFFERENT exit reason fires (e.g.
    TARGET_HIT) through the normal reactive path, blindly placing a
    fresh SELL for the full stored position.quantity would try to sell
    MORE than is actually still held - exactly the kind of unintended
    naked short caught (and immediately covered) in the controlled live
    test that led to this feature. Re-checking real broker truth here,
    every time a stale order is found, closes that gap regardless of
    which broker-side order type ever gets used for the resting one -
    every reference to position.quantity below (the fresh SELL's own
    quantity, its OrderRecord, and the final pnl calc) picks up the
    corrected value for free since Position is mutated in place."""
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

    # get_pending_order_id scans the broker order book by symbol + status,
    # so it can MISS an order that's only seconds old (Dhan OMS lag) or one
    # sitting in a status the scan doesn't list. That is exactly how OIL's
    # broker-side SL-L was orphaned on 10 Sep 2026: the position hit
    # PROFIT_PROTECTION_HIT 13 seconds after entry, this scan returned None,
    # the position closed via a fresh SELL, and the SL-L was left resting at
    # the broker with no position behind it - a naked short waiting for its
    # trigger, cancelled by hand. We hold the SL-L's real order_id on the
    # Position, so fall back to it: the cancel + get_broker_net_quantity
    # reconciliation below then handle every case (still resting -> just
    # cancelled; already fired -> broker flat -> close at its own fill
    # price; partial fill -> sell only the real remainder).
    if not stale_order_id and config.BROKER_STOP_LOSS_ENABLED and position.stop_loss_order_id:
        stale_order_id = position.stop_loss_order_id

    if stale_order_id:
        logger.warning(
            "%s: found an already-outstanding SELL order %s for %s (a stale order surviving a "
            "restart, or this position's own broker-side SL-L) - cancelling it before placing a "
            "fresh exit order.",
            symbol, stale_order_id, position.option_trading_symbol,
        )
        try:
            await loop.run_in_executor(None, dhan_wrapper.cancel_order, stale_order_id)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not cancel stale SELL order %s - proceeding with a new order anyway",
                              symbol, stale_order_id)

        try:
            broker_qty = await loop.run_in_executor(
                None, dhan_wrapper.get_broker_net_quantity, position.option_trading_symbol
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not reconcile broker quantity after cancelling stale order %s - proceeding "
                "with the stored quantity (%d), which WOULD oversell into a naked short if that order "
                "had actually partially filled", symbol, stale_order_id, position.quantity,
            )
            broker_qty = None
        if broker_qty is not None and broker_qty != position.quantity:
            if broker_qty == 0:
                logger.warning(
                    "%s: broker shows this position already FLAT after cancelling stale order %s - it "
                    "must have fully filled (e.g. our own broker-side stop-loss) right before/during "
                    "the cancel race. Reconciling as closed using that order's own real fill price "
                    "instead of placing a fresh SELL.", symbol, stale_order_id,
                )
                try:
                    stale_result = await loop.run_in_executor(
                        None, dhan_wrapper.refresh_order_status, stale_order_id
                    )
                    final_exit_price = stale_result.fill_price or exit_price
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "%s: could not fetch stale order %s's own fill price - using %.2f instead",
                        symbol, stale_order_id, exit_price,
                    )
                    final_exit_price = exit_price
                await position_store.close_position(symbol, final_exit_price, reason)
                await loop.run_in_executor(
                    None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol
                )
                return
            logger.warning(
                "%s: broker shows only %d qty left (stored position says %d) after cancelling stale "
                "order %s - a PARTIAL fill happened on that resting order. Selling only the real "
                "remaining %d qty instead of the stale %d to avoid an unintended naked short. NOTE: "
                "the pnl logged for this exit covers only this remaining leg, not a blended figure "
                "across both fills - check /luxury/positions' own orders_today (or Dhan's real order "
                "record for %s) for the earlier partial fill's own price/quantity if an exact total is "
                "needed.", symbol, broker_qty, position.quantity, stale_order_id, broker_qty,
                position.quantity, stale_order_id,
            )
            position.quantity = broker_qty

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
    instance/cache (Luxury has no LTP cache of its own). The REST fallback
    is gated by dhan_wrapper.ltp_rest_fallback_semaphore - the SAME
    semaphore Options'/Futures' own _get_ltp uses, since all strategies
    compete for the same real Dhan rate-limit budget."""
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
    lives in the shared dhan_client.py - see Luxury/config.py's own
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


def _exit_reason_for(
    position: Position, ltp: float, supertrend_against_position: bool = False,
    liquidity_guard_triggered: bool = False,
) -> Optional[str]:
    """See Options/trading_engine.py's version - identical logic, including
    the current_max_loss_per_trade_rs() absolute rupee-loss cap checked
    first and the current_profit_protection_threshold_rs() rupee profit-
    lock checked after TARGET_HIT - both split into a before/after-
    config.RISK_THRESHOLD_CUTOFF_TIME pair.

    liquidity_guard_triggered (added 2 Sep 2026, this package only so
    far - see config.LIQUIDITY_GUARD_ENABLED's own docstring) is checked
    LAST, deliberately - it's an independent, additional early-warning
    trigger (the option's own contract has gone quiet for several
    minutes straight), not meant to override a genuine profit-taking
    exit that already fired first on the exact same tick; it only
    matters when NONE of the price-threshold checks above have fired
    yet, which is exactly the CHOLAFIN scenario this was built from -
    price was still roughly flat (nowhere near any threshold) when the
    illiquidity was already visible, several minutes before the gap."""
    loss_rs = (position.entry_price - ltp) * position.quantity
    if loss_rs >= current_max_loss_per_trade_rs():
        return "MAX_LOSS_HIT"
    if ltp >= position.target_price:
        return "TARGET_HIT"
    peak_profit_rs = (position.highest_price - position.entry_price) * position.quantity
    if peak_profit_rs > current_profit_protection_threshold_rs() and ltp < position.highest_price:
        return "PROFIT_PROTECTION_HIT"
    trailing_sl = position.current_trailing_sl
    if ltp <= trailing_sl:
        return "TRAILING_SL_HIT" if trailing_sl > position.hard_stop_loss else "STOP_LOSS_HIT"
    if config.ENABLE_SUPERTREND_EXIT and supertrend_against_position:
        return "SUPERTREND_EXIT"
    if config.LIQUIDITY_GUARD_ENABLED and liquidity_guard_triggered:
        return "LIQUIDITY_GUARD_ZERO_VOLUME"
    return None


async def _check_broker_stop_already_filled(symbol: str, position: Position) -> bool:
    """See config.BROKER_STOP_LOSS_ENABLED's own docstring for the full
    design. Cheap (cache-only unless the order has actually gone
    terminal - see check_if_order_filled's own docstring) - safe to call
    on every single monitor tick/price tick for every open position.
    Returns True if the broker's own stop-loss order had ALREADY filled
    (in which case the position is closed here directly, no fresh SELL
    needed - it's already flat at the broker) - callers should stop
    processing this position for the current tick either way."""
    if not config.BROKER_STOP_LOSS_ENABLED or not position.stop_loss_order_id:
        return False
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, dhan_wrapper.check_if_order_filled, position.stop_loss_order_id
        )
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not check broker stop-loss order %s status - falling through to "
                          "the normal poll/tick-driven check this tick", symbol, position.stop_loss_order_id)
        return False
    if result is None:
        return False  # still resting, unfired - nothing to do
    if result.status == OrderStatus.TRADED:
        final_exit_price = result.fill_price or position.hard_stop_loss
        logger.info(
            "%s: broker-side stop-loss order %s ALREADY FILLED (exchange fired it directly, ahead of "
            "our own poll/tick check) - closing at the real fill price %.2f",
            symbol, position.stop_loss_order_id, final_exit_price,
        )
        await position_store.close_position(symbol, final_exit_price, "MAX_LOSS_HIT")
        await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol)
        return True
    # REJECTED/CANCELLED/EXPIRED - the broker-side stop is gone without
    # firing (e.g. a margin/RMS rejection after placement). The position
    # is now protected ONLY by the existing poll/tick-driven MAX_LOSS_HIT
    # check from here on - logged loudly since this is a real, if rare,
    # loss of the faster backstop this feature exists to provide.
    logger.warning(
        "%s: broker-side stop-loss order %s ended as %s without firing - this position now relies "
        "solely on the regular poll/tick-driven MAX_LOSS_HIT check",
        symbol, position.stop_loss_order_id, result.status,
    )
    return False


async def _check_one_position(symbol: str, position: Position) -> None:
    if position.pending_exit_order_id or _exit_on_cooldown(position):
        return

    if await _check_broker_stop_already_filled(symbol, position):
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

    liquidity_guard_triggered = False
    if config.LIQUIDITY_GUARD_ENABLED:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, dhan_wrapper.refresh_liquidity_signal, position.option_trading_symbol)
        liquidity_guard_triggered = bool(dhan_wrapper.get_cached_illiquid(position.option_trading_symbol))

    reason = _exit_reason_for(position, ltp, supertrend_against_position, liquidity_guard_triggered)
    if reason and await position_store.try_start_exit(symbol):
        await _exit_position(symbol, position, ltp, reason)


async def on_price_tick(trading_symbol: str, ltp: float) -> None:
    """See Options/trading_engine.py's version - identical logic, this
    package's own position_store. Wired up in luxury_main.py's lifespan
    via dhan_wrapper.add_price_tick_subscriber() - a list, not a single
    slot, specifically so this can coexist with Options'/Futures' own
    subscribers without any of them silently overwriting another (see
    dhan_client.py's _on_price_tick_subscribers docstring)."""
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

        if await _check_broker_stop_already_filled(symbol, position):
            return

        await position_store.update_highest_price(symbol, ltp)
        supertrend_against_position = config.ENABLE_SUPERTREND_EXIT and _supertrend_signal_for(position)
        liquidity_guard_triggered = config.LIQUIDITY_GUARD_ENABLED and bool(
            dhan_wrapper.get_cached_illiquid(position.option_trading_symbol)
        )

        reason = _exit_reason_for(position, ltp, supertrend_against_position, liquidity_guard_triggered)
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
    logger.info("Luxury monitor loop started.")
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
            logger.exception("Error in Luxury monitor loop tick")

        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)
