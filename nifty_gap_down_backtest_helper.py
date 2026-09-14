"""
Shared backtest helper (added 13 Sep 2026, user request) that faithfully
replicates Options/dhan_client.py's live Nifty50 open gap-down / sharp-
fall CE cool-off (evaluate_nifty_open_condition / is_nifty_recovering /
should_delay_ce_entry) for use in ANY backtest script - Options, Futures,
and Luxury all gate CE entries through the identical shared logic in the
live bot (see that module's own docstring), so this one helper covers
all three rather than being reimplemented per script.

Every prior backtest this session disclosed this as NOT modeled - "if any
of these days had a real >=100pt Nifty gap-down morning, this backtest
would be OPTIMISTIC for early-morning CE entries that day." This module
closes that gap by reconstructing the exact same decision from NIFTY50's
own historical 1-min candles, so future backtests can call
`should_delay_ce_entry_at(dt)` at the same point the live webhook handler
calls `dhan_wrapper.should_delay_ce_entry()`.

Faithfulness notes (mirrors the live code's own real-time quirks, not
just its steady-state formula):
  - `evaluate_open_condition_at(dt)` is a ONE-SHOT-PER-DAY computation,
    exactly like the live cache - the first call for a given date freezes
    that day's gap_points/fall_pct/delay_until/hard_cap_until using only
    data at-or-before that first call's timestamp. A later call the same
    day returns the SAME cached result, even though more candles have
    since printed - this replicates the live bot's actual behavior
    (whichever CE alert is first to ask "locks in" the day's judgment),
    not a full-hindsight version of it.
  - `is_recovering_at(today_open, dt)` re-evaluates fresh every call
    (unlike the live code's 30s throttle - irrelevant in a backtest,
    since we're not rate-limiting a real API).
  - `should_delay_ce_entry_at(dt)` combines both exactly like the live
    should_delay_ce_entry(): delay_until is a hard minimum (bigger gap ->
    longer wait, regardless of how fast price bounces); past that, if the
    recovery gate is enabled, the delay EXTENDS until Nifty's own still-
    forming daily candle turns green, capped at hard_cap_until.

Only ever gates CE entries - PE entries must never call this (same
restriction as the live code; a falling Nifty is exactly when a PE-buying
alert should be allowed to act).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
NIFTY_SECURITY_ID = "13"  # NSE index (spot), IDX_I segment - same ID used live


def fetch_nifty_continuous_cached(dhan_wrapper, _retry, cache_dir: Path, days_back: int = 90) -> dict:
    """Fetches NIFTY50's own continuous 1-min series once and caches it to
    disk - market-wide data, identical regardless of which strategy/CSV a
    given backtest is testing, so every backtest script should point its
    own CACHE_DIR's sibling (or a shared location) here rather than
    re-fetching per run."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "NIFTY_1min.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data,
                  security_id=NIFTY_SECURITY_ID, exchange_segment="IDX_I", instrument_type="INDEX",
                  from_date=from_date, to_date=to_date, interval=1)
    data = resp.get("data") or {}
    result = {"opens": data.get("open") or [], "closes": data.get("close") or [],
              "timestamps": data.get("timestamp") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


class NiftyGapDownGate:
    def __init__(self, nifty_data: dict, *, threshold_points: float, sharp_fall_pct: float,
                 ce_delay_minutes: float, extra_delay_minutes_per_100_points: float,
                 max_delay_minutes: float, recovery_gate_enabled: bool,
                 market_open_time: str = "09:15"):
        self.bars = sorted(zip(nifty_data["timestamps"], nifty_data["opens"], nifty_data["closes"]))
        self.threshold_points = threshold_points
        self.sharp_fall_pct = sharp_fall_pct
        self.ce_delay_minutes = ce_delay_minutes
        self.extra_delay_minutes_per_100_points = extra_delay_minutes_per_100_points
        self.max_delay_minutes = max_delay_minutes
        self.recovery_gate_enabled = recovery_gate_enabled
        self.market_open_time = market_open_time
        self._day_cache: dict = {}
        if not self.bars:
            print("[nifty_gap_down] WARNING: no NIFTY data available - gap-down gate will never trigger "
                  "(fails open, same philosophy as the live code).")

    def evaluate_open_condition_at(self, dt: datetime) -> dict:
        day = dt.date()
        if day in self._day_cache:
            return self._day_cache[day]

        ts_cutoff = dt.timestamp()
        bars_upto = [b for b in self.bars if b[0] <= ts_cutoff]
        today_bars = [b for b in bars_upto if datetime.fromtimestamp(b[0], tz=IST).date() == day]
        prior_bars = [b for b in bars_upto if datetime.fromtimestamp(b[0], tz=IST).date() < day]
        not_yet = {"evaluated": False, "delay_ce": False, "delay_until": None, "hard_cap_until": None}
        if not today_bars or not prior_bars:
            return not_yet

        prev_close = prior_bars[-1][2]
        today_open = today_bars[0][1]
        latest_close = today_bars[-1][2]
        if not today_open or not prev_close:
            return not_yet

        gap_points = today_open - prev_close
        gap_down = gap_points <= -self.threshold_points
        fall_pct = (today_open - latest_close) / today_open
        sharp_falling = fall_pct >= self.sharp_fall_pct
        delay_ce = gap_down or sharp_falling

        if gap_down:
            excess_points = max(0.0, abs(gap_points) - self.threshold_points)
            scaled_minutes = min(
                self.max_delay_minutes,
                self.ce_delay_minutes + self.extra_delay_minutes_per_100_points * (excess_points / 100.0),
            )
        else:
            scaled_minutes = min(self.max_delay_minutes, self.ce_delay_minutes)

        market_open_dt = datetime.combine(day, dtime.fromisoformat(self.market_open_time), tzinfo=IST)
        result = {
            "evaluated": True, "today_open": today_open, "prev_close": prev_close,
            "gap_points": round(gap_points, 2), "gap_down": gap_down,
            "fall_pct": round(fall_pct * 100, 3), "sharp_falling": sharp_falling,
            "delay_ce": delay_ce,
            "scaled_delay_minutes": round(scaled_minutes, 1) if delay_ce else None,
            "delay_until": (market_open_dt + timedelta(minutes=scaled_minutes)) if delay_ce else None,
            "hard_cap_until": (market_open_dt + timedelta(minutes=self.max_delay_minutes)) if delay_ce else None,
        }
        self._day_cache[day] = result
        if delay_ce:
            print(f"  [nifty_gap_down] {day}: gap={gap_points:+.1f}pts (open={today_open:.2f} "
                  f"prev_close={prev_close:.2f}) fall={fall_pct*100:.2f}% from open -> CE delayed "
                  f"until {result['delay_until'].strftime('%H:%M')} (hard cap {result['hard_cap_until'].strftime('%H:%M')})")
        return result

    def is_recovering_at(self, today_open: float, dt: datetime) -> bool:
        day = dt.date()
        ts_cutoff = dt.timestamp()
        today_closes = [c for t, o, c in self.bars
                        if t <= ts_cutoff and datetime.fromtimestamp(t, tz=IST).date() == day]
        if not today_closes or not today_open:
            return True
        return today_closes[-1] >= today_open

    def should_delay_ce_entry_at(self, dt: datetime) -> tuple[bool, dict]:
        result = self.evaluate_open_condition_at(dt)
        if not result.get("delay_ce") or result.get("delay_until") is None:
            return False, result
        if dt >= result["hard_cap_until"]:
            return False, result
        if dt < result["delay_until"]:
            return True, result
        if not self.recovery_gate_enabled:
            return False, result
        return (not self.is_recovering_at(result["today_open"], dt)), result
