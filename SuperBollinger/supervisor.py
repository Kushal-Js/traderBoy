"""
Super Bollinger SUPERVISOR (30 Sep 2026, user request: "build this
supervisor ... deploy ... work from today ... hedging with the rules we have
been discussing ... log everything").

Watches every open Super Bollinger CE trade and:
  1. HEDGE (settings.hedge_mode: off | shadow | paper | real) - the best
     rule from the 60-combination backtest grid (research_super_bollinger_
     hedge_indicators.py, "T2000 ATR_DROP TRAIL": +Rs 10,831 on 35 hedges,
     positive in both halves, not yet validated out-of-sample):
       when the CE's open loss >= hedge_trigger_rs AND the stock is
       >= hedge_atr_mult x ATR(14, 5-min, last completed bar) below the CE's
       entry spot -> buy 1 lot of the same stock's ATM PE. The CE is left
       untouched (its own rules still apply). Exit the PE once its profit
       has reached hedge_trail_arm_rs and then given back
       hedge_trail_giveback of its best profit, or at a hedge_stop_rs loss,
       or at the square-off time. One hedge per CE trade; no new hedge from
       hedge_cutoff_time. A PAPER CE is only ever hedged on paper.
  2. DISASTER BRAKE - if the day's realized + open REAL PnL (CE + hedges)
     reaches -disaster_brake_rs: no more entries or hedges today and every
     real position is squared off. A malfunction guard, not a performance
     rule.
  3. SHADOW RULES - logged only, never acted on: stop-and-re-enter at
     shadow_stop_reenter_rs (would exit the CE, would re-buy at its entry
     price).
  4. LOGS everything to history/<date>_super_bollinger_supervisor.log
     (JSONL): every trigger seen, confirmation inputs (loss, ATR, stock
     drop), hedge decisions, orders/fills and slippage vs the decision
     price, exits and PnL, brake checks, shadow-rule events.

Real hedge orders reuse the incident-hardened Bollinger order/exit machinery
(Bollinger.trading_engine: _exit_position, broker SL check, LTP staleness,
pending-order sync) against the hedge's OWN position store (state.
hedge_store, trade-history tag "SuperBollingerHedge").
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cross_strategy_registry
import fund_allocation
from trade_history import attribute_open_broker_position
from Bollinger import config as bcfg, signals
from Bollinger import trading_engine as engine
from Bollinger.position_store import OrderRecord, Position
from Options.dhan_client import OrderResult, OrderStatus, dhan_wrapper
from Swing import candle_feed
from Swing.position_store import broker_stop_trigger_and_limit
from SuperTrader.strategy import atr as atr_series

from . import best_price_memory, live_state, pricing, scale, settings, stop_ratchet
from .state import (EVENTS_LOG, HEDGE_STRATEGY, STRATEGY, SUPERVISOR_LOG, halted, hedge_paper_book, hedge_store,
                    paper_book, position_store)
from .trading_engine import (NO_TRAILING, PROFILE, retry_unfilled_buy, settle_unfilled_order,
                             square_off_all as square_off_ce)

logger = logging.getLogger("super_bollinger_supervisor")
LOOP_SECONDS = 2
MARK_CHECK_FROM = 0.6      # from 60% of the hedge trigger on, also judge the CE's loss by the bid/ask mid
_track: dict = {}          # (symbol, CE opened_at) -> per-CE-trade supervisor state
_hedge_inflight: set = set()


def _now() -> datetime:
    return engine._now_ist()


async def _log(event: str, symbol: str, **detail) -> None:
    await engine._record_bollinger_event(event, symbol, {"component": "supervisor", **detail}, SUPERVISOR_LOG)


def halted_today() -> bool:
    return halted["day"] == _now().date()


def _spot(symbol: str) -> Optional[float]:
    forming = candle_feed.forming_bar(symbol) if candle_feed.is_fresh(symbol, bcfg.WS_STALE_AFTER_SECONDS) else None
    if forming and forming.get("last"):
        return float(forming["last"])
    base = signals._rest_series_cache.get(symbol)
    return float(base["close"][-1]) if base and base.get("close") else None


def _atr(symbol: str) -> Optional[float]:
    base = signals._rest_series_cache.get(symbol)
    if not base or len(base.get("close") or []) < 30:
        return None
    return atr_series(base["high"], base["low"], base["close"], 14)[-1]


# --------------------------------------------------------------------------- #
# Data for HELD symbols (30 Sep 2026 incident: GLENMARK's hedge fired 7 minutes
# late). The entry scan is what normally subscribes a stock's candle feed and
# loads its 5-min series - and it skips stocks that are already held (and stops
# at the entry cutoff). After a restart a held stock that only Super Bollinger
# trades therefore had no spot and no ATR, and the hedge waited forever. The
# supervisor now keeps both alive itself for everything it holds.
# --------------------------------------------------------------------------- #
SERIES_RECHECK_SECONDS = 15
NO_DATA_ALARM_SECONDS = 30
_series_checked: dict[str, float] = {}
_series_running = {"on": False}
_no_data_since: dict[str, float] = {}
_no_data_logged: dict[str, float] = {}


async def ensure_series(symbol: str, force: bool = False) -> bool:
    """Subscribe the underlying's candle feed and make sure its 5-min series
    is loaded and current (one REST call per symbol per bar at most - the
    signal module only fetches when a newer closed bar should exist)."""
    now = time.monotonic()
    if not force and now - _series_checked.get(symbol, 0.0) < SERIES_RECHECK_SECONDS:
        return bool(signals._rest_series_cache.get(symbol))
    _series_checked[symbol] = now

    def work():
        sid, seg, inst = signals._underlying_reference(symbol)
        return signals._get_intraday_series(symbol, sid, seg, inst)

    try:
        data = await asyncio.get_running_loop().run_in_executor(None, work)
        return bool(data and data.get("close"))
    except Exception:  # noqa: BLE001
        logger.exception("[supervisor] %s: could not load the candle series for a held position", symbol)
        return False


async def _ensure_series_for(symbols: list[str]) -> None:
    try:
        for sym in symbols:
            await ensure_series(sym)
    finally:
        _series_running["on"] = False


def _keep_held_data_fresh() -> None:
    """Fire-and-forget (never delays the hedge/exit checks of this tick)."""
    held = sorted(set(position_store.live_positions) | set(paper_book.positions) | set(hedge_store.live_positions)
                  | set(hedge_paper_book.positions))
    if held and not _series_running["on"]:
        _series_running["on"] = True
        asyncio.create_task(_ensure_series_for(held))


async def _check_held_data(symbol: str, real: bool) -> None:
    """Loud event when a held CE has had no spot or no ATR for a while - the
    hedge cannot be evaluated without them."""
    spot, atr_v = _spot(symbol), _atr(symbol)
    now = time.monotonic()
    if spot is not None and atr_v is not None:
        _no_data_since.pop(symbol, None)
        return
    since = _no_data_since.setdefault(symbol, now)
    if now - since >= NO_DATA_ALARM_SECONDS and now - _no_data_logged.get(symbol, 0.0) >= 60:
        _no_data_logged[symbol] = now
        logger.error("[supervisor] %s: held position has no %s for %.0fs - the hedge cannot be evaluated", symbol,
                     "spot price" if spot is None else "ATR", now - since)
        await _log("HELD_POSITION_NO_DATA", symbol, spot=spot, atr=atr_v, seconds=round(now - since), real_ce=real)
        _series_checked.pop(symbol, None)     # retry the load right away


def _logged_entry_spot(symbol: str) -> Optional[float]:
    """Entry spot for a CE re-adopted after a restart (the in-memory _track
    is gone): the trigger price of today's last POSITION_OPENED event for
    the symbol - Super Bollinger enters when the stock touches its trigger.
    Without this the ATR-drop check would measure from the spot at restart."""
    path = Path("history") / f"{engine._now_ist().date().isoformat()}_{EVENTS_LOG}.log"
    found = None
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("event") == "POSITION_OPENED" and d.get("underlying_symbol") == symbol:
                    found = d.get("trigger_price")
    except OSError:
        return None
    try:
        return float(found) if found else None
    except (TypeError, ValueError):
        return None


def _state(symbol: str, pos: Position) -> dict:
    key = (symbol, pos.opened_at.isoformat())
    st = _track.get(key)
    if st is None:
        entry_spot = (_logged_entry_spot(symbol) if pos.reconciled else None) or _spot(symbol)
        st = {"entry_spot": entry_spot, "hedged": False, "waiting_logged": False,
              "shadow_stopped": False, "shadow_reentered": False}
        _track[key] = st
    elif st["entry_spot"] is None:
        st["entry_spot"] = _spot(symbol)
    return st


# --------------------------------------------------------------------------- #
# CE watch: shadow rules + hedge trigger
# --------------------------------------------------------------------------- #
async def _scale_safe(coro) -> None:
    """The scale-in variant is paper-only research: a failure there must
    never stop the supervisor's real work."""
    try:
        await coro
    except Exception:  # noqa: BLE001
        logger.exception("[supervisor] scale-in variant hook failed")


