"""
Momentum vs sideways/choppy classifier for Swing (1 Oct 2026, user request: "skip trading on choppy days of a
stock, create a logic to identify when a stock is in momentum and when it is sideways or choppy with clear
signals").

Why: on Swing's 30-day simulation (research_swing_chop_regime_30day.py, 1 Sep - 1 Oct, live rules, real option
prices) the trades on stock-days whose whole day was choppy (day efficiency ratio < 0.15) lost -133k of Swing's
-110k, and trades on days with 3-5 Supertrend flips -153k; days with 0-2 flips made +43k. The tradable signal that
held up (every 1h30-2h window, thresholds 0.20-0.35, both halves of the month) is Kaufman's efficiency ratio:

    ER = |close now - close N bars ago| / sum of |bar-to-bar close changes| over those N bars

1.0 = price went straight one way, 0 = it went nowhere however much it moved. The rule (as tested, +41.9k after
slippage vs -109.5k, 53% winners, max drawdown -18.5k vs -156k):

    MOMENTUM  ER(last 24 closed 5-min bars = 2 h) >= REGIME_ER_MIN (0.25)
              AND today's ER so far (today's open -> last close) >= REGIME_TODAY_ER_MIN (0.15)  -> trade
    SIDEWAYS  ER(2 h) between REGIME_ER_CHOPPY (0.15) and 0.25                              -> skip
    CHOPPY    ER(2 h) < 0.15, OR today's ER so far < 0.15 (the day is going nowhere)         -> skip
    UNKNOWN   not enough bars yet (fail open - the entry is allowed, as every Swing gate does)

Today's ER needs REGIME_TODAY_MIN_BARS (6) bars after today's first one (09:45 close); before that only the 2 h ER decides (it spans
yesterday's bars - one continuous series, as every Swing indicator). CHOP(14) on 15-min and the 5-min Supertrend
flips of the last 2 h are reported for context only - they are NOT part of the decision (CHOP alone helped less,
flip counts did not help as an entry gate).

Pure functions + one blocking reader (call via run_in_executor) that uses the same REST-base + websocket series
every other Swing signal reads (Swing/signals._get_intraday_series), closed bars only - no extra Dhan calls in normal
operation (the REST base is shared and cached per instrument + interval).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from Options.dhan_client import IST, _compute_supertrend
from . import config, signals


@dataclass(frozen=True)
class RegimeReading:
    state: str                              # MOMENTUM | SIDEWAYS | CHOPPY | UNKNOWN
    allows_entry: bool
    er_2h: Optional[float]
    er_today: Optional[float]
    chop_15m: Optional[float] = None        # context only
    st_flips_2h: Optional[int] = None       # context only
    candle_start: Optional[datetime] = None
    reasons: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict:
        r = lambda v: None if v is None else round(v, 3)
        return {"state": self.state, "allows_entry": self.allows_entry, "er_2h": r(self.er_2h),
                "er_today": r(self.er_today), "chop_15m": r(self.chop_15m), "st_flips_2h": self.st_flips_2h,
                "candle_start": self.candle_start.isoformat() if self.candle_start else None,
                "reasons": list(self.reasons)}


def efficiency_ratio(closes: list[float], n: int) -> Optional[float]:
    """ER over the last n bar-to-bar changes; None if fewer than n + 1 closes."""
    if n < 1 or len(closes) < n + 1:
        return None
    window = closes[-(n + 1):]
    path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    return abs(window[-1] - window[0]) / path if path else 0.0


def today_efficiency_ratio(opens: list[float], closes: list[float], days: list, today, min_bars: int) -> Optional[float]:
    """Today's ER so far: |last close - today's first open| / the path of today's closes (bar-to-bar changes after
    the first bar) - exactly the backtested definition. None until min_bars bars AFTER today's first bar exist."""
    idx = [i for i, d in enumerate(days) if d == today]
    if len(idx) - 1 < min_bars:
        return None
    first = idx[0]
    path = sum(abs(closes[i] - closes[i - 1]) for i in idx[1:])
    return abs(closes[idx[-1]] - opens[first]) / path if path else 0.0


