"""
Scalper (1 Oct 2026, user request: "create another strategy ... scalping only for BankNifty and copy all
SWING rules for entry and exit with 1 min candle timeframe as we saw right now in backtest"; named
"Scalper"; "Stop at 3000 max loss for rest of the day with no new entries in this strategy").

  Universe  settings.symbols - BANKNIFTY today; any NSE index or F&O stock can be added at runtime (user: "some
            allowance to add more Index or stocks later") - see Scalper/signals.py. At most
            capacity_control "Scalper" (SCALPER_MAX_CONCURRENT_TRADES, default 1) open positions at once, real and
            paper counted separately; options cheaper than min_premium_rs are skipped.
  Entry     Swing's live v3 signal on CLOSED 1-minute candles (Scalper/signals.py): BULLISH -> buy 1 lot of the
            ATM CE, BEARISH -> the ATM PE (nearest expiry, rolled on expiry day). One position per index at a
            time. Re-entry rule as Swing: after a trade on one side, that side waits until the EMA200 regime has
            been seen on the other side. No new entries from square_off_time, nor for the rest of the day once
            today's realised loss reaches daily_loss_limit_rs (DAILY_LOSS_STOP).
  Exit      Swing's options ladder, checked every MONITOR_INTERVAL_SECONDS and on every option tick:
            MAX_LOSS_HIT (Rs max_loss_rs) -> TARGET_HIT (+target_pct) -> PROFIT_PROTECTION_HIT (peak profit >
            profit_protection_rs, then giveback_pct off the best price) -> STOP_LOSS_HIT (-hard_stop_pct) ->
            SUPERTREND_REVERSAL_TICK (the live index price crossing the last closed 1-min candle's Supertrend
            line against the trade, never on the entry candle) -> DAILY_SQUARE_OFF at square_off_time.
            REAL positions also get a broker SL-L at the max-loss level (disaster backstop).
  Mode      paper_mode_control "Scalper" (runtime, no restart). Every rule above in settings.py (runtime).

Real order placement and every exit/order-sync mechanic reuse the incident-hardened Bollinger engine
functions (exit double-sell guard, stale-order cancel, broker-quantity reconcile, manual-exit detection,
LTP-staleness forced exit) and order_safety's unfilled-order cancel, run against Scalper's OWN store.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

import capacity_control
import cross_strategy_registry
import fund_allocation
import order_safety
import paper_mode_control
from trade_history import REAL_TRADES_NAME, attribute_open_broker_position, dated_path
from Bollinger import config as bcfg
from Bollinger import trading_engine as engine
from Bollinger.position_store import OrderRecord, Position, position_store as bollinger_store
from Options.dhan_client import IST, OrderResult, OrderStatus, dhan_wrapper
from Swing.position_store import broker_stop_trigger_and_limit

from . import settings, signals
from .state import EVENTS_LOG, PAPER_TRADES_LOG, STRATEGY, memory_load, memory_save, paper_book, position_store

logger = logging.getLogger("scalper_engine")

ORDER_TAG_PREFIX = "Scal"
MONITOR_INTERVAL_SECONDS = 1.0
STOCK_DATA_END = datetime.strptime("15:15", "%H:%M").time()
NO_TRAILING = 1e12          # Position's trailing fields are unused - keeps apply_price_to_trailing from arming
PROFILE = engine.Profile(name=STRATEGY, entry_mode="bar_close", sides="both", exit_mode="hold_to_close", roll_days=0,
                         daily_square_off_time="15:25", paper_book=paper_book, events_log=EVENTS_LOG, paper_only=False)

_last_eval: dict[str, dict] = {}          # symbol -> last signal evaluation (for GET /scalper/signal)
_evaluated_bar: dict[str, int] = {}       # symbol -> bar start already evaluated
_consumed: dict[str, Optional[int]] = {}  # symbol -> side of the last entry (+1 CE / -1 PE) until the regime flips
_entry_candle: dict[str, int] = {}        # trading_symbol -> entry 1-min bar start (Supertrend-exit guard)
_day_pnl_cache: dict = {"at": 0.0, "day": None, "real": 0.0, "paper": 0.0}
_stop_logged: dict = {"day": None}


def _now() -> datetime:
    return engine._now_ist()


async def _event(event: str, symbol: str, detail: dict) -> None:
    await engine._record_bollinger_event(event, symbol, {"strategy": STRATEGY, **detail}, EVENTS_LOG)


def is_paper() -> bool:
    return paper_mode_control.is_paper_mode_enabled(STRATEGY)


# --------------------------------------------------------------------------- #
# Rules (pure)
# --------------------------------------------------------------------------- #
def exit_reason_for(entry: float, best: float, ltp: float, multiplier: float, side: int,
                    spot: Optional[float], st_line: Optional[float], st_check_allowed: bool) -> Optional[str]:
    """Swing's options exit ladder, in Swing's order. best must already include ltp. side +1 = CE, -1 = PE."""
    if (entry - ltp) * multiplier >= settings.get("max_loss_rs"):
        return "MAX_LOSS_HIT"
    if ltp >= entry * (1 + settings.get("target_pct")):
        return "TARGET_HIT"
    if (best - entry) * multiplier > settings.get("profit_protection_rs") and \
            ltp <= best * (1 - settings.get("profit_protection_giveback_pct")):
        return "PROFIT_PROTECTION_HIT"
    if ltp <= entry * (1 - settings.get("hard_stop_pct")):
        return "STOP_LOSS_HIT"
    if settings.get("supertrend_exit_enabled") and st_check_allowed and spot is not None and st_line is not None:
        if (side == 1 and spot < st_line) or (side == -1 and spot > st_line):
            return "SUPERTREND_REVERSAL_TICK"
    return None


def _side(pos: Position) -> int:
    return 1 if pos.resolved_option_type == "CE" else -1


def _position_exit_reason(pos: Position, ltp: float) -> Optional[str]:
    ev = _last_eval.get(pos.underlying_symbol) or {}
    entry_bar = _entry_candle.get(pos.trading_symbol)
    st_ok = ev.get("bar_start") is not None and (entry_bar is None or ev["bar_start"] > entry_bar)
    return exit_reason_for(pos.entry_price, max(pos.best_price, ltp), ltp, pos.pnl_multiplier, _side(pos),
                           signals.live_spot(pos.underlying_symbol), ev.get("st_line"), st_ok)


def _square_off_now() -> bool:
    now = _now()
    return now.weekday() < 5 and now >= engine._parse_hhmm_today(settings.get("square_off_time"))


def _entries_open_now() -> bool:
    now = _now()
    return (now.weekday() < 5 and engine._parse_hhmm_today("09:15") <= now
            < engine._parse_hhmm_today(settings.get("square_off_time")))


# --------------------------------------------------------------------------- #
# Daily loss stop
# --------------------------------------------------------------------------- #
def _read_day_pnl() -> tuple[float, float]:
    """Blocking. Today's realised P&L (real, paper) from the trade logs - survives restarts."""
    import json
    real = paper = 0.0
    for name, key, flt in ((REAL_TRADES_NAME, "pnl", True), (PAPER_TRADES_LOG, "pnl_raw", False)):
        p = dated_path(name)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            try:
                row = json.loads(line[line.index("{"):])
            except Exception:  # noqa: BLE001
                continue
            if flt and row.get("strategy") != STRATEGY:
                continue
            try:
                v = float(row.get(key) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if flt:
                real += v
            else:
                paper += v
    return real, paper


async def day_pnl(force: bool = False) -> dict:
    c = _day_pnl_cache
    today = _now().date()
    if force or c["day"] != today or time.monotonic() - c["at"] > 15:
        try:
            real, paper = await asyncio.get_running_loop().run_in_executor(None, _read_day_pnl)
            c.update({"at": time.monotonic(), "day": today, "real": real, "paper": paper})
        except Exception:  # noqa: BLE001
            logger.exception("[%s] could not read today's P&L", STRATEGY)
    return {"real": round(c["real"], 2), "paper": round(c["paper"], 2)}


async def daily_stop_hit(paper: bool) -> bool:
    pnl = await day_pnl()
    loss = -(pnl["paper"] if paper else pnl["real"])
    hit = loss >= settings.get("daily_loss_limit_rs")
    if hit and _stop_logged["day"] != _now().date():
        _stop_logged["day"] = _now().date()
        await _event("DAILY_LOSS_STOP", "-", {"mode": "paper" if paper else "real", "realised_today": -loss,
                                              "limit": settings.get("daily_loss_limit_rs")})
        logger.warning("[%s] daily loss stop: %s realised %.0f <= -%.0f - no new entries today", STRATEGY,
                       "paper" if paper else "real", -loss, settings.get("daily_loss_limit_rs"))
    return hit


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
async def _resolve_leg(symbol: str, signal: str) -> tuple[dict, Optional[float]]:
    leg = await engine._resolve_option_leg(symbol, signal, PROFILE)
    leg["quantity"] = leg["lot_size"] * settings.get("quantity_lots")
    leg["pnl_multiplier"] = leg["quantity"]
    try:
        price = await dhan_wrapper.get_option_ltp_async(leg["trading_symbol"])
    except Exception:  # noqa: BLE001
        price = None
    if price is not None and price < settings.get("min_premium_rs"):
        await _event("ENTRY_SKIPPED_LOW_PREMIUM", symbol, {"trading_symbol": leg["trading_symbol"], "premium": price,
                                                           "minimum": settings.get("min_premium_rs")})
        raise engine._SkipEntry({"status": "skipped", "reason": "premium_below_minimum", "premium": price})
    return leg, price


def _new_position(symbol: str, leg: dict, option_type: str, entry_price: float, order_id: str,
                  stop_loss_order_id: Optional[str] = None, reconciled: bool = False) -> Position:
    return Position(
        underlying_symbol=symbol, trading_symbol=leg["trading_symbol"], resolved_option_type=option_type,
        instrument_side="LONG", exchange_segment="NSE_FNO", product_type=leg["product_type"],
        quantity=leg["quantity"], lot_size=leg.get("lot_size"), entry_price=entry_price, best_price=entry_price,
        stop_pct=settings.get("hard_stop_pct"), hard_stop_loss=entry_price * (1 - settings.get("hard_stop_pct")),
        trailing_stop_dist=NO_TRAILING, trailing_step=NO_TRAILING, pnl_multiplier=leg["quantity"],
        order_id=order_id, reconciled=reconciled, stop_loss_order_id=stop_loss_order_id,
    )


def _remember(pos: Position) -> None:
    rows = memory_load()
    rows[pos.trading_symbol] = {"day": _now().date().isoformat(), "best_price": pos.best_price,
                                "entry_price": pos.entry_price, "entry_bar": _entry_candle.get(pos.trading_symbol),
                                "option_type": pos.resolved_option_type}
    memory_save(rows)


def _forget(trading_symbol: str) -> None:
    rows = memory_load()
    if rows.pop(trading_symbol, None) is not None:
        memory_save(rows)


async def enter_paper(symbol: str, signal: str, ev: dict) -> dict:
    if symbol in paper_book.positions:
        return {"status": "skipped", "reason": "paper_position_open"}
    if len(paper_book.positions) >= capacity_control.get_max_concurrent_trades(STRATEGY):
        return {"status": "skipped", "reason": "paper_capacity_full"}
    try:
        leg, price = await _resolve_leg(symbol, signal)
    except engine._SkipEntry as skip:
        return skip.result
    if not price:
        await _event("ENTRY_SKIPPED_NO_PRICE", symbol, {"trading_symbol": leg["trading_symbol"], "mode": "paper"})
        return {"status": "skipped", "reason": "no_price"}
    opt_type = "CE" if signal == "BULLISH" else "PE"
    pos = _new_position(symbol, leg, opt_type, price, "PAPER")
    if not await paper_book.open(pos):
        return {"status": "skipped", "reason": "paper_position_open"}
    _entry_candle[pos.trading_symbol] = ev["bar_start"]
    try:
        await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.subscribe_option_price, leg["trading_symbol"])
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not subscribe %s - REST prices will be used", STRATEGY, leg["trading_symbol"])
    await _event("PAPER_POSITION_OPENED", symbol, {"trading_symbol": leg["trading_symbol"], "signal": signal,
                                                   "entry_price": price, "quantity": leg["quantity"], **_ev_brief(ev)})
    return {"status": "paper_entered", "trading_symbol": leg["trading_symbol"], "entry_price": price}


