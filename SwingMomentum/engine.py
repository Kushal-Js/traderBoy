"""
SwingMomentum - Swing's entry and exit rules + the momentum / sideways / choppy classifier (Swing/regime.py),
PAPER ONLY (1 Oct 2026, user: "create, and deploy this updated swing strategy with paper trading on").

A SEPARATE strategy (the user's rule, 28 Sep 2026: a new approach never changes or mixes into a deployed
strategy's results, even on paper): its own open positions (data/swing_momentum_paper_positions.json), trade log
(history/<date>_swing_momentum_paper_trades.log), events log (history/<date>_swing_momentum_events.log), re-entry
state and endpoints (/swing-momentum/...). Swing itself is untouched and keeps trading as before.

Read-only from Swing: its watchlist, its cached signal states (signals.get_regime_state / get_supertrend_state -
the same cache, no extra Dhan calls), its entry direction (trading_engine._entry_direction - pure) and its exit
helpers (_exit_reason_for, _evaluate_exit_signal, _get_ltp, the Friday / expiry-day square-offs).

Rules = Swing as live (v3 on closed 5-min candles, NSE volume floor, re-entry on a fresh formation (33916b1),
options basket, ATM of the nearest expiry, 1 lot, the options exit ladder, Friday / expiry-day 15:25 square-off,
overnight carry) PLUS: an entry is taken only when the stock's regime is MOMENTUM (or UNKNOWN = too little data):
efficiency ratio of the last 2 h >= 0.25 and today's >= 0.15 (Swing/regime.py, SWING_REGIME_* thresholds).
Backtest 1 Sep - 1 Oct (research_swing_regime_classifier_30day.py): 97 trades, 53% won, +41,929 after modelled
slippage vs Swing's -109,536; max drawdown -18,491 vs -156,377.

NSE stocks only (index / MCX symbols on Swing's watchlist are skipped - the classifier was tested on stocks).
There is NO order path in this module: it never calls place_*_order, so it can never trade real money.

Option price subscriptions are never UNsubscribed here: Swing's paper book often holds the same contract and
dhan_wrapper's unsubscribe is not reference-counted. Open contracts are re-subscribed if they drop off the feed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import fund_allocation
import trade_history
from Options.dhan_client import dhan_wrapper
from Swing import config, regime, signals
from Swing import swing_paper_engine as spe
from Swing import trading_engine as swing_te
from Swing.position_store import Position, entry_transaction_type, resolve_instrument_side, resolved_option_type_for
from Swing.watchlist import watchlist_store

logger = logging.getLogger("swing_momentum")

STRATEGY = "SwingMomentum"
ENABLED = os.getenv("SWING_MOMENTUM_ENABLED", "true").strip().lower() == "true"
MAX_POSITIONS = int(os.getenv("SWING_MOMENTUM_MAX_POSITIONS", str(config.MAX_CONCURRENT_TRADES)))
POSITIONS_FILE = Path(os.getenv("SWING_MOMENTUM_POSITIONS_FILE", "data/swing_momentum_paper_positions.json"))
TRADES_LOG = "swing_momentum_paper_trades"
EVENTS_LOG = "swing_momentum_events"
RESUBSCRIBE_SECONDS = 60

_positions: dict[str, Optional[Position]] = {}     # symbol -> Position (None = entry in progress)
_entry_regime: dict[str, dict] = {}                 # symbol -> RegimeReading.as_dict() at entry
_lock = asyncio.Lock()
_consumed: dict[str, Optional[int]] = {}            # own re-entry state (never Swing's)
_consumed_candle: dict[str, Optional[datetime]] = {}
_seen: dict[tuple, object] = {}                      # (symbol, direction, candle) -> RegimeReading | "skip"
_resub = {"at": 0.0}


# --------------------------------------------------------------------------- #
# Persistence + logs
# --------------------------------------------------------------------------- #
def _save_locked() -> None:
    try:
        POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = POSITIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"positions": [spe._to_json(p) for p in _positions.values() if p is not None],
                                   "entry_regime": _entry_regime,
                                   "consumed": {s: [v, (_consumed_candle.get(s).isoformat()
                                                        if _consumed_candle.get(s) else None)]
                                                for s, v in _consumed.items() if v is not None}}, indent=2))
        os.replace(tmp, POSITIONS_FILE)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not persist positions to %s", STRATEGY, POSITIONS_FILE)


def load_positions() -> list[Position]:
    """Startup (main.py lifespan): restore open positions + re-entry state. Never blocks startup."""
    if not POSITIONS_FILE.exists():
        return []
    try:
        d = json.loads(POSITIONS_FILE.read_text())
        restored = [spe._from_json(x) for x in d.get("positions") or []]
        _entry_regime.update(d.get("entry_regime") or {})
        for s, (side, candle) in (d.get("consumed") or {}).items():
            _consumed[s] = side
            _consumed_candle[s] = datetime.fromisoformat(candle) if candle else None
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not load %s - starting with none", STRATEGY, POSITIONS_FILE)
        return []
    _positions.clear()
    _positions.update({p.underlying_symbol: p for p in restored})
    if restored:
        logger.info("[%s] restored %d open PAPER position(s): %s", STRATEGY, len(restored), sorted(_positions))
    return restored


async def _append(name: str, record: dict) -> None:
    try:
        await asyncio.get_running_loop().run_in_executor(None, trade_history.append_jsonl, name, record)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not append to %s", STRATEGY, name)


async def _event(event: str, symbol: str, detail: dict) -> None:
    await _append(EVENTS_LOG, {"event": event, "strategy": STRATEGY, "underlying_symbol": symbol,
                               "logged_at": swing_te._now_ist().isoformat(), **detail})


# --------------------------------------------------------------------------- #
# Signal (Swing's, read-only) + own re-entry state + the regime gate
# --------------------------------------------------------------------------- #
def _release(symbol: str, is_bullish: bool, st) -> None:
    """Same rule as Swing/signals.release_regime_entry_side, on this strategy's own state."""
    side = _consumed.get(symbol)
    if side is None:
        return
    entry_candle = _consumed_candle.get(symbol)
    later = st is not None and st.candle_start is not None and (entry_candle is None or st.candle_start > entry_candle)
    if ((1 if is_bullish else -1) != side or (later and (1 if st.is_above else -1) != side)
            or (later and (st.crossed_above if side == 1 else st.crossed_below))):
        _consumed[symbol] = None


