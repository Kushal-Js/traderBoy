"""
Paper-trading engine for "K01", the daily F&O stock screener strategy -
PAPER ONLY.

SAFETY INVARIANT: this module must NEVER call `dhan_wrapper.place_market_order`
(the only real order-placement entry point in Options/dhan_client.py) or
`dhan_wrapper.client.order_placement` directly. Every Dhan/Tradehull call in
this file is read-only: `instruments()`, `get_atm_option`, `get_option_ltp`,
`historical_daily_data`, `intraday_minute_data`. A "PAPER ENTRY"/"PAPER EXIT"
log line and an in-memory/on-disk trade record are the only side effects -
no broker interaction ever results from a signal firing. If this strategy
is ever promoted to placing real orders, that is a deliberate, separate,
explicitly-requested change - not something this file does or should be
modified to do casually.

MVP scope (30 Aug 2026, see config.py's module docstring and
trading-skills/designs/k01.md for the full plan): Stage 0
(Minervini Trend Template) + Stage 1 (liquidity/anti-thin-option floor) run
ONCE per day, frozen, producing a WATCHLIST (not yet directional - these
two stages measure "is this stock structurally worth watching," not
bullish/bearish). Stage 3 (intraday momentum - RSI band + 5-min Supertrend
regime + 1-min Supertrend crossover + ROC sign, all four must agree) is
then re-checked every poll cycle per watchlist symbol to decide entries.
Stage 2 (OI-buildup gating) is NOT implemented yet - see config.py.

Deliberately imports the ALREADY-authenticated Options.dhan_client
singleton rather than creating a second Dhan connection - same reasoning
as IndexScalping/paper_engine.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, date as ddate, time as dtime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from Options.dhan_client import dhan_wrapper, _compute_supertrend

from . import config

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("k01")


# --------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------- #
@dataclass
class WatchlistEntry:
    symbol: str
    atr_pct: float
    avg_turnover_cr: float
    trend_detail: str          # human-readable Trend Template summary
    last_signal: Optional[str] = None       # "CE" / "PE" / None - most recent Stage 3 read
    last_signal_at: Optional[datetime] = None


@dataclass
class PaperPosition:
    symbol: str
    option_type: str
    trading_symbol: str
    quantity: int
    entry_time: datetime
    entry_price: float
    target_price: float
    hard_stop_loss: float
    highest_price: float


class PaperTradeStore:
    """In-memory completed-trade history + append-only on-disk log
    (config.PAPER_LOG_PATH, JSONL) - same pattern as IndexScalping's own
    store, so a multi-day paper-trading run survives a process restart."""

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

    def snapshot(self, watchlist: Dict[str, WatchlistEntry], open_positions: Dict[str, PaperPosition],
                 screen_date: Optional[ddate], limit: int = 50) -> dict:
        recent = list(reversed(self.completed))[:limit]
        gross_total = sum(t["pnl"] for t in self.completed)
        wins = sum(1 for t in self.completed if t["pnl"] > 0)
        return {
            "strategy": "K01",
            "strategy_enabled": config.STRATEGY_ENABLED,
            "paper_trading_only": config.PAPER_TRADING_ONLY,
            "mvp_scope_note": "Stage 2 (OI-buildup) and VCP detection not implemented yet - "
                               "momentum-only entries (RSI band + Supertrend regime/crossover + ROC sign). "
                               "See trading-skills/designs/k01.md for the phase-2 plan.",
            "screen_date": screen_date.isoformat() if screen_date else None,
            "watchlist_size": len(watchlist),
            "watchlist": {
                s: {
                    "atr_pct": round(w.atr_pct, 3), "avg_turnover_cr": round(w.avg_turnover_cr, 1),
                    "trend_detail": w.trend_detail, "last_signal": w.last_signal,
                    "last_signal_at": w.last_signal_at.isoformat() if w.last_signal_at else None,
                }
                for s, w in watchlist.items()
            },
            "total_completed_trades": len(self.completed),
            "gross_pnl_total": gross_total,
            "win_rate": (wins / len(self.completed)) if self.completed else None,
            "open_positions": {s: vars(p) for s, p in open_positions.items()},
            "recent_trades": recent,
        }


paper_trade_store = PaperTradeStore()


# --------------------------------------------------------------------- #
# Pure indicator helpers - duplicated rather than shared across packages,
# per this repo's existing per-package independence convention (see
# IndexScalping/CopperOptions's own _compute_rsi duplicates).
# --------------------------------------------------------------------- #
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


def _compute_atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[Optional[float]]:
    """Standard Wilder ATR - extracted separately from
    Options.dhan_client._compute_supertrend (which computes ATR internally
    but doesn't expose it) since Stage 1's liquidity floor needs the raw
    ATR value, not just the Supertrend line built on top of it."""
    n = len(closes)
    atr: list[Optional[float]] = [None] * n
    if n < period + 1:
        return atr
    tr = [0.0] * n
    for i in range(n):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _compute_roc(closes: list[float], period: int) -> list[Optional[float]]:
    n = len(closes)
    roc: list[Optional[float]] = [None] * n
    for i in range(period, n):
        prev = closes[i - period]
        if prev:
            roc[i] = (closes[i] - prev) / prev * 100
    return roc


def _crossed(closes: list[float], st: list[Optional[float]], above: bool) -> bool:
    """Edge-detected crossover against the prior confirmed bar - same
    helper as IndexScalping's own."""
    if len(closes) < 2 or st[-1] is None or st[-2] is None:
        return False
    if above:
        return closes[-2] <= st[-2] and closes[-1] > st[-1]
    return closes[-2] >= st[-2] and closes[-1] < st[-1]


def _drop_forming_bar(bar_times: list[datetime], highs: list[float], lows: list[float],
                       closes: list[float], interval_minutes: int, now: datetime) -> tuple:
    if bar_times and now < bar_times[-1] + timedelta(minutes=interval_minutes):
        return bar_times[:-1], highs[:-1], lows[:-1], closes[:-1]
    return bar_times, highs, lows, closes


# --------------------------------------------------------------------- #
# Data-fetch helpers (all read-only REST calls via the shared dhan_wrapper)
# --------------------------------------------------------------------- #
def _fetch_fno_universe() -> list[str]:
    """Unique underlying symbols with listed stock options (OPTSTK) on
    NSE - the F&O universe (~208 stocks as of 2026, per NSE's periodic
    F&O-segment additions). Same filter bt_common.py's nearest_expiry_
    strikes uses for a single symbol, applied here across the whole
    instrument master at once."""
    df = dhan_wrapper.instruments()
    optstk = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")]
    underlyings = set()
    for trading_symbol in optstk["SEM_TRADING_SYMBOL"]:
        try:
            underlyings.add(dhan_wrapper._underlying_from_trading_symbol(str(trading_symbol)))
        except Exception:  # noqa: BLE001
            continue
    return sorted(underlyings)


def _fetch_daily_ohlc(symbol: str, lookback_days: int) -> dict:
    df = dhan_wrapper.instruments()
    row = df[(df["SEM_TRADING_SYMBOL"] == symbol) & (df["SEM_EXM_EXCH_ID"] == "NSE")
             & (df["SEM_INSTRUMENT_NAME"] == "EQUITY")]
    if row.empty:
        raise ValueError(f"No equity instrument found for {symbol!r}")
    security_id = str(int(row.iloc[0]["SEM_SMST_SECURITY_ID"]))
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    from_str = (datetime.now(IST) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    resp = dhan_wrapper.client.Dhan.historical_daily_data(
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=from_str, to_date=today_str,
    )
    return resp.get("data") or {}


def _fetch_intraday(symbol: str, date_str: str, interval: int) -> dict:
    df = dhan_wrapper.instruments()
    row = df[(df["SEM_TRADING_SYMBOL"] == symbol) & (df["SEM_EXM_EXCH_ID"] == "NSE")
             & (df["SEM_INSTRUMENT_NAME"] == "EQUITY")]
    if row.empty:
        raise ValueError(f"No equity instrument found for {symbol!r}")
    security_id = str(int(row.iloc[0]["SEM_SMST_SECURITY_ID"]))
    resp = dhan_wrapper.client.Dhan.intraday_minute_data(
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=date_str, to_date=date_str, interval=interval,
    )
    return resp.get("data") or {}


# --------------------------------------------------------------------- #
# Stage 0 - Minervini Trend Template (hard gate, daily timeframe)
# See trading-skills/learnings/technical-patterns/minervini-trend-template.md
# Implements 7 of the 8 criteria (skips #8, relative strength vs. index -
# flagged in that file as softer/contextual, not a hard numeric gate).
# --------------------------------------------------------------------- #
def _sma(values: list[float], period: int) -> list[Optional[float]]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def trend_template_pass(closes: list[float]) -> tuple[bool, str]:
    if len(closes) < config.MA_LONG + config.MA_LONG_RISING_LOOKBACK_DAYS:
        return False, "insufficient daily history"
    sma50 = _sma(closes, config.MA_SHORT)
    sma150 = _sma(closes, config.MA_MID)
    sma200 = _sma(closes, config.MA_LONG)
    if sma50[-1] is None or sma150[-1] is None or sma200[-1] is None:
        return False, "MA warmup incomplete"
    if sma200[-1 - config.MA_LONG_RISING_LOOKBACK_DAYS] is None:
        return False, "insufficient history for 200-MA rising check"

    price = closes[-1]
    window = closes[-252:] if len(closes) >= 252 else closes
    low_52w, high_52w = min(window), max(window)

    checks = {
        "price>50MA": price > sma50[-1],
        "price>150MA": price > sma150[-1],
        "price>200MA": price > sma200[-1],
        "50MA>150MA": sma50[-1] > sma150[-1],
        "150MA>200MA": sma150[-1] > sma200[-1],
        "200MA_rising": sma200[-1] > sma200[-1 - config.MA_LONG_RISING_LOOKBACK_DAYS],
        "within30%_of_52w_low": price >= low_52w * (1 + config.PCT_ABOVE_52W_LOW_MIN / 100),
        "within25%_of_52w_high": price >= high_52w * (1 - config.PCT_WITHIN_52W_HIGH_MAX / 100),
    }
    passed = all(checks.values())
    detail = f"price={price:.2f} 50MA={sma50[-1]:.2f} 150MA={sma150[-1]:.2f} 200MA={sma200[-1]:.2f} " \
              f"52wRange=[{low_52w:.2f},{high_52w:.2f}] " + \
              ", ".join(f"{k}={'Y' if v else 'N'}" for k, v in checks.items())
    return passed, detail


# --------------------------------------------------------------------- #
# Stage 1 - liquidity/volatility floor (hard gate)
# --------------------------------------------------------------------- #
def liquidity_floor_pass(highs: list[float], lows: list[float], closes: list[float],
                          volumes: list[float]) -> tuple[bool, float, float, str]:
    """Returns (passed, atr_pct, avg_turnover_cr, reason-if-failed). The
    anti-SAGILITY premium/lot-size band is checked separately in the
    screen loop (needs an actual ATM option quote, an extra REST call only
    worth making for candidates that already pass this cheaper check)."""
    if len(closes) < config.ATR_PERIOD + 1 or len(volumes) < config.AVG_TURNOVER_LOOKBACK_DAYS:
        return False, 0.0, 0.0, "insufficient history"
    atr_vals = _compute_atr(highs, lows, closes, config.ATR_PERIOD)
    atr = atr_vals[-1]
    if atr is None:
        return False, 0.0, 0.0, "ATR warmup incomplete"
    price = closes[-1]
    atr_pct = (atr / price) * 100 if price else 0.0

    recent_turnover = [c * v for c, v in zip(closes[-config.AVG_TURNOVER_LOOKBACK_DAYS:],
                                              volumes[-config.AVG_TURNOVER_LOOKBACK_DAYS:])]
    avg_turnover_cr = (sum(recent_turnover) / len(recent_turnover)) / 1e7  # rupees -> crores

    if atr_pct < config.ATR_PCT_MIN:
        return False, atr_pct, avg_turnover_cr, f"ATR%={atr_pct:.2f} below floor {config.ATR_PCT_MIN}%"
    if avg_turnover_cr < config.MIN_AVG_TURNOVER_CR:
        return False, atr_pct, avg_turnover_cr, f"avg turnover Rs.{avg_turnover_cr:.1f}cr below floor Rs.{config.MIN_AVG_TURNOVER_CR}cr"
    return True, atr_pct, avg_turnover_cr, ""


# --------------------------------------------------------------------- #
# Stage 3 - intraday momentum signal (all four must agree)
# --------------------------------------------------------------------- #
def momentum_signal(c5: list[float], h5: list[float], l5: list[float],
                     c1: list[float], h1: list[float], l1: list[float]) -> Optional[str]:
    if len(c5) < max(config.RSI_PERIOD, config.SUPERTREND_5MIN_PERIOD, config.ROC_PERIOD) + 2:
        return None
    if len(c1) < config.SUPERTREND_1MIN_PERIOD + 2:
        return None

    rsi5 = _compute_rsi(c5, config.RSI_PERIOD)[-1]
    st5 = _compute_supertrend(h5, l5, c5, period=config.SUPERTREND_5MIN_PERIOD,
                               multiplier=config.SUPERTREND_5MIN_MULTIPLIER)
    roc5 = _compute_roc(c5, config.ROC_PERIOD)[-1]
    if rsi5 is None or st5[-1] is None or roc5 is None:
        return None
    close5 = c5[-1]

    st1 = _compute_supertrend(h1, l1, c1, period=config.SUPERTREND_1MIN_PERIOD,
                               multiplier=config.SUPERTREND_1MIN_MULTIPLIER)
    crossed_above = _crossed(c1, st1, above=True)
    crossed_below = _crossed(c1, st1, above=False)

    bullish = (config.RSI_BULLISH_MIN <= rsi5 <= config.RSI_BULLISH_MAX
               and close5 > st5[-1] and crossed_above and roc5 > 0)
    bearish = (config.RSI_BEARISH_MIN <= rsi5 <= config.RSI_BEARISH_MAX
               and close5 < st5[-1] and crossed_below and roc5 < 0)

    if bullish:
        return "CE"
    if bearish:
        return "PE"
    return None


# --------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------- #
_watchlist: Dict[str, WatchlistEntry] = {}
_open_positions: Dict[str, PaperPosition] = {}
_screen_date: Optional[ddate] = None
_screen_in_progress = False


async def _run_daily_screen(loop: asyncio.AbstractEventLoop) -> None:
    global _watchlist
    logger.info("K01 daily screen starting (Stage 0 Trend Template + Stage 1 liquidity floor)...")
    try:
        universe = await loop.run_in_executor(None, _fetch_fno_universe)
    except Exception:  # noqa: BLE001
        logger.exception("Could not fetch F&O universe - screen aborted for today")
        return
    logger.info("K01 F&O universe: %d underlyings", len(universe))

    candidates: list[WatchlistEntry] = []
    for symbol in universe:
        await asyncio.sleep(config.UNIVERSE_SCAN_DELAY_SECONDS)  # rate-limit pacing, NOTES.md bug #5
        try:
            daily = await loop.run_in_executor(None, _fetch_daily_ohlc, symbol, config.TREND_TEMPLATE_LOOKBACK_DAYS)
            closes = daily.get("close") or []
            highs = daily.get("high") or []
            lows = daily.get("low") or []
            volumes = daily.get("volume") or []
            if not closes:
                continue
            passed_tt, tt_detail = trend_template_pass(closes)
            if not passed_tt:
                continue
            passed_liq, atr_pct, avg_turnover_cr, liq_reason = liquidity_floor_pass(highs, lows, closes, volumes)
            if not passed_liq:
                logger.info("%s: passed Trend Template but failed liquidity floor (%s)", symbol, liq_reason)
                continue

            # Anti-SAGILITY band - needs an actual ATM CE quote, only fetched
            # for candidates that already cleared the cheaper checks above.
            try:
                atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, "CE")
                premium = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, atm.trading_symbol)
            except Exception:  # noqa: BLE001
                logger.info("%s: could not fetch ATM premium for anti-thin-option check - excluding", symbol)
                continue
            if premium < config.ANTI_SAGILITY_MAX_PREMIUM_RS and atm.lot_size >= config.ANTI_SAGILITY_MIN_LOT_SIZE:
                logger.info("%s: excluded by anti-thin-option band (premium=%.2f lot_size=%d)",
                            symbol, premium, atm.lot_size)
                continue

            candidates.append(WatchlistEntry(symbol=symbol, atr_pct=atr_pct, avg_turnover_cr=avg_turnover_cr,
                                              trend_detail=tt_detail))
        except Exception:  # noqa: BLE001
            logger.exception("%s: error during daily screen - excluding", symbol)
            continue

    candidates.sort(key=lambda w: w.atr_pct, reverse=True)
    capped = candidates[: config.WATCHLIST_CAP]
    _watchlist = {w.symbol: w for w in capped}
    logger.info("K01 daily screen complete: %d/%d passed Stage 0+1, watchlist capped to %d",
                len(candidates), len(universe), len(_watchlist))
    for w in capped:
        logger.info("  watchlist: %s ATR%%=%.2f turnover=Rs.%.1fcr", w.symbol, w.atr_pct, w.avg_turnover_cr)