async def enter_real(symbol: str, signal: str, ev: dict) -> dict:
    """REAL-money entry. Claims the index (and its same-contract key) in the shared cross-strategy registry
    for the whole attempt and refuses it if Bollinger or Swing holds a real position in this index."""
    if not await cross_strategy_registry.try_claim(symbol, STRATEGY):
        return {"status": "skipped", "reason": "entry_in_progress_by_other_strategy"}
    key = cross_strategy_registry.same_contract_key(symbol)
    try:
        if not await cross_strategy_registry.try_claim(key, STRATEGY):
            return {"status": "skipped", "reason": "entry_in_progress_by_other_strategy"}
        from SuperBollinger.state import position_store as super_bollinger_store
        if (symbol in bollinger_store.live_positions or symbol in bollinger_store.reserved_symbols
                or symbol in super_bollinger_store.live_positions or symbol in super_bollinger_store.reserved_symbols
                or engine.swing_real_holds(symbol)):
            await _event("ENTRY_SKIPPED_HELD_BY_OTHER_STRATEGY", symbol, {})
            return {"status": "skipped", "reason": "held_by_other_strategy"}
        if not await position_store.reserve_symbol(symbol):
            return {"status": "skipped", "reason": "already_open_or_reserved"}
        try:
            return await _enter_real_reserved(symbol, signal, ev)
        finally:
            if symbol not in position_store.live_positions:
                await position_store.record_failed_entry(symbol)
                await position_store.release_symbol(symbol)
    finally:
        await cross_strategy_registry.release_claim(key, STRATEGY)
        await cross_strategy_registry.release_claim(symbol, STRATEGY)