async def _signal(symbol: str) -> Optional[tuple]:
    reg = await signals.get_regime_state(symbol)
    if reg is None:
        return None
    st = await signals.get_supertrend_state(symbol)
    _release(symbol, reg.is_bullish, st)
    if st is None:
        return None
    direction = await swing_te._entry_direction(symbol, reg, st)
    if not direction or _consumed.get(symbol) == (1 if direction == "BULLISH" else -1):
        return None
    key = (symbol, direction, st.candle_start)
    reading = _seen.get(key)
    if reading is None:
        try:
            reading = await asyncio.get_running_loop().run_in_executor(None, regime.read, symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: regime read failed - signal allowed (fail open)", STRATEGY, symbol)
            reading = regime.RegimeReading(state="UNKNOWN", allows_entry=True, er_2h=None, er_today=None,
                                           reasons=("read failed",))
        if len(_seen) > 5000:
            _seen.clear()
        _seen[key] = reading
        await _event("SIGNAL", symbol, {"direction": direction, "taken_if_possible": reading.allows_entry,
                                        "signal_candle": st.candle_start.isoformat() if st.candle_start else None,
                                        **reading.as_dict()})
        logger.info("[%s] %s: %s signal - regime %s%s", STRATEGY, symbol, direction, reading.state,
                    "" if reading.allows_entry else " -> skipped")
    if not reading.allows_entry:
        return None
    return direction, st, reading


# --------------------------------------------------------------------------- #
# Entry (paper) - Swing paper's own steps, on this book
# --------------------------------------------------------------------------- #
async def _enter(symbol: str, direction: str, st, reading) -> None:
    async with _lock:
        if symbol in _positions or len(_positions) >= MAX_POSITIONS:
            return
        _positions[symbol] = None
    try:
        res = await _open(symbol, direction, st, reading)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] %s: entry failed", STRATEGY, symbol)
        res = {"status": "error", "reason": str(exc)}
    if res.get("status") != "entered":
        async with _lock:
            if _positions.get(symbol) is None:
                _positions.pop(symbol, None)
        key = (symbol, direction, st.candle_start, res.get("reason"))
        if key not in _seen:                         # one skip event per signal candle and reason
            _seen[key] = "skip"
            await _event("ENTRY_SKIPPED", symbol, {"direction": direction, **res})


