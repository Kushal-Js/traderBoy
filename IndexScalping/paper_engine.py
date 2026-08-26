"""
Paper-trading engine for the index scalping strategy - PAPER ONLY.

SAFETY INVARIANT: this module must NEVER call `dhan_wrapper.place_market_order`
(the only real order-placement entry point in Options/dhan_client.py) or
`dhan_wrapper.client.order_placement` directly. Every Dhan/Tradehull call
in this file is read-only: `instruments()`, `ATM_Strike_Selection`,
`intraday_minute_data`, `historical_daily_data`, `get_option_ltp`. A
"PAPER ENTRY"/"PAPER EXIT" log line and an in-memory/on-disk trade record
are the only side effects - no broker interaction ever results from a
signal firing. If this strategy is ever promoted to placing real orders,
that is a deliberate, separate, explicitly-requested change - not
something this file does or should be modified to do casually.

Rules implemented: see config.py's module docstring for the full
CE/PE entry/exit rules and the assumptions made where they were
underspecified (RSI/open timeframe, plain-state vs. edge-detected
"crossed", still-forming-candle handling).

Deliberately imports the ALREADY-authenticated Options.dhan_client
singleton rather than creating a second Dhan connection/WebSocket/
instrument-master download - broker connectivity is genuinely shared
infrastructure, not options-specific, even though the module still lives
under Options/ today. See index_main.py's docstring for why this is
REST-polling rather than tick-driven.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from Options.dhan_client import dhan_wrapper, _compute_supertrend

from . import config

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("index_scalping")


@dataclass
class PaperPosition:
    underlying: str
    option_type: str
    trading_symbol: str
    quantity: int
    entry_time: datetime
    entry_price: float


@dataclass
class IndexState:
    underlying: str
    security_id: str
    gate_date: Optional[object] = None
    bullish_gate: Optional[bool] = None
    bearish_gate: Optional[bool] = None
    open_position: Optional[PaperPosition] = None


class PaperTradeStore:
    """In-memory completed-trade history + append-only on-disk log
    (config.PAPER_LOG_PATH, JSONL - one completed trade per line) so a
    multi-week paper-trading run survives a process restart, unlike the
    live options bot's in-memory-only state (which relies on the broker
    itself as the source of truth to reconcile from - there's no
    equivalent "broker" to reconcile paper trades from)."""

    def __init__(self) -> None:
        self.completed: List[dict] = []
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        try:
            with open(config.PAPER_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.completed.append(json.loads(line))
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Could not load existing paper trade log - starting fresh in memory (file untouched).")

    def record(self, trade: dict) -> None:
        self.completed.append(trade)
        try:
            with open(config.PAPER_LOG_PATH, "a") as f:
                f.write(json.dumps(trade, default=str) + "\n")
        except Exception:  # noqa: BLE001
            logger.exception("Could not persist paper trade to disk - kept in memory only for this run.")

    def snapshot(self, states: Dict[str, IndexState], limit: int = 50) -> dict:
        recent = list(reversed(self.completed))[:limit]
        gross_total = sum(t["gross_pnl"] for t in self.completed)
        net_total = sum(t["net_pnl"] for t in self.completed)
        wins = sum(1 for t in self.completed if t["net_pnl"] > 0)
        return {
            "paper_trading_only": config.PAPER_TRADING_ONLY,
            "total_completed_trades": len(self.completed),
            "gross_pnl_total": gross_total,
            "net_pnl_total": net_total,
            "win_rate_net": (wins / len(self.completed)) if self.completed else None,
            "daily_gate": {
                u: {"bullish": s.bullish_gate, "bearish": s.bearish_gate} for u, s in states.items()
            },
            "open_positions": {
                u: vars(s.open_position) for u, s in states.items() if s.open_position
            },
            "recent_trades": recent,
        }


paper_trade_store = PaperTradeStore()


def _compute_rsi(closes: list[float], period: int) -> list[Optional[float]]:
    """Standard RSI(period), Wilder smoothing - see CopperOptions/paper_engine.py's
    identical helper. Pure function, duplicated rather than shared across
    packages per this repo's existing per-package independence convention."""
    n = len(closes)
    rsi: list[Optional[float]] = [None] * n
    if n < period + 1:
        return rsi
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        gain, loss = gains[i - 1], losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return rsi


def _fetch_index_daily(security_id: str) -> dict:
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    from_str = (datetime.now(IST) - timedelta(days=45)).strftime("%Y-%m-%d")
    resp = dhan_wrapper.client.Dhan.historical_daily_data(
        security_id=security_id, exchange_segment="IDX_I", instrument_type="INDEX",
        from_date=from_str, to_date=today_str,
    )
    return resp.get("data") or {}


def _fetch_index_intraday(security_id: str, date_str: str, interval: int) -> dict:
    resp = dhan_wrapper.client.Dhan.intraday_minute_data(
        security_id=security_id, exchange_segment="IDX_I", instrument_type="INDEX",
        from_date=date_str, to_date=date_str, interval=interval,
    )
    return resp.get("data") or {}


