"""
Core Swing v2 strategy logic (complete rewrite, 12 Sep 2026 - see
Swing/config.py's module docstring for the full user request and design).

Flow:
  0. reconcile_broker_positions() - at startup, import positions already
     open at Dhan and attributed to "Swing" by our own opened-position
     history (never guessed) - see trade_history.attribute_open_broker_
     position.
  1. monitor_loop() - one background loop, every tick: check open
     positions for an exit signal first (cheapest/most urgent), then scan
     the watchlist for a fresh entry signal if there's spare capacity.
  2. Continuous WebSocket price ticks (futures/options positions only -
     see on_price_tick) drive a fast, cache-only exit check between polls;
     the poll loop itself is what keeps the underlying signal caches
     (Swing/signals.py) warm and is the only path for equity positions,
     which have no WS feed.

Every exit/order-sync mechanic below (the stale-order-cancel-and-
broker-quantity-reconcile sequence in _exit_position, the broker-side
SL-L already-filled check) is a direct, deliberately UNMODIFIED port of
Options/trading_engine.py's own incident-hardened design - see that
file's own extensive comments for the real incidents (Luxury's OIL SL-L
orphan, the TECHM naked short) that shaped each piece of it. The one
substantive addition here is direction-awareness (a Swing position can
be LONG or SHORT, unlike Options/Futures/Luxury which are always long) -
handled entirely through Swing/position_store.py's pure helper functions
(exit_transaction_type, broker_stop_trigger_and_limit, etc.) rather than
scattered branches in this file.
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
from trade_history import append_jsonl, attribute_open_broker_position

from . import config, signals
from .position_store import (
    EXIT_CLAIMED, OrderRecord, Position, position_store,
    broker_stop_trigger_and_limit, entry_transaction_type, exit_transaction_type,
    giveback_floor, hard_stop_for, is_more_favorable, price_past_giveback_floor,
    price_past_hard_stop, price_past_target, resolve_instrument_side,
    resolved_option_type_for, target_price_for, unrealized_pnl_rs,
)
from Options.dhan_client import IST, OrderStatus, dhan_wrapper

logger = logging.getLogger("swing_trading_engine")

# Order-placement dispatch, keyed by Position.exchange_segment - added 12
# Sep 2026 (Swing v2's Copper/MCX options support) to replace what used to
# be a 2-way `if exchange_segment == "NSE_FNO": ... else: ...` at every
# call site (that binary shape silently routed a THIRD segment into the
# equity placer, which would be wrong - e.g. it would try exchange="NSE"
# for an MCX order). Deliberately a NAME dict, resolved via getattr at
# CALL time (not a dict of bound methods captured once at import time) -
# this codebase's whole test suite mocks by reassigning an attribute on
# the dhan_wrapper singleton at runtime (e.g. odc.dhan_wrapper.place_
# market_order = fake_fn in tests/test_swing_v2_entry_exit.py); a dict
# built once at import would have silently captured the pre-mock function
# and ignored every test's monkey-patch. A missing key raises KeyError
# rather than silently defaulting to the wrong exchange, which is the
# correct failure mode here.
_MARKET_ORDER_PLACER_NAMES = {
    "NSE_FNO": "place_market_order",
    "NSE_EQ": "place_equity_market_order",
    "MCX_COMM": "place_mcx_market_order",
}
_SL_LIMIT_PLACER_NAMES = {
    "NSE_FNO": "place_stop_loss_limit_order",
    "NSE_EQ": "place_equity_stop_loss_limit_order",
    "MCX_COMM": "place_mcx_stop_loss_limit_order",
}


def _market_order_placer(exchange_segment: str):
    return getattr(dhan_wrapper, _MARKET_ORDER_PLACER_NAMES[exchange_segment])


def _sl_limit_placer(exchange_segment: str):
    return getattr(dhan_wrapper, _SL_LIMIT_PLACER_NAMES[exchange_segment])


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
    """Durable, queryable event log for Swing (unchanged purpose from the
    old design - see history/swing_events for the full record). No
    longer carries a "strategy_mode" field - that concept doesn't exist
    in this rewrite (one strategy, not three coexisting modes)."""
    record = {"event": event, "underlying_symbol": symbol, "logged_at": _now_ist().isoformat(), **detail}
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, append_jsonl, SWING_EVENTS_LOG_NAME, record)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not append Swing event record (%s, %s) - the action itself is unaffected, "
            "this is logging-only.", event, symbol,
        )


# --------------------------------------------------------------------------- #
# Signal evaluation
# --------------------------------------------------------------------------- #
async def _evaluate_entry_signal(symbol: str) -> Optional[str]:
    """None unless BOTH the regime and Supertrend signals have real data.
    "BULLISH" if the regime is bullish AND the 5-min close just crossed
    ABOVE the 5-min Supertrend; "BEARISH" if the regime is bearish AND it
    just crossed BELOW. Uses the crossover EDGE (crossed_above/
    crossed_below - a state change between the last two closed candles),
    not a standing "is above", so an established trend doesn't re-fire
    an entry signal on every single bar."""
    regime = await signals.get_regime_state(symbol)
    if regime is None:
        return None
    st = await signals.get_supertrend_state(symbol)
    if st is None:
        return None
    if regime.is_bullish and st.crossed_above:
        return "BULLISH"
    if not regime.is_bullish and st.crossed_below:
        return "BEARISH"
    return None


async def _evaluate_exit_signal(symbol: str, position: Position) -> Optional[str]:
    """Supertrend reversal against the held direction - but only once the
    cached signal has moved past the candle the position was entered on
    (position.supertrend_entry_candle_start), so the very breakout candle
    that triggered entry can't immediately "reverse" it. Identical
    reasoning to Options/trading_engine.py's own _supertrend_signal_for
    guard."""
    if not config.ENABLE_SUPERTREND_EXIT:
        return None
    st = await signals.get_supertrend_state(symbol)
    if st is None or st.candle_start is None:
        return None
    if position.supertrend_entry_candle_start and st.candle_start <= position.supertrend_entry_candle_start:
        return None  # still reading the entry candle itself - not a real reversal yet
    reversed_against_long = position.instrument_side == "LONG" and st.crossed_below
    reversed_against_short = position.instrument_side == "SHORT" and st.crossed_above
    if reversed_against_long or reversed_against_short:
        return "SUPERTREND_REVERSAL"
    return None


def _exit_reason_for(position: Position, ltp: float) -> Optional[str]:
    """Pure function - flat rupee/percent thresholds (user request: MAX
    LOSS PROTECTION=4500, PROFIT PROTECTION=2000, TARGET=20%, HARD STOP
    LOSS=20%), checked in the same relative order as every other package
    in this codebase (rupee cap first, then target, then profit-lock,
    then the hard stop). Direction-aware via Swing/position_store.py's
    pure helpers - see that module for the LONG vs SHORT math, especially
    giveback_floor's mirrored sign for a SHORT."""
    side = position.instrument_side
    loss_rs = -unrealized_pnl_rs(side, position.entry_price, ltp, position.pnl_multiplier)
    if loss_rs >= config.MAX_LOSS_PROTECTION_RS:
        return "MAX_LOSS_HIT"
    if config.ENABLE_TARGET_EXIT and price_past_target(side, ltp, position.target_price):
        return "TARGET_HIT"
    peak_profit_rs = unrealized_pnl_rs(side, position.entry_price, position.best_price, position.pnl_multiplier)
    if peak_profit_rs > config.PROFIT_PROTECTION_RS:
        floor = giveback_floor(side, position.best_price, config.PROFIT_PROTECTION_GIVEBACK_PCT)
        if price_past_giveback_floor(side, ltp, floor):
            return "PROFIT_PROTECTION_HIT"
    if price_past_hard_stop(side, ltp, position.hard_stop_loss):
        return "STOP_LOSS_HIT"
    return None


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
async def enter_position_for_stock(symbol: str, regime: str) -> dict:
    """regime: "BULLISH"/"BEARISH", already confirmed by _evaluate_entry_
    signal. Reads config.BASKET_TYPE FRESH (not cached anywhere) so a
    config change takes effect on the very next entry, no restart needed."""
    if not config.STRATEGY_ENABLED:
        return {"symbol": symbol, "status": "ignored", "reason": "strategy_disabled"}

    basket_type = config.BASKET_TYPE.upper()
    side = resolve_instrument_side(basket_type, regime)
    if side is None:
        # Only EQUITY+BEARISH takes this path today (see resolve_instrument_
        # side's own docstring) - checked BEFORE reserve_symbol so this
        # doesn't burn a capacity slot for a trade that was never going to
        # be placed.
        logger.info("%s: skipped - %s basket-type is long-only, regime is BEARISH", symbol, basket_type)
        await _record_swing_event("ENTRY_SKIPPED_EQUITY_LONG_ONLY", symbol, {"basket_type": basket_type})
        return {"symbol": symbol, "status": "skipped", "reason": "equity_long_only"}

    # Copper/MCX futures trading is explicitly NOT enabled (user request 12
    # Sep 2026: "disable Copper Future trading as of now, only Options
    # trading for Copper") - checked BEFORE reserve_symbol, same reasoning
    # as the equity+bearish skip above, so a MCX+FUTURES combination never
    # burns a capacity slot for a trade that will never be placed. The
    # futures contract for an MCX symbol is still resolved elsewhere (see
    # Swing/signals.py) purely as the regime/Supertrend signal reference -
    # this only blocks actually OPENING a real futures position.
    is_mcx = symbol in config.MCX_SYMBOLS
    if is_mcx and basket_type == "FUTURES":
        logger.info("%s: skipped - Copper/MCX futures trading is not enabled (options only for now)", symbol)
        await _record_swing_event("ENTRY_SKIPPED_MCX_FUTURES_DISABLED", symbol, {"basket_type": basket_type})
        return {"symbol": symbol, "status": "skipped", "reason": "mcx_futures_disabled"}

    if not await position_store.reserve_symbol(symbol):
        return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}

    loop = asyncio.get_running_loop()
    try:
        option_type = resolved_option_type_for(basket_type, regime)
        try:
            if basket_type == "FUTURES":
                # is_mcx+FUTURES is already blocked above, so this branch
                # only ever runs for an NSE underlying.
                contract = await loop.run_in_executor(None, dhan_wrapper.get_futures_contract, symbol)
                trading_symbol, security_id, lot_size = contract.trading_symbol, contract.security_id, contract.lot_size
                exchange_segment, product_type = "NSE_FNO", config.FUTURES_PRODUCT
                quantity = lot_size * config.QUANTITY_LOTS
                pnl_multiplier = quantity
            elif basket_type == "OPTIONS":
                # get_atm_option is already MCX-capable for a Copper-style
                # symbol (Tradehull's own ATM_Strike_Selection has a native
                # commodity_step_dict branch; the only thing that used to
                # reject the MCX row was _instrument_meta's NSE-only filter,
                # widened 12 Sep 2026) - same call for NSE and MCX symbols.
                atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, option_type)
                if atm.expiry_date == _now_ist().date():
                    logger.info("%s: skipped - %s expires today and no later expiry is available yet",
                                symbol, atm.trading_symbol)
                    return {"symbol": symbol, "status": "skipped_expiry_day", "option_trading_symbol": atm.trading_symbol}
                trading_symbol, security_id, lot_size = atm.trading_symbol, atm.security_id, atm.lot_size
                quantity = lot_size * config.QUANTITY_LOTS
                if is_mcx:
                    exchange_segment, product_type = "MCX_COMM", config.MCX_PRODUCT
                    # NOT quantity - see Position.pnl_multiplier's own
                    # docstring for why MCX needs a real, separately-
                    # configured rupee-per-point multiplier here instead
                    # of the tiny lot-count `quantity` (correct for order
                    # placement, wrong for rupee-threshold math).
                    pnl_multiplier = config.MCX_PNL_MULTIPLIERS[symbol] * config.QUANTITY_LOTS
                else:
                    exchange_segment, product_type = "NSE_FNO", config.OPTIONS_PRODUCT
                    pnl_multiplier = quantity
            else:  # EQUITY
                meta = await loop.run_in_executor(None, dhan_wrapper._equity_instrument_meta, symbol)
                trading_symbol, security_id, lot_size = symbol, meta["security_id"], None
                exchange_segment, product_type = "NSE_EQ", config.EQUITY_PRODUCT
                quantity = config.EQUITY_QUANTITY
                pnl_multiplier = quantity
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not resolve the %s instrument for entry", symbol, basket_type)
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
                await _record_swing_event("ENTRY_SKIPPED_INSUFFICIENT_FUNDS", symbol, {"basket_type": basket_type})
                return {"symbol": symbol, "status": "skipped", "reason": "insufficient_funds", "trading_symbol": trading_symbol}

        transaction_type = entry_transaction_type(side)
        if exchange_segment == "NSE_FNO":
            await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, trading_symbol)
        # WS ticks are ONLY subscribed for NSE_FNO above - equity and MCX
        # both have no WS feed today (see Swing/config.py's MCX_SYMBOLS
        # docstring for MCX; _get_ltp's own docstring for both) and fall
        # back to the REST poll loop only.
        place_fn = _market_order_placer(exchange_segment)
        order_resp = await loop.run_in_executor(
            None, place_fn, trading_symbol, quantity, transaction_type, tag, product_type,
        )
        order_id, is_amo = order_resp["order_id"], order_resp["is_amo"]
        await position_store.record_order(OrderRecord(
            order_id=order_id, underlying_symbol=symbol, trading_symbol=trading_symbol,
            transaction_type=transaction_type, quantity=quantity, status=OrderStatus.TRANSIT,
            is_amo=is_amo, lot_size=lot_size,
        ))

        result = await loop.run_in_executor(None, dhan_wrapper.wait_for_order_result, order_id, is_amo)
        await position_store.update_order_status(order_id, result.status, result.remark)

        # Swing v2 requires a LITERAL TRADED fill to count as a real entry -
        # no AMO-promotion path for entries (unlike Options/Futures/Luxury's
        # own _sync_pending_orders). This is the exact fill-confirmation
        # discipline that came out of the real MAHABANK phantom-exit
        # incident (a non-terminal order status must never be counted as a
        # position) - anything other than TRADED here is a FAILED entry,
        # not "pending," and is released rather than left half-tracked.
        if result.status != OrderStatus.TRADED:
            if exchange_segment == "NSE_FNO":
                await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, trading_symbol)
            logger.warning("%s: entry order %s did not reach TRADED (status=%s remark=%s) - treating as a failed entry",
                            symbol, order_id, result.status, result.remark)
            return {"symbol": symbol, "status": "failed", "order_status": result.status, "trading_symbol": trading_symbol}

        fill_price = result.fill_price or await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, trading_symbol)

        st = await signals.get_supertrend_state(symbol)
        entry_candle_start = st.candle_start if st else None

        stop_loss_order_id = None
        if config.BROKER_STOP_LOSS_ENABLED:
            # pnl_multiplier, NOT quantity - the rupee cap must be divided
            # by the REAL per-unit exposure, not the (possibly much
            # smaller, for MCX) order-placement quantity. See Position.
            # pnl_multiplier's own docstring.
            trigger_price, limit_price = broker_stop_trigger_and_limit(
                side, fill_price, pnl_multiplier, config.MAX_LOSS_PROTECTION_RS, config.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE,
            )
            try:
                stop_tag = _gen_tag("SL", symbol)
                sl_placer = _sl_limit_placer(exchange_segment)
                stop_resp = await loop.run_in_executor(
                    None, sl_placer, trading_symbol, quantity, exit_transaction_type(side),
                    trigger_price, limit_price, stop_tag, product_type,
                )
                stop_loss_order_id = stop_resp["order_id"]
                logger.info("%s: broker-side %s STOP-LOSS LIMIT order %s placed for %s, trigger=%.2f limit=%.2f",
                            symbol, exit_transaction_type(side), stop_loss_order_id, trading_symbol, trigger_price, limit_price)
                await position_store.record_order(OrderRecord(
                    order_id=stop_loss_order_id, underlying_symbol=symbol, trading_symbol=trading_symbol,
                    transaction_type=exit_transaction_type(side), quantity=quantity, status="PENDING", is_amo=False,
                ))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "%s: could not place the broker-side stop-loss order for %s - proceeding without it, "
                    "the existing poll/tick-driven MAX_LOSS_HIT check still protects this position exactly as before",
                    symbol, trading_symbol,
                )

        position = Position(
            underlying_symbol=symbol, trading_symbol=trading_symbol, basket_type=basket_type, regime=regime,
            instrument_side=side, exchange_segment=exchange_segment, product_type=product_type,
            quantity=quantity, lot_size=lot_size, entry_price=fill_price, best_price=fill_price,
            target_price=target_price_for(side, fill_price, config.TARGET_PCT),
            hard_stop_loss=hard_stop_for(side, fill_price, config.HARD_STOP_LOSS_PCT),
            order_id=order_id, pnl_multiplier=pnl_multiplier, resolved_option_type=option_type,
            supertrend_entry_candle_start=entry_candle_start, stop_loss_order_id=stop_loss_order_id,
        )
        await position_store.add_position(position)
        await _record_swing_event("POSITION_OPENED", symbol, {
            "basket_type": basket_type, "regime": regime, "instrument_side": side,
            "trading_symbol": trading_symbol, "entry_price": fill_price, "quantity": quantity,
        })
        return {"symbol": symbol, "status": "entered", "trading_symbol": trading_symbol, "entry_price": fill_price}
    except Exception:  # noqa: BLE001
        logger.exception("%s: unexpected error entering position", symbol)
        return {"symbol": symbol, "status": "error"}
    finally:
        # Any path above that didn't reach add_position() must release the
        # reservation so a later signal can retry this symbol.
        if symbol not in position_store.live_positions:
            await position_store.release_symbol(symbol)