async def _record_exit(symbol: str, exit_price: float, reason: str, now: datetime) -> None:
    pos = _open_positions.pop(symbol, None)
    if pos is None:
        return
    pnl = (exit_price - pos.entry_price) * pos.quantity
    trade = {
        "date": pos.entry_time.date().isoformat(), "symbol": symbol, "option_type": pos.option_type,
        "trading_symbol": pos.trading_symbol, "entry_time": pos.entry_time.isoformat(),
        "entry_price": pos.entry_price, "exit_time": now.isoformat(), "exit_price": exit_price,
        "exit_reason": reason, "quantity": pos.quantity, "pnl": pnl,
    }
    paper_trade_store.record(trade)
    logger.info("PAPER EXIT (no real order placed) %s %s %s reason=%s pnl=%.2f",
                symbol, pos.option_type, pos.trading_symbol, reason, pnl)


def _exit_reason_for(pos: PaperPosition, ltp: float) -> Optional[str]:
    if ltp >= pos.target_price:
        return "TARGET_HIT"
    loss = (pos.entry_price - ltp) * pos.quantity
    if loss >= config.MAX_LOSS_PER_TRADE_RS:
        return "MAX_LOSS_HIT"
    if ltp <= pos.hard_stop_loss:
        return "STOP_LOSS_HIT"
    return None