async def _enter_real_reserved(symbol: str, signal: str, ev: dict) -> dict:
    loop = asyncio.get_running_loop()
    try:
        try:
            leg, price = await _resolve_leg(symbol, signal)
        except engine._SkipEntry as skip:
            return skip.result
        ts, qty, product = leg["trading_symbol"], leg["quantity"], leg["product_type"]
        opt_type = "CE" if signal == "BULLISH" else "PE"
        if settings.get("funds_check_enabled") and price:
            try:
                ok = await fund_allocation.has_sufficient_bucket_funds(
                    bcfg.FUND_BUCKET, symbol, [(leg["security_id"], product, qty, price, "NSE_FNO")],
                    buffer_rs=bcfg.FUNDS_CHECK_BUFFER_RS)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] %s: funds check failed - proceeding", STRATEGY, symbol)
                ok = True
            if not ok:
                await _event("ENTRY_SKIPPED_INSUFFICIENT_FUNDS", symbol, {"trading_symbol": ts, "premium": price})
                return {"status": "skipped", "reason": "insufficient_funds"}
        try:
            existing = await loop.run_in_executor(None, dhan_wrapper.get_pending_order_id, ts, "BUY", "NSE")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not check for a resting BUY - proceeding", STRATEGY, symbol)
            existing = None
        if existing:
            logger.warning("[%s] %s: BUY %s already pending for %s - no duplicate", STRATEGY, symbol, existing, ts)
            return {"status": "already_pending", "order_id": existing}

        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, ts)
        order_resp = await loop.run_in_executor(None, dhan_wrapper.place_market_order, ts, qty, "BUY",
                                                engine._gen_tag(ORDER_TAG_PREFIX, symbol), product)
        order_id, is_amo = order_resp["order_id"], order_resp["is_amo"]
        await position_store.record_order(OrderRecord(order_id=order_id, underlying_symbol=symbol, trading_symbol=ts,
                                                      transaction_type="BUY", quantity=qty, status=OrderStatus.TRANSIT,
                                                      is_amo=is_amo, lot_size=leg["lot_size"]))
        try:
            result = await asyncio.wait_for(dhan_wrapper.wait_for_order_result_async(order_id, is_amo),
                                            timeout=engine._ORDER_RESULT_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not confirm entry order %s", STRATEGY, symbol, order_id)
            result = OrderResult(order_id=order_id, status=OrderStatus.TRANSIT, remark="order_confirmation_timeout",
                                 fill_price=0.0, filled_quantity=0, is_amo=is_amo)
        await position_store.update_order_status(order_id, result.status, result.remark)
        if result.status in OrderStatus.OPEN_STATUSES:
            # Never leave an unfilled order resting at the broker (SONACOMS, 30 Sep): cancel, re-read.
            final, cancel_error = await order_safety.cancel_unfilled(order_id, result, is_amo)
            await _event(order_safety.outcome_event(final), symbol, {
                "what": "entry", "trading_symbol": ts, "order_id": order_id, "status_at_timeout": result.status,
                "final_status": final.status, "filled_quantity": final.filled_quantity, "cancel_error": cancel_error})
            result = final
            await position_store.update_order_status(order_id, result.status, result.remark)
        if result.status != OrderStatus.TRADED:
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, ts)
            logger.warning("[%s] %s: entry order %s not TRADED (status=%s remark=%s)", STRATEGY, symbol, order_id,
                           result.status, result.remark)
            await _event("ENTRY_FAILED", symbol, {"trading_symbol": ts, "order_id": order_id, "status": result.status,
                                                  "remark": result.remark})
            return {"status": "failed", "order_status": result.status}
        fill = result.fill_price or price or await dhan_wrapper.get_option_ltp_async(ts)

        stop_id = None
        if settings.get("broker_stop_enabled"):
            trigger, limit = broker_stop_trigger_and_limit("LONG", fill, qty, settings.get("max_loss_rs"),
                                                           bcfg.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE, hard_stop_pct=0.95)
            try:
                stop = await loop.run_in_executor(None, dhan_wrapper.place_stop_loss_limit_order, ts, qty, "SELL",
                                                  trigger, limit, engine._gen_tag("SL", symbol), product)
                stop_id = stop["order_id"]
                await position_store.record_order(OrderRecord(order_id=stop_id, underlying_symbol=symbol,
                                                              trading_symbol=ts, transaction_type="SELL", quantity=qty,
                                                              status="PENDING", is_amo=False))
                logger.info("[%s] %s: broker SL-L %s for %s trigger=%.2f limit=%.2f", STRATEGY, symbol, stop_id, ts,
                            trigger, limit)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] %s: could not place the broker stop for %s - the bot's own max-loss check "
                                 "still protects it", STRATEGY, symbol, ts)
                await _event("BROKER_STOP_FAILED", symbol, {"trading_symbol": ts})
        pos = _new_position(symbol, leg, opt_type, fill, order_id, stop_id)
        await position_store.add_position(pos)
        _entry_candle[ts] = ev["bar_start"]
        _remember(pos)
        await _event("POSITION_OPENED", symbol, {"trading_symbol": ts, "signal": signal, "entry_price": fill,
                                                 "quantity": qty, "order_id": order_id, "stop_loss_order_id": stop_id,
                                                 **_ev_brief(ev)})
        return {"status": "entered", "trading_symbol": ts, "entry_price": fill}
    except Exception:  # noqa: BLE001
        logger.exception("[%s] %s: unexpected error entering", STRATEGY, symbol)
        return {"status": "error"}