async def check_ce(symbol: str, pos: Position, ltp: float, ce_is_real: bool) -> None:
    st = _state(symbol, pos)
    await _scale_safe(scale.on_ce_price(symbol, pos, ltp, ce_is_real, st, _spot(symbol)))
    loss = (pos.entry_price - ltp) * pos.pnl_multiplier
    now = _now()

    shadow_rs = settings.get("shadow_stop_reenter_rs")
    if shadow_rs > 0:
        if not st["shadow_stopped"] and loss >= shadow_rs:
            st["shadow_stopped"] = True
            await _log("SHADOW_STOP_REENTER_WOULD_EXIT", symbol, ce=pos.trading_symbol, ce_entry=pos.entry_price,
                       ce_ltp=ltp, loss=round(loss), real_ce=ce_is_real)
        elif st["shadow_stopped"] and not st["shadow_reentered"] and ltp >= pos.entry_price and now.time().strftime("%H:%M") < "15:00":
            st["shadow_reentered"] = True
            await _log("SHADOW_STOP_REENTER_WOULD_REBUY", symbol, ce=pos.trading_symbol, ce_ltp=ltp, real_ce=ce_is_real)

    mode = settings.get("hedge_mode")
    trigger = settings.get("hedge_trigger_rs")
    if (mode == "off" or st["hedged"] or symbol in _hedge_inflight or halted_today()
            or now.strftime("%H:%M") >= settings.get("hedge_cutoff_time")):
        return
    if ce_is_real and trigger * MARK_CHECK_FROM <= loss < trigger:
        # Close to the trigger on the last TRADED price: a thin option's last trade can be minutes old
        # (APLAPOLLO 2240 CE, 30 Sep: seen at the trigger 15:03:56, the stock's low was 14:55). Judge the
        # loss by the live bid/ask mid as well.
        mark, source = await pricing.mark_for_loss(pos, ltp)
        if source == "mid":
            ltp, loss = mark, (pos.entry_price - mark) * pos.pnl_multiplier
            if loss >= trigger and not st.get("mark_logged"):
                st["mark_logged"] = True
                await _log("HEDGE_TRIGGER_SEEN_ON_MID", symbol, ce=pos.trading_symbol, ce_mid=round(mark, 2),
                           loss=round(loss))
    if loss < trigger:
        return
    spot, atr_v, mult = _spot(symbol), _atr(symbol), settings.get("hedge_atr_mult")
    drop = (st["entry_spot"] - spot) if (spot is not None and st["entry_spot"] is not None) else None
    confirmed = mult == 0 or (atr_v is not None and drop is not None and drop >= mult * atr_v)
    if not confirmed:
        if not st["waiting_logged"]:
            st["waiting_logged"] = True
            await _log("HEDGE_TRIGGER_WAITING_FOR_ATR", symbol, ce=pos.trading_symbol, loss=round(loss),
                       entry_spot=st["entry_spot"], spot=spot, drop=drop, atr=atr_v, needed=None if atr_v is None else mult * atr_v)
        return
    st["hedged"] = True
    _hedge_inflight.add(symbol)
    try:
        effective = "paper" if (mode == "real" and not ce_is_real) else mode
        await _open_hedge(symbol, pos, ltp, loss, spot, atr_v, drop, effective)
    except Exception:  # noqa: BLE001
        logger.exception("[supervisor] %s: hedge open failed", symbol)
        await _log("HEDGE_ERROR", symbol, stage="open")
    finally:
        _hedge_inflight.discard(symbol)


