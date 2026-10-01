"""
Super Bollinger (30 Sep 2026, user request) - the Bollinger Hold-Long paper
strategy promoted to its own strategy with the best rules from the 29 Sep
2026 exit-policy backtest (backtest_bollinger_hold_long_exit_variants.py,
policy "G'" - +Rs 1.02 lakh over 31 Aug-29 Sep with 5 concurrent trades,
NSE stocks only; see trading-skills learnings/bollinger-hold-long-30day-
backtest-and-data-gotchas.md):

  Universe  data/super_bollinger_watchlist - its OWN list, picked by the
            HYBRID selection (stock_selection.py) and refreshed every Friday
            (SuperBollinger/watchlist.py; falls back to data/bollinger_watchlist
            if missing). NSE STOCKS
            ONLY - index symbols (Bollinger config.INDEX_SYMBOLS) and MCX
            commodities are always skipped, plus settings.excluded_symbols.
  Entry     BULLISH resting trigger only (the deployed Bollinger/Vortex
            pending order, read from the SAME shared signal cache - no extra
            Dhan calls), buy 1 lot of the ATM CE (roll to the next expiry
            within N trading days), min premium Rs 5, no new entries from
            the entry cutoff (14:00), max N open trades (capacity_control
            "SuperBollinger", default 5), signals seen while full are skipped.
            TICK-DRIVEN (30 Sep 2026): every tick of a watchlist stock checks
            its pending trigger and a touch goes straight to the order (see
            "Tick-driven entries" below); the 5s scan remains as a backup.
  Exit      MAX_LOSS_HIT at the rupee cap (4500); BREAKEVEN_STOP_HIT - once
            the trade has been >= Rs 1500 in profit, exit if the premium
            falls back to the entry price; otherwise DAILY_SQUARE_OFF at
            15:15. No percentage or trailing stop. A broker-side SL-L sits at
            the max-loss level as a disaster backstop (if the bot is down).
  Mode      real or paper via paper_mode_control ("SuperBollinger"), every
            rule above via settings.py - all runtime, no restart.

Order placement and every exit/order-sync mechanic reuse the deployed
Bollinger engine's incident-hardened functions (stale-order cancel, broker
quantity reconcile, manual-exit detection, LTP-staleness forced exit), run
against Super Bollinger's OWN position store. One REAL Bollinger-family
position per stock: see Bollinger.trading_engine.super_bollinger_real_holds
- the two strategies share the signal and would buy the same contract.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import capacity_control
import cross_strategy_registry
import order_safety
import fund_allocation
import paper_mode_control
from trade_history import attribute_open_broker_position
from Bollinger import config as bcfg, signals
from Bollinger import trading_engine as engine
from Bollinger.position_store import OrderRecord, Position, position_store as bollinger_store
from . import watchlist as super_watchlist
from Options.dhan_client import OrderResult, OrderStatus, dhan_wrapper
from Swing import candle_feed
from Swing.position_store import broker_stop_trigger_and_limit

from . import entry_filters, live_state, pricing, settings
from .state import EVENTS_LOG, STRATEGY, halted, paper_book, position_store

logger = logging.getLogger("super_bollinger_engine")

ORDER_TAG_PREFIX = "SBol"
MARKET_OPEN_TIME = "09:15"
NO_TRAILING = 1e12  # Position's trailing fields are unused here - this keeps apply_price_to_trailing from ever arming
_scan_turn = {"i": 0}

# Signal-consumption bookkeeping and roll setting for the shared entry
# evaluator (Bollinger.trading_engine._evaluate_entry_signal) - Super
# Bollinger's own, so acting on a signal here never uses it up for Bollinger.
PROFILE = engine.Profile(
    name=STRATEGY, entry_mode="resting", sides="long", exit_mode="hold_to_close",
    roll_days=settings.get("roll_expiry_within_trading_days"),
    daily_square_off_time=settings.get("square_off_time"),
    paper_book=paper_book, events_log=EVENTS_LOG, paper_only=False,
)


def _now() -> datetime:
    return engine._now_ist()


async def _event(event: str, symbol: str, detail: dict) -> None:
    await engine._record_bollinger_event(event, symbol, {"strategy": STRATEGY, **detail}, EVENTS_LOG)


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
def exit_reason_for(entry_price: float, best_price: float, ltp: float, multiplier: float,
                    max_loss_rs: float, breakeven_after_rs: float) -> Optional[str]:
    """Pure. best_price must already include `ltp`."""
    if (entry_price - ltp) * multiplier >= max_loss_rs:
        return "MAX_LOSS_HIT"
    if breakeven_after_rs > 0 and (best_price - entry_price) * multiplier >= breakeven_after_rs and ltp <= entry_price:
        return "BREAKEVEN_STOP_HIT"
    return None


def _position_exit_reason(pos: Position, ltp: float) -> Optional[str]:
    return exit_reason_for(pos.entry_price, pos.best_price, ltp, pos.pnl_multiplier,
                           settings.get("max_loss_rs"), settings.get("breakeven_after_rs"))


INDEX_STRATEGY = "SuperBollingerIndex"   # paper_mode_control toggle for the permanent index symbols


def index_symbols() -> list[str]:
    """The permanent index symbols (NIFTY/BANKNIFTY) currently switched on."""
    if not settings.get("index_enabled"):
        return []
    return [s for s in settings.get("index_symbols")
            if s in bcfg.INDEX_SYMBOLS and s not in settings.get("excluded_symbols")]


def is_paper_symbol(symbol: str) -> bool:
    """Paper or real for a NEW entry: index symbols follow their own runtime
    toggle, everything else the strategy's."""
    return paper_mode_control.is_paper_mode_enabled(INDEX_STRATEGY if symbol in bcfg.INDEX_SYMBOLS else STRATEGY)