def _lot_size_for(trading_symbol: str) -> Optional[int]:
    df = dhan_wrapper.instruments()
    row = df[df["SEM_CUSTOM_SYMBOL"] == trading_symbol]
    if row.empty:
        return None
    return int(float(row.iloc[0]["SEM_LOT_UNITS"]))


def _drop_forming_bar(bar_times: list[datetime], highs: list[float], lows: list[float],
                       closes: list[float], interval_minutes: int, now: datetime) -> tuple:
    """Drops the last candle if it hasn't reached its own close time yet -
    same reasoning as Options/dhan_client.py's refresh_supertrend_signal.
    Without this, a Supertrend/crossover computed against a still-forming
    bar can flicker true/false multiple times within that same minute as
    new ticks arrive."""
    if bar_times and now < bar_times[-1] + timedelta(minutes=interval_minutes):
        return bar_times[:-1], highs[:-1], lows[:-1], closes[:-1]
    return bar_times, highs, lows, closes


def _crossed(closes: list[float], st: list[Optional[float]], above: bool) -> bool:
    """Edge-detected crossover against the prior confirmed bar - True only
    on the bar where the close was on the wrong side of the Supertrend the
    PRIOR bar and is on the right side THIS bar. See config.py's docstring
    for why this (not a plain state check) is used for the 1-min
    condition specifically."""
    if len(closes) < 2 or st[-1] is None or st[-2] is None:
        return False
    if above:
        return closes[-2] <= st[-2] and closes[-1] > st[-1]
    return closes[-2] >= st[-2] and closes[-1] < st[-1]


async def _record_exit(state: IndexState, exit_ltp: float, reason: str, now: datetime) -> None:
    pos = state.open_position
    exit_price = exit_ltp * (1 - config.SLIPPAGE_PCT)
    gross_pnl = (exit_price - pos.entry_price) * pos.quantity
    net_pnl = gross_pnl - config.ROUND_TRIP_COST_RS
    trade = {
        "date": pos.entry_time.date().isoformat(), "underlying": pos.underlying,
        "option_type": pos.option_type, "trading_symbol": pos.trading_symbol,
        "entry_time": pos.entry_time.isoformat(), "entry_price": pos.entry_price,
        "exit_time": now.isoformat(), "exit_price": exit_price, "exit_reason": reason,
        "quantity": pos.quantity, "gross_pnl": gross_pnl, "net_pnl": net_pnl,
    }
    paper_trade_store.record(trade)
    logger.info(
        "PAPER EXIT (no real order placed) %s %s %s reason=%s net_pnl=%.2f",
        pos.underlying, pos.option_type, pos.trading_symbol, reason, net_pnl,
    )
    state.open_position = None