async def _open_hedge(symbol, ce: Position, ce_ltp, loss, spot, atr_v, drop, mode) -> None:
    decision = {"ce": ce.trading_symbol, "ce_entry": ce.entry_price, "ce_ltp": ce_ltp, "ce_loss": round(loss),
                "spot": spot, "atr": atr_v, "drop": drop, "mode": mode}
    if mode == "shadow":
        await _log("HEDGE_DECISION_SHADOW", symbol, **decision)
        return
    try:
        leg = await engine._resolve_option_leg(symbol, "BEARISH", PROFILE)
    except engine._SkipEntry as skip:
        await _log("HEDGE_SKIPPED", symbol, reason=skip.result.get("reason") or skip.result.get("status"), **decision)
        return
    qty = leg["lot_size"] * settings.get("quantity_lots")
    try:
        price = await dhan_wrapper.get_option_ltp_async(leg["trading_symbol"])
    except Exception:  # noqa: BLE001
        price = None
    if price is None or price < settings.get("min_premium_rs"):
        await _log("HEDGE_SKIPPED", symbol, reason="premium_below_minimum_or_unknown", pe=leg["trading_symbol"],
                   premium=price, **decision)
        return
    loop = asyncio.get_running_loop()

    if mode == "paper":
        pos = _hedge_position(symbol, leg, qty, price, "PAPER")
        if await hedge_paper_book.open(pos):
            try:
                await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, leg["trading_symbol"])
            except Exception:  # noqa: BLE001
                pass
            await _log("HEDGE_OPENED", symbol, pe=leg["trading_symbol"], qty=qty, pe_entry=price, **decision)
        return

    # ---- real ----
    if not await cross_strategy_registry.try_claim(symbol, HEDGE_STRATEGY):
        await _log("HEDGE_SKIPPED", symbol, reason="entry_in_progress_by_other_strategy", **decision)
        return
    intent, outcome_known = None, False
    try:
        if symbol in hedge_store.live_positions:
            return
        if settings.get("funds_check_enabled"):
            try:
                ok = await fund_allocation.has_sufficient_bucket_funds(
                    bcfg.FUND_BUCKET, symbol, [(leg["security_id"], leg["product_type"], qty, price, "NSE_FNO")],
                    buffer_rs=bcfg.FUNDS_CHECK_BUFFER_RS)
            except Exception:  # noqa: BLE001
                logger.exception("[supervisor] %s: funds check failed - proceeding", symbol)
                ok = True
            if not ok:
                await _log("HEDGE_SKIPPED", symbol, reason="insufficient_funds", pe=leg["trading_symbol"], **decision)
                return
        existing = await loop.run_in_executor(None, dhan_wrapper.get_pending_order_id, leg["trading_symbol"], "BUY", "NSE")
        if existing:
            await _log("HEDGE_SKIPPED", symbol, reason="buy_order_already_pending", order_id=existing, **decision)
            return
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, leg["trading_symbol"])
        intent = live_state.intent_begin("hedge", symbol, leg, qty, ce=ce.trading_symbol)   # on file BEFORE the order
        resp = await loop.run_in_executor(None, dhan_wrapper.place_market_order, leg["trading_symbol"], qty, "BUY",
                                          engine._gen_tag("SBH", symbol), leg["product_type"])
        order_id, is_amo = resp["order_id"], resp["is_amo"]
        live_state.intent_order(intent, order_id)
        await hedge_store.record_order(OrderRecord(order_id=order_id, underlying_symbol=symbol,
                                                   trading_symbol=leg["trading_symbol"], transaction_type="BUY",
                                                   quantity=qty, status=OrderStatus.TRANSIT, is_amo=is_amo,
                                                   lot_size=leg["lot_size"]))
        try:
            result = await asyncio.wait_for(loop.run_in_executor(None, dhan_wrapper.wait_for_order_result, order_id, is_amo),
                                            timeout=engine._ORDER_RESULT_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            result = OrderResult(order_id=order_id, status=OrderStatus.TRANSIT, remark="order_confirmation_timeout",
                                 fill_price=0.0, filled_quantity=0, is_amo=is_amo)
        await hedge_store.update_order_status(order_id, result.status, result.remark)
        if result.status != OrderStatus.TRADED:   # never leave an unfilled hedge order resting at the broker
            result = await settle_unfilled_order(symbol, leg["trading_symbol"], order_id, result, is_amo, "hedge")
            await hedge_store.update_order_status(order_id, result.status, result.remark)
        if result.status == OrderStatus.CANCELLED and not is_amo:
            # Confirmed unfilled: re-price at the live ask while the CE is still open and still past the trigger.
            await live_state.intent_finish(intent, True)
            intent = None

            async def still_valid() -> Optional[str]:
                live_ce = position_store.live_positions.get(symbol)
                if live_ce is None or live_ce.pending_exit_order_id:
                    return "ce_closed"
                if halted_today():
                    return "halted"
                if _now().strftime("%H:%M") >= settings.get("hedge_cutoff_time"):
                    return "hedge_window_closed"
                ce_now = await _ltp(live_ce)
                if ce_now is not None and (live_ce.entry_price - ce_now) * live_ce.pnl_multiplier < settings.get("hedge_trigger_rs"):
                    return "ce_recovered"
                return None

            leg_q = {**leg, "quantity": qty}
            retried, retry_order_id, retry_intent, why = await retry_unfilled_buy(
                symbol, leg_q, qty, price, still_valid, "hedge", hedge_store, "SBH")
            if retried is not None:
                result, order_id, intent = retried, retry_order_id, retry_intent
        if result.status != OrderStatus.TRADED:
            outcome_known = result.status not in OrderStatus.OPEN_STATUSES
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, leg["trading_symbol"])
            await _log("HEDGE_ORDER_NOT_FILLED", symbol, pe=leg["trading_symbol"], order_id=order_id,
                       status=result.status, remark=result.remark, **decision)
            return
        fill = result.fill_price or price
        sl_id = None
        if bcfg.BROKER_STOP_LOSS_ENABLED:
            trig, limit = broker_stop_trigger_and_limit("LONG", fill, qty, settings.get("hedge_stop_rs"),
                                                        bcfg.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE, hard_stop_pct=0.95)
            try:
                sl = await loop.run_in_executor(None, dhan_wrapper.place_stop_loss_limit_order, leg["trading_symbol"],
                                                qty, "SELL", trig, limit, engine._gen_tag("SL", symbol), leg["product_type"])
                sl_id = sl["order_id"]
                await hedge_store.record_order(OrderRecord(order_id=sl_id, underlying_symbol=symbol,
                                                           trading_symbol=leg["trading_symbol"], transaction_type="SELL",
                                                           quantity=qty, status="PENDING", is_amo=False))
            except Exception:  # noqa: BLE001
                logger.exception("[supervisor] %s: could not place the hedge's broker SL - bot stop still applies", symbol)
        await hedge_store.add_position(_hedge_position(symbol, leg, qty, fill, order_id, sl_id))
        outcome_known = True
        await _log("HEDGE_OPENED", symbol, pe=leg["trading_symbol"], qty=qty, pe_entry=fill, pe_ltp_at_decision=price,
                   slippage=round(fill - price, 2), order_id=order_id, broker_sl=sl_id, **decision)
    finally:
        await live_state.intent_finish(intent, outcome_known)
        await cross_strategy_registry.release_claim(symbol, HEDGE_STRATEGY)