# --------------------------------------------------------------------------- #
# Exit - direct port of Options/trading_engine.py's own proven sequence
# --------------------------------------------------------------------------- #
async def _check_broker_stop_already_filled(symbol: str, position: Position) -> bool:
    """Direct port of Options/trading_engine.py's identical function - see
    its own docstring for the full design. Checked first on every tick."""
    if not config.BROKER_STOP_LOSS_ENABLED or not position.stop_loss_order_id:
        return False
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, dhan_wrapper.check_if_order_filled, position.stop_loss_order_id)
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
        await position_store.close_position(symbol, final_exit_price, "MAX_LOSS_HIT")
        if position.exchange_segment == "NSE_FNO":
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)
        return True
    logger.warning("%s: broker-side stop-loss order %s ended as %s without firing - this position now relies "
                    "solely on the regular poll/tick-driven MAX_LOSS_HIT check",
                    symbol, position.stop_loss_order_id, result.status)
    return False


async def _exit_position(symbol: str, position: Position, exit_price: float, reason: str) -> None:
    """Caller MUST have already claimed via position_store.try_start_exit.
    Direct, deliberately unmodified port of Options/trading_engine.py's
    own _exit_position - see that function's docstring for the full
    reasoning behind every step (the OMS-lag stale-order fallback, the
    partial-fill reconciliation, the >=1 retry-reconciliation threshold).
    The one real change: exit_transaction_type(position.instrument_side)
    everywhere Options' version hardcodes "SELL" - a SHORT position's
    resting order and exit order are both BUYs."""
    loop = asyncio.get_running_loop()
    exit_side = exit_transaction_type(position.instrument_side)
    net_qty_fn = dhan_wrapper.get_broker_net_quantity

    try:
        stale_order_id = await loop.run_in_executor(
            None, dhan_wrapper.get_pending_order_id, position.trading_symbol, exit_side
        )
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not check for an already-outstanding %s order before placing a new one - "
                          "proceeding anyway", symbol, exit_side)
        stale_order_id = None

    if not stale_order_id and config.BROKER_STOP_LOSS_ENABLED and position.stop_loss_order_id:
        stale_order_id = position.stop_loss_order_id

    if stale_order_id:
        logger.warning("%s: found an already-outstanding %s order %s for %s (a stale order surviving a "
                        "restart, or this position's own broker-side SL-L) - cancelling it before placing "
                        "a fresh exit order.", symbol, exit_side, stale_order_id, position.trading_symbol)
        try:
            await loop.run_in_executor(None, dhan_wrapper.cancel_order, stale_order_id)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not cancel stale %s order %s - proceeding with a new order anyway",
                              symbol, exit_side, stale_order_id)

        try:
            broker_qty = await loop.run_in_executor(None, net_qty_fn, position.trading_symbol, position.exchange_segment)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not reconcile broker quantity after cancelling stale order %s - "
                              "proceeding with the stored quantity (%d), which WOULD oversell if that order "
                              "had actually partially filled", symbol, stale_order_id, position.quantity)
            broker_qty = None
        if broker_qty is not None and broker_qty != position.quantity:
            if broker_qty == 0:
                logger.warning("%s: broker shows this position already FLAT after cancelling stale order %s - "
                                "it must have fully filled right before/during the cancel race. Reconciling "
                                "as closed using that order's own real fill price instead of placing a fresh exit.",
                                symbol, stale_order_id)
                try:
                    stale_result = await loop.run_in_executor(None, dhan_wrapper.refresh_order_status, stale_order_id)
                    final_exit_price = stale_result.fill_price or exit_price
                except Exception:  # noqa: BLE001
                    logger.exception("%s: could not fetch stale order %s's own fill price - using %.2f instead",
                                      symbol, stale_order_id, exit_price)
                    final_exit_price = exit_price
                await position_store.close_position(symbol, final_exit_price, reason)
                if position.exchange_segment == "NSE_FNO":
                    await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)
                return
            logger.warning("%s: broker shows only %d qty left (stored position says %d) after cancelling stale "
                            "order %s - a PARTIAL fill happened. Exiting only the real remaining %d qty instead "
                            "of the stale %d to avoid an unintended over-trade.",
                            symbol, broker_qty, position.quantity, stale_order_id, broker_qty, position.quantity)
            # NOTE for when Swing's BROKER_STOP_LOSS_ENABLED is ever turned
            # on for an MCX symbol: this partial-fill path is only reached
            # via a stale broker-side SL-L order (see the gate a few lines
            # up), which stays impossible for Copper in this rollout
            # (BROKER_STOP_LOSS_ENABLED is off - Swing/config.py). If that
            # ever changes, pnl_multiplier should be scaled down by the
            # same ratio as quantity here (qty and pnl_multiplier both
            # represent "how many lots/units are still actually held," so
            # a partial fill shrinks both, not just the order quantity).
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
            if position.exchange_segment == "NSE_FNO":
                await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)
            return

    tag = _gen_tag("Ext", symbol)
    place_fn = _market_order_placer(position.exchange_segment)
    try:
        order_resp = await loop.run_in_executor(
            None, place_fn, position.trading_symbol, position.quantity, exit_side, tag, position.product_type,
        )
    except Exception:  # noqa: BLE001
        logger.exception("%s exit order failed for %s (%s) - backing off before retrying",
                          exit_side, symbol, position.trading_symbol)
        await position_store.record_exit_failure(symbol)
        return

    try:
        order_id, is_amo = order_resp["order_id"], order_resp["is_amo"]
        await position_store.record_order(OrderRecord(
            order_id=order_id, underlying_symbol=symbol, trading_symbol=position.trading_symbol,
            transaction_type=exit_side, quantity=position.quantity, status=OrderStatus.TRANSIT, is_amo=is_amo,
        ))
        await position_store.set_pending_exit_order(symbol, order_id, reason)

        result = await loop.run_in_executor(None, dhan_wrapper.wait_for_order_result, order_id, is_amo)
        await position_store.update_order_status(order_id, result.status, result.remark)

        if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
            logger.warning("%s exit order %s for %s rejected: status=%s remark=%s - backing off before retrying",
                            exit_side, order_id, symbol, result.status, result.remark)
            await position_store.set_pending_exit_order(symbol, None)
            await position_store.record_exit_failure(symbol)
            return

        await position_store.clear_exit_failure(symbol)

        if result.is_queued_amo:
            logger.info("%s exit order %s for %s queued as AMO - will confirm fill next session.",
                        exit_side, order_id, symbol)
            return

        final_exit_price = result.fill_price or exit_price
        await position_store.close_position(symbol, final_exit_price, reason)
        if position.exchange_segment == "NSE_FNO":
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)
        pnl = unrealized_pnl_rs(position.instrument_side, position.entry_price, final_exit_price, position.pnl_multiplier)
        logger.info("%s exit order %s FILLED for %s (%s): reason=%s entry=%s exit=%s qty=%s pnl=%.2f",
                    exit_side, order_id, symbol, position.trading_symbol, reason,
                    position.entry_price, final_exit_price, position.quantity, pnl)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error resolving %s exit order for %s (%s) - backing off before retrying",
                          exit_side, symbol, position.trading_symbol)
        await position_store.record_exit_failure(symbol)