def _ev_brief(ev: dict) -> dict:
    return {k: ev.get(k) for k in ("bar_start", "close", "st_line", "regime_bullish", "st15_above", "trend_bull",
                                   "trend_bear", "day_range_bull", "day_range_bear", "rsi", "ema_gap")}


# --------------------------------------------------------------------------- #
# Exits
# --------------------------------------------------------------------------- #
async def _apply_price_real(symbol: str, pos: Position, ltp: float) -> None:
    before = pos.best_price
    await position_store.update_best_price(symbol, ltp)
    if pos.best_price != before:
        _remember(pos)
    reason = _position_exit_reason(pos, ltp)
    if reason and await position_store.try_start_exit(symbol):
        logger.info("[%s] %s: exit %s (%s) ltp=%.2f entry=%.2f best=%.2f", STRATEGY, symbol, reason,
                    pos.trading_symbol, ltp, pos.entry_price, pos.best_price)
        await engine._exit_position(symbol, pos, ltp, reason, position_store)
        if symbol not in position_store.live_positions:
            _forget(pos.trading_symbol)
            await day_pnl(force=True)


async def _check_real(symbol: str, pos: Position) -> None:
    if pos.pending_exit_order_id or engine._exit_on_cooldown(pos):
        return
    if await engine._check_broker_stop_already_filled(symbol, pos, position_store):
        _forget(pos.trading_symbol)
        await day_pnl(force=True)
        return
    try:
        ltp = await engine._get_ltp(pos)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not fetch LTP for %s", STRATEGY, pos.trading_symbol)
        await engine._handle_ltp_staleness(symbol, pos, position_store)
        return
    engine._ltp_failure_since.pop((symbol, pos.opened_at), None)
    await _apply_price_real(symbol, pos, ltp)


