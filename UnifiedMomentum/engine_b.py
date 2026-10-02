"""
Unified Momentum - ENGINE B, MOMENTUM PUTS (1 Oct 2026). SwingMomentum's signal (moved here when the SwingMomentum
strategy was deleted), made intraday and real-money capable, on Unified Momentum's own put book (state.put_store /
put_paper_book, trade-history tag "UnifiedMomentumPut").

  Universe  the same weekly watchlist as engine A (data/unified_momentum_watchlist), NSE stocks only.
  Signal    Swing v3 entry on CLOSED 5-min candles (Swing's cached regime / Supertrend states, read-only, and its
            pure Swing.trading_engine._entry_direction), BEARISH only -> 1 lot ATM PUT. Taken only when Swing/regime
            says MOMENTUM (2 h efficiency ratio >= 0.25 and today's >= 0.15; UNKNOWN fails open), the signal candle's
            volume >= b_volume_floor_ratio x its 20-bar average, a fresh formation since the last put in that stock
            (Swing's rule, own state), entries 09:15 - b_entry_cutoff_time (14:30) on a candle of today's session
            (the previous day's last candle never trades - the first possible signal is the 09:15 candle at 09:20).
  Gates     shared with engine A: one position per stock across both engines, the market chop gate, the disaster
            brake, premium >= min_premium_rs, funds check; own slot limit b_max_concurrent_trades (2).
  Exits     Swing's options ladder - MAX_LOSS_HIT (b_max_loss_rs) -> TARGET_HIT (+b_target_pct) -> PROFIT_PROTECTION_HIT
            (peak above b_profit_protection_rs, b_profit_protection_giveback off the best price) -> STOP_LOSS_HIT
            (-b_hard_stop_pct) -> SUPERTREND_REVERSAL[_TICK] (a bullish cross, or the live price crossing the last
            closed candle's line, on a later candle than the entry's) -> DAILY_SQUARE_OFF at square_off_time (15:15).
            Broker SL-L at the max-loss level. Every real exit goes through Bollinger's exit guard (_exit_position).
  Orders    the same incident-hardened path as engine A (cross-strategy claim, funds check, duplicate-order guard,
            write-ahead intent kind "put_entry", retry at the live ask, settle unfilled orders), tag "UMP-...".
Backtest (research_unified_super_strategy.py): engine B's puts were positive in both months (+17.6k); its calls lost
(left out). Real or paper follows the strategy's one paper_mode_control toggle "UnifiedMomentum".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import cross_strategy_registry
import fund_allocation
from trade_history import attribute_open_broker_position
from Bollinger import config as bcfg
from Bollinger import trading_engine as engine
from Bollinger.position_store import OrderRecord, Position, position_store as bollinger_store
from Options.dhan_client import OrderResult, OrderStatus, dhan_wrapper
from Swing import candle_feed, regime
from Swing import config as swing_config
from Swing import signals as swing_signals
from Swing import trading_engine as swing_te
from Swing.position_store import broker_stop_trigger_and_limit

from . import live_state, market_gate, pricing, settings
from . import trading_engine as te
from .state import (EVENTS_LOG, PUT_STRATEGY, STRATEGY, halted, hedge_paper_book, hedge_store, paper_book,
                    position_store, put_paper_book, put_store)

logger = logging.getLogger("unified_momentum_engine_b")

ORDER_TAG_PREFIX = "UMP"
MARKET_OPEN_TIME = "09:15"
# The fresh-formation state outlives the day (the backtest carries it across days; the live-state ledger is
# day-scoped and the bot restarts every morning), so it is also kept in its own file.
STATE_FILE = Path("data/unified_momentum_engine_b_state.json")

PROFILE_B = engine.Profile(
    name=PUT_STRATEGY, entry_mode="bar_close", sides="both", exit_mode="hold_to_close",
    roll_days=settings.get("roll_expiry_within_trading_days"), daily_square_off_time=settings.get("square_off_time"),
    paper_book=put_paper_book, events_log=EVENTS_LOG, paper_only=False,
)

_consumed: dict[str, Optional[int]] = {}             # symbol -> -1 after a put entry, until a fresh formation
_consumed_candle: dict[str, Optional[datetime]] = {}  # symbol -> that entry's signal candle
_seen: dict[tuple, object] = {}                       # (symbol, direction, candle[, reason]) -> reading / "skip"
_inflight: set[str] = set()


def _now() -> datetime:
    return engine._now_ist()


async def _event(event: str, symbol: str, detail: dict) -> None:
    await engine._record_bollinger_event(event, symbol, {"strategy": STRATEGY, "engine": "B", **detail}, EVENTS_LOG)


def _remember(key: tuple, value) -> None:
    if len(_seen) > 5000:
        _seen.clear()
    _seen[key] = value


# --------------------------------------------------------------------------- #
# Restart memory: its own file (STATE_FILE, written on every change), also shown in the live-state ledger
# --------------------------------------------------------------------------- #
def state_snapshot() -> dict:
    return {"consumed": {s: [v, _consumed_candle[s].isoformat() if _consumed_candle.get(s) else None]
                         for s, v in _consumed.items() if v is not None}}


def restore_state(data: Optional[dict]) -> int:
    for sym, (side, candle) in ((data or {}).get("consumed") or {}).items():
        try:
            _consumed[sym] = int(side)
            _consumed_candle[sym] = datetime.fromisoformat(candle) if candle else None
        except (TypeError, ValueError):
            continue
    return len(_consumed)


def _save_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved_at": _now().isoformat(), **state_snapshot()}, indent=1))
        os.replace(tmp, STATE_FILE)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not write %s", PUT_STRATEGY, STATE_FILE)


def load_state() -> int:
    """Startup (unified_momentum_main.lifespan) - the only place the fresh-formation state is restored from."""
    if not STATE_FILE.exists():
        return 0
    try:
        return restore_state(json.loads(STATE_FILE.read_text()))
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not read %s - starting without the fresh-formation state", PUT_STRATEGY,
                         STATE_FILE)
        return 0


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def _entries_open_now() -> bool:
    now = _now()
    return (now.weekday() < 5 and engine._parse_hhmm_today(MARKET_OPEN_TIME) <= now
            < engine._parse_hhmm_today(settings.get("b_entry_cutoff_time")))


def _square_off_now() -> bool:
    now = _now()
    return now.weekday() < 5 and now >= engine._parse_hhmm_today(settings.get("square_off_time"))


def _halted_today() -> bool:
    return halted["day"] == _now().date()


def open_count(paper: bool) -> int:
    return len(put_paper_book.positions) if paper else len(put_store.reserved_symbols | set(put_store.live_positions))


def held_by_engine_a(symbol: str) -> bool:
    """Engine A's call (real, paper, or an entry on its way) - or one of its supervisor's PUT hedges, which can
    outlive the call: a put here would be the SAME ATM PE contract, and Dhan nets one contract into one position."""
    return (symbol in position_store.live_positions or symbol in position_store.reserved_symbols
            or symbol in paper_book.positions or symbol in te._entry_inflight
            or symbol in hedge_store.live_positions or symbol in hedge_store.reserved_symbols
            or symbol in hedge_paper_book.positions)


def held_here(symbol: str) -> bool:
    return (symbol in put_store.live_positions or symbol in put_store.reserved_symbols
            or symbol in put_paper_book.positions or symbol in _inflight)


async def can_enter(symbol: str) -> bool:
    return (settings.get("strategy_enabled") and settings.get("engine_b_enabled") and not _halted_today()
            and _entries_open_now() and not _square_off_now() and te.is_eligible_symbol(symbol)
            and open_count(te.is_paper_symbol(symbol)) < settings.get("b_max_concurrent_trades")
            and not held_here(symbol) and not held_by_engine_a(symbol)
            and not await put_store.is_in_entry_cooldown(symbol)
            and swing_signals._symbol_market_open(symbol) and market_gate.is_open())


# --------------------------------------------------------------------------- #
# Signal (Swing's, read-only) + own fresh-formation state + the momentum gate
# --------------------------------------------------------------------------- #
def _release(symbol: str, is_bullish: bool, st) -> None:
    """Swing's fresh-formation rule (Swing/signals.release_regime_entry_side) on this engine's own state."""
    side = _consumed.get(symbol)
    if side is None:
        return
    entry_candle = _consumed_candle.get(symbol)
    later = st is not None and st.candle_start is not None and (entry_candle is None or st.candle_start > entry_candle)
    if ((1 if is_bullish else -1) != side or (later and (1 if st.is_above else -1) != side)
            or (later and (st.crossed_above if side == 1 else st.crossed_below))):
        _consumed[symbol] = None
        _save_state()


async def _refresh_release(symbol: str) -> None:
    """Keep a used stock's fresh-formation state current on every tick - also while it is held or no slot is
    free (the backtest checks it on every candle)."""
    reg = await swing_signals.get_regime_state(symbol)
    if reg is not None:
        _release(symbol, reg.is_bullish, await swing_signals.get_supertrend_state(symbol))


async def _signal(symbol: str) -> Optional[tuple]:
    reg = await swing_signals.get_regime_state(symbol)
    if reg is None:
        return None
    st = await swing_signals.get_supertrend_state(symbol)
    _release(symbol, reg.is_bullish, st)
    if st is None or st.candle_start is None:
        return None
    if st.candle_start.date() != _now().date():
        # The previous session's last candle (09:15-09:20 every day, until today's first candle closes): it
        # was never taken (entries stop at 14:30) and the backtest never trades it a day later (2 Oct 2026).
        return None
    direction = await swing_te._entry_direction(symbol, reg, st)
    if direction != "BEARISH" or _consumed.get(symbol) == -1:      # PUTs only; same formation stays blocked
        return None
    key = (symbol, direction, st.candle_start)
    reading = _seen.get(key)
    if reading is None:
        try:
            reading = await asyncio.get_running_loop().run_in_executor(dhan_wrapper.history_executor(), regime.read,
                                                                       symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: regime read failed - signal allowed (fail open)", PUT_STRATEGY, symbol)
            reading = regime.RegimeReading(state="UNKNOWN", allows_entry=True, er_2h=None, er_today=None,
                                           reasons=("read failed",))
        _remember(key, reading)
        await _event("B_SIGNAL", symbol, {"direction": direction, "signal_candle": st.candle_start.isoformat(),
                                          "taken_if_possible": reading.allows_entry, **reading.as_dict()})
    if not reading.allows_entry:
        return None
    return st, reading


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
async def _resolve_leg(symbol: str) -> tuple[dict, float]:
    """ATM PUT (expiry roll as engine A) + the minimum-premium gate. Raises engine._SkipEntry."""
    PROFILE_B.roll_days = settings.get("roll_expiry_within_trading_days")
    leg = await engine._resolve_option_leg(symbol, "BEARISH", PROFILE_B)
    leg["quantity"] = leg["lot_size"] * settings.get("quantity_lots")
    leg["pnl_multiplier"] = leg["quantity"]
    price, source = await pricing.price_for_entry(leg["trading_symbol"])
    minimum = settings.get("min_premium_rs")
    if price is None:
        raise engine._SkipEntry({"symbol": symbol, "status": "skipped", "reason": "no_price",
                                 "trading_symbol": leg["trading_symbol"]})
    if price < minimum:
        raise engine._SkipEntry({"symbol": symbol, "status": "skipped", "reason": "premium_below_minimum",
                                 "trading_symbol": leg["trading_symbol"], "premium": price, "minimum": minimum,
                                 "price_source": source})
    return leg, price


def _new_put(symbol: str, leg: dict, price: float, order_id: str, sl_id: Optional[str], st,
             reconciled: bool = False) -> Position:
    pos = te._new_position(symbol, leg, price, order_id, sl_id, reconciled=reconciled, option_type="PE",
                           max_loss_rs=settings.get("b_max_loss_rs"))
    pos.entry_candle_start = st.candle_start if st is not None else None
    return pos


async def _enter(symbol: str, st, reading) -> None:
    vol = st.volume_ratio
    floor = settings.get("b_volume_floor_ratio")
    if floor > 0 and vol is not None and vol < floor:
        key = (symbol, "BEARISH", st.candle_start, "volume")
        if key not in _seen:
            _remember(key, "skip")
            await _event("B_ENTRY_SKIPPED", symbol, {"reason": "volume_floor", "vol_ratio": vol, "floor": floor,
                                                     "signal_candle": st.candle_start.isoformat()})
        return
    _inflight.add(symbol)
    try:
        if te.is_paper_symbol(symbol):
            res = await _enter_paper(symbol, st, reading)
        else:
            res = await _enter_real(symbol, st, reading)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] %s: entry failed", PUT_STRATEGY, symbol)
        res = {"status": "error", "reason": repr(exc)}
    finally:
        _inflight.discard(symbol)
    if res.get("status") in ("entered", "paper_entered"):
        _consumed[symbol], _consumed_candle[symbol] = -1, st.candle_start
        _save_state()
        live_state.save(force=True)
    else:
        key = (symbol, "BEARISH", st.candle_start, res.get("reason") or res.get("status"))
        if key not in _seen:
            _remember(key, "skip")
            await _event("B_ENTRY_SKIPPED", symbol, {"signal_candle": st.candle_start.isoformat(), **res})
    logger.info("[%s] %s: BEARISH momentum signal on the %s candle -> %s", PUT_STRATEGY, symbol,
                st.candle_start.strftime("%H:%M"), res)


async def _enter_paper(symbol: str, st, reading) -> dict:
    try:
        leg, price = await _resolve_leg(symbol)
    except engine._SkipEntry as skip:
        return skip.result
    pos = _new_put(symbol, leg, price, "PAPER", None, st)
    if not await put_paper_book.open(pos):
        return {"symbol": symbol, "status": "skipped", "reason": "paper_position_already_open"}
    try:
        await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.subscribe_option_price, leg["trading_symbol"])
    except Exception:  # noqa: BLE001
        logger.exception("[%s] %s: could not WS-subscribe %s", PUT_STRATEGY, symbol, leg["trading_symbol"])
    await _event("B_PAPER_POSITION_OPENED", symbol, {"trading_symbol": leg["trading_symbol"], "entry_price": price,
                                                     "quantity": leg["quantity"], "regime": reading.as_dict()})
    return {"symbol": symbol, "status": "paper_entered", "trading_symbol": leg["trading_symbol"], "entry_price": price}


async def _enter_real(symbol: str, st, reading) -> dict:
    """REAL-money entry: the same claim / guard / reserve sequence as engine A's enter_real."""
    if not await cross_strategy_registry.try_claim(symbol, PUT_STRATEGY):
        return {"symbol": symbol, "status": "skipped", "reason": "entry_in_progress_by_other_strategy"}
    key = cross_strategy_registry.same_contract_key(symbol)
    try:
        if not await cross_strategy_registry.try_claim(key, PUT_STRATEGY):
            return {"symbol": symbol, "status": "skipped", "reason": "entry_in_progress_by_other_strategy"}
        if held_by_engine_a(symbol):                     # re-checked under the claim
            return {"symbol": symbol, "status": "skipped", "reason": "held_by_engine_a"}
        if symbol in bollinger_store.live_positions or symbol in bollinger_store.reserved_symbols:
            return {"symbol": symbol, "status": "skipped", "reason": "held_by_bollinger"}
        if engine.swing_real_holds(symbol):
            return {"symbol": symbol, "status": "skipped", "reason": "held_by_swing"}
        if engine.super_bollinger_real_holds(symbol):
            return {"symbol": symbol, "status": "skipped", "reason": "held_by_super_bollinger"}
        if not await put_store.reserve_symbol(symbol):
            return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}
        try:
            return await _enter_real_reserved(symbol, st, reading)
        finally:
            if symbol not in put_store.live_positions:
                await put_store.record_failed_entry(symbol)
                await put_store.release_symbol(symbol)
    finally:
        await cross_strategy_registry.release_claim(key, PUT_STRATEGY)
        await cross_strategy_registry.release_claim(symbol, PUT_STRATEGY)