def _hedge_position(symbol, leg, qty, price, order_id, sl_id=None, reconciled=False) -> Position:
    return Position(
        underlying_symbol=symbol, trading_symbol=leg["trading_symbol"], resolved_option_type="PE",
        instrument_side="LONG", exchange_segment="NSE_FNO", product_type=leg["product_type"], quantity=qty,
        lot_size=leg.get("lot_size"), entry_price=price, best_price=price, stop_pct=0.0,
        hard_stop_loss=max(price - settings.get("hedge_stop_rs") / qty, 0.05),
        trailing_stop_dist=NO_TRAILING, trailing_step=NO_TRAILING, pnl_multiplier=qty,
        order_id=order_id, reconciled=reconciled, stop_loss_order_id=sl_id)


# --------------------------------------------------------------------------- #
# Hedge exits
# --------------------------------------------------------------------------- #
def hedge_exit_reason(entry: float, best: float, ltp: float, qty: float) -> Optional[str]:
    """Pure. best must already include ltp."""
    profit, peak = (ltp - entry) * qty, (best - entry) * qty
    if profit <= -settings.get("hedge_stop_rs"):
        return "HEDGE_STOP"
    if peak >= settings.get("hedge_trail_arm_rs") and profit <= peak * (1 - settings.get("hedge_trail_giveback")):
        return "HEDGE_TRAIL"
    return None