def _exit_on_cooldown(position: Position) -> bool:
    return bool(position.next_exit_retry_at and datetime.now() < position.next_exit_retry_at)


async def _get_ltp(position: Position) -> float:
    """FNO positions use the same WS-cache-then-REST-fallback pattern as
    Options/Futures/Luxury (_get_ltp there). Equity and MCX have no WS
    feed today (subscribe_option_price hardcodes the NSE_FNO market-feed
    segment - see Swing/config.py's own note on this), so both are plain
    REST always - a 5s poll is adequate for a swing strategy's equity/
    Copper leg (MCX WS support deliberately deferred, 12 Sep 2026 - see
    Swing/config.py's MCX_SYMBOLS docstring)."""
    loop = asyncio.get_running_loop()
    if position.exchange_segment != "NSE_FNO":
        return await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, position.trading_symbol)
    ltp = await loop.run_in_executor(None, dhan_wrapper.get_cached_option_ltp, position.trading_symbol)
    if ltp is not None:
        return ltp
    async with dhan_wrapper.ltp_rest_fallback_semaphore:
        ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, position.trading_symbol)
        await loop.run_in_executor(None, dhan_wrapper.note_rest_ltp, position.trading_symbol, ltp)
    return ltp


async def _check_one_position(symbol: str, position: Position) -> None:
    if position.pending_exit_order_id or _exit_on_cooldown(position):
        return
    if await _check_broker_stop_already_filled(symbol, position):
        return
    try:
        ltp = await _get_ltp(position)
    except Exception:  # noqa: BLE001
        logger.exception("Could not fetch LTP for %s", position.trading_symbol)
        return

    await position_store.update_best_price(symbol, ltp)

    reason = _exit_reason_for(position, ltp)
    if not reason:
        st_reason = await _evaluate_exit_signal(symbol, position)
        reason = st_reason
    if reason and await position_store.try_start_exit(symbol):
        await _exit_position(symbol, position, ltp, reason)