def choppiness(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> Optional[float]:
    """Choppiness Index of the last n bars (> 61.8 choppy, < 38.2 trending)."""
    if len(closes) < n + 1:
        return None
    tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
          for i in range(len(closes) - n, len(closes))]
    rng = max(highs[-n:]) - min(lows[-n:])
    return 100 * math.log10(sum(tr) / rng) / math.log10(n) if rng > 0 else None


def supertrend_flips(highs, lows, closes, bars: int) -> Optional[int]:
    st = _compute_supertrend(highs, lows, closes, period=config.SUPERTREND_PERIOD,
                             multiplier=config.SUPERTREND_MULTIPLIER)
    side = [None if st[i] is None else closes[i] > st[i] for i in range(len(closes))]
    recent = side[-(bars + 1):]
    if len(recent) < bars + 1 or any(s is None for s in recent):
        return None
    return sum(1 for i in range(1, len(recent)) if recent[i] != recent[i - 1])


def classify(bars5: dict, bars15: Optional[dict], now: datetime) -> RegimeReading:
    """Pure. bars5 / bars15 = Dhan dict-of-lists (timestamp = bar START epoch). Only bars closed by `now` count."""
    def closed(b: Optional[dict], minutes: int) -> dict:
        if not b or not b.get("timestamp"):
            return {"timestamp": [], "open": [], "high": [], "low": [], "close": []}
        cut = now.timestamp() - minutes * 60
        keep = [i for i, t in enumerate(b["timestamp"]) if t <= cut]
        return {k: [b[k][i] for i in keep] for k in ("timestamp", "open", "high", "low", "close")}

    b5, b15 = closed(bars5, 5), closed(bars15, 15)
    days = [datetime.fromtimestamp(t, IST).date() for t in b5["timestamp"]]
    er2 = efficiency_ratio(b5["close"], config.REGIME_ER_BARS)
    ert = today_efficiency_ratio(b5["open"], b5["close"], days, now.date(), config.REGIME_TODAY_MIN_BARS)
    chop = choppiness(b15["high"], b15["low"], b15["close"]) if b15["close"] else None
    flips = supertrend_flips(b5["high"], b5["low"], b5["close"], config.REGIME_ER_BARS) if b5["close"] else None
    candle = datetime.fromtimestamp(b5["timestamp"][-1], IST) if b5["timestamp"] else None
    reasons = []
    if er2 is None:
        state = "UNKNOWN"
        reasons.append("not enough 5-min bars for the 2 h efficiency ratio")
    else:
        if er2 < config.REGIME_ER_CHOPPY:
            reasons.append(f"2h ER {er2:.2f} < {config.REGIME_ER_CHOPPY}")
        if ert is not None and ert < config.REGIME_TODAY_ER_MIN:
            reasons.append(f"today's ER {ert:.2f} < {config.REGIME_TODAY_ER_MIN} (the day is going nowhere)")
        if reasons:
            state = "CHOPPY"
        elif er2 < config.REGIME_ER_MIN:
            state = "SIDEWAYS"
            reasons.append(f"2h ER {er2:.2f} < {config.REGIME_ER_MIN}")
        else:
            state = "MOMENTUM"
            reasons.append(f"2h ER {er2:.2f} >= {config.REGIME_ER_MIN}"
                           + ("" if ert is None else f", today's ER {ert:.2f} >= {config.REGIME_TODAY_ER_MIN}"))
    return RegimeReading(state=state, allows_entry=state in ("MOMENTUM", "UNKNOWN"), er_2h=er2, er_today=ert,
                         chop_15m=chop, st_flips_2h=flips, candle_start=candle, reasons=tuple(reasons))


def read(symbol: str) -> RegimeReading:
    """Blocking - call via run_in_executor. The same series every other Swing signal reads."""
    sid, seg, inst = signals._underlying_reference(symbol)
    bars5 = signals._get_intraday_series(symbol, sid, seg, inst, 5, min_bars=config.REGIME_ER_BARS + 1)
    bars15 = signals._get_intraday_series(symbol, sid, seg, inst, config.REGIME_SLOW_INTERVAL_MINUTES,
                                          min_bars=config.SUPERTREND_PERIOD + 2)
    return classify(bars5, bars15, signals._now_ist())