def is_eligible_symbol(symbol: str) -> bool:
    """NSE stocks, plus the permanent index symbols when index_enabled; never
    an MCX commodity."""
    if symbol in settings.get("excluded_symbols"):
        return False
    if symbol in bcfg.INDEX_SYMBOLS:
        return symbol in index_symbols()
    try:
        return not dhan_wrapper.is_mcx_commodity(symbol)
    except Exception:  # noqa: BLE001
        return False


async def eligible_symbols() -> list[str]:
    """The weekly HYBRID stock watchlist + the permanent index symbols. The
    indices are added here, never stored in the watchlist file, so the Friday
    refresh cannot drop or reshuffle them."""
    syms, _source = await super_watchlist.symbols()
    out = [s for s in syms if s not in bcfg.INDEX_SYMBOLS and is_eligible_symbol(s)]
    return out + [s for s in index_symbols() if s not in out]


def _square_off_now() -> bool:
    now = _now()
    return now.weekday() < 5 and now >= engine._parse_hhmm_today(settings.get("square_off_time"))


def _entries_open_now() -> bool:
    now = _now()
    return (now.weekday() < 5 and engine._parse_hhmm_today(MARKET_OPEN_TIME) <= now
            < engine._parse_hhmm_today(settings.get("entry_cutoff_time")))


def open_count() -> int:
    """REAL slots in use (real positions + real entries in flight). Paper
    positions no longer take real slots (1 Oct 2026): with the index on paper
    and the limit at 2 (capital), one paper NIFTY/BANKNIFTY position used to
    block half of the real stock trades. Paper has its own, equal limit."""
    return len(position_store.reserved_symbols | set(position_store.live_positions))


def paper_open_count() -> int:
    return len(paper_book.positions)


def _slot_free_for(symbol: str) -> bool:
    cap = capacity_control.get_max_concurrent_trades(STRATEGY)
    return (paper_open_count() if is_paper_symbol(symbol) else open_count()) < cap


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
async def _resolve_leg(symbol: str) -> tuple[dict, float]:
    """ATM CE (with the expiry roll) + the minimum-premium gate. Raises
    engine._SkipEntry when the entry should not happen."""
    PROFILE.roll_days = settings.get("roll_expiry_within_trading_days")
    leg = await engine._resolve_option_leg(symbol, "BULLISH", PROFILE)
    leg["quantity"] = leg["lot_size"] * settings.get("quantity_lots")
    leg["pnl_multiplier"] = leg["quantity"]
    # Last traded price, else the live order book (30 Sep 2026: two real entries were lost as
    # "low premium" with premium = null when the price call returned nothing - see pricing.py).
    price, source = await pricing.price_for_entry(leg["trading_symbol"])
    minimum = settings.get("min_premium_rs")
    if price is None:
        logger.error("[%s] %s: no price at all for %s - entry skipped", STRATEGY, symbol, leg["trading_symbol"])
        await _event("ENTRY_SKIPPED_NO_PRICE", symbol, {"trading_symbol": leg["trading_symbol"]})
        raise engine._SkipEntry({"symbol": symbol, "status": "skipped", "reason": "no_price",
                                 "trading_symbol": leg["trading_symbol"], "premium": None})
    if price < minimum:
        await _event("ENTRY_SKIPPED_LOW_PREMIUM", symbol, {"trading_symbol": leg["trading_symbol"], "premium": price,
                                                           "minimum": minimum, "price_source": source})
        raise engine._SkipEntry({"symbol": symbol, "status": "skipped", "reason": "premium_below_minimum",
                                 "trading_symbol": leg["trading_symbol"], "premium": price})
    return leg, price


async def settle_unfilled_order(symbol: str, trading_symbol: str, order_id: str, result: OrderResult,
                                is_amo: bool, what: str) -> OrderResult:
    """An order that is not TRADED when the wait ends may still be RESTING at
    the broker. Found live 30 Sep 2026 (SONACOMS 27 OCT 830 CALL x1225): the
    market BUY came back PENDING after ~6s, the entry was treated as failed
    and the order was simply left at Dhan - blocking ~Rs 34k of funds and, had
    it filled later, leaving a real position nobody managed (no stop, no
    square-off). Now: cancel it, then read its final status once more - a fill
    that raced the cancel comes back TRADED and is handled as a normal fill
    by the caller. If it is STILL open after that, log loudly (event
    ORDER_STILL_RESTING_AT_BROKER) - it needs a manual cancel."""
    if result.status not in OrderStatus.OPEN_STATUSES:
        return result
    final, cancel_error = await order_safety.cancel_unfilled(order_id, result, is_amo)
    event = order_safety.outcome_event(final)
    await _event(event, symbol, {"what": what, "trading_symbol": trading_symbol, "order_id": order_id,
                                 "status_at_timeout": result.status, "final_status": final.status,
                                 "filled_quantity": final.filled_quantity, "cancel_error": cancel_error})
    return final


