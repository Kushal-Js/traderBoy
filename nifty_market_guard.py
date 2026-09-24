"""
Global NIFTY-bearish-market guard (prototype, added 24 Sep 2026, user
request) - BUILD + BACKTEST ONLY, not wired into any live entry path.
See backtest_nifty_market_guard.py for the real-trade backtest this is
meant to be judged on before any decision to deploy it.

FINAL SPEC (simplified twice, 24 Sep 2026, both user corrections):
  1st correction: dropped the 3-tier "LUXURY_PE_ONLY" middle ground -
    binary only, whole day, every package, both CE and PE.
  2nd correction (this version): dropped the 3-consecutive-falling-day
    trigger ENTIRELY. Gap-down only, and explicitly NEVER on a gap up -
    "gap up shouldn't impact trading at all. Only if there is a gap down
    of more than hundred points for Nifty, then only the trading should
    be blocked for whole day. Otherwise, there shouldn't be any blockage
    at all."

The whole rule is now exactly one condition:

  BLOCK_ALL  - NIFTY's open gapped DOWN from yesterday's close by
               GAP_BLOCK_ABS (100) points or more.
  ALLOW_ALL  - everything else - any gap up (any size), or a gap down
               under 100 points. No other input (multi-day trend,
               intraday continuation, etc.) is considered at all.

Decided ONCE per trading day (mirrors evaluate_nifty_open_condition's
own "one-shot fact about the day" design) - no intraday re-check, no
time-based expiry, applies for the entire session once set (whole-day,
not the ~30min window the existing should_delay_ce_entry uses).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls
from typing import Literal

GAP_BLOCK_ABS = 100.0   # points - a gap DOWN of at least this triggers BLOCK_ALL; a gap UP never does

Tier = Literal["ALLOW_ALL", "BLOCK_ALL"]


@dataclass(frozen=True)
class NiftyDayState:
    """One trading day's facts, knowable as soon as the market opens."""
    trading_date: date_cls
    prev_close: float
    open: float

    @property
    def gap_points(self) -> float:
        return self.open - self.prev_close

    @property
    def gap_down_abs(self) -> float:
        """0 for any gap UP (or flat) - a gap up is never a "gap down
        magnitude", by construction, so it can never trigger BLOCK_ALL."""
        return max(0.0, -self.gap_points)


def classify_day(state: NiftyDayState) -> Tier:
    if state.gap_down_abs >= GAP_BLOCK_ABS:
        return "BLOCK_ALL"
    return "ALLOW_ALL"


def allowed_for_trade(tier: Tier) -> bool:
    """True if any trade (any strategy, any CE/PE) is allowed under this
    tier - the single function a live integration would eventually call
    at the top of each package's _breakout_entry_fn, mirroring
    should_delay_ce_entry's own call-site shape."""
    return tier == "ALLOW_ALL"