async def _close_paper(symbol: str, price: float, reason: str) -> None:
    pos = paper_book.positions.get(symbol)
    record = await paper_book.close(symbol, price, reason)
    if record is None:
        return
    await _event("PAPER_POSITION_CLOSED", symbol, record)
    await day_pnl(force=True)
    if pos is not None:
        _entry_candle.pop(pos.trading_symbol, None)
        if not engine._option_still_needed(pos.trading_symbol):
            try:
                await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.unsubscribe_option_price,
                                                                 pos.trading_symbol)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] could not unsubscribe %s", STRATEGY, pos.trading_symbol)


async def _apply_price_paper(symbol: str, ltp: float) -> None:
    pos = await paper_book.update(symbol, ltp)
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
            logger.warning("[%s] %s: no price for PAPER %s this tick", STRATEGY, symbol, pos.trading_symbol)
            continue
        if square_off:
            await _close_paper(symbol, ltp, "DAILY_SQUARE_OFF")
        else:
            await _apply_price_paper(symbol, ltp)


async def square_off_all(reason: str) -> None:
    for symbol, pos in list(position_store.live_positions.items()):
        if pos.pending_exit_order_id or engine._exit_on_cooldown(pos):
            continue
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            ltp = pos.entry_price
        if await position_store.try_start_exit(symbol):
            await engine._exit_position(symbol, pos, ltp, reason, position_store)
            if symbol not in position_store.live_positions:
                _forget(pos.trading_symbol)
    await day_pnl(force=True)