async def retry_unfilled_buy(symbol: str, leg: dict, quantity: int, reference_price: float, still_valid, what: str,
                             store, tag_prefix: str) -> tuple[Optional[OrderResult], Optional[str], Optional[str], str]:
    """Re-price and retry a BUY whose first order was cancelled unfilled
    (settings entry_retry_*). Each attempt: check the setup still holds
    (`still_valid()` -> None, or the reason to give up), read the live book,
    send a LIMIT at the best ask (capped at reference_price + entry_chase_
    max_pct), wait, cancel if unfilled. Returns (result, order_id, intent_id,
    reason): on a fill the result is TRADED and the order's write-ahead intent
    is STILL OPEN - the caller finishes it once the position is recorded."""
    loop = asyncio.get_running_loop()
    ts, product = leg["trading_symbol"], leg["product_type"]
    attempts = settings.get("entry_retry_max")
    cap = reference_price * (1 + settings.get("entry_chase_max_pct") / 100)
    reason = "retries_off" if attempts <= 0 else "retries_exhausted"
    for attempt in range(1, attempts + 1):
        why = await still_valid()
        if why:
            reason = why
            break
        try:
            quote = await dhan_wrapper.get_option_quote_async(ts)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: no quote for %s - cannot re-price the %s", STRATEGY, symbol, ts, what)
            reason = "no_quote"
            break
        if quote.get("ask"):
            price = quote["ask"]
        elif quote.get("ltp"):
            price = quote["ltp"] * (1 + settings.get("entry_retry_limit_buffer_pct") / 100)
        else:
            reason = "no_quote"
            break
        if price > cap:
            reason = "price_ran_away"
            await _event("ORDER_RETRY_SKIPPED", symbol, {"what": what, "attempt": attempt, "trading_symbol": ts,
                                                         "ask": quote.get("ask"), "limit_wanted": round(price, 2),
                                                         "cap": round(cap, 2), "reference_price": reference_price})
            break
        intent = live_state.intent_begin(what, symbol, leg, quantity, retry=attempt)
        keep_intent, outcome_known = False, False
        try:
            resp = await loop.run_in_executor(None, dhan_wrapper.place_limit_order, ts, quantity, "BUY", price,
                                              engine._gen_tag(tag_prefix, symbol), product)
            order_id = resp["order_id"]
            live_state.intent_order(intent, order_id)
            await store.record_order(OrderRecord(order_id=order_id, underlying_symbol=symbol, trading_symbol=ts,
                                                 transaction_type="BUY", quantity=quantity, status=OrderStatus.TRANSIT,
                                                 is_amo=False, lot_size=leg.get("lot_size")))
            polls = max(1, int(round(settings.get("entry_retry_wait_seconds"))))
            try:
                result = await asyncio.wait_for(
                    dhan_wrapper.wait_for_order_result_async(order_id, False, polls, 1.0),
                    timeout=polls + engine._ORDER_RESULT_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001
                result = OrderResult(order_id=order_id, status=OrderStatus.TRANSIT, remark="order_confirmation_timeout",
                                     fill_price=0.0, filled_quantity=0, is_amo=False)
            if result.status != OrderStatus.TRADED:
                result = await settle_unfilled_order(symbol, ts, order_id, result, False, f"{what} retry {attempt}")
            await store.update_order_status(order_id, result.status, result.remark)
            await _event("ORDER_RETRY", symbol, {"what": what, "attempt": attempt, "trading_symbol": ts,
                                                 "bid": quote.get("bid"), "ask": quote.get("ask"), "ltp": quote.get("ltp"),
                                                 "limit_price": resp.get("price"), "reference_price": reference_price,
                                                 "order_id": order_id, "status": result.status,
                                                 "fill_price": result.fill_price or None})
            if result.status == OrderStatus.TRADED:
                keep_intent = True
                return result, order_id, intent, "filled"
            outcome_known = result.status not in OrderStatus.OPEN_STATUSES
        finally:
            if not keep_intent:
                await live_state.intent_finish(intent, outcome_known)
    await _event("ENTRY_ABANDONED" if what == "entry" else "HEDGE_ABANDONED", symbol,
                 {"trading_symbol": ts, "reason": reason, "reference_price": reference_price, "attempts_allowed": attempts})
    return None, None, None, reason


def _new_position(symbol: str, leg: dict, entry_price: float, order_id: str,
                  stop_loss_order_id: Optional[str] = None, reconciled: bool = False) -> Position:
    return Position(
        underlying_symbol=symbol, trading_symbol=leg["trading_symbol"], resolved_option_type="CE",
        instrument_side="LONG", exchange_segment="NSE_FNO", product_type=leg["product_type"],
        quantity=leg["quantity"], lot_size=leg["lot_size"], entry_price=entry_price, best_price=entry_price,
        stop_pct=0.0, hard_stop_loss=max(entry_price - settings.get("max_loss_rs") / leg["quantity"], 0.05),
        trailing_stop_dist=NO_TRAILING, trailing_step=NO_TRAILING, pnl_multiplier=leg["quantity"],
        order_id=order_id, reconciled=reconciled, stop_loss_order_id=stop_loss_order_id,
    )


async def enter_paper(symbol: str, trigger_price: float, stop_price: float, source: str = "scan") -> dict:
    if symbol in paper_book.positions:
        return {"symbol": symbol, "status": "skipped", "reason": "paper_position_already_open"}
    try:
        leg, price = await _resolve_leg(symbol)
    except engine._SkipEntry as skip:
        return skip.result
    pos = _new_position(symbol, leg, price, "PAPER")
    if not await paper_book.open(pos):
        return {"symbol": symbol, "status": "skipped", "reason": "paper_position_already_open"}
    try:
        await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.subscribe_option_price, leg["trading_symbol"])
    except Exception:  # noqa: BLE001
        logger.exception("[%s] %s: could not WS-subscribe %s - REST fallback will be used", STRATEGY, symbol,
                         leg["trading_symbol"])
    await _event("PAPER_POSITION_OPENED", symbol, {"trading_symbol": leg["trading_symbol"], "entry_price": price,
                                                   "quantity": leg["quantity"], "trigger_price": trigger_price,
                                                   "stop_price": stop_price, "entry_source": source})
    return {"symbol": symbol, "status": "paper_entered", "trading_symbol": leg["trading_symbol"], "entry_price": price}