async def _open(symbol: str, direction: str, st, reading) -> dict:
    vol = st.volume_ratio
    if config.NSE_VOLUME_FLOOR_GATE_ENABLED and vol is not None and vol < config.NSE_VOLUME_FLOOR_RATIO_MIN:
        return {"status": "skipped", "reason": "nse_volume_floor_gate", "vol_ratio": vol}
    side = resolve_instrument_side("OPTIONS", direction)
    option_type = resolved_option_type_for("OPTIONS", direction)
    loop = asyncio.get_running_loop()
    atm = await loop.run_in_executor(None, dhan_wrapper.get_liquid_atm_option, symbol, option_type)
    if atm is None:
        return {"status": "skipped", "reason": "no_liquid_contract_available"}
    if atm.expiry_date == swing_te._now_ist().date():
        return {"status": "skipped", "reason": "expiry_day_contract", "trading_symbol": atm.trading_symbol}
    qty = atm.lot_size * config.QUANTITY_LOTS
    if config.FUNDS_CHECK_ENABLED:
        try:
            price = await dhan_wrapper.get_option_ltp_async(atm.trading_symbol)
            ok = await fund_allocation.has_sufficient_bucket_funds(
                config.FUND_BUCKET, symbol, [(atm.security_id, config.OPTIONS_PRODUCT, qty, price, "NSE_FNO")],
                buffer_rs=config.FUNDS_CHECK_BUFFER_RS)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: funds check failed - proceeding (paper)", STRATEGY, symbol)
            ok = True
        if not ok:
            return {"status": "skipped", "reason": "insufficient_funds", "trading_symbol": atm.trading_symbol}
    await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, atm.trading_symbol)
    fill = await dhan_wrapper.get_option_ltp_async(atm.trading_symbol)
    if not fill:
        return {"status": "error", "reason": "no_ltp_available", "trading_symbol": atm.trading_symbol}
    position = Position(
        underlying_symbol=symbol, trading_symbol=atm.trading_symbol, basket_type="OPTIONS", regime=direction,
        instrument_side=side, exchange_segment="NSE_FNO", product_type="PAPER", quantity=qty, lot_size=atm.lot_size,
        entry_price=fill, best_price=fill,
        target_price=swing_te.target_price_for(side, fill, swing_te.current_target_pct(symbol)),
        hard_stop_loss=swing_te.hard_stop_for(side, fill, config.HARD_STOP_LOSS_PCT),
        order_id="", pnl_multiplier=qty, resolved_option_type=option_type,
        supertrend_entry_candle_start=st.candle_start, stop_loss_order_id=None)
    async with _lock:
        _positions[symbol] = position
        _entry_regime[symbol] = reading.as_dict()
        _consumed[symbol] = 1 if direction == "BULLISH" else -1
        _consumed_candle[symbol] = st.candle_start
        _save_locked()
    logger.warning("[%s] %s: PAPER entry %s %s @ %.2f qty=%d (regime %s, 2h ER %s, today ER %s)", STRATEGY, symbol,
                   direction, atm.trading_symbol, fill, qty, reading.state, reading.as_dict()["er_2h"],
                   reading.as_dict()["er_today"])
    await _event("POSITION_OPENED", symbol, {"direction": direction, "trading_symbol": atm.trading_symbol,
                                             "entry_price": fill, "quantity": qty, "regime": reading.as_dict()})
    return {"status": "entered", "trading_symbol": atm.trading_symbol, "entry_price": fill}


# --------------------------------------------------------------------------- #
# Exits - Swing's own ladder and square-offs (read-only helpers), on this book
# --------------------------------------------------------------------------- #
async def _exit_one(symbol: str, position: Position, exit_price: float, reason: str) -> None:
    async with _lock:
        if _positions.get(symbol) is not position:
            return
        _positions.pop(symbol, None)
        at_entry = _entry_regime.pop(symbol, None)
        _save_locked()
    position.exit_price, position.exit_reason = exit_price, reason
    position.closed_at, position.status = swing_te._now_ist(), "CLOSED"
    pnl = swing_te.unrealized_pnl_rs(position.instrument_side, position.entry_price, exit_price, position.pnl_multiplier)
    pnl_mod = round(spe.modeled_pnl(position.instrument_side, position.entry_price, exit_price, position.pnl_multiplier,
                                    position.basket_type), 2)
    logger.warning("[%s] %s: PAPER exit %s @ %.2f reason=%s pnl=%+.2f pnl_modeled=%+.2f", STRATEGY, symbol,
                   position.trading_symbol, exit_price, reason, pnl, pnl_mod)
    await _append(TRADES_LOG, {
        "strategy": STRATEGY, "underlying_symbol": symbol, "option_trading_symbol": position.trading_symbol,
        "option_type": position.option_type, "basket_type": position.basket_type,
        "instrument_side": position.instrument_side, "quantity": position.quantity,
        "pnl_multiplier": position.pnl_multiplier, "product_type": "PAPER", "entry_price": position.entry_price,
        "exit_price": exit_price, "exit_reason": reason, "pnl": pnl, "pnl_modeled": pnl_mod,
        "opened_at": position.opened_at.isoformat() if position.opened_at else None,
        "closed_at": position.closed_at.isoformat(), "mode": "paper", "regime_at_entry": at_entry,
        "logged_at": swing_te._now_ist().isoformat()})
    await _event("POSITION_CLOSED", symbol, {"trading_symbol": position.trading_symbol, "exit_price": exit_price,
                                             "exit_reason": reason, "pnl": pnl, "pnl_modeled": pnl_mod})