async def _enter_real_reserved(symbol: str, st, reading) -> dict:
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
                    buffer_rs=bcfg.FUNDS_CHECK_BUFFER_RS)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] %s: funds check failed - proceeding optimistically", PUT_STRATEGY, symbol)
                sufficient = True
            if not sufficient:
                return {"symbol": symbol, "status": "skipped", "reason": "insufficient_funds",
                        "trading_symbol": trading_symbol}
        try:
            existing = await loop.run_in_executor(None, dhan_wrapper.get_pending_order_id, trading_symbol, "BUY", "NSE")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not check for a resting BUY order - proceeding", PUT_STRATEGY, symbol)
            existing = None
        if existing:
            return {"symbol": symbol, "status": "already_pending", "order_id": existing}

        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, trading_symbol)
        tag = engine._gen_tag(ORDER_TAG_PREFIX, symbol)
        intent = live_state.intent_begin("put_entry", symbol, leg, quantity,
                                         signal_candle=st.candle_start.isoformat())   # on file BEFORE the order
        order_resp = await loop.run_in_executor(
            None, dhan_wrapper.place_market_order, trading_symbol, quantity, "BUY", tag, product_type)
        order_id, is_amo = order_resp["order_id"], order_resp["is_amo"]
        live_state.intent_order(intent, order_id)
        await put_store.record_order(OrderRecord(
            order_id=order_id, underlying_symbol=symbol, trading_symbol=trading_symbol, transaction_type="BUY",
            quantity=quantity, status=OrderStatus.TRANSIT, is_amo=is_amo, lot_size=leg["lot_size"]))
        try:
            result = await asyncio.wait_for(dhan_wrapper.wait_for_order_result_async(order_id, is_amo),
                                            timeout=engine._ORDER_RESULT_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not confirm entry order %s - treating as failed", PUT_STRATEGY, symbol,
                             order_id)
            result = OrderResult(order_id=order_id, status=OrderStatus.TRANSIT, remark="order_confirmation_timeout",
                                 fill_price=0.0, filled_quantity=0, is_amo=is_amo)
        await put_store.update_order_status(order_id, result.status, result.remark)
        if result.status != OrderStatus.TRADED:
            result = await te.settle_unfilled_order(symbol, trading_symbol, order_id, result, is_amo, "put_entry")
            await put_store.update_order_status(order_id, result.status, result.remark)
        if result.status == OrderStatus.CANCELLED and not is_amo:
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
                return "momentum_gone" if spot > st.supertrend else None     # back above the Supertrend line

            retried, retry_order_id, retry_intent, _why = await te.retry_unfilled_buy(
                symbol, leg, quantity, gate_price, still_valid, "put_entry", put_store, ORDER_TAG_PREFIX)
            if retried is not None:
                result, order_id, intent = retried, retry_order_id, retry_intent
        if result.status != OrderStatus.TRADED:
            outcome_known = result.status not in OrderStatus.OPEN_STATUSES
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, trading_symbol)
            return {"symbol": symbol, "status": "failed", "reason": f"order_{result.status}", "order_id": order_id}
        fill_price = result.fill_price or await dhan_wrapper.get_option_ltp_async(trading_symbol)

        sl_id = None
        if bcfg.BROKER_STOP_LOSS_ENABLED:
            trigger, limit = broker_stop_trigger_and_limit(
                "LONG", fill_price, quantity, settings.get("b_max_loss_rs"), bcfg.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE,
                hard_stop_pct=0.95)
            try:
                stop_resp = await loop.run_in_executor(
                    None, dhan_wrapper.place_stop_loss_limit_order, trading_symbol, quantity, "SELL", trigger, limit,
                    engine._gen_tag("SL", symbol), product_type)
                sl_id = stop_resp["order_id"]
                await put_store.record_order(OrderRecord(
                    order_id=sl_id, underlying_symbol=symbol, trading_symbol=trading_symbol, transaction_type="SELL",
                    quantity=quantity, status="PENDING", is_amo=False))
                logger.info("[%s] %s: broker SL-L %s for %s trigger=%.2f limit=%.2f", PUT_STRATEGY, symbol, sl_id,
                            trading_symbol, trigger, limit)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] %s: could not place the broker-side stop-loss for %s - the bot's own "
                                 "checks still protect it", PUT_STRATEGY, symbol, trading_symbol)

        await put_store.add_position(_new_put(symbol, leg, fill_price, order_id, sl_id, st))
        outcome_known = True
        await _event("B_POSITION_OPENED", symbol, {"trading_symbol": trading_symbol, "entry_price": fill_price,
                                                   "quantity": quantity, "order_id": order_id, "stop_loss_order_id": sl_id,
                                                   "signal_candle": st.candle_start.isoformat(), "regime": reading.as_dict()})
        return {"symbol": symbol, "status": "entered", "trading_symbol": trading_symbol, "entry_price": fill_price}
    except Exception:  # noqa: BLE001
        logger.exception("[%s] %s: unexpected error entering position", PUT_STRATEGY, symbol)
        return {"symbol": symbol, "status": "error"}
    finally:
        await live_state.intent_finish(intent, outcome_known)