async def square_off_paper(reason: str) -> None:
    for symbol, pos in list(paper_book.positions.items()):
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            ltp = pos.entry_price
        await _close_paper(symbol, ltp, reason)


async def on_price_tick(trading_symbol: str, ltp: float) -> None:
    """Option-price WebSocket fast path."""
    try:
        for symbol, pos in list(position_store.live_positions.items()):
            if pos.trading_symbol == trading_symbol:
                if pos.pending_exit_order_id or engine._exit_on_cooldown(pos):
                    return
                if await engine._check_broker_stop_already_filled(symbol, pos, position_store):
                    return
                await _apply_price_real(symbol, pos, ltp)
                return
        for symbol, pos in list(paper_book.positions.items()):
            if pos.trading_symbol == trading_symbol:
                await _apply_price_paper(symbol, ltp)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] on_price_tick failed for %s", STRATEGY, trading_symbol)


# --------------------------------------------------------------------------- #
# Signal + entries, once per closed 1-minute candle
# --------------------------------------------------------------------------- #
async def _evaluate_new_bar(symbol: str) -> None:
    now_ts = time.time()
    minute_start = int(now_ts) - int(now_ts) % 60
    closed_start = minute_start - 60
    if _evaluated_bar.get(symbol) == closed_start:
        return
    if signals._refs.get(symbol, ("", ""))[1] == "NSE_EQ" and _now().time() >= STOCK_DATA_END:
        return                      # Dhan's stock minute data ends at 15:14 - nothing more to evaluate today
    if not await signals.fill_gap(symbol, closed_start):
        if now_ts - minute_start < 15:      # wait a little for the bar (next tick / REST)
            return
    fast, slow = signals.series(symbol, now_ts)
    _evaluated_bar[symbol] = closed_start
    if not fast["ts"] or fast["ts"][-1] != closed_start:
        _last_eval[symbol] = {"bar_start": None, "note": "no candle for the last minute"}
        return
    ev = await asyncio.get_running_loop().run_in_executor(None, signals.evaluate, fast, slow)
    if ev is None:
        _last_eval[symbol] = {"bar_start": closed_start, "note": "not enough history"}
        return
    ev["evaluated_at"] = datetime.now(IST).isoformat()
    _last_eval[symbol] = ev
    reg_side = 1 if ev["regime_bullish"] else -1
    if _consumed.get(symbol) is not None and reg_side != _consumed[symbol]:
        _consumed[symbol] = None                         # the regime left the traded side - fresh formation
    side = 1 if ev["bull"] else -1 if ev["bear"] else 0
    if not side:
        return
    signal = "BULLISH" if side == 1 else "BEARISH"
    paper = is_paper()
    if not settings.get("strategy_enabled") or not _entries_open_now() or _square_off_now():
        return
    if symbol in position_store.live_positions or symbol in position_store.reserved_symbols \
            or symbol in paper_book.positions:
        return
    if _consumed.get(symbol) == side:
        await _event("ENTRY_SKIPPED_SAME_FORMATION", symbol, {"signal": signal, **_ev_brief(ev)})
        return
    if await daily_stop_hit(paper):
        await _event("ENTRY_SKIPPED_DAILY_LOSS_STOP", symbol, {"signal": signal, **(await day_pnl())})
        return
    if not paper and await position_store.is_in_entry_cooldown(symbol):
        return
    result = await (enter_paper(symbol, signal, ev) if paper else enter_real(symbol, signal, ev))
    if result.get("status") in ("entered", "paper_entered"):
        _consumed[symbol] = side
    logger.info("[%s] %s: %s signal on the %s candle -> %s", STRATEGY, symbol, signal,
                datetime.fromtimestamp(closed_start, IST).strftime("%H:%M"), result)