async def _check_one(symbol: str, position: Position) -> None:
    try:
        ltp = await swing_te._get_ltp(position)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not fetch LTP for %s", STRATEGY, position.trading_symbol)
        return
    if ltp is None:
        return
    if swing_te.is_more_favorable(position.instrument_side, ltp, position.best_price):
        position.best_price = ltp
        async with _lock:
            _save_locked()
    if config.FRIDAY_SQUARE_OFF_ENABLED and swing_te._is_friday_square_off_time():
        await _exit_one(symbol, position, ltp, "FRIDAY_SQUARE_OFF")
        return
    if await swing_te._expires_today_and_due(position):
        await _exit_one(symbol, position, ltp, "EXPIRY_DAY_SQUARE_OFF")
        return
    reason = swing_te._exit_reason_for(position, ltp) or await swing_te._evaluate_exit_signal(symbol, position)
    if reason:
        await _exit_one(symbol, position, ltp, reason)


async def _resubscribe_open() -> None:
    """Re-add an open contract the feed dropped (another book's unsubscribe of the same contract)."""
    if time.monotonic() - _resub["at"] < RESUBSCRIBE_SECONDS:
        return
    _resub["at"] = time.monotonic()
    held = {i[1] for i in getattr(dhan_wrapper, "_market_feed_instruments", set())}
    for pos in [p for p in _positions.values() if p is not None]:
        try:
            meta = dhan_wrapper._instrument_meta(pos.trading_symbol, expected_exchange="NSE")
            if meta["security_id"] not in held:
                await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.subscribe_option_price,
                                                                 pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] could not re-subscribe %s", STRATEGY, pos.trading_symbol)


# --------------------------------------------------------------------------- #
async def _tick() -> None:
    async with _lock:
        snapshot = [(s, p) for s, p in _positions.items() if p is not None]
    await asyncio.gather(*[_check_one(s, p) for s, p in snapshot])
    await _resubscribe_open()
    if not (ENABLED and config.STRATEGY_ENABLED and config.ENTRY_ENABLED):
        return
    if config.FRIDAY_SQUARE_OFF_ENABLED and swing_te._is_friday_square_off_time():
        return
    symbols = [s for s in await watchlist_store.symbols()
               if s not in config.INDEX_SYMBOLS and not dhan_wrapper.is_mcx_commodity(s)]
    for i, symbol in enumerate(symbols):
        if symbol in _positions or len(_positions) >= MAX_POSITIONS:
            continue
        if not signals._symbol_market_open(symbol):
            continue
        if i and not signals.is_symbol_ws_fresh(symbol):
            await asyncio.sleep(config.SYMBOL_PACING_SECONDS)
        try:
            sig = await _signal(symbol)
            if sig:
                await _enter(symbol, *sig)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: signal/entry failed", STRATEGY, symbol)


async def monitor_loop() -> None:
    logger.info("[%s] PAPER monitor loop started (enabled=%s, max positions=%d)", STRATEGY, ENABLED, MAX_POSITIONS)
    while True:
        try:
            if dhan_wrapper.is_market_open():
                await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[%s] monitor tick failed", STRATEGY)
        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)


async def snapshot() -> dict:
    async with _lock:
        items = [(s, p) for s, p in _positions.items() if p is not None]
    return {s: {"trading_symbol": p.trading_symbol, "direction": p.regime, "entry_price": p.entry_price,
                "best_price": p.best_price, "quantity": p.quantity,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                "regime_at_entry": _entry_regime.get(s)} for s, p in items}
