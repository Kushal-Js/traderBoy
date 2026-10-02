"""
Entry filters for Unified Momentum (30 Sep 2026).

1-HOUR GREEN - the user's idea ("the current 1 hour should be above the open
or close of the last candles ... to avoid choppy movements"), in the form that
tested best out of ~60 anti-chop filters on the 3 Aug - 29 Sep walk-forward
trades (research_unified_momentum_chop_filters.py, ..._chop_filter_resim.py):
take an entry only if the stock's last CLOSED 1-hour candle closed above its
open. Re-simulated with the 5-slot limit: 163 trades instead of 193, plain
calls +94.9k vs +63.5k, max drawdown -22.8k vs -45.8k; the three losing August
weeks went from -40.3k to -3.3k. The drawdown cut showed for every candle
length from 30 to 90 minutes; the extra profit only for 60/90 minutes, so the
drawdown cut is the dependable part. In-sample - no untouched data was left.

Candles are 60-minute buckets anchored at 09:15 each day (09:15, 10:15 ...
14:15, and the 15:15-15:30 stub), built from the same continuous 5-minute
series the signal uses; the newest bucket counts only once its scheduled end
has passed. Before 10:15 the "last closed candle" is therefore the previous
session's last one - exactly what the backtest did.

settings.entry_filter_1h: off | shadow (log what would be skipped) | on.
A missing series never blocks an entry (the filter fails open and says so).

MOMENTUM READINGS + BYPASS SHADOW (1 Oct 2026, user: "let the one hour rule
get bypassed when there is very strong momentum"): every refused entry now
also carries momentum readings (momentum_readings) in its event, so a bypass
can be judged later on live data. Backtests on 9 stock lists
(research_unified_momentum_1h_override_lists.py) found most momentum bypasses
let through losers; the only near-neutral one was "stock up >=
entry_filter_1h_bypass_day_up_pct (1.5%) on the day and above its open"
(bypass_would_take). With settings.entry_filter_1h_bypass = shadow it is
logged as ENTRY_FILTER_1H_BYPASS_WOULD_ENTER - nothing is traded.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from Bollinger import signals
from Bollinger import trading_engine as engine
from Options.dhan_client import IST, dhan_wrapper

from . import settings

logger = logging.getLogger("unified_momentum_entry_filters")

SESSION_START_MINUTE = 9 * 60 + 15
SESSION_END_MINUTE = 15 * 60 + 30


def last_closed_candle(timestamps, opens, closes, now: datetime, minutes: int = 60) -> Optional[dict]:
    """The last `minutes` candle (09:15-anchored) whose scheduled end is <= now,
    from 5-minute bars (bar START epochs). -> {"start", "open", "close"} or None.
    Pure."""
    now_key = (now.date(), (now.hour * 60 + now.minute - SESSION_START_MINUTE) // minutes)
    now_minute = now.hour * 60 + now.minute + now.second / 60
    best, cur = None, None      # cur = [key, start_dt, open, close]
    for t, o, c in zip(timestamps, opens, closes):
        dt = datetime.fromtimestamp(t, IST)
        if dt >= now:
            break
        key = (dt.date(), (dt.hour * 60 + dt.minute - SESSION_START_MINUTE) // minutes)
        if cur is None or key != cur[0]:
            if cur is not None:
                best = cur
            cur = [key, dt, o, c]
        else:
            cur[3] = c
    if cur is not None:
        if cur[0][0] < now.date():
            best = cur                                     # an earlier session's last candle is closed
        elif cur[0] != now_key:
            best = cur                                     # an earlier bucket of today
        else:
            end = min(SESSION_START_MINUTE + (cur[0][1] + 1) * minutes, SESSION_END_MINUTE)
            if now_minute >= end:
                best = cur
    if best is None:
        return None
    return {"start": best[1], "open": best[2], "close": best[3]}


def momentum_readings(timestamps, opens, closes, volumes, now: datetime, minutes: int = 60,
                      bar_minutes: int = 5) -> dict:
    """Momentum at `now` from CLOSED 5-minute bars (bar START epochs), the same
    readings the 1 Oct bypass backtests used. Pure; {} when there is no
    closed bar today or no previous session.
      spot                 last closed bar's close
      day_up_pct           spot vs the previous session's last close, in %
      above_day_open       spot above today's first bar open
      forming_hour_up_pct  spot vs the open of the current 09:15-anchored
                           `minutes` bucket (when it started today), in %
      vol_ratio            last closed bar's volume / average of the 20 before
      bar_green            last closed bar closed above its open"""
    k = None
    for i in range(len(timestamps) - 1, -1, -1):
        if datetime.fromtimestamp(timestamps[i], IST) + timedelta(minutes=bar_minutes) <= now:
            k = i
            break
    if k is None:
        return {}
    day = datetime.fromtimestamp(timestamps[k], IST).date()
    if day != now.date():
        return {}
    first_today = k
    while first_today > 0 and datetime.fromtimestamp(timestamps[first_today - 1], IST).date() == day:
        first_today -= 1
    if first_today == 0:
        return {}
    spot, prev_close = closes[k], closes[first_today - 1]
    out = {"spot": spot, "day_up_pct": round((spot / prev_close - 1) * 100, 3) if prev_close else None,
           "above_day_open": spot > opens[first_today], "bar_green": closes[k] > opens[k]}
    bucket = (now.hour * 60 + now.minute - SESSION_START_MINUTE) // minutes
    for i in range(first_today, k + 1):
        dt = datetime.fromtimestamp(timestamps[i], IST)
        if (dt.hour * 60 + dt.minute - SESSION_START_MINUTE) // minutes == bucket:
            out["forming_hour_up_pct"] = round((spot / opens[i] - 1) * 100, 3) if opens[i] else None
            break
    vols = list(volumes or [])
    if len(vols) == len(closes) and k >= 1:
        prior = vols[max(0, k - 20):k]
        avg = sum(prior) / len(prior) if prior else 0
        out["vol_ratio"] = round(vols[k] / avg, 2) if avg else None
    return out


def bypass_would_take(detail: dict, day_up_pct_min: float) -> bool:
    """The only bypass the 1 Oct backtests did not reject: stock up >=
    day_up_pct_min % on the day and above its day open."""
    up = detail.get("day_up_pct")
    return up is not None and up >= day_up_pct_min and bool(detail.get("above_day_open"))


async def last_hour_green(symbol: str) -> tuple[Optional[bool], dict]:
    """(True/False, detail) - None when the series is not available. A red
    result's detail also carries momentum_readings (for the bypass shadow)."""
    minutes = settings.get("entry_filter_1h_minutes")

    def work():
        sid, seg, inst = signals._underlying_reference(symbol)
        return signals._get_intraday_series(symbol, sid, seg, inst)

    try:
        data = await asyncio.get_running_loop().run_in_executor(dhan_wrapper.history_executor(), work)
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not load the series for the 1-hour entry filter", symbol)
        return None, {"reason": "series_error"}
    if not data or not data.get("close"):
        return None, {"reason": "no_series"}
    candle = last_closed_candle(data["timestamp"], data["open"], data["close"], engine._now_ist(), minutes)
    if candle is None:
        return None, {"reason": "no_closed_candle"}
    # the series must be current: its newest bar no older than two signal intervals + the candle length
    newest = datetime.fromtimestamp(data["timestamp"][-1], IST)
    if engine._now_ist() - newest > timedelta(days=5):
        return None, {"reason": "stale_series", "newest_bar": newest.isoformat()}
    green = candle["close"] > candle["open"]
    detail = {"candle_start": candle["start"].isoformat(), "candle_open": candle["open"],
              "candle_close": candle["close"], "minutes": minutes}
    if not green:
        try:
            detail.update(momentum_readings(data["timestamp"], data["open"], data["close"], data.get("volume"),
                                            engine._now_ist(), minutes))
        except Exception:  # noqa: BLE001 - readings are informational, never block the filter
            logger.exception("%s: could not compute momentum readings", symbol)
    return green, detail