async def _check_open_positions(loop: asyncio.AbstractEventLoop, now: datetime) -> None:
    for symbol in list(_open_positions.keys()):
        pos = _open_positions[symbol]
        try:
            ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: paper LTP fetch failed for open position", symbol)
            continue
        pos.highest_price = max(pos.highest_price, ltp)
        reason = _exit_reason_for(pos, ltp)
        if reason:
            await _record_exit(symbol, ltp, reason, now)


async def _check_watchlist_for_entries(loop: asyncio.AbstractEventLoop, now: datetime, today: ddate) -> None:
    ce_open = sum(1 for p in _open_positions.values() if p.option_type == "CE")
    pe_open = sum(1 for p in _open_positions.values() if p.option_type == "PE")
    for symbol, entry in _watchlist.items():
        if symbol in _open_positions:
            continue
        try:
            c5d = await loop.run_in_executor(None, _fetch_intraday, symbol, today.isoformat(), 5)
            c1d = await loop.run_in_executor(None, _fetch_intraday, symbol, today.isoformat(), 1)
        except Exception:  # noqa: BLE001
            logger.exception("%s: intraday candle fetch failed", symbol)
            continue

        t5 = [datetime.fromtimestamp(e, tz=IST) for e in (c5d.get("timestamp") or [])]
        t5, h5, l5, c5 = _drop_forming_bar(t5, c5d.get("high") or [], c5d.get("low") or [], c5d.get("close") or [], 5, now)
        t1 = [datetime.fromtimestamp(e, tz=IST) for e in (c1d.get("timestamp") or [])]
        t1, h1, l1, c1 = _drop_forming_bar(t1, c1d.get("high") or [], c1d.get("low") or [], c1d.get("close") or [], 1, now)

        signal = momentum_signal(c5, h5, l5, c1, h1, l1)
        entry.last_signal = signal
        entry.last_signal_at = now
        if signal is None:
            continue
        if signal == "CE" and ce_open >= config.MAX_CONCURRENT_CE:
            continue
        if signal == "PE" and pe_open >= config.MAX_CONCURRENT_PE:
            continue

        try:
            atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, signal)
            raw_ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, atm.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: paper entry pricing failed", symbol)
            continue

        quantity = atm.lot_size * config.QUANTITY_LOTS
        position = PaperPosition(
            symbol=symbol, option_type=signal, trading_symbol=atm.trading_symbol, quantity=quantity,
            entry_time=now, entry_price=raw_ltp, target_price=raw_ltp * (1 + config.TARGET_PCT),
            hard_stop_loss=raw_ltp * (1 - config.STOP_LOSS_PCT), highest_price=raw_ltp,
        )
        _open_positions[symbol] = position
        if signal == "CE":
            ce_open += 1
        else:
            pe_open += 1
        logger.info("PAPER ENTRY (no real order placed) %s %s %s @ %.2f (watchlist ATR%%=%.2f)",
                    symbol, signal, atm.trading_symbol, raw_ltp, entry.atr_pct)