async def _apply_hedge_price(symbol: str, ltp: float, real: bool, square_off: bool = False) -> None:
    if real:
        pos = hedge_store.live_positions.get(symbol)
        if pos is None or pos.pending_exit_order_id or engine._exit_on_cooldown(pos):
            return
        await hedge_store.update_best_price(symbol, ltp)
        reason = "DAILY_SQUARE_OFF" if square_off else hedge_exit_reason(pos.entry_price, pos.best_price, ltp, pos.pnl_multiplier)
        if reason and await hedge_store.try_start_exit(symbol):
            await _log("HEDGE_EXIT_DECIDED", symbol, pe=pos.trading_symbol, reason=reason, pe_entry=pos.entry_price,
                       pe_ltp=ltp, pe_best=pos.best_price, est_pnl=round((ltp - pos.entry_price) * pos.pnl_multiplier), mode="real")
            await engine._exit_position(symbol, pos, ltp, reason, hedge_store)
        elif not reason:
            await stop_ratchet.maybe_ratchet("hedge", symbol, pos, ltp)    # broker stop follows the 30% trail
            await _scale_safe(scale.on_hedge_price(symbol, pos, ltp, True))
    else:
        pos = await hedge_paper_book.update(symbol, ltp)
        if pos is None:
            return
        reason = "DAILY_SQUARE_OFF" if square_off else hedge_exit_reason(pos.entry_price, pos.best_price, ltp, pos.pnl_multiplier)
        if reason:
            record = await hedge_paper_book.close(symbol, ltp, reason)
            if record:
                await _log("HEDGE_CLOSED", symbol, mode="paper", **record)
        else:
            await _scale_safe(scale.on_hedge_price(symbol, pos, ltp, False))


async def on_price_tick(trading_symbol: str, ltp: float) -> None:
    """WS fast path: hedge exits and CE hedge triggers."""
    await _scale_safe(scale.on_option_tick(trading_symbol, ltp))
    try:
        for sym, pos in list(hedge_store.live_positions.items()):
            if pos.trading_symbol == trading_symbol:
                if await engine._check_broker_stop_already_filled(sym, pos, hedge_store):
                    return
                await _apply_hedge_price(sym, ltp, True)
                return
        for sym, pos in list(hedge_paper_book.positions.items()):
            if pos.trading_symbol == trading_symbol:
                await _apply_hedge_price(sym, ltp, False)
                return
        for sym, pos in list(position_store.live_positions.items()):
            if pos.trading_symbol == trading_symbol and not pos.pending_exit_order_id:
                await check_ce(sym, pos, ltp, True)
                return
        for sym, pos in list(paper_book.positions.items()):
            if pos.trading_symbol == trading_symbol:
                await check_ce(sym, pos, ltp, False)
                return
    except Exception:  # noqa: BLE001
        logger.exception("[supervisor] on_price_tick failed for %s", trading_symbol)


# --------------------------------------------------------------------------- #
# Loop
# --------------------------------------------------------------------------- #
async def _ltp(pos: Position) -> Optional[float]:
    try:
        return await pricing.live_price(pos)   # price call, else the order book's mid (real positions)
    except Exception:  # noqa: BLE001
        return None


