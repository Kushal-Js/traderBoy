"""
Paper-trading engine for the Copper (MCX) options-buying strategy -
PAPER ONLY.

SAFETY INVARIANT: this module must NEVER call `dhan_wrapper.place_market_order`
(the only real order-placement entry point in Options/dhan_client.py) or
`dhan_wrapper.client.order_placement` directly. Every Dhan/Tradehull call
here is read-only: `instruments()`, `intraday_minute_data`,
`historical_daily_data`, `get_option_ltp`. A "PAPER ENTRY"/"PAPER EXIT"
log line and an in-memory/on-disk trade record are the only side effects.

Rules implemented (see config.py's module docstring for the assumptions
made where the given rules were underspecified - strike-offset direction
for PE, RSI/open timeframe, and the market-close time assumption):

  CE entry: today's open > yesterday's close AND today's RSI(14) >
            yesterday's RSI(14) [both daily, on the underlying Copper
            futures contract] AND the future's 5-min close is above BOTH
            Supertrend(12,3) and Supertrend(11,2).
  PE entry: the exact mirror (open <, RSI <, close below both Supertrends).
  Exit (either side): the future's 5-min close crosses back through
            Supertrend(12,3), or the paper position's unrealized loss
            exceeds config.MAX_LOSS_RS - whichever comes first. Also
            force-closed at config.MARKET_CLOSE_TIME regardless.

Active only between config.STRATEGY_START_TIME and
config.MARKET_CLOSE_TIME, and only at all if config.STRATEGY_ENABLED is
True - the on/off switch requested for after paper results are in.

Options are on FUTURES (MCX OPTFUT), not a spot index - the underlying
reference for every rule above is the Copper futures contract for the
chosen expiry cycle (see _resolve_expiry_cycle), not the option itself.
Automatically rolls to the next monthly cycle if the nearest one has
fewer than config.MIN_DAYS_TO_EXPIRY days left - the nearest cycle at
the time this was built expires the day after first deploy, which would
otherwise mean trading a same-day-expiry contract.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from Options.dhan_client import dhan_wrapper, _compute_supertrend

from . import config

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("copper_options")


@dataclass
class CopperPaperPosition:
    option_type: str
    trading_symbol: str
    quantity: int
    entry_time: datetime
    entry_price: float


class _State:
    gate_date: Optional[object] = None
    bullish_gate: Optional[bool] = None
    bearish_gate: Optional[bool] = None
    expiry_cycle_date: Optional[object] = None
    expiry_date: Optional[str] = None
    future_security_id: Optional[str] = None
    future_symbol: Optional[str] = None
    open_position: Optional[CopperPaperPosition] = None


_state = _State()


def _compute_rsi(closes: list[float], period: int) -> list[Optional[float]]:
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


def _resolve_expiry_cycle(today) -> Optional[tuple]:
    df = dhan_wrapper.instruments()
    opts = df[
        (df["SEM_EXM_EXCH_ID"] == "MCX")
        & (df["SEM_INSTRUMENT_NAME"] == "OPTFUT")
        & (df["SEM_TRADING_SYMBOL"].str.startswith(config.UNDERLYING + "-"))
    ]
    if opts.empty:
        return None
    expiries = sorted(opts["SEM_EXPIRY_DATE"].unique())
    chosen = None
    for e in expiries:
        e_ts = pd.Timestamp(e)
        if (e_ts.date() - today).days >= config.MIN_DAYS_TO_EXPIRY:
            chosen = e
            break
    if chosen is None:
        return None
    chosen_ts = pd.Timestamp(chosen)

    futs = df[
        (df["SEM_EXM_EXCH_ID"] == "MCX")
        & (df["SEM_INSTRUMENT_NAME"] == "FUTCOM")
        & (df["SEM_TRADING_SYMBOL"].str.startswith(config.UNDERLYING + "-"))
    ]
    match = futs[futs["SEM_EXPIRY_DATE"].apply(
        lambda d: pd.Timestamp(d).month == chosen_ts.month and pd.Timestamp(d).year == chosen_ts.year
    )]
    if match.empty:
        return None
    fut_row = match.iloc[0]
    return chosen, str(int(fut_row["SEM_SMST_SECURITY_ID"])), str(fut_row["SEM_TRADING_SYMBOL"])


def _fetch_future_daily(security_id: str) -> dict:
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    from_str = (datetime.now(IST) - timedelta(days=45)).strftime("%Y-%m-%d")
    resp = dhan_wrapper.client.Dhan.historical_daily_data(
        security_id=security_id, exchange_segment="MCX_COMM", instrument_type="FUTCOM",
        from_date=from_str, to_date=today_str,
    )
    return resp.get("data") or {}


def _fetch_future_5min(security_id: str) -> dict:
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    resp = dhan_wrapper.client.Dhan.intraday_minute_data(
        security_id=security_id, exchange_segment="MCX_COMM", instrument_type="FUTCOM",
        from_date=today_str, to_date=today_str, interval=config.SUPERTREND_INTERVAL_MINUTES,
    )
    return resp.get("data") or {}


def _resolve_option_row(option_type: str, future_price: float):
    df = dhan_wrapper.instruments()
    opts = df[
        (df["SEM_EXM_EXCH_ID"] == "MCX")
        & (df["SEM_INSTRUMENT_NAME"] == "OPTFUT")
        & (df["SEM_TRADING_SYMBOL"].str.startswith(config.UNDERLYING + "-"))
        & (df["SEM_OPTION_TYPE"] == option_type)
        & (df["SEM_EXPIRY_DATE"] == _state.expiry_date)
    ]
    if opts.empty:
        return None
    atm_idx = (opts["SEM_STRIKE_PRICE"] - future_price).abs().idxmin()
    atm_strike = opts.loc[atm_idx, "SEM_STRIKE_PRICE"]
    offset = config.STRIKE_OFFSET_POINTS if option_type == "CE" else -config.STRIKE_OFFSET_POINTS
    target_strike = atm_strike + offset
    idx2 = (opts["SEM_STRIKE_PRICE"] - target_strike).abs().idxmin()
    return opts.loc[idx2]


class PaperTradeStore:
    def __init__(self) -> None:
        self.completed: list[dict] = []
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
            logger.exception("Could not load existing Copper paper trade log - starting fresh in memory.")

    def record(self, trade: dict) -> None:
        self.completed.append(trade)
        try:
            with open(config.PAPER_LOG_PATH, "a") as f:
                f.write(json.dumps(trade, default=str) + "\n")
        except Exception:  # noqa: BLE001
            logger.exception("Could not persist Copper paper trade to disk - kept in memory only for this run.")

    def snapshot(self, limit: int = 50) -> dict:
        recent = list(reversed(self.completed))[:limit]
        gross_total = sum(t["pnl"] for t in self.completed)
        wins = sum(1 for t in self.completed if t["pnl"] > 0)
        return {
            "strategy_enabled": config.STRATEGY_ENABLED,
            "paper_trading_only": config.PAPER_TRADING_ONLY,
            "total_completed_trades": len(self.completed),
            "pnl_total": gross_total,
            "win_rate": (wins / len(self.completed)) if self.completed else None,
            "open_position": vars(_state.open_position) if _state.open_position else None,
            "daily_gate": {"bullish": _state.bullish_gate, "bearish": _state.bearish_gate},
            "expiry_in_use": _state.expiry_date,
            "recent_trades": recent,
        }


paper_trade_store = PaperTradeStore()


async def _record_exit(exit_ltp: float, reason: str, now: datetime) -> None:
    pos = _state.open_position
    pnl = (exit_ltp - pos.entry_price) * pos.quantity
    trade = {
        "date": pos.entry_time.date().isoformat(), "option_type": pos.option_type,
        "trading_symbol": pos.trading_symbol, "entry_time": pos.entry_time.isoformat(),
        "entry_price": pos.entry_price, "exit_time": now.isoformat(), "exit_price": exit_ltp,
        "exit_reason": reason, "quantity": pos.quantity, "pnl": pnl,
    }
    paper_trade_store.record(trade)
    logger.info("COPPER PAPER EXIT (no real order placed) %s %s reason=%s pnl=%.2f",
                pos.option_type, pos.trading_symbol, reason, pnl)
    _state.open_position = None


async def _poll_copper(loop: asyncio.AbstractEventLoop) -> None:
    if not config.STRATEGY_ENABLED:
        return

    now = datetime.now(IST)
    today = now.date()

    if _state.gate_date != today:
        _state.gate_date = today
        _state.bullish_gate = None
        _state.bearish_gate = None

    start_dt = datetime.combine(today, dtime.fromisoformat(config.STRATEGY_START_TIME), tzinfo=IST)
    close_dt = datetime.combine(today, dtime.fromisoformat(config.MARKET_CLOSE_TIME), tzinfo=IST)

    if now > close_dt and _state.open_position:
        try:
            ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, _state.open_position.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("Copper: window-close LTP fetch failed")
            return
        await _record_exit(ltp, "WINDOW_CLOSE", now)
        return

    if now < start_dt or now > close_dt:
        return

    if _state.expiry_cycle_date != today:
        try:
            cycle = await loop.run_in_executor(None, _resolve_expiry_cycle, today)
        except Exception:  # noqa: BLE001
            logger.exception("Copper: expiry-cycle resolution failed")
            return
        if cycle is None:
            logger.warning("Copper: no usable expiry cycle resolved - skipping today.")
            return
        _state.expiry_date, _state.future_security_id, _state.future_symbol = cycle
        _state.expiry_cycle_date = today
        logger.info("Copper: using expiry %s, future %s", _state.expiry_date, _state.future_symbol)

    if _state.bullish_gate is None:
        try:
            daily = await loop.run_in_executor(None, _fetch_future_daily, _state.future_security_id)
        except Exception:  # noqa: BLE001
            logger.exception("Copper: daily candle fetch failed")
            return
        opens, closes_d = daily.get("open") or [], daily.get("close") or []
        if len(closes_d) < config.RSI_PERIOD + 2:
            return
        rsi = _compute_rsi(closes_d, config.RSI_PERIOD)
        if rsi[-1] is None or rsi[-2] is None:
            return
        today_open, yesterday_close = opens[-1], closes_d[-2]
        today_rsi, yesterday_rsi = rsi[-1], rsi[-2]
        _state.bullish_gate = today_open > yesterday_close and today_rsi > yesterday_rsi
        _state.bearish_gate = today_open < yesterday_close and today_rsi < yesterday_rsi
        logger.info(
            "Copper daily gate: bullish=%s bearish=%s (open=%.2f prev_close=%.2f rsi=%.2f prev_rsi=%.2f)",
            _state.bullish_gate, _state.bearish_gate, today_open, yesterday_close, today_rsi, yesterday_rsi,
        )

    try:
        candles = await loop.run_in_executor(None, _fetch_future_5min, _state.future_security_id)
    except Exception:  # noqa: BLE001
        logger.exception("Copper: 5-min candle fetch failed")
        return
    highs, lows, closes = candles.get("high") or [], candles.get("low") or [], candles.get("close") or []
    if len(closes) < config.SUPERTREND_1_PERIOD + 1 or len(closes) < config.SUPERTREND_2_PERIOD + 1:
        return
    st1 = _compute_supertrend(highs, lows, closes, period=config.SUPERTREND_1_PERIOD,
                               multiplier=config.SUPERTREND_1_MULTIPLIER)
    st2 = _compute_supertrend(highs, lows, closes, period=config.SUPERTREND_2_PERIOD,
                               multiplier=config.SUPERTREND_2_MULTIPLIER)
    if st1[-1] is None or st2[-1] is None:
        return
    future_close = closes[-1]

    if _state.open_position is not None:
        pos = _state.open_position
        try:
            ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("Copper: open-position LTP fetch failed")
            return
        unrealized = (ltp - pos.entry_price) * pos.quantity
        exit_signal = (future_close < st1[-1]) if pos.option_type == "CE" else (future_close > st1[-1])
        if exit_signal:
            await _record_exit(ltp, "SUPERTREND_EXIT", now)
        elif unrealized < -config.MAX_LOSS_RS:
            await _record_exit(ltp, "MAX_LOSS_HIT", now)
        return

    option_type = None
    if _state.bullish_gate and future_close > st1[-1] and future_close > st2[-1]:
        option_type = "CE"
    elif _state.bearish_gate and future_close < st1[-1] and future_close < st2[-1]:
        option_type = "PE"
    if option_type is None:
        return

    try:
        row = await loop.run_in_executor(None, _resolve_option_row, option_type, future_close)
    except Exception:  # noqa: BLE001
        logger.exception("Copper: option strike resolution failed")
        return
    if row is None:
        return
    trading_symbol = str(row["SEM_CUSTOM_SYMBOL"])
    lot_size = int(float(row["SEM_LOT_UNITS"]))
    try:
        ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception("Copper: entry LTP fetch failed for %s", trading_symbol)
        return

    _state.open_position = CopperPaperPosition(
        option_type=option_type, trading_symbol=trading_symbol,
        quantity=lot_size * config.QUANTITY_LOTS, entry_time=now, entry_price=ltp,
    )
    logger.info("COPPER PAPER ENTRY (no real order placed) %s %s @ %.2f", option_type, trading_symbol, ltp)


async def poll_loop() -> None:
    assert config.PAPER_TRADING_ONLY, "Refusing to start: PAPER_TRADING_ONLY must stay True for this engine."
    loop = asyncio.get_running_loop()
    logger.info(
        "Copper options paper-trading poll loop started (PAPER ONLY - no real orders will be placed). "
        "strategy_enabled=%s", config.STRATEGY_ENABLED,
    )
    while True:
        try:
            await _poll_copper(loop)
        except Exception:  # noqa: BLE001
            logger.exception("Copper: unhandled error in paper-trading poll")
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)


def snapshot() -> dict:
    return paper_trade_store.snapshot()