# --------------------------------------------------------------------------- #
# Exits
# --------------------------------------------------------------------------- #
def exit_reason(entry: float, best: float, ltp: float, qty: float) -> Optional[str]:
    """Pure (settings aside). best must already include ltp. Swing's options ladder order."""
    pnl, peak = (ltp - entry) * qty, (best - entry) * qty
    if -pnl >= settings.get("b_max_loss_rs"):
        return "MAX_LOSS_HIT"
    target = settings.get("b_target_pct")
    if target > 0 and ltp >= entry * (1 + target):
        return "TARGET_HIT"
    if peak > settings.get("b_profit_protection_rs") and ltp <= best * (1 - settings.get("b_profit_protection_giveback")):
        return "PROFIT_PROTECTION_HIT"
    if ltp <= entry * (1 - settings.get("b_hard_stop_pct")):
        return "STOP_LOSS_HIT"
    return None


async def _supertrend_reversal(symbol: str, pos: Position) -> Optional[str]:
    """A PUT is bearish exposure: a BULLISH cross is against it (Swing's _evaluate_exit_signal), never on the entry
    candle; with Swing's tick exit timing also the live price crossing the last closed candle's line, once the
    forming candle is a later one than the candle the position was opened in."""
    if not settings.get("b_supertrend_exit"):
        return None
    st = await swing_signals.get_supertrend_state(symbol)
    if st is None or st.candle_start is None:
        return None
    entry_candle = pos.entry_candle_start
    if (entry_candle is None or st.candle_start > entry_candle) and st.crossed_above:
        return "SUPERTREND_REVERSAL"
    if (swing_config.EXIT_TIMING == "tick" and pos.opened_at is not None
            and swing_te._as_ist(st.candle_start) >= swing_te._candle_start_of(pos.opened_at)):
        bullish_touch, _bearish = swing_te._tick_supertrend_touch(symbol, st, swing_config.EXIT_TIMING)
        if bullish_touch:
            return "SUPERTREND_REVERSAL_TICK"
    return None