async def enter_real(symbol: str, trigger_price: float, stop_price: float, source: str = "scan") -> dict:
    """REAL-money entry. Claims the stock in the shared cross-strategy
    registry for the whole attempt, then refuses it if the deployed
    Bollinger strategy holds (or is entering) a real position in it."""
    if not await cross_strategy_registry.try_claim(symbol, STRATEGY):
        return {"symbol": symbol, "status": "skipped", "reason": "entry_in_progress_by_other_strategy"}
    key = cross_strategy_registry.same_contract_key(symbol)   # also mutually exclusive with Swing (30 Sep 2026)
    try:
        if not await cross_strategy_registry.try_claim(key, STRATEGY):
            return {"symbol": symbol, "status": "skipped", "reason": "entry_in_progress_by_other_strategy"}
        if symbol in bollinger_store.live_positions or symbol in bollinger_store.reserved_symbols:
            logger.info("[%s] %s: skipped - Bollinger already holds a real position in this stock", STRATEGY, symbol)
            await _event("ENTRY_SKIPPED_HELD_BY_BOLLINGER", symbol, {})
            return {"symbol": symbol, "status": "skipped", "reason": "held_by_bollinger"}
        if engine.swing_real_holds(symbol):
            logger.info("[%s] %s: skipped - Swing already holds a real option position in this underlying", STRATEGY, symbol)
            await _event("ENTRY_SKIPPED_HELD_BY_SWING", symbol, {})
            return {"symbol": symbol, "status": "skipped", "reason": "held_by_swing"}
        if not await position_store.reserve_symbol(symbol):
            return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}
        try:
            return await _enter_real_reserved(symbol, trigger_price, stop_price, source)
        finally:
            if symbol not in position_store.live_positions:
                await position_store.record_failed_entry(symbol)
                await position_store.release_symbol(symbol)
    finally:
        await cross_strategy_registry.release_claim(key, STRATEGY)
        await cross_strategy_registry.release_claim(symbol, STRATEGY)


