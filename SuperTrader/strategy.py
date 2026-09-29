"""
Super Trader (30 Sep 2026, user request): scan the F&O stock universe
(every NIFTY 50 stock is in it), and when a stock breaks out with strong
momentum, buy its ATM option in that direction (CE up, PE down), ride the
move, and exit when the move reverses - intraday only.

This module is the strategy itself as PURE functions over 5-min bars, so
the backtest (backtest_super_trader_30day.py) and any live engine run the
exact same rules. No I/O here.

Bars are one CONTINUOUS multi-day 5-min series per stock (house rule: no
lookback resets at day boundaries - ATR/EMA/Supertrend run straight through).
Only session VWAP and "today's high/low" are per-day, by definition.

ENTRY (evaluated on each CLOSED 5-min bar i; the order goes at the next
minute - never inside bar i):
  1. time: bar closes at/after `first_signal_time` (09:45 - the first 30 min
     are the least reliable part of the session) and at/before
     `last_entry_time` (14:30 - too little time left after that);
  2. breakout: close > every earlier high of TODAY (long) / close < every
     earlier low of today (short), with >= `min_day_bars` bars already today;
  3. range expansion: bar range >= `range_atr_min` x ATR(14);
  4. conviction: body >= `body_min` of the range and the close in the top
     (long) / bottom (short) `close_zone` of the bar;
  5. volume: RVOL >= `rvol_min`, where RVOL = this bar's volume / the mean
     volume of the SAME clock-time bar over the previous `rvol_days`
     sessions (time-of-day adjusted);
  6. trend alignment: close above session VWAP and EMA fast > EMA slow
     (long); mirrored for short.
  Signals at the same moment are ranked by score = RVOL x range/ATR.

EXIT (on each closed 5-min bar after entry, executed at the next minute),
first that applies:
  - STRUCTURE_STOP: close back below the breakout bar's low (long) / above
    its high (short) - the breakout failed;
  - TREND_REVERSAL: per `exit_mode`
      "supertrend" - Supertrend(`st_period`, `st_mult`) flips against us;
      "chandelier" - close < highest high since entry - `chandelier_mult` x ATR
                     (long; mirrored short);
      "ema"        - EMA fast crosses back through EMA slow;
  plus, outside this module (they need option prices / the clock):
  MAX_LOSS_HIT on the option premium (rupee cap) and DAILY_SQUARE_OFF.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class Params:
    first_signal_time: dtime = dtime(9, 45)
    last_entry_time: dtime = dtime(14, 30)
    square_off_time: dtime = dtime(15, 15)
    min_day_bars: int = 6
    atr_period: int = 14
    range_atr_min: float = 1.2
    body_min: float = 0.6
    close_zone: float = 0.25
    rvol_min: float = 2.0
    rvol_days: int = 10
    ema_fast: int = 9
    ema_slow: int = 21
    exit_mode: str = "supertrend"  # "supertrend" | "chandelier" | "ema"
    st_period: int = 10
    st_mult: float = 3.0
    chandelier_mult: float = 3.0
    max_loss_rs: float = 4500.0
    min_premium_rs: float = 5.0
    max_concurrent: int = 5
    max_entries_per_symbol_per_day: int = 2


# --------------------------------------------------------------------------- #
# Indicators (continuous series)
# --------------------------------------------------------------------------- #
def ema(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def atr(highs, lows, closes, period: int) -> list[Optional[float]]:
    """Wilder ATR."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    trs = []
    for i in range(n):
        tr = highs[i] - lows[i] if i == 0 else max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                                                   abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if n < period:
        return out
    a = sum(trs[:period]) / period
    out[period - 1] = a
    for i in range(period, n):
        a = (a * (period - 1) + trs[i]) / period
        out[i] = a
    return out


def supertrend(highs, lows, closes, period: int, mult: float) -> list[Optional[int]]:
    """+1 uptrend / -1 downtrend per bar (standard final-band Supertrend)."""
    n = len(closes)
    a = atr(highs, lows, closes, period)
    direction: list[Optional[int]] = [None] * n
    fu = fl = None
    d = 1
    for i in range(n):
        if a[i] is None:
            continue
        mid = (highs[i] + lows[i]) / 2
        bu, bl = mid + mult * a[i], mid - mult * a[i]
        if fu is None:
            fu, fl = bu, bl
        else:
            fu = bu if (bu < fu or closes[i - 1] > fu) else fu
            fl = bl if (bl > fl or closes[i - 1] < fl) else fl
        if d == 1 and closes[i] < fl:
            d = -1
        elif d == -1 and closes[i] > fu:
            d = 1
        direction[i] = d
    return direction


# --------------------------------------------------------------------------- #
# Per-bar features
# --------------------------------------------------------------------------- #
@dataclass
class Features:
    ts: list[int]
    day: list
    slot: list
    o: list[float]
    h: list[float]
    l: list[float]
    c: list[float]
    v: list[float]
    atr: list
    ema_f: list
    ema_s: list
    st: list
    vwap: list
    day_hi_before: list   # highest high of TODAY's earlier bars (None on the first bar)
    day_lo_before: list
    day_bar_no: list      # 0-based index of the bar within its day
    rvol: list


