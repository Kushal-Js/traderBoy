"""
Paper-trading engine for the index scalping strategy - PAPER ONLY.

SAFETY INVARIANT: this module must NEVER call `dhan_wrapper.place_market_order`
(the only real order-placement entry point in Options/dhan_client.py) or
`dhan_wrapper.client.order_placement` directly. Every Dhan/Tradehull call
in this file is read-only: `instruments()`, `ATM_Strike_Selection`,
`intraday_minute_data`, `get_option_ltp`. A "PAPER ENTRY"/"PAPER EXIT"
log line and an in-memory/on-disk trade record are the only side effects
- no broker interaction ever results from a signal firing. If this
strategy is ever promoted to placing real orders, that is a deliberate,
separate, explicitly-requested change - not something this file does or
should be modified to do casually.

Same signal/exit logic already backtested (see NOTES.md's index-scalping
entry and BACKTEST_RESULTS.md): opening-range breakout + short EMA
momentum on the index's own 1-min candles, buy ATM CE/PE, exit on a
tight target/stop (on the option's own premium) or a hard time-box,
whichever comes first. Reused here as a live polling loop rather than a
backtest replay - see index_main.py's docstring for why this is
REST-polling rather than tick-driven.

Deliberately imports the ALREADY-authenticated Options.dhan_client
singleton rather than creating a second Dhan connection/WebSocket/
instrument-master download - broker connectivity is genuinely shared
infrastructure, not options-specific, even though the module still lives
under Options/ today. Worth promoting to a real shared location if a
third strategy ever needs it too - not done now to avoid touching a
live, real-money-integrated module for a paper-only feature.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from Options.dhan_client import dhan_wrapper

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
    target_price: float
    stop_price: float
    hold_until: datetime
    signal_bar_time: datetime


@dataclass
class IndexState:
    underlying: str
    security_id: str
    trades_today_date: Optional[object] = None
    trades_today: int = 0
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    last_acted_bar_time: Optional[datetime] = None
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
            "open_positions": {
                u: vars(s.open_position) for u, s in states.items() if s.open_position
            },
            "recent_trades": recent,
        }


paper_trade_store = PaperTradeStore()


def _compute_ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _fetch_index_candles(security_id: str, date_str: str) -> dict:
    resp = dhan_wrapper.client.Dhan.intraday_minute_data(
        security_id=security_id, exchange_segment="IDX_I", instrument_type="INDEX",
        from_date=date_str, to_date=date_str, interval=1,
    )
    return resp.get("data") or {}


def _lot_size_for(trading_symbol: str) -> Optional[int]:
    df = dhan_wrapper.instruments()
    row = df[df["SEM_CUSTOM_SYMBOL"] == trading_symbol]
    if row.empty:
        return None
    return int(float(row.iloc[0]["SEM_LOT_UNITS"]))


async def _record_exit(state: IndexState, exit_ltp: float, reason: str, now: datetime) -> None:
    pos = state.open_position
    exit_price = exit_ltp * (1 - config.SLIPPAGE_PCT)
    gross_pnl = (exit_price - pos.entry_price) * pos.quantity
    net_pnl = gross_pnl - config.ROUND_TRIP_COST_RS
    trade = {
        "date": pos.entry_time.date().isoformat(), "underlying": pos.underlying,
        "option_type": pos.option_type, "trading_symbol": pos.trading_symbol,
        "signal_time": pos.signal_bar_time.isoformat(), "entry_time": pos.entry_time.isoformat(),
        "entry_price": pos.entry_price, "exit_time": now.isoformat(), "exit_price": exit_price,
        "exit_reason": reason, "quantity": pos.quantity, "gross_pnl": gross_pnl, "net_pnl": net_pnl,
    }
    paper_trade_store.record(trade)
    logger.info(
        "PAPER EXIT (no real order placed) %s %s %s reason=%s net_pnl=%.2f",
        pos.underlying, pos.option_type, pos.trading_symbol, reason, net_pnl,
    )
    state.open_position = None
    state.trades_today += 1


async def _poll_one_index(loop: asyncio.AbstractEventLoop, state: IndexState) -> None:
    now = datetime.now(IST)
    today = now.date()

    if state.trades_today_date != today:
        state.trades_today_date = today
        state.trades_today = 0
        state.or_high = state.or_low = None
        state.last_acted_bar_time = None
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

    try:
        candles = await loop.run_in_executor(None, _fetch_index_candles, state.security_id, today.isoformat())
    except Exception:  # noqa: BLE001
        logger.exception("%s: index candle fetch failed", state.underlying)
        return

    ts = candles.get("timestamp") or []
    if not ts:
        return
    highs, lows, closes = candles.get("high") or [], candles.get("low") or [], candles.get("close") or []
    bar_times = [datetime.fromtimestamp(e, tz=IST) for e in ts]

    if state.or_high is None:
        or_end = market_open_dt + timedelta(minutes=config.OPENING_RANGE_MINUTES)
        if bar_times[-1] < or_end:
            return  # opening range still forming
        or_bars = [i for i, t in enumerate(bar_times) if t < or_end]
        if not or_bars:
            return
        state.or_high = max(highs[i] for i in or_bars)
        state.or_low = min(lows[i] for i in or_bars)
        logger.info("%s: opening range set high=%.2f low=%.2f", state.underlying, state.or_high, state.or_low)

    if state.open_position is not None:
        pos = state.open_position
        try:
            ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: paper LTP fetch failed for open position", state.underlying)
            return
        if ltp >= pos.target_price:
            await _record_exit(state, ltp, "TARGET_HIT", now)
        elif ltp <= pos.stop_price:
            await _record_exit(state, ltp, "STOP_LOSS_HIT", now)
        elif now >= pos.hold_until:
            await _record_exit(state, ltp, "TIME_EXIT", now)
        return  # one position at a time per index

    if state.trades_today >= config.MAX_TRADES_PER_DAY:
        return
    if len(closes) < config.EMA_SLOW_PERIOD + 1:
        return

    ema_fast = _compute_ema(closes, config.EMA_FAST_PERIOD)
    ema_slow = _compute_ema(closes, config.EMA_SLOW_PERIOD)
    last_i = len(closes) - 1
    last_bar_time = bar_times[last_i]
    if state.last_acted_bar_time is not None and last_bar_time <= state.last_acted_bar_time:
        return  # already evaluated this bar - wait for the next one

    close = closes[last_i]
    bullish = close > state.or_high and ema_fast[last_i] > ema_slow[last_i]
    bearish = close < state.or_low and ema_fast[last_i] < ema_slow[last_i]
    option_type = "CE" if bullish else ("PE" if bearish else None)
    state.last_acted_bar_time = last_bar_time
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
        target_price=entry_price * (1 + config.TARGET_PCT),
        stop_price=entry_price * (1 - config.STOP_LOSS_PCT),
        hold_until=now + timedelta(minutes=config.MAX_HOLD_MINUTES),
        signal_bar_time=last_bar_time,
    )
    logger.info(
        "PAPER ENTRY (no real order placed) %s %s %s @ %.2f target=%.2f stop=%.2f",
        state.underlying, option_type, trading_symbol, entry_price,
        state.open_position.target_price, state.open_position.stop_price,
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