async def _enter_real_reserved(symbol: str, trigger_price: float, stop_price: float, source: str) -> dict:
    loop = asyncio.get_running_loop()
    intent, outcome_known = None, False
    try:
        try:
            leg, gate_price = await _resolve_leg(symbol)
        except engine._SkipEntry as skip:
            return skip.result
        trading_symbol, quantity, product_type = leg["trading_symbol"], leg["quantity"], leg["product_type"]

        if settings.get("funds_check_enabled"):
            try:
                sufficient = await fund_allocation.has_sufficient_bucket_funds(
                    bcfg.FUND_BUCKET, symbol, [(leg["security_id"], product_type, quantity, gate_price, "NSE_FNO")],
                    buffer_rs=bcfg.FUNDS_CHECK_BUFFER_RS,
                )
            except Exception:  # noqa: BLE001
                logger.exception("[%s] %s: funds check failed - proceeding optimistically", STRATEGY, symbol)
                sufficient = True
            if not sufficient:
                await _event("ENTRY_SKIPPED_INSUFFICIENT_FUNDS", symbol, {"trading_symbol": trading_symbol})
                return {"symbol": symbol, "status": "skipped", "reason": "insufficient_funds"}

        # Duplicate-real-order guard (same as Bollinger's / Swing's).
        try:
            existing = await loop.run_in_executor(None, dhan_wrapper.get_pending_order_id, trading_symbol, "BUY", "NSE")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not check for a resting BUY order - proceeding", STRATEGY, symbol)
            existing = None
        if existing:
            logger.warning("[%s] %s: BUY order %s already pending for %s - not placing a duplicate",
                           STRATEGY, symbol, existing, trading_symbol)
            return {"symbol": symbol, "status": "already_pending", "order_id": existing}

        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, trading_symbol)
        tag = engine._gen_tag(ORDER_TAG_PREFIX, symbol)
        intent = live_state.intent_begin("entry", symbol, leg, quantity, trigger_price=trigger_price)  # on file BEFORE the order
        order_resp = await loop.run_in_executor(
            None, dhan_wrapper.place_market_order, trading_symbol, quantity, "BUY", tag, product_type)
        order_id, is_amo = order_resp["order_id"], order_resp["is_amo"]
        live_state.intent_order(intent, order_id)
        await position_store.record_order(OrderRecord(
            order_id=order_id, underlying_symbol=symbol, trading_symbol=trading_symbol, transaction_type="BUY",
            quantity=quantity, status=OrderStatus.TRANSIT, is_amo=is_amo, lot_size=leg["lot_size"]))
        try:
            result = await asyncio.wait_for(
                dhan_wrapper.wait_for_order_result_async(order_id, is_amo),
                timeout=engine._ORDER_RESULT_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not confirm entry order %s - treating as failed", STRATEGY, symbol, order_id)
            result = OrderResult(order_id=order_id, status=OrderStatus.TRANSIT, remark="order_confirmation_timeout",
                                 fill_price=0.0, filled_quantity=0, is_amo=is_amo)
        await position_store.update_order_status(order_id, result.status, result.remark)
        if result.status != OrderStatus.TRADED:
            result = await settle_unfilled_order(symbol, trading_symbol, order_id, result, is_amo, "entry")
            await position_store.update_order_status(order_id, result.status, result.remark)
        if result.status == OrderStatus.CANCELLED and not is_amo:
            # Confirmed unfilled and gone: re-price at the live ask while the breakout still holds.
            await live_state.intent_finish(intent, True)
            intent = None

            async def still_valid() -> Optional[str]:
                if not _entries_open_now() or _square_off_now():
                    return "entry_window_closed"
                if _halted_today():
                    return "halted"
                forming = (candle_feed.forming_bar(symbol)
                           if candle_feed.is_fresh(symbol, bcfg.WS_STALE_AFTER_SECONDS) else None)
                spot = float(forming["last"]) if forming and forming.get("last") else None
                if spot is None:
                    return "no_spot_price"
                return "momentum_gone" if spot < trigger_price else None

            retried, retry_order_id, retry_intent, _why = await retry_unfilled_buy(
                symbol, leg, quantity, gate_price, still_valid, "entry", position_store, ORDER_TAG_PREFIX)
            if retried is not None:
                result, order_id, intent = retried, retry_order_id, retry_intent
        if result.status != OrderStatus.TRADED:  # literal TRADED-only fill discipline
            outcome_known = result.status not in OrderStatus.OPEN_STATUSES   # still resting -> intent_finish looks again
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, trading_symbol)
            logger.warning("[%s] %s: entry order %s not TRADED (status=%s remark=%s)", STRATEGY, symbol, order_id,
                           result.status, result.remark)
            return {"symbol": symbol, "status": "failed", "order_status": result.status}
        fill_price = result.fill_price or await dhan_wrapper.get_option_ltp_async(trading_symbol)

        stop_loss_order_id = None
        if bcfg.BROKER_STOP_LOSS_ENABLED:
            # Disaster backstop at the max-loss level (the bot's own 5s/tick
            # checks handle max-loss and breakeven normally).
            trigger, limit = broker_stop_trigger_and_limit(
                "LONG", fill_price, quantity, settings.get("max_loss_rs"),
                bcfg.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE, hard_stop_pct=0.95)
            try:
                stop_resp = await loop.run_in_executor(
                    None, dhan_wrapper.place_stop_loss_limit_order, trading_symbol, quantity, "SELL",
                    trigger, limit, engine._gen_tag("SL", symbol), product_type)
                stop_loss_order_id = stop_resp["order_id"]
                await position_store.record_order(OrderRecord(
                    order_id=stop_loss_order_id, underlying_symbol=symbol, trading_symbol=trading_symbol,
                    transaction_type="SELL", quantity=quantity, status="PENDING", is_amo=False))
                logger.info("[%s] %s: broker SL-L %s placed for %s trigger=%.2f limit=%.2f", STRATEGY, symbol,
                            stop_loss_order_id, trading_symbol, trigger, limit)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] %s: could not place the broker-side stop-loss for %s - the bot's own "
                                 "max-loss check still protects it", STRATEGY, symbol, trading_symbol)

        await position_store.add_position(_new_position(symbol, leg, fill_price, order_id, stop_loss_order_id))
        outcome_known = True
        await _event("POSITION_OPENED", symbol, {"trading_symbol": trading_symbol, "entry_price": fill_price,
                                                 "quantity": quantity, "trigger_price": trigger_price,
                                                 "stop_price": stop_price, "order_id": order_id,
                                                 "entry_source": source})
        return {"symbol": symbol, "status": "entered", "trading_symbol": trading_symbol, "entry_price": fill_price}
    except Exception:  # noqa: BLE001
        logger.exception("[%s] %s: unexpected error entering position", STRATEGY, symbol)
        return {"symbol": symbol, "status": "error"}
    finally:
        await live_state.intent_finish(intent, outcome_known)


# --------------------------------------------------------------------------- #
# Exits
# --------------------------------------------------------------------------- #
async def _check_real(symbol: str, position: Position) -> None:
    if position.pending_exit_order_id or engine._exit_on_cooldown(position):
        return
    if await engine._check_broker_stop_already_filled(symbol, position, position_store):
        return
    try:
        ltp = await pricing.live_price(position)   # price call, else the order book's mid
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not fetch LTP for %s", STRATEGY, position.trading_symbol)
        await engine._handle_ltp_staleness(symbol, position, position_store)
        return
    engine._ltp_failure_since.pop((symbol, position.opened_at), None)
    await _apply_price_real(symbol, position, ltp)