async def _day_real_pnl() -> float:
    total = 0.0
    for store in (position_store, hedge_store):
        for p in store.closed_positions_today:
            if p.exit_price is not None:
                total += (p.exit_price - p.entry_price) * p.pnl_multiplier
        for p in list(store.live_positions.values()):
            ltp = await _ltp(p)
            if ltp is not None:
                total += (ltp - p.entry_price) * p.pnl_multiplier
    return total


async def _tick() -> None:
    await hedge_store.maybe_reset_for_new_day()
    await engine._sync_pending_exit_orders(hedge_store)
    _keep_held_data_fresh()
    _maybe_sweep()
    now = _now()
    square = now.weekday() < 5 and now.strftime("%H:%M") >= settings.get("square_off_time")

    for sym, pos in list(hedge_store.live_positions.items()):
        if pos.pending_exit_order_id or engine._exit_on_cooldown(pos):
            continue
        if await engine._check_broker_stop_already_filled(sym, pos, hedge_store):
            await _log("HEDGE_CLOSED_BY_BROKER_SL", sym, pe=pos.trading_symbol)
            continue
        try:
            ltp = await pricing.position_price(pos)   # one read per cycle, shared with the brake's
        except Exception:  # noqa: BLE001
            await engine._handle_ltp_staleness(sym, pos, hedge_store)
            continue
        engine._ltp_failure_since.pop((sym, pos.opened_at), None)
        await _apply_hedge_price(sym, ltp, True, square)
    for sym, pos in list(hedge_paper_book.positions.items()):
        ltp = await _ltp(pos)
        if ltp is not None:
            await _apply_hedge_price(sym, ltp, False, square)

    if not square:
        for sym, pos in list(position_store.live_positions.items()):
            await _check_held_data(sym, True)
            if not pos.pending_exit_order_id:
                ltp = await _ltp(pos)
                if ltp is not None:
                    await check_ce(sym, pos, ltp, True)
        for sym, pos in list(paper_book.positions.items()):
            await _check_held_data(sym, False)
            ltp = await _ltp(pos)
            if ltp is not None:
                await check_ce(sym, pos, ltp, False)

    brake = settings.get("disaster_brake_rs")
    if brake > 0 and not halted_today() and (position_store.live_positions or hedge_store.live_positions
                                               or position_store.closed_positions_today or hedge_store.closed_positions_today):
        pnl = await _day_real_pnl()
        if pnl <= -brake:
            halted["day"], halted["reason"] = now.date(), f"day PnL {pnl:,.0f} <= -{brake:,.0f}"
            logger.error("[supervisor] DISASTER BRAKE: %s - halting entries/hedges and squaring off", halted["reason"])
            await _log("DISASTER_BRAKE", "*", day_pnl=round(pnl), brake=brake)
            await square_off_ce("DISASTER_BRAKE")
            for sym, pos in list(hedge_store.live_positions.items()):
                ltp = await _ltp(pos) or pos.entry_price
                if await hedge_store.try_start_exit(sym):
                    await engine._exit_position(sym, pos, ltp, "DISASTER_BRAKE", hedge_store)

    await _scale_safe(scale.tick())
    try:
        best_price_memory.record(STRATEGY, list(position_store.live_positions.values()))
        best_price_memory.record(HEDGE_STRATEGY, list(hedge_store.live_positions.values()))
    except Exception:  # noqa: BLE001
        logger.exception("[supervisor] best-price memory write failed")

    live_keys = {(s, p.opened_at.isoformat()) for s, p in list(position_store.live_positions.items()) + list(paper_book.positions.items())}
    for key in [k for k in _track if k not in live_keys and k[1][:10] != now.date().isoformat()]:
        _track.pop(key, None)
    live_state.save()   # state file for restarts (only written when something changed)


# --------------------------------------------------------------------------- #
# Orphan sweep (30 Sep 2026, user: "a mechanism to retry or close any such
# positions going forward"). A last line of defence behind the write-ahead
# intents: nothing of ours may sit at the broker unmanaged.
# --------------------------------------------------------------------------- #
ORDER_TAG_PREFIXES = ("SBol-", "SBH-")     # trading_engine.ORDER_TAG_PREFIX / the hedge tag
ORPHAN_MIN_AGE_SECONDS = 20
_sweep = {"last": 0.0, "running": False}
_untracked_logged: dict[str, float] = {}


def _order_age_seconds(created: Optional[str]) -> float:
    try:
        made = datetime.strptime(created, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_now().tzinfo)
        return (_now() - made).total_seconds()
    except (TypeError, ValueError):
        return 1e9   # unknown age = old


_stop_orphan_seen: dict[str, float] = {}   # bot stop order id -> when it was first seen on a flat contract