async def _apply_real(symbol: str, pos: Position, ltp: float, with_supertrend: bool) -> None:
    await put_store.update_best_price(symbol, ltp)
    reason = exit_reason(pos.entry_price, pos.best_price, ltp, pos.pnl_multiplier)
    if reason is None and with_supertrend:
        reason = await _supertrend_reversal(symbol, pos)
    if reason and await put_store.try_start_exit(symbol):
        await _event("B_EXIT_DECIDED", symbol, {"trading_symbol": pos.trading_symbol, "reason": reason,
                                                "entry_price": pos.entry_price, "ltp": ltp, "best_price": pos.best_price,
                                                "est_pnl": round((ltp - pos.entry_price) * pos.pnl_multiplier)})
        await engine._exit_position(symbol, pos, ltp, reason, put_store)


async def _close_paper(symbol: str, price: float, reason: str) -> None:
    pos = put_paper_book.positions.get(symbol)
    record = await put_paper_book.close(symbol, price, reason)
    if record is None:
        return
    await _event("B_PAPER_POSITION_CLOSED", symbol, record)
    if pos is not None and not engine._option_still_needed(pos.trading_symbol):
        try:
            await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.unsubscribe_option_price,
                                                             pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not unsubscribe %s", PUT_STRATEGY, symbol, pos.trading_symbol)