async def _apply_price_real(symbol: str, position: Position, ltp: float) -> None:
    await position_store.update_best_price(symbol, ltp)
    reason = _position_exit_reason(position, ltp)
    if reason and await position_store.try_start_exit(symbol):
        await engine._exit_position(symbol, position, ltp, reason, position_store)
    elif not reason:
        from . import stop_ratchet          # broker stop to the entry price once the breakeven rule is armed
        await stop_ratchet.maybe_ratchet("ce", symbol, position, ltp)


async def _close_paper(symbol: str, exit_price: float, reason: str) -> None:
    pos = paper_book.positions.get(symbol)
    record = await paper_book.close(symbol, exit_price, reason)
    if record is None:
        return
    await _event("PAPER_POSITION_CLOSED", symbol, record)
    if pos is not None and not engine._option_still_needed(pos.trading_symbol):
        try:
            await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.unsubscribe_option_price,
                                                             pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not unsubscribe %s", STRATEGY, symbol, pos.trading_symbol)


async def _apply_price_paper(symbol: str, ltp: float) -> None:
    pos = await paper_book.update(symbol, ltp)  # tracks best_price
    if pos is None:
        return
    reason = _position_exit_reason(pos, ltp)
    if reason:
        await _close_paper(symbol, ltp, reason)


async def _check_paper(square_off: bool) -> None:
    for symbol, pos in list(paper_book.positions.items()):
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            logger.warning("[%s] %s: no price for PAPER position %s this tick - will retry", STRATEGY, symbol,
                           pos.trading_symbol)
            continue
        if square_off:
            await _close_paper(symbol, ltp, "DAILY_SQUARE_OFF")
        else:
            await _apply_price_paper(symbol, ltp)


async def square_off_all(reason: str) -> None:
    """Every open REAL Super Bollinger position (the daily square-off and
    the manual kill switch). Paper positions are closed by _check_paper."""
    for symbol, position in list(position_store.live_positions.items()):
        if position.pending_exit_order_id or engine._exit_on_cooldown(position):
            continue
        try:
            ltp = await engine._get_ltp(position)
        except Exception:  # noqa: BLE001
            ltp = position.entry_price
        if await position_store.try_start_exit(symbol):
            await engine._exit_position(symbol, position, ltp, reason, position_store)


async def on_price_tick(trading_symbol: str, ltp: float) -> None:
    """WebSocket fast path - same two-speed design as Bollinger's own."""
    try:
        for symbol, position in list(position_store.live_positions.items()):
            if position.trading_symbol == trading_symbol:
                if position.pending_exit_order_id or engine._exit_on_cooldown(position):
                    return
                if await engine._check_broker_stop_already_filled(symbol, position, position_store):
                    return
                await _apply_price_real(symbol, position, ltp)
                return
        for symbol, pos in list(paper_book.positions.items()):
            if pos.trading_symbol == trading_symbol:
                await _apply_price_paper(symbol, ltp)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] on_price_tick failed for %s", STRATEGY, trading_symbol)


# --------------------------------------------------------------------------- #
# Entry gate - shared by the tick path and the 5s scan
# --------------------------------------------------------------------------- #
# Refreshed every monitor tick; read (never written) by the WS-thread tick
# listener as a cheap pre-filter. Every real decision is re-checked on the
# event loop in _can_enter_symbol.
_eligible: set[str] = set()
_gate = {"open": False}
_entry_inflight: set[str] = set()   # symbols with an entry attempt already on its way
_refresh_asked: dict[str, tuple] = {}  # symbol -> (bar start, monotonic time) of the last forced signal refresh
REFRESH_RETRY_SECONDS = 3.0
_loop: Optional[asyncio.AbstractEventLoop] = None


def _halted_today() -> bool:
    """The supervisor's disaster brake (SuperBollinger/supervisor.py)."""
    return halted["day"] == _now().date()


async def _can_enter_symbol(symbol: str) -> bool:
    return (settings.get("strategy_enabled") and not _halted_today() and _entries_open_now() and not _square_off_now()
            and symbol in _eligible
            and _slot_free_for(symbol)
            and symbol not in position_store.live_positions and symbol not in position_store.reserved_symbols
            and symbol not in paper_book.positions
            and not await position_store.is_in_entry_cooldown(symbol)
            and signals._symbol_market_open(symbol))