_setup_tasks: dict[str, asyncio.Task] = {}


def _ensure_symbol(symbol: str) -> bool:
    """True when the symbol's candles are ready; otherwise starts (once at a time) its setup in the background -
    a symbol added at runtime starts trading on its own, no restart."""
    if signals.book(symbol).ready:
        return True
    task = _setup_tasks.get(symbol)
    if task is None or task.done():
        _setup_tasks[symbol] = asyncio.create_task(signals.ensure_ready(symbol))
    return False


async def _monitor_tick() -> None:
    square_off = _square_off_now()
    if square_off:
        await square_off_all("DAILY_SQUARE_OFF")
    await asyncio.gather(*[_check_real(s, p) for s, p in list(position_store.live_positions.items())])
    await _check_paper(square_off)
    for symbol in settings.get("symbols"):
        if _ensure_symbol(symbol):
            await _evaluate_new_bar(symbol)


async def _refresh_loop() -> None:
    while True:
        for symbol in settings.get("symbols"):
            if not signals.book(symbol).ready:
                continue
            try:
                await signals.refresh(symbol)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] %s: history refresh failed", STRATEGY, symbol)
        await asyncio.sleep(20)


async def monitor_loop() -> None:
    logger.info("[%s] monitor loop started (mode=%s, symbols=%s)", STRATEGY, "PAPER" if is_paper() else "REAL",
                settings.get("symbols"))
    for symbol in settings.get("symbols"):
        try:
            await signals.ensure_ready(symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: candle setup failed - retried by the monitor loop", STRATEGY, symbol)
    asyncio.create_task(_refresh_loop())
    while True:
        try:
            await position_store.maybe_reset_for_new_day()
            await engine._sync_pending_exit_orders(position_store)
            await _monitor_tick()
        except Exception:  # noqa: BLE001
            logger.exception("[%s] error in monitor loop tick", STRATEGY)
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# Startup reconciliation
# --------------------------------------------------------------------------- #
async def reconcile_broker_positions() -> list[Position]:
    """Broker positions whose OPEN is recorded under "Scalper" in our own trade history (never guessed),
    with their best price and entry candle from data/scalper_position_memory.json."""
    loop = asyncio.get_running_loop()
    mem = memory_load()
    out = []
    for bp in await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions):
        if not bp.get("avg_price") or bp["quantity"] <= 0 or bp.get("option_type") not in ("CE", "PE"):
            continue
        if await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"]) != STRATEGY:
            continue
        qty = abs(bp["quantity"])
        leg = {"trading_symbol": bp["trading_symbol"], "product_type": bp.get("product_type") or bcfg.OPTIONS_PRODUCT,
               "quantity": qty, "lot_size": bp.get("lot_size")}
        stop_id = None
        try:
            stop_id = await loop.run_in_executor(None, dhan_wrapper.get_pending_order_id, bp["trading_symbol"], "SELL",
                                                 "NSE")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not look up a resting SELL", STRATEGY, bp["trading_symbol"])
        pos = _new_position(bp["underlying_symbol"], leg, bp["option_type"], bp["avg_price"], "", stop_id,
                            reconciled=True)
        row = mem.get(bp["trading_symbol"])
        if row and row.get("day") == _now().date().isoformat():
            pos.best_price = max(pos.best_price, float(row.get("best_price") or 0))
            if row.get("entry_bar"):
                _entry_candle[pos.trading_symbol] = int(row["entry_bar"])
        out.append(pos)
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, pos.trading_symbol)
    return out
