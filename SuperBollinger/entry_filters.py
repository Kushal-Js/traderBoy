"""
Entry filters for Super Bollinger (30 Sep 2026).

1-HOUR GREEN - the user's idea ("the current 1 hour should be above the open
or close of the last candles ... to avoid choppy movements"), in the form that
tested best out of ~60 anti-chop filters on the 3 Aug - 29 Sep walk-forward
trades (research_super_bollinger_chop_filters.py, ..._chop_filter_resim.py):
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
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from Bollinger import signals
from Bollinger import trading_engine as engine
from Options.dhan_client import IST

from . import settings

logger = logging.getLogger("super_bollinger_entry_filters")

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


async def last_hour_green(symbol: str) -> tuple[Optional[bool], dict]:
    """(True/False, detail) - None when the series is not available."""
    minutes = settings.get("entry_filter_1h_minutes")

    def work():
        sid, seg, inst = signals._underlying_reference(symbol)
        return signals._get_intraday_series(symbol, sid, seg, inst)

    try:
        data = await asyncio.get_running_loop().run_in_executor(None, work)
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
    return candle["close"] > candle["open"], {"candle_start": candle["start"].isoformat(), "candle_open": candle["open"],
                                              "candle_close": candle["close"], "minutes": minutes}