async def _passes_entry_filters(symbol: str, trigger_price: float, source: str) -> bool:
    """1-hour-green filter (entry_filters.py). The trigger that got here is
    already used up, so a refused signal is not retried - as in the backtest."""
    mode = settings.get("entry_filter_1h")
    if mode == "off":
        return True
    green, detail = await entry_filters.last_hour_green(symbol)
    if green is None:
        logger.warning("[%s] %s: 1-hour filter has no data (%s) - entry allowed", STRATEGY, symbol, detail)
        await _event("ENTRY_FILTER_1H_NO_DATA", symbol, {"trigger_price": trigger_price, **detail})
        return True
    if green:
        return True
    if mode == "shadow":
        await _event("ENTRY_FILTER_1H_WOULD_SKIP", symbol, {"trigger_price": trigger_price, "entry_source": source, **detail})
        return True
    logger.info("[%s] %s: BULLISH trigger %.2f (%s) skipped - last 1-hour candle is red (%s -> %s)", STRATEGY, symbol,
                trigger_price, source, detail.get("candle_open"), detail.get("candle_close"))
    await _event("ENTRY_SKIPPED_1H_RED", symbol, {"trigger_price": trigger_price, "entry_source": source, **detail})
    if settings.get("entry_filter_1h_bypass") == "shadow":
        threshold = settings.get("entry_filter_1h_bypass_day_up_pct")
        if entry_filters.bypass_would_take(detail, threshold):
            # LOG ONLY (1 Oct 2026): the momentum bypass would have taken this entry.
            logger.info("[%s] %s: 1-hour bypass WOULD ENTER (shadow, not traded) - stock up %.2f%% on the day >= %.2f%%",
                        STRATEGY, symbol, detail.get("day_up_pct"), threshold)
            await _event("ENTRY_FILTER_1H_BYPASS_WOULD_ENTER", symbol, {
                "trigger_price": trigger_price, "entry_source": source, "rule_day_up_pct": threshold,
                "paper_symbol": is_paper_symbol(symbol), **detail})
    return False


async def _enter(symbol: str, trigger_price: float, stop_price: float, source: str) -> None:
    if not await _passes_entry_filters(symbol, trigger_price, source):
        return
    if is_paper_symbol(symbol):
        result = await enter_paper(symbol, trigger_price, stop_price, source)
    else:
        result = await enter_real(symbol, trigger_price, stop_price, source)
    logger.info("[%s] %s: BULLISH trigger %.2f (%s) -> %s", STRATEGY, symbol, trigger_price, source, result)


# --------------------------------------------------------------------------- #
# Tick-driven entries (30 Sep 2026, user request: "make entries tick based")
#
# Swing.candle_feed calls _on_underlying_tick from the WS feed thread after
# every tick of a subscribed underlying has updated its forming 5-min bar.
# It only reads in-memory state (no I/O) and hands work to the event loop:
#   - first tick of a new bar: the just-closed bar now exists, so force one
#     refresh of that symbol's Bollinger signal (the new bar's pending
#     order) instead of waiting up to SIGNAL_REFRESH_SECONDS (retried after
#     REFRESH_RETRY_SECONDS if it still hasn't caught up);
#   - a tick whose bar high has reached the pending BULLISH trigger: start
#     the entry immediately (same guards, same order path as the scan).
# The pending order is consumed once (PROFILE.consumed) whichever path gets
# there first, so the scan and the tick path can never both enter.
# --------------------------------------------------------------------------- #
def install_tick_entries(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop
    candle_feed.add_tick_listener(_on_underlying_tick)


def _on_underlying_tick(symbol: str, ltp: float, tick_time: datetime) -> None:
    """WS feed thread. Must stay cheap and non-blocking."""
    if _loop is None or not _gate["open"] or symbol not in _eligible or symbol in _entry_inflight:
        return
    if (symbol in position_store.live_positions or symbol in position_store.reserved_symbols
            or symbol in paper_book.positions):
        return
    forming = candle_feed.forming_bar(symbol)
    if forming is None:
        return
    state = signals.peek_signal_state(symbol)
    expected = forming["candle_start"] - timedelta(minutes=bcfg.SIGNAL_INTERVAL_MINUTES)
    if state is None or state.candle_start != expected:
        asked = _refresh_asked.get(symbol)
        now = time.monotonic()
        if asked is None or asked[0] != forming["candle_start"] or now - asked[1] >= REFRESH_RETRY_SECONDS:
            _refresh_asked[symbol] = (forming["candle_start"], now)
            asyncio.run_coroutine_threadsafe(_refresh_signal(symbol), _loop)
        return
    if (state.pending_side != "BULLISH" or state.pending_trigger_price is None
            or forming["high"] < state.pending_trigger_price or PROFILE.consumed.get(symbol) == state.candle_start):
        return
    _entry_inflight.add(symbol)
    asyncio.run_coroutine_threadsafe(_tick_entry(symbol), _loop)


# One forced refresh at a time (30 Sep 2026): every Dhan REST call runs on the
# shared, small worker-thread pool (EXECUTOR_MAX_WORKERS=5) and history fetches
# are paced by SLEEPING inside a worker. Refreshing all watchlist stocks at the
# same instant at each bar start could tie up every worker for several seconds
# and delay entry/exit/hedge orders; queued one by one they use at most one
# worker. A stock already queued is not queued again.
_refresh_lock = asyncio.Semaphore(1)
_refresh_pending: set[str] = set()


async def _refresh_signal(symbol: str) -> None:
    if symbol in _refresh_pending:
        return
    _refresh_pending.add(symbol)
    try:
        async with _refresh_lock:
            await signals.get_signal_state(symbol, force=True)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] %s: forced signal refresh failed", STRATEGY, symbol)
    finally:
        _refresh_pending.discard(symbol)