async def _apply_paper(symbol: str, ltp: float, with_supertrend: bool) -> None:
    pos = await put_paper_book.update(symbol, ltp)
    if pos is None:
        return
    reason = exit_reason(pos.entry_price, pos.best_price, ltp, pos.pnl_multiplier)
    if reason is None and with_supertrend:
        reason = await _supertrend_reversal(symbol, pos)
    if reason:
        await _close_paper(symbol, ltp, reason)


async def square_off_all(reason: str) -> None:
    for symbol, pos in list(put_store.live_positions.items()):
        if pos.pending_exit_order_id or engine._exit_on_cooldown(pos):
            continue
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            ltp = pos.entry_price
        if await put_store.try_start_exit(symbol):
            await engine._exit_position(symbol, pos, ltp, reason, put_store)
    for symbol, pos in list(put_paper_book.positions.items()):
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            continue
        await _close_paper(symbol, ltp, reason)


async def on_price_tick(trading_symbol: str, ltp: float) -> None:
    """Option-price WebSocket fast path (price ladder only; the Supertrend exit runs on the 5 s tick)."""
    for symbol, pos in list(put_store.live_positions.items()):
        if pos.trading_symbol == trading_symbol:
            if pos.pending_exit_order_id or engine._exit_on_cooldown(pos):
                return
            if await engine._check_broker_stop_already_filled(symbol, pos, put_store):
                return
            await _apply_real(symbol, pos, ltp, with_supertrend=False)
            return
    for symbol, pos in list(put_paper_book.positions.items()):
        if pos.trading_symbol == trading_symbol:
            await _apply_paper(symbol, ltp, with_supertrend=False)
            return


