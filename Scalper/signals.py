"""
Scalper candles + signal (1 Oct 2026).

SYMBOLS - any NSE index Dhan knows (Options.dhan_client.INDEX_SECURITY_ID: NIFTY, BANKNIFTY ...) or NSE F&O
stock (user, 1 Oct 2026: "some allowance to add more Index or stocks later"). reference() resolves the spot
series the same way Swing does (index -> IDX_I, stock -> NSE_EQ cash series); MCX is refused (other session
hours, futures-based reference). A symbol added to settings.symbols at runtime is set up on the next monitor
tick (ensure_ready), no restart. Note for stocks: Dhan's NSE STOCK minute data ends at 15:14 (since Aug 2026),
so a stock produces no signals after 15:14.

CANDLES - one continuous multi-day series per index (user rule: never reset at day boundaries):
  1-min  Dhan REST history (dhan_wrapper.fetch_continuous_intraday - the shared, DH-904-throttled fetch,
         REST_1M_LOOKBACK_DAYS) refreshed every REST_1M_REFRESH_SECONDS, extended by 1-minute bars built here
         from the live index ticks (Swing.candle_feed tick listener - no extra Dhan calls). REST wins where
         both have a bar. If the just-closed minute is in neither (no ticks), one REST fetch fills it
         (at most every GAP_FETCH_MIN_SECONDS).
  15-min Dhan REST history (45 days - EMA200 on 15-min needs ~8 sessions) refreshed every 15 minutes,
         extended by 15-min buckets built from the 1-min series. Only CLOSED candles are ever used.

SIGNAL - Swing's live v3 entry (Swing/trading_engine._evaluate_entry_signal) with the fast layer on 1-minute
candles, exactly as research_swing_index_1min_vs_5min.py backtested it:
  BULLISH: ((15-min Supertrend green) OR (trend-aware: 1-min EMA200 > 15-min EMA200 and the gap widened over
            8 one-minute candles) OR (1-min EMA200 > 15-min EMA200)) AND the 1-min close crossed above the
            1-min Supertrend(10,3)
           OR (Day Range Bull: today's open > yesterday's close, close > today's open, close above the 1-min
            Supertrend, RSI(14) crossing above 60 - AND the trend-aware leg)
  BEARISH: the mirror.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import threading
import time
from datetime import datetime
from typing import Optional

from Options.dhan_client import IST, _compute_ema, _compute_rsi, _compute_supertrend, dhan_wrapper
from Swing import candle_feed

logger = logging.getLogger("scalper_signals")

_refs: dict[str, tuple[str, str, str]] = {}   # symbol -> (security_id, exchange_segment, instrument_type)
REST_1M_LOOKBACK_DAYS = 6
REST_15M_LOOKBACK_DAYS = 45
REST_1M_REFRESH_SECONDS = 600
REST_15M_REFRESH_SECONDS = 900
REST_RETRY_SECONDS = 60
GAP_FETCH_MIN_SECONDS = 20
WS_KEEP_BARS = 600
SPOT_FRESH_SECONDS = 5
GAP_LOOKBACK = 8              # Swing REGIME_GAP_WIDENING_LOOKBACK_CANDLES


class _Book:
    def __init__(self) -> None:
        self.rest1: dict[int, tuple] = {}      # bar start epoch -> (o, h, l, c)
        self.rest15: dict[int, tuple] = {}
        self.ws: dict[int, tuple] = {}
        self.forming: Optional[dict] = None
        self.last_ltp: Optional[float] = None
        self.last_tick_at = 0.0
        self.rest1_at = 0.0
        self.rest15_at = 0.0
        self.gap_fetch_at = 0.0
        self.ready = False


_books: dict[str, _Book] = {}
_lock = threading.Lock()


def reference(symbol: str) -> tuple[str, str, str]:
    """Blocking. (security_id, exchange_segment, instrument_type) of the symbol's spot series. Raises
    ValueError for MCX or an unknown symbol."""
    if symbol in _refs:
        return _refs[symbol]
    if dhan_wrapper.is_mcx_commodity(symbol):
        raise ValueError(f"{symbol}: MCX commodities are not supported by the Scalper")
    try:
        ref = (dhan_wrapper.index_security_id(symbol), "IDX_I", "INDEX")
    except ValueError:
        ref = (dhan_wrapper._equity_security_id(symbol), "NSE_EQ", "EQUITY")
    _refs[symbol] = ref
    return ref


def book(symbol: str) -> _Book:
    with _lock:
        if symbol not in _books:
            _books[symbol] = _Book()
        return _books[symbol]


# --------------------------------------------------------------------------- #
# Live ticks -> 1-minute bars (WS feed thread - cheap, no I/O)
# --------------------------------------------------------------------------- #
def on_tick(symbol: str, ltp: float, tick_time: datetime) -> None:
    b = _books.get(symbol)
    if b is None or not ltp or ltp <= 0:
        return
    ts = int(tick_time.timestamp())
    start = ts - ts % 60
    with _lock:
        f = b.forming
        if f is None or start > f["start"]:
            if f is not None:
                b.ws[f["start"]] = (f["o"], f["h"], f["l"], f["c"])
                if len(b.ws) > WS_KEEP_BARS:
                    for k in sorted(b.ws)[: len(b.ws) - WS_KEEP_BARS]:
                        del b.ws[k]
            b.forming = {"start": start, "o": ltp, "h": ltp, "l": ltp, "c": ltp}
        elif start == f["start"]:
            f["h"] = max(f["h"], ltp)
            f["l"] = min(f["l"], ltp)
            f["c"] = ltp
        b.last_ltp = ltp
        b.last_tick_at = time.time()


def live_spot(symbol: str) -> Optional[float]:
    """Last index tick, if fresh."""
    b = _books.get(symbol)
    if b is None or b.last_ltp is None or time.time() - b.last_tick_at > SPOT_FRESH_SECONDS:
        return None
    return b.last_ltp


# --------------------------------------------------------------------------- #
# REST history
# --------------------------------------------------------------------------- #
def _rest_bars(symbol: str, interval: int, days: int) -> dict[int, tuple]:
    """Blocking. Closed bars only."""
    sid, seg, inst = reference(symbol)
    raw = dhan_wrapper.fetch_continuous_intraday(sid, seg, inst, interval, lookback_days_override=days)
    now = time.time()
    out = {}
    for t, o, h, l, c in zip(raw.get("timestamp") or [], raw.get("open") or [], raw.get("high") or [],
                             raw.get("low") or [], raw.get("close") or []):
        t = int(t)
        if t + interval * 60 <= now:
            out[t] = (float(o), float(h), float(l), float(c))
    return out


async def ensure_ready(symbol: str) -> bool:
    """Resolves the symbol, subscribes its ticks (idempotent) and loads REST history. True once usable."""
    b = book(symbol)
    if b.ready:
        return True
    loop = asyncio.get_running_loop()
    try:
        sid, seg, _inst = await loop.run_in_executor(None, reference, symbol)
    except Exception:  # noqa: BLE001
        logger.exception("[Scalper] %s: cannot resolve the symbol - not traded", symbol)
        return False
    try:
        await loop.run_in_executor(None, candle_feed.ensure_subscribed, symbol, sid, seg)
    except Exception:  # noqa: BLE001
        logger.exception("[Scalper] %s: could not subscribe its ticks - REST only", symbol)
    await refresh(symbol, force=True)
    b.ready = bool(b.rest1 and b.rest15)
    logger.info("[Scalper] %s (%s %s): candles %s - %d 1-min REST bars, %d 15-min REST bars", symbol, seg, sid,
                "ready" if b.ready else "NOT ready (will retry)", len(b.rest1), len(b.rest15))
    return b.ready


async def refresh(symbol: str, force: bool = False) -> None:
    b = book(symbol)
    loop = asyncio.get_running_loop()
    now = time.time()
    if force or now >= b.rest1_at:
        try:
            bars = await loop.run_in_executor(None, _rest_bars, symbol, 1, REST_1M_LOOKBACK_DAYS)
            if bars:
                b.rest1 = bars
            b.rest1_at = now + (REST_1M_REFRESH_SECONDS if bars else REST_RETRY_SECONDS)
        except Exception:  # noqa: BLE001
            logger.exception("[Scalper] %s: 1-min history fetch failed", symbol)
            b.rest1_at = now + REST_RETRY_SECONDS
    if force or now >= b.rest15_at:
        try:
            bars = await loop.run_in_executor(None, _rest_bars, symbol, 15, REST_15M_LOOKBACK_DAYS)
            if bars:
                b.rest15 = bars
            b.rest15_at = now + (REST_15M_REFRESH_SECONDS if bars else REST_RETRY_SECONDS)
        except Exception:  # noqa: BLE001
            logger.exception("[Scalper] %s: 15-min history fetch failed", symbol)
            b.rest15_at = now + REST_RETRY_SECONDS


async def fill_gap(symbol: str, minute_start: int) -> bool:
    """The just-closed minute is in neither source (no ticks): one REST fetch, rate-limited."""
    b = book(symbol)
    with _lock:
        forming_start = b.forming["start"] if b.forming else None
    if minute_start in b.rest1 or minute_start in b.ws or forming_start == minute_start:
        return True
    if time.time() - b.gap_fetch_at < GAP_FETCH_MIN_SECONDS:
        return False
    b.gap_fetch_at = time.time()
    try:
        bars = await asyncio.get_running_loop().run_in_executor(None, _rest_bars, symbol, 1, 2)
        b.rest1.update(bars)
    except Exception:  # noqa: BLE001
        logger.exception("[Scalper] %s: gap fetch failed", symbol)
    return minute_start in b.rest1


# --------------------------------------------------------------------------- #
# Series
# --------------------------------------------------------------------------- #
def _as_series(bars: dict[int, tuple]) -> dict:
    ts = sorted(bars)
    return {"ts": ts, "o": [bars[t][0] for t in ts], "h": [bars[t][1] for t in ts], "l": [bars[t][2] for t in ts],
            "c": [bars[t][3] for t in ts]}


def series(symbol: str, now_ts: float) -> tuple[dict, dict]:
    """(1-min, 15-min) CLOSED candles up to now_ts."""
    b = book(symbol)
    with _lock:
        one = dict(b.ws)
        f = dict(b.forming) if b.forming else None
    if f and f["start"] + 60 <= now_ts:
        one[f["start"]] = (f["o"], f["h"], f["l"], f["c"])
    one.update(b.rest1)
    one = {t: v for t, v in one.items() if t + 60 <= now_ts}
    fifteen: dict[int, tuple] = {}
    for t in sorted(one):
        k = t - t % 900
        o, h, l, c = one[t]
        if k in fifteen:
            fo, fh, fl, _fc = fifteen[k]
            fifteen[k] = (fo, max(fh, h), min(fl, l), c)
        else:
            fifteen[k] = (o, h, l, c)
    fifteen = {k: v for k, v in fifteen.items() if k + 900 <= now_ts}
    fifteen.update({k: v for k, v in b.rest15.items() if k + 900 <= now_ts})
    return _as_series(one), _as_series(fifteen)


# --------------------------------------------------------------------------- #
# Signal (pure)
# --------------------------------------------------------------------------- #
def evaluate(fast: dict, slow: dict) -> Optional[dict]:
    """The entry decision for the LAST closed fast candle, plus its Supertrend line (the tick exit compares
    the spot with it) and the EMA200 regime (the re-entry rule). None when there is not enough history."""
    n = len(fast["ts"])
    if n < 250 or len(slow["ts"]) < 210:
        return None
    ema_f = _compute_ema(fast["c"], 200)
    ema_s = _compute_ema(slow["c"], 200)
    st_f = _compute_supertrend(fast["h"], fast["l"], fast["c"], period=10, multiplier=3.0)
    st_s = _compute_supertrend(slow["h"], slow["l"], slow["c"], period=10, multiplier=3.0)
    rsi = _compute_rsi(fast["c"], 14)
    slow_end = [t + 900 for t in slow["ts"]]

    def regime_at(k: int):
        j = bisect.bisect_right(slow_end, fast["ts"][k] + 60) - 1      # last CLOSED 15-min candle
        if j < 0 or ema_f[k] is None or ema_s[j] is None:
            return None, None, None
        st15 = None if st_s[j] is None else slow["c"][j] > st_s[j]
        return ema_f[k] > ema_s[j], ema_f[k] - ema_s[j], st15

    k = n - 1
    reg, gap, st15_above = regime_at(k)
    reg_back, gap_back, _ = regime_at(k - GAP_LOOKBACK)
    if reg is None or st_f[k] is None or st_f[k - 1] is None:
        return None
    above, above_prev = fast["c"][k] > st_f[k], fast["c"][k - 1] > st_f[k - 1]
    crossed_up, crossed_dn = (not above_prev) and above, above_prev and not above
    widened = gap_back is not None and (gap > gap_back if reg else gap < gap_back)
    trend_bull, trend_bear = reg and widened, (not reg) and widened
    day = datetime.fromtimestamp(fast["ts"][k], IST).date()
    first = next(i for i in range(k, -1, -1) if i == 0 or datetime.fromtimestamp(fast["ts"][i - 1], IST).date() != day)
    today_open = fast["o"][first]
    prev_close = fast["c"][first - 1] if first > 0 else None
    rsi_up = rsi[k] is not None and rsi[k - 1] is not None and rsi[k - 1] <= 60 < rsi[k]
    rsi_dn = rsi[k] is not None and rsi[k - 1] is not None and rsi[k - 1] >= 40 > rsi[k]
    close = fast["c"][k]
    dr_bull = prev_close is not None and today_open > prev_close and close > today_open and above and rsi_up
    dr_bear = prev_close is not None and today_open < prev_close and close < today_open and (not above) and rsi_dn
    bull = (((st15_above is True) or trend_bull or reg) and crossed_up) or (dr_bull and trend_bull)
    bear = (((st15_above is False) or trend_bear or (not reg)) and crossed_dn) or (dr_bear and trend_bear)
    return {"bar_start": fast["ts"][k], "close": close, "st_line": st_f[k], "regime_bullish": reg,
            "bull": bool(bull), "bear": bool(bear), "crossed_up": crossed_up, "crossed_down": crossed_dn,
            "st15_above": st15_above, "trend_bull": bool(trend_bull), "trend_bear": bool(trend_bear),
            "day_range_bull": bool(dr_bull), "day_range_bear": bool(dr_bear),
            "rsi": None if rsi[k] is None else round(rsi[k], 2), "ema_gap": round(gap, 2)}