def _peek_entry_signal(symbol: str) -> Optional[tuple]:
    """engine._evaluate_entry_signal without the fetch: cached signal state +
    the live forming bar. Consumes the pending order like the scan does."""
    state = signals.peek_signal_state(symbol)
    if state is None:
        return None
    forming = (candle_feed.forming_bar(symbol)
               if candle_feed.is_fresh(symbol, bcfg.WS_STALE_AFTER_SECONDS) else None)
    entry = signals.resting_trigger_hit(state, forming, bcfg.SIGNAL_INTERVAL_MINUTES, _now().date())
    if entry is None or PROFILE.consumed.get(symbol) == state.candle_start:
        return None
    PROFILE.consumed[symbol] = state.candle_start
    return engine._direction_allowed(entry, PROFILE)


async def _tick_entry(symbol: str) -> None:
    try:
        if not await _can_enter_symbol(symbol):
            return
        entry = _peek_entry_signal(symbol)
        if entry:
            await _enter(symbol, entry[1], entry[2], "tick")
    except Exception:  # noqa: BLE001
        logger.exception("[%s] %s: tick-driven entry failed", STRATEGY, symbol)
    finally:
        _entry_inflight.discard(symbol)


# --------------------------------------------------------------------------- #
# Monitor loop (exits' poll path, square-off, gate refresh, backup entry scan)
# --------------------------------------------------------------------------- #
async def _refresh_gate() -> None:
    global _eligible
    _eligible = set(await eligible_symbols())  # swapped whole - the WS thread reads it
    cap = capacity_control.get_max_concurrent_trades(STRATEGY)
    _gate["open"] = (settings.get("strategy_enabled") and not _halted_today() and _entries_open_now() and not _square_off_now()
                     and (open_count() < cap or paper_open_count() < cap))


async def _scan_for_entries() -> None:
    """Backup to the tick path (e.g. a trigger touched while the signal was
    still refreshing) - also keeps every symbol's signal cache warm."""
    symbols = sorted(_eligible)
    if symbols:
        start = _scan_turn["i"] % len(symbols)
        symbols = symbols[start:] + symbols[:start]
        _scan_turn["i"] = (_scan_turn["i"] + 1) % len(symbols)
    for i, symbol in enumerate(symbols):
        if symbol in _entry_inflight or not await _can_enter_symbol(symbol):
            continue
        if i and not signals.is_symbol_ws_fresh(symbol):
            await asyncio.sleep(bcfg.SYMBOL_PACING_SECONDS)
        if symbol in _entry_inflight:
            continue
        _entry_inflight.add(symbol)
        try:
            entry = await engine._evaluate_entry_signal(symbol, PROFILE)
            if entry and await _can_enter_symbol(symbol):
                await _enter(symbol, entry[1], entry[2], "scan")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not evaluate entry signal", STRATEGY, symbol)
        finally:
            _entry_inflight.discard(symbol)


async def _monitor_tick() -> None:
    square_off = _square_off_now()
    if square_off:
        await square_off_all("DAILY_SQUARE_OFF")
    await asyncio.gather(*[_check_real(s, p) for s, p in list(position_store.live_positions.items())])
    await _check_paper(square_off)
    await _refresh_gate()
    try:
        from . import shadow_list   # paper-only shadow watchlist, background task (never delays this tick)
        shadow_list.kick(square_off, bool(settings.get("strategy_enabled") and not _halted_today() and _entries_open_now()))
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not start the shadow-list pass", STRATEGY)
    if _gate["open"]:
        await _scan_for_entries()


async def monitor_loop() -> None:
    logger.info("[%s] monitor loop started.", STRATEGY)
    while True:
        try:
            await position_store.maybe_reset_for_new_day()
            await engine._sync_pending_exit_orders(position_store)
            await _monitor_tick()
        except Exception:  # noqa: BLE001
            logger.exception("[%s] error in monitor loop tick", STRATEGY)
        await asyncio.sleep(bcfg.MONITOR_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# Startup reconciliation (a mid-day restart - Super Bollinger never holds
# overnight, so the 08:00 IST refresh restart always finds nothing)
# --------------------------------------------------------------------------- #
async def reconcile_broker_positions() -> list[Position]:
    """Broker positions whose OPEN is recorded under "SuperBollinger" in our
    own trade history (never guessed - attribute_open_broker_position). The
    peak-profit memory (best_price) restarts from the entry price."""
    loop = asyncio.get_running_loop()
    positions = []
    for bp in await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions):
        if not bp.get("avg_price") or bp["quantity"] <= 0 or bp.get("option_type") != "CE":
            continue
        if await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"]) != STRATEGY:
            continue
        quantity = abs(bp["quantity"])
        leg = {"trading_symbol": bp["trading_symbol"], "product_type": bp.get("product_type") or bcfg.OPTIONS_PRODUCT,
               "quantity": quantity, "lot_size": bp.get("lot_size")}
        stop_loss_order_id = None
        try:
            stop_loss_order_id = await loop.run_in_executor(
                None, dhan_wrapper.get_pending_order_id, bp["trading_symbol"], "SELL", "NSE")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not look up a resting SELL order", STRATEGY, bp["trading_symbol"])
        positions.append(_new_position(bp["underlying_symbol"], leg, bp["avg_price"], "", stop_loss_order_id,
                                       reconciled=True))
    for pos in positions:
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, pos.trading_symbol)
    return positions
