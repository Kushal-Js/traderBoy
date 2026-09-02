"""
Relative Strength (RS) momentum-ranking signal - user request 2 Sep 2026
follow-up to the HH/HL momentum-continuation signal (see momentum_signal.py,
put ON HOLD same day): "explore a different signal idea." BACKTEST-ONLY,
same status as momentum_signal.py - not wired into production ranking.

The idea: the classic, well-documented momentum-persistence effect (the
academic "momentum factor" literature, and IBD's own "Relative Strength
Rating") - a stock that has already been outperforming the broad market
(NIFTY) over a recent trailing window tends, on average, to keep doing so
over the near term. Deliberately a DIFFERENT KIND of signal from
momentum_signal.py's own HH/HL approach: no chart-pattern geometry, no
fractal swing detection, no k-bar confirmation lag - just a single plain
number (trailing excess return vs the benchmark) with ONE tunable
parameter (the lookback window), not four weighted components. This is a
deliberate design choice, not an oversight: the HH/HL signal's own
parameter-tuning sweep (see trading-skills' designs/hhhl-momentum-
continuation.md) showed real overfitting risk from a many-parameter
composite score on a modest sample - fewer knobs here is meant to make
this signal's own backtest result easier to trust either way it comes
out.

Nothing here does any I/O - same convention every other pure-function
module in this codebase follows (_compute_supertrend, _compute_ema,
momentum_signal.py). See tests/test_swing_relative_strength.py.
"""
from __future__ import annotations

from typing import List, Optional, Tuple


def align_series_by_date(
    dates_a: List[str], values_a: List[float], dates_b: List[str], values_b: List[float],
) -> Tuple[List[str], List[float], List[float]]:
    """Keeps only the dates present in BOTH series, sorted chronologically
    - protects against ever comparing a stock's close on one day against
    the benchmark's close from a DIFFERENT day, which a naive same-
    position zip would risk if either series has its own data gap (a
    real, observed possibility in this codebase - see the MOTHERSON gap
    found while backtesting the HH/HL signal) or the two providers'
    trading calendars don't line up perfectly for some other reason."""
    map_a = dict(zip(dates_a, values_a))
    map_b = dict(zip(dates_b, values_b))
    common = sorted(set(map_a) & set(map_b))
    return common, [map_a[d] for d in common], [map_b[d] for d in common]


def relative_strength_score(
    stock_closes_aligned: List[float], benchmark_closes_aligned: List[float],
    as_of_index: int, lookback_days: int,
) -> Optional[float]:
    """Excess return (in percentage points) of the stock over the
    benchmark across the last `lookback_days` TRADING days ending at
    as_of_index (inclusive) - both series must already share the same
    trading-day calendar (see align_series_by_date). Positive means the
    stock outperformed the benchmark over the window; negative means it
    lagged. Returns None if there isn't `lookback_days` of history yet
    before as_of_index - callers should treat that as "no score
    available yet", never as a 0 (a genuine 0 would mean the stock
    exactly matched the benchmark, a real and different thing)."""
    start = as_of_index - lookback_days
    if start < 0 or as_of_index >= len(stock_closes_aligned) or as_of_index >= len(benchmark_closes_aligned):
        return None
    if stock_closes_aligned[start] <= 0 or benchmark_closes_aligned[start] <= 0:
        return None
    stock_return_pct = (stock_closes_aligned[as_of_index] / stock_closes_aligned[start] - 1) * 100
    benchmark_return_pct = (benchmark_closes_aligned[as_of_index] / benchmark_closes_aligned[start] - 1) * 100
    return stock_return_pct - benchmark_return_pct