async def _poll_one_index(loop: asyncio.AbstractEventLoop, state: IndexState) -> None:
    now = datetime.now(IST)
    today = now.date()

    if state.gate_date != today:
        state.gate_date = today
        state.bullish_gate = None
        state.bearish_gate = None
        if state.open_position:
            logger.warning(
                "%s: paper position from a previous day never closed (%s) - "
                "force-closing at its own entry price rather than losing track of it.",
                state.underlying, state.open_position.trading_symbol,
            )
            await _record_exit(state, state.open_position.entry_price, "STALE_CARRYOVER", now)

    market_open_dt = datetime.combine(today, dtime.fromisoformat(config.MARKET_OPEN), tzinfo=IST)
    square_off_dt = datetime.combine(today, dtime.fromisoformat(config.SQUARE_OFF_TIME), tzinfo=IST)

    if now > square_off_dt and state.open_position:
        try:
            ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, state.open_position.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: EOD paper LTP fetch failed", state.underlying)
            return
        await _record_exit(state, ltp, "EOD_SQUARE_OFF", now)
        return

    if now < market_open_dt or now > square_off_dt:
        return

    if state.bullish_gate is None:
        try:
            daily = await loop.run_in_executor(None, _fetch_index_daily, state.security_id)
        except Exception:  # noqa: BLE001
            logger.exception("%s: daily candle fetch failed", state.underlying)
            return
        opens, closes_d = daily.get("open") or [], daily.get("close") or []
        if len(closes_d) < config.RSI_PERIOD + 2:
            return
        rsi = _compute_rsi(closes_d, config.RSI_PERIOD)
        if rsi[-1] is None or rsi[-2] is None:
            return
        today_open, yesterday_close = opens[-1], closes_d[-2]
        today_rsi, yesterday_rsi = rsi[-1], rsi[-2]
        state.bullish_gate = today_open > yesterday_close and today_rsi > yesterday_rsi
        state.bearish_gate = today_open < yesterday_close and today_rsi < yesterday_rsi
        logger.info(
            "%s daily gate: bullish=%s bearish=%s (open=%.2f prev_close=%.2f rsi=%.2f prev_rsi=%.2f)",
            state.underlying, state.bullish_gate, state.bearish_gate,
            today_open, yesterday_close, today_rsi, yesterday_rsi,
        )

    try:
        candles_5m = await loop.run_in_executor(
            None, _fetch_index_intraday, state.security_id, today.isoformat(), 5)
        candles_1m = await loop.run_in_executor(
            None, _fetch_index_intraday, state.security_id, today.isoformat(), 1)
    except Exception:  # noqa: BLE001
        logger.exception("%s: index candle fetch failed", state.underlying)
        return

    t5 = [datetime.fromtimestamp(e, tz=IST) for e in (candles_5m.get("timestamp") or [])]
    t5, h5, l5, c5 = _drop_forming_bar(
        t5, candles_5m.get("high") or [], candles_5m.get("low") or [], candles_5m.get("close") or [], 5, now)
    if len(c5) < config.SUPERTREND_5MIN_PERIOD + 1:
        return
    st5 = _compute_supertrend(h5, l5, c5, period=config.SUPERTREND_5MIN_PERIOD,
                               multiplier=config.SUPERTREND_5MIN_MULTIPLIER)
    if st5[-1] is None:
        return
    close5 = c5[-1]

    t1 = [datetime.fromtimestamp(e, tz=IST) for e in (candles_1m.get("timestamp") or [])]
    t1, h1, l1, c1 = _drop_forming_bar(
        t1, candles_1m.get("high") or [], candles_1m.get("low") or [], candles_1m.get("close") or [], 1, now)
    if len(c1) < config.SUPERTREND_1MIN_PERIOD + 2:
        return
    st1 = _compute_supertrend(h1, l1, c1, period=config.SUPERTREND_1MIN_PERIOD,
                               multiplier=config.SUPERTREND_1MIN_MULTIPLIER)
    crossed_above_1min = _crossed(c1, st1, above=True)
    crossed_below_1min = _crossed(c1, st1, above=False)

    if state.open_position is not None:
        pos = state.open_position
        try:
            ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: paper LTP fetch failed for open position", state.underlying)
            return
        unrealized = (ltp - pos.entry_price) * pos.quantity
        exit_signal = crossed_below_1min if pos.option_type == "CE" else crossed_above_1min
        if exit_signal:
            await _record_exit(state, ltp, "SUPERTREND_EXIT", now)
        elif unrealized < -config.MAX_LOSS_RS:
            await _record_exit(state, ltp, "MAX_LOSS_HIT", now)
        return  # one position at a time per index

    option_type = None
    if state.bullish_gate and close5 > st5[-1] and crossed_above_1min:
        option_type = "CE"
    elif state.bearish_gate and close5 < st5[-1] and crossed_below_1min:
        option_type = "PE"
    if option_type is None:
        return

    try:
        ce_symbol, pe_symbol, _strike = await loop.run_in_executor(
            None, dhan_wrapper.client.ATM_Strike_Selection, state.underlying, 0
        )
    except Exception:  # noqa: BLE001
        logger.exception("%s: ATM strike selection failed", state.underlying)
        return
    trading_symbol = ce_symbol if option_type == "CE" else pe_symbol
    if not trading_symbol:
        return

    try:
        raw_ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, trading_symbol)
        lot_size = await loop.run_in_executor(None, _lot_size_for, trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception("%s: paper entry pricing failed for %s", state.underlying, trading_symbol)
        return
    if not lot_size:
        logger.warning("%s: no lot size resolved for %s - skipping signal", state.underlying, trading_symbol)
        return

    entry_price = raw_ltp * (1 + config.SLIPPAGE_PCT)
    state.open_position = PaperPosition(
        underlying=state.underlying, option_type=option_type, trading_symbol=trading_symbol,
        quantity=lot_size * config.QUANTITY_LOTS, entry_time=now, entry_price=entry_price,
    )
    logger.info(
        "PAPER ENTRY (no real order placed) %s %s %s @ %.2f",
        state.underlying, option_type, trading_symbol, entry_price,
    )


_states: Dict[str, IndexState] = {
    u: IndexState(underlying=u, security_id=sid) for u, sid in config.INDEX_SECURITY_ID.items()
}


async def poll_loop() -> None:
    assert config.PAPER_TRADING_ONLY, "Refusing to start: PAPER_TRADING_ONLY must stay True for this engine."
    loop = asyncio.get_running_loop()
    logger.info("Index scalping paper-trading poll loop started (PAPER ONLY - no real orders will be placed).")
    while True:
        for state in _states.values():
            try:
                await _poll_one_index(loop, state)
            except Exception:  # noqa: BLE001
                logger.exception("%s: unhandled error in paper-trading poll", state.underlying)
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)


def snapshot() -> dict:
    return paper_trade_store.snapshot(_states)
