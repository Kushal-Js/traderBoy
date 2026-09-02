"""
Opening Range Breakout (ORB) + VWAP + Volume confluence re-ranking signal
- user request 2 Sep 2026, the second of two "different signal ideas"
explored after putting the HH/HL momentum-continuation signal
(momentum_signal.py) on hold. BACKTEST-ONLY, same status as the other two
signal modules in this codebase - not wired into production ranking.

The idea (sourced, cited in trading-skills' own learnings/intraday-
options-trading/momentum-entry-conditions-research.md, never built
anywhere in this codebase until now): a commonly-cited "good" intraday
momentum confirmation requires ALL of - a candle closing beyond the
day's own opening range (not just an intra-bar poke through it), price
trading above VWAP (confirms broad participation, not just a spike one
side is fighting against), and volume on the confirming bar meaningfully
above its own recent average. A DIFFERENT mechanism from both other
signals already tried: no chart-pattern swing geometry (unlike HH/HL),
no cross-sectional benchmark comparison (unlike Relative Strength) - a
purely intraday, single-day, session-anchored confluence check.

Nothing here does any I/O - same convention every other pure-function
module in this codebase follows. See tests/test_swing_orb_vwap_signal.py.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from Swing.momentum_signal import rvol_score  # reused as-is - the RVOL leg is identical in spirit


def day_boundaries(timestamps: List[int]) -> List[Tuple[int, int]]:
    """Returns (start_index, end_index_exclusive) for each trading day
    present in a chronologically-sorted timestamp series - the day
    changes whenever the calendar date changes, same grouping logic
    every other resampling function in this codebase already uses."""
    from datetime import datetime
    if not timestamps:
        return []
    boundaries = []
    day_start = 0
    current_day = datetime.fromtimestamp(timestamps[0]).date()
    for i in range(1, len(timestamps)):
        day = datetime.fromtimestamp(timestamps[i]).date()
        if day != current_day:
            boundaries.append((day_start, i))
            day_start = i
            current_day = day
    boundaries.append((day_start, len(timestamps)))
    return boundaries


def opening_range(highs: List[float], lows: List[float], day_start: int, or_bars: int) -> Optional[Tuple[float, float]]:
    """The (high, low) of the first `or_bars` candles of a trading day
    (day_start is that day's own first bar index, from day_boundaries).
    Returns None if the day doesn't even have `or_bars` candles yet
    (e.g. scoring a bar that itself falls inside the opening range -
    there's no completed range to compare against yet, not "the range
    was zero-width")."""
    end = day_start + or_bars
    if end > len(highs):
        return None
    window_highs = highs[day_start:end]
    window_lows = lows[day_start:end]
    if not window_highs:
        return None
    return max(window_highs), min(window_lows)


def vwap_series(highs: List[float], lows: List[float], closes: List[float], volumes: List[float],
                 boundaries: List[Tuple[int, int]]) -> List[Optional[float]]:
    """Cumulative intraday VWAP per bar, RESETTING at the start of every
    trading day (VWAP is a session-anchored measure, not a running
    average across days) - typical price (H+L+C)/3 weighted by volume,
    the standard VWAP formula. None for a bar with zero cumulative
    volume so far that day (can't divide by zero - a real, if rare,
    possibility right at the open of an illiquid symbol)."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    for start, end in boundaries:
        cum_pv = 0.0
        cum_vol = 0.0
        for i in range(start, end):
            typical = (highs[i] + lows[i] + closes[i]) / 3.0
            cum_pv += typical * volumes[i]
            cum_vol += volumes[i]
            out[i] = (cum_pv / cum_vol) if cum_vol > 0 else None
    return out


def orb_breakout_score(entry_close: float, or_high: float, atr: float, cap_atr_multiples: float = 1.0) -> float:
    """0.0 if the entry bar hasn't actually closed beyond the opening
    range's own high yet (an intra-bar poke through it doesn't count,
    per the sourced pattern's own "closing beyond", not "touching") -
    otherwise scales up to 1.0 by how many ATRs above the range's high
    the close sits, saturating at `cap_atr_multiples` (a small margin is
    enough to count as "broke out"; this isn't the same as
    momentum_signal.py's extension PENALTY, which fires at a much wider
    2-ATR cap and measures distance above a swing PIVOT, not an opening
    range - the two are deliberately not reused as the same function,
    since a fresh ORB confirmation and an over-extended chase are
    different concerns even though both are "distance above a
    reference price in ATRs")."""
    if atr <= 0:
        return 1.0 if entry_close > or_high else 0.0
    if entry_close <= or_high:
        return 0.0
    margin_in_atrs = (entry_close - or_high) / atr
    return min(1.0, margin_in_atrs / cap_atr_multiples)


def vwap_confluence_score(entry_close: float, vwap_at_entry: Optional[float], atr: float, cap_atr_multiples: float = 0.5) -> Optional[float]:
    """0.0 if price is AT or BELOW VWAP (no real participation
    confirmation), scaling up to 1.0 by distance above VWAP, saturating
    at a much smaller `cap_atr_multiples` than the ORB score above -
    VWAP is a same-session average, so even a modest distance above it
    is a meaningfully different confirmation than a large one. None if
    VWAP itself isn't computable yet (see vwap_series's own docstring on
    zero cumulative volume)."""
    if vwap_at_entry is None:
        return None
    if atr <= 0:
        return 1.0 if entry_close > vwap_at_entry else 0.0
    if entry_close <= vwap_at_entry:
        return 0.0
    margin_in_atrs = (entry_close - vwap_at_entry) / atr
    return min(1.0, margin_in_atrs / cap_atr_multiples)


DEFAULT_WEIGHTS = {"orb": 0.35, "vwap": 0.35, "rvol": 0.30}


def orb_vwap_composite_score(orb: Optional[float], vwap: Optional[float], rvol: Optional[float],
                              weights: Optional[dict] = None) -> Optional[float]:
    """Combines the three confluence components into one [0,1]-ish
    score. Returns None if VWAP or RVOL couldn't be computed at all
    (not enough of the session, or not enough prior days respectively) -
    ORB itself is never None (0.0 is a real, meaningful "didn't break
    out" answer, not a missing-data case) once the opening range itself
    exists."""
    if orb is None or vwap is None or rvol is None:
        return None
    w = weights or DEFAULT_WEIGHTS
    rvol_component = max(0.0, min(1.0, rvol / 2.0))  # same RVOL>=2.0 saturation convention as momentum_signal.py
    return w["orb"] * orb + w["vwap"] * vwap + w["rvol"] * rvol_component