async def _sweep_orphan_stops(loop, out: dict) -> None:
    """1 Oct 2026 (ratcheted broker stop): cancel any bot stop-loss order resting on a contract the account
    holds NONE of - it can only ever open a new position (e.g. a stop left live after a failed cancel during
    a bot exit). Both sides: a SELL stop protects a long (every options strategy), a BUY stop protects a
    Swing SHORT (futures / MCX / equity) - on a flat contract either one would open the opposite side. Only
    the bots' own stops (tag "SL-...", which every strategy uses for its protective exit stop and for nothing
    else; a manual order has no tag), checked against Dhan's raw position list in every segment. Cancelled
    only when seen on a flat contract in two sweeps in a row (a stop placed right after a fill never races
    the position list)."""
    try:
        stops = [o for o in await loop.run_in_executor(None, dhan_wrapper.list_open_orders, None)
                 if o.get("transaction_type") in ("SELL", "BUY")
                 and str(o.get("order_type") or "").startswith("STOP_LOSS") and str(o.get("tag") or "").startswith("SL-")]
    except Exception:  # noqa: BLE001
        logger.exception("[supervisor] stop sweep: could not read the order book")
        return
    if not stops:
        _stop_orphan_seen.clear()
        return
    try:
        held = await loop.run_in_executor(None, dhan_wrapper.held_security_ids)
    except Exception:  # noqa: BLE001
        logger.exception("[supervisor] stop sweep: could not read the broker positions - nothing cancelled")
        return
    now, seen_now = time.monotonic(), set()
    for o in stops:
        if not o.get("security_id") or o["security_id"] in held:
            continue
        seen_now.add(o["order_id"])
        if o["order_id"] not in _stop_orphan_seen:
            _stop_orphan_seen[o["order_id"]] = now
            continue
        try:
            await loop.run_in_executor(None, dhan_wrapper.cancel_order, o["order_id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[supervisor] stop sweep: cancel of %s failed (%r)", o["order_id"], exc)
        try:
            final = await loop.run_in_executor(None, dhan_wrapper.refresh_order_status, o["order_id"])
            status = final.status
        except Exception:  # noqa: BLE001
            status = "UNKNOWN"
        await _log("ORPHAN_STOP_CANCELLED" if status == OrderStatus.CANCELLED else "ORPHAN_STOP_FOUND", "*",
                   order_id=o["order_id"], side=o["transaction_type"], broker_symbol=o["trading_symbol"],
                   tag=o.get("tag"), status_before=o["status"], status_after=status, price=o.get("price"))
        logger.error("[supervisor] ORPHAN %s STOP order %s (%s) on a contract the account does not hold - now %s",
                     o["transaction_type"], o["order_id"], o["trading_symbol"], status)
        out.setdefault("stops_cancelled", []).append(o["order_id"])
    for oid in [k for k in _stop_orphan_seen if k not in seen_now]:
        _stop_orphan_seen.pop(oid, None)


async def sweep_orphans(stops_only: bool = False) -> dict:
    """Cancel open BUY orders carrying this strategy's tags that no order in
    flight owns (adopting a fill that raced the cancel), flag broker
    positions in our books' contracts that nothing tracks, and (1 Oct 2026)
    cancel bot stop orders (SELL or BUY) resting on contracts the account no longer holds.
    stops_only: just the stop part (the MCX evening session, after NSE has closed)."""
    loop = asyncio.get_running_loop()
    out = {"cancelled": [], "adopted": [], "untracked": []}
    try:
        await _sweep_orphan_stops(loop, out)
    except Exception:  # noqa: BLE001
        logger.exception("[supervisor] stop sweep failed")
    if stops_only:
        return out
    in_flight = {it.get("order_id") for it in live_state._intents.values() if it.get("order_id")}
    try:
        orders = await loop.run_in_executor(None, dhan_wrapper.list_open_orders, "BUY")
    except Exception:  # noqa: BLE001
        logger.exception("[supervisor] orphan sweep: could not read the order book")
        return out
    broker = None
    for o in orders:
        if not o["tag"].startswith(ORDER_TAG_PREFIXES) or o["order_id"] in in_flight:
            continue
        if live_state._intents and any(not it.get("order_id") for it in live_state._intents.values()):
            continue   # an order is being placed right now and has no id yet - look again next sweep
        if _order_age_seconds(o.get("created")) < ORPHAN_MIN_AGE_SECONDS:
            continue
        kind = "hedge" if o["tag"].startswith("SBH-") else "entry"
        try:
            await loop.run_in_executor(None, dhan_wrapper.cancel_order, o["order_id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[supervisor] orphan sweep: cancel of %s failed (%r)", o["order_id"], exc)
        try:
            final = await loop.run_in_executor(None, dhan_wrapper.refresh_order_status, o["order_id"])
        except Exception:  # noqa: BLE001
            final = None
        status = final.status if final else "UNKNOWN"
        await _log("ORPHAN_ORDER_CANCELLED" if status == OrderStatus.CANCELLED else "ORPHAN_ORDER_FOUND", "*",
                   order_id=o["order_id"], broker_symbol=o["trading_symbol"], tag=o["tag"], kind=kind,
                   status_before=o["status"], status_after=status, order_type=o.get("order_type"), price=o.get("price"))
        logger.error("[supervisor] ORPHAN %s order %s (%s, tag %s) found at the broker - now %s", kind, o["order_id"],
                     o["trading_symbol"], o["tag"], status)
        out["cancelled"].append(o["order_id"])
        if final is not None and final.status == OrderStatus.TRADED:
            # It filled before the cancel: find the broker position by security id and adopt it.
            try:
                broker = broker if broker is not None else await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)
                for bp in broker:
                    sid = await loop.run_in_executor(
                        None, lambda t=bp["trading_symbol"]: str(dhan_wrapper._instrument_meta(t, expected_exchange="NSE")["security_id"]))
                    if sid == o["security_id"] and bp["quantity"] > 0:
                        tracked = {p.trading_symbol for p in list(position_store.live_positions.values())
                                   + list(hedge_store.live_positions.values())}
                        it = {"kind": kind, "symbol": bp["underlying_symbol"], "trading_symbol": bp["trading_symbol"],
                              "quantity": abs(bp["quantity"]), "lot_size": bp.get("lot_size"),
                              "product_type": bp.get("product_type"), "order_id": o["order_id"]}
                        res = await live_state._resolve_intent("sweep", it, tracked, None)
                        await _log("ORPHAN_FILL_ADOPTED", bp["underlying_symbol"], **res)
                        out["adopted"].append(res)
                        break
            except Exception:  # noqa: BLE001
                logger.exception("[supervisor] orphan sweep: could not adopt the fill of order %s", o["order_id"])
    # Broker positions that are ours by trade history (or nobody's) but that no book is managing.
    try:
        broker = broker if broker is not None else await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)
        tracked = {p.trading_symbol for p in list(position_store.live_positions.values())
                   + list(hedge_store.live_positions.values())}
        for bp in broker:
            ts = bp["trading_symbol"]
            if bp.get("quantity", 0) <= 0 or ts in tracked:
                continue
            owner = await loop.run_in_executor(None, attribute_open_broker_position, ts)
            if owner not in (STRATEGY, HEDGE_STRATEGY):
                continue   # another strategy's position, or not ours by our own records
            now = time.monotonic()
            if now - _untracked_logged.get(ts, 0.0) >= 300:
                _untracked_logged[ts] = now
                logger.error("[supervisor] UNTRACKED position at the broker: %s x%s (history says %s) - not managed by "
                             "any book", ts, bp["quantity"], owner)
                await _log("UNTRACKED_POSITION_AT_BROKER", bp["underlying_symbol"], trading_symbol=ts,
                           quantity=bp["quantity"], avg_price=bp.get("avg_price"), history_owner=owner)
            out["untracked"].append(ts)
    except Exception:  # noqa: BLE001
        logger.exception("[supervisor] orphan sweep: could not check broker positions")
    return out