async def on_price_tick(trading_symbol: str, ltp: float) -> None:
    """Event-driven fast path, fired on every WebSocket tick (futures/
    options positions only - equity isn't subscribed to the WS feed).
    Cache-only checks (_check_broker_stop_already_filled, _exit_reason_for
    against already-cached signal state) so a MAX_LOSS_HIT/TARGET/
    PROFIT_PROTECTION/STOP_LOSS exit fires the instant price crosses,
    not on the next 5s poll. The poll loop (_monitor_tick) remains the
    layer that refreshes the underlying regime/Supertrend caches and
    provides the slower Supertrend-reversal check and the equity backstop -
    same two-speed design Options/Futures/Luxury already run."""
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
        await position_store.update_best_price(symbol, ltp)
        reason = _exit_reason_for(position, ltp)
        if reason and await position_store.try_start_exit(symbol):
            await _exit_position(symbol, position, ltp, reason)
    except Exception:  # noqa: BLE001
        logger.exception("on_price_tick failed for %s", trading_symbol)


async def _square_off_all(reason: str) -> None:
    positions = dict(position_store.live_positions)
    if not positions:
        return
    logger.info("Square-off triggered (%s) for %d open Swing position(s)", reason, len(positions))
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
    """Minimal AMO-exit resync (no entry-side equivalent - see enter_
    position_for_stock's own docstring for why entries require a literal
    TRADED fill with no promotion path). Only relevant if an exit order
    placed right at session close ends up queued as AMO rather than
    filling immediately - re-checks it each tick until it resolves,
    exactly mirroring the exit-side half of Options/trading_engine.py's
    own _sync_pending_orders."""
    loop = asyncio.get_running_loop()
    for symbol, position in dict(position_store.live_positions).items():
        if not position.pending_exit_order_id or position.pending_exit_order_id == EXIT_CLAIMED:
            continue
        try:
            result = await loop.run_in_executor(None, dhan_wrapper.refresh_order_status, position.pending_exit_order_id, True)
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
            if position.exchange_segment == "NSE_FNO":
                await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)