# --------------------------------------------------------------------------- #
# Tick, on engine B's own loop (2 Oct 2026). It used to run at the end of engine
# A's monitor tick: an exception earlier in A's tick skipped it - including
# the 15:15 square-off poll - and A's entry scan delayed it every cycle.
# --------------------------------------------------------------------------- #
async def loop() -> None:
    logger.info("[%s] engine B loop started.", PUT_STRATEGY)
    while True:
        try:
            await tick(_square_off_now())
        except Exception:  # noqa: BLE001
            logger.exception("[%s] error in engine B loop tick", PUT_STRATEGY)
        await asyncio.sleep(bcfg.MONITOR_INTERVAL_SECONDS)


async def tick(square_off: bool) -> None:
    await put_store.maybe_reset_for_new_day()
    await engine._sync_pending_exit_orders(put_store)
    if square_off:
        await square_off_all("DAILY_SQUARE_OFF")
    for symbol, pos in list(put_store.live_positions.items()):
        if pos.pending_exit_order_id or engine._exit_on_cooldown(pos):
            continue
        if await engine._check_broker_stop_already_filled(symbol, pos, put_store):
            continue
        try:
            ltp = await pricing.live_price(pos)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] could not fetch LTP for %s", PUT_STRATEGY, pos.trading_symbol)
            await engine._handle_ltp_staleness(symbol, pos, put_store)
            continue
        engine._ltp_failure_since.pop((symbol, pos.opened_at), None)
        await _apply_real(symbol, pos, ltp, with_supertrend=True)
    for symbol, pos in list(put_paper_book.positions.items()):
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            continue
        await _apply_paper(symbol, ltp, with_supertrend=True)
    for symbol in [s for s, v in list(_consumed.items()) if v is not None]:
        try:
            if swing_signals._symbol_market_open(symbol):
                await _refresh_release(symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: fresh-formation check failed", PUT_STRATEGY, symbol)
    if square_off or not (settings.get("strategy_enabled") and settings.get("engine_b_enabled")):
        return
    if not _entries_open_now() or _halted_today() or not market_gate.is_open():
        return
    for i, symbol in enumerate(await te.eligible_symbols()):
        try:
            if not await can_enter(symbol):
                continue
            if i and not swing_signals.is_symbol_ws_fresh(symbol):
                await asyncio.sleep(swing_config.SYMBOL_PACING_SECONDS)
            sig = await _signal(symbol)
            if sig and await can_enter(symbol):
                await _enter(symbol, *sig)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: signal/entry failed", PUT_STRATEGY, symbol)


# --------------------------------------------------------------------------- #
# Startup reconciliation (a mid-day restart; positions never carry overnight)
# --------------------------------------------------------------------------- #
async def reconcile_broker_positions() -> list[Position]:
    """Broker PE positions whose OPEN is recorded under "UnifiedMomentumPut" in our own trade history."""
    loop = asyncio.get_running_loop()
    positions = []
    for bp in await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions):
        if not bp.get("avg_price") or bp["quantity"] <= 0 or bp.get("option_type") != "PE":
            continue
        if await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"]) != PUT_STRATEGY:
            continue
        quantity = abs(bp["quantity"])
        leg = {"trading_symbol": bp["trading_symbol"], "product_type": bp.get("product_type") or bcfg.OPTIONS_PRODUCT,
               "quantity": quantity, "lot_size": bp.get("lot_size")}
        sl_id = None
        try:
            sl_id = await loop.run_in_executor(None, dhan_wrapper.get_pending_order_id, bp["trading_symbol"], "SELL", "NSE")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not look up a resting SELL order", PUT_STRATEGY, bp["trading_symbol"])
        positions.append(_new_put(bp["underlying_symbol"], leg, bp["avg_price"], "", sl_id, None, reconciled=True))
    for pos in positions:
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, pos.trading_symbol)
    return positions