def compute_features(bars: dict, p: Params) -> Features:
    """bars: {"timestamps","opens","highs","lows","closes","volumes"} - one
    continuous 5-min series, oldest first, closed bars only."""
    ts = [int(t) for t in bars["timestamps"]]
    o, h, l, c = bars["opens"], bars["highs"], bars["lows"], bars["closes"]
    v = bars.get("volumes") or [0.0] * len(c)
    dts = [datetime.fromtimestamp(t, IST) for t in ts]
    day = [d.date() for d in dts]
    slot = [d.strftime("%H:%M") for d in dts]
    n = len(c)
    vwap, dhi, dlo, dno = [None] * n, [None] * n, [None] * n, [0] * n
    cum_pv = cum_v = 0.0
    hi = lo = None
    k = 0
    slot_hist: dict[str, list[float]] = {}
    rvol = [None] * n
    for i in range(n):
        if i == 0 or day[i] != day[i - 1]:
            cum_pv = cum_v = 0.0
            hi = lo = None
            k = 0
        dhi[i], dlo[i], dno[i] = hi, lo, k
        typical = (h[i] + l[i] + c[i]) / 3
        cum_pv += typical * v[i]
        cum_v += v[i]
        vwap[i] = cum_pv / cum_v if cum_v else c[i]
        hi = h[i] if hi is None else max(hi, h[i])
        lo = l[i] if lo is None else min(lo, l[i])
        k += 1
        past = slot_hist.get(slot[i], [])
        if len(past) >= max(3, p.rvol_days // 2):
            ref = sum(past[-p.rvol_days:]) / len(past[-p.rvol_days:])
            rvol[i] = v[i] / ref if ref > 0 else None
        slot_hist.setdefault(slot[i], []).append(v[i])
    return Features(ts, day, slot, o, h, l, c, v, atr(h, l, c, p.atr_period), ema(c, p.ema_fast), ema(c, p.ema_slow),
                    supertrend(h, l, c, p.st_period, p.st_mult), vwap, dhi, dlo, dno, rvol)


def bar_close_time(f: Features, i: int) -> dtime:
    return datetime.fromtimestamp(f.ts[i] + 300, IST).time()


def entry_signal(f: Features, i: int, p: Params) -> Optional[dict]:
    """Signal on CLOSED bar i, or None. Returns side ("LONG" -> CE,
    "SHORT" -> PE), score and the structure stop level."""
    close_t = bar_close_time(f, i)
    if close_t < p.first_signal_time or close_t > p.last_entry_time or f.day_bar_no[i] < p.min_day_bars:
        return None
    a, r, e_f, e_s, rv = f.atr[i], f.h[i] - f.l[i], f.ema_f[i], f.ema_s[i], f.rvol[i]
    if a is None or a <= 0 or r <= 0 or e_f is None or e_s is None or rv is None:
        return None
    if r < p.range_atr_min * a or rv < p.rvol_min or abs(f.c[i] - f.o[i]) < p.body_min * r:
        return None
    score = rv * r / a
    if (f.day_hi_before[i] is not None and f.c[i] > f.day_hi_before[i] and f.c[i] > f.o[i]
            and f.c[i] >= f.h[i] - p.close_zone * r and f.c[i] > f.vwap[i] and e_f > e_s):
        return {"side": "LONG", "score": score, "stop": f.l[i], "rvol": rv, "range_atr": r / a}
    if (f.day_lo_before[i] is not None and f.c[i] < f.day_lo_before[i] and f.c[i] < f.o[i]
            and f.c[i] <= f.l[i] + p.close_zone * r and f.c[i] < f.vwap[i] and e_f < e_s):
        return {"side": "SHORT", "score": score, "stop": f.h[i], "rvol": rv, "range_atr": r / a}
    return None


def exit_signal(f: Features, i: int, side: str, stop: float, peak: float, p: Params) -> Optional[str]:
    """On CLOSED bar i after entry. `peak` = highest high (long) / lowest low
    (short) since entry, including bar i."""
    long = side == "LONG"
    if (long and f.c[i] < stop) or (not long and f.c[i] > stop):
        return "STRUCTURE_STOP"
    if p.exit_mode == "supertrend":
        # A FLIP against us on this bar - Supertrend lags, so it can still be
        # pointing the other way on a fresh breakout; that alone is no exit.
        against, with_ = (-1, 1) if long else (1, -1)
        if i > 0 and f.st[i] == against and f.st[i - 1] == with_:
            return "TREND_REVERSAL"
    elif p.exit_mode == "chandelier":
        a = f.atr[i]
        if a is not None and ((long and f.c[i] < peak - p.chandelier_mult * a)
                              or (not long and f.c[i] > peak + p.chandelier_mult * a)):
            return "TREND_REVERSAL"
    elif p.exit_mode == "ema":
        if f.ema_f[i] is not None and f.ema_s[i] is not None and (
                (long and f.ema_f[i] < f.ema_s[i]) or (not long and f.ema_f[i] > f.ema_s[i])):
            return "TREND_REVERSAL"
    return None