# --------------------------------------------------------------------------- #
# Monitor loop
# --------------------------------------------------------------------------- #
async def _monitor_tick() -> None:
    # Exits first - more urgent than looking for new entries.
    for symbol, position in list(position_store.live_positions.items()):
        await _check_one_position(symbol, position)

    if not (config.STRATEGY_ENABLED and config.ENTRY_ENABLED):
        return
    if await position_store.remaining_capacity() <= 0:
        return

    from .watchlist import watchlist_store  # local import - avoids a circular import at module load time
    symbols = await watchlist_store.symbols()
    candidates: list[tuple[str, str]] = []
    for i, symbol in enumerate(symbols):
        if symbol in position_store.reserved_symbols:
            continue
        if i:
            await asyncio.sleep(config.SYMBOL_PACING_SECONDS)
        try:
            regime = await _evaluate_entry_signal(symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not evaluate entry signal", symbol)
            continue
        if regime:
            candidates.append((symbol, regime))

    for symbol, regime in candidates:
        if await position_store.remaining_capacity() <= 0:
            break
        await enter_position_for_stock(symbol, regime)


async def monitor_loop() -> None:
    """Runs forever regardless of config.STRATEGY_ENABLED (so flipping
    that flag needs no restart) - no EOD or Friday square-off anywhere in
    this loop, Swing positions are meant to carry for days by design."""
    logger.info("Swing v2 monitor loop started.")
    while True:
        try:
            await position_store.maybe_reset_for_new_day()
            await _sync_pending_exit_orders()
            await _monitor_tick()
        except Exception:  # noqa: BLE001
            logger.exception("Error in Swing v2 monitor loop tick")
        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# Startup reconciliation
# --------------------------------------------------------------------------- #
async def reconcile_broker_positions() -> list[Position]:
    """Best-effort import of positions already open at Dhan and attributed
    to "Swing" specifically by our own opened-position history (never
    guessed - see attribute_open_broker_position's own docstring for why
    an unattributable position is left alone rather than assumed).
    `regime` is set to "UNKNOWN" since it isn't recoverable from broker
    state alone - _evaluate_exit_signal only actually needs instrument_
    side, which IS recoverable from the sign of the broker's net
    quantity. This matters more here than anywhere else in this codebase:
    Swing positions genuinely live across restarts by design."""
    loop = asyncio.get_running_loop()
    fno_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)
    equity_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_equity_positions)
    # MCX scan added 12 Sep 2026, Swing v2's Copper options support - a
    # real open Copper position surviving a restart is picked back up the
    # same way an NSE one already is, rather than being silently invisible
    # to reconciliation (see get_open_mcx_positions' own docstring).
    mcx_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_mcx_positions)

    positions: list[Position] = []
    for bp, exchange_segment in (
        [(p, "NSE_FNO") for p in fno_positions]
        + [(p, "NSE_EQ") for p in equity_positions]
        + [(p, "MCX_COMM") for p in mcx_positions]
    ):
        avg_price = bp["avg_price"]
        if not avg_price:
            logger.warning("Skipping Swing reconciliation for %s - broker reported no average price.", bp["trading_symbol"])
            continue
        owner = await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"])
        if owner != "Swing":
            logger.warning(
                "Skipping Swing reconciliation for %s - attributed to %s (not Swing) by our own "
                "opened-position history. Real broker position is unaffected; this process just won't manage it.",
                bp["trading_symbol"], owner or "no strategy (no record found)",
            )
            continue

        side = "LONG" if bp["quantity"] > 0 else "SHORT"
        quantity = abs(bp["quantity"])
        basket_type = "EQUITY" if exchange_segment == "NSE_EQ" else ("OPTIONS" if bp.get("option_type") else "FUTURES")
        underlying_symbol = bp["underlying_symbol"]
        # pnl_multiplier: identical to quantity for NSE (see Position's own
        # docstring) - looked up from MCX_PNL_MULTIPLIERS for a reconciled
        # MCX position instead, same as a fresh entry would compute it.
        # Fails open to `quantity` (a WRONG but at least non-crashing
        # value) if this underlying was somehow never configured, logging
        # loudly so it doesn't go unnoticed - a KeyError here would break
        # startup reconciliation for every OTHER already-open position too.
        if exchange_segment == "MCX_COMM":
            if underlying_symbol in config.MCX_PNL_MULTIPLIERS:
                pnl_multiplier = config.MCX_PNL_MULTIPLIERS[underlying_symbol] * config.QUANTITY_LOTS
            else:
                logger.error(
                    "%s: reconciled MCX position has no configured MCX_PNL_MULTIPLIERS entry - "
                    "falling back to quantity (%d) as the P&L multiplier, which is almost certainly "
                    "WRONG for a commodity. Add SWING_MCX_PNL_MULTIPLIER_%s to .env.",
                    underlying_symbol, quantity, underlying_symbol,
                )
                pnl_multiplier = quantity
        else:
            pnl_multiplier = quantity
        positions.append(Position(
            underlying_symbol=underlying_symbol, trading_symbol=bp["trading_symbol"],
            basket_type=basket_type, regime="UNKNOWN", instrument_side=side,
            exchange_segment=exchange_segment, product_type=bp.get("product_type") or config.FUTURES_PRODUCT,
            quantity=quantity, lot_size=bp.get("lot_size"), entry_price=avg_price, best_price=avg_price,
            target_price=target_price_for(side, avg_price, config.TARGET_PCT),
            hard_stop_loss=hard_stop_for(side, avg_price, config.HARD_STOP_LOSS_PCT),
            order_id="", pnl_multiplier=pnl_multiplier, resolved_option_type=bp.get("option_type") or None,
            reconciled=True,
        ))
    return positions