async def poll_loop() -> None:
    global _screen_date, _screen_in_progress
    assert config.PAPER_TRADING_ONLY, "Refusing to start: PAPER_TRADING_ONLY must stay True for this engine."
    loop = asyncio.get_running_loop()
    logger.info("K01 paper-trading poll loop started (PAPER ONLY - no real orders will be placed). "
                "strategy_enabled=%s", config.STRATEGY_ENABLED)
    while True:
        try:
            if not config.STRATEGY_ENABLED:
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                continue

            now = datetime.now(IST)
            today = now.date()
            market_open_dt = datetime.combine(today, dtime.fromisoformat(config.MARKET_OPEN), tzinfo=IST)
            screen_dt = datetime.combine(today, dtime.fromisoformat(config.DAILY_SCREEN_TIME), tzinfo=IST)
            square_off_dt = datetime.combine(today, dtime.fromisoformat(config.SQUARE_OFF_TIME), tzinfo=IST)

            if now > square_off_dt:
                if _open_positions:
                    for symbol in list(_open_positions.keys()):
                        pos = _open_positions[symbol]
                        try:
                            ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, pos.trading_symbol)
                        except Exception:  # noqa: BLE001
                            ltp = pos.entry_price
                        await _record_exit(symbol, ltp, "EOD_SQUARE_OFF", now)
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                continue

            if now < market_open_dt:
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                continue

            if _screen_date != today and now >= screen_dt and not _screen_in_progress:
                _screen_in_progress = True
                try:
                    await _run_daily_screen(loop)
                    _screen_date = today
                finally:
                    _screen_in_progress = False

            await _check_open_positions(loop, now)
            if _screen_date == today:
                await _check_watchlist_for_entries(loop, now, today)
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in K01 poll loop")
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)


def snapshot() -> dict:
    return paper_trade_store.snapshot(_watchlist, _open_positions, _screen_date)