async def _sweep_task(stops_only: bool = False) -> None:
    try:
        await sweep_orphans(stops_only)
    finally:
        _sweep["running"] = False


def _maybe_sweep() -> None:
    every = settings.get("orphan_sweep_seconds")
    now = time.monotonic()
    if every <= 0 or _sweep["running"] or now - _sweep["last"] < every:
        return
    nse_open = dhan_wrapper.is_market_open()
    if not nse_open and not dhan_wrapper.is_market_open("MCX_COMM"):   # MCX evening: Swing MCX stops still swept
        return
    _sweep["last"], _sweep["running"] = now, True
    asyncio.create_task(_sweep_task(stops_only=not nse_open))


async def supervisor_loop() -> None:
    logger.info("[supervisor] loop started (hedge_mode=%s)", settings.get("hedge_mode"))
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001
            logger.exception("[supervisor] tick failed")
        await asyncio.sleep(LOOP_SECONDS)


async def reconcile_hedges() -> list[Position]:
    loop = asyncio.get_running_loop()
    out = []
    for bp in await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions):
        if not bp.get("avg_price") or bp["quantity"] <= 0 or bp.get("option_type") != "PE":
            continue
        if await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"]) != HEDGE_STRATEGY:
            continue
        leg = {"trading_symbol": bp["trading_symbol"], "product_type": bp.get("product_type") or bcfg.OPTIONS_PRODUCT,
               "lot_size": bp.get("lot_size")}
        sl_id = None
        try:
            sl_id = await loop.run_in_executor(None, dhan_wrapper.get_pending_order_id, bp["trading_symbol"], "SELL", "NSE")
        except Exception:  # noqa: BLE001
            pass
        out.append(_hedge_position(bp["underlying_symbol"], leg, abs(bp["quantity"]), bp["avg_price"], "", sl_id, True))
    for pos in out:
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, pos.trading_symbol)
    return out
