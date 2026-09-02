"""
HH/HL momentum-continuation composite score - user request 2 Sep 2026.
BACKTEST-ONLY as of this writing: nothing here is wired into production
ranking yet (see Swing/trading_engine.py's own `_entry_candidate_rank_key`
for the ranking signal actually live today) - this module exists so
`backtest_momentum_signal.py` (repo root) has a well-tested, pure
implementation to score historical entries against, per the user's own
plan: "build and backtest... show me results first... then we will think
of deploying it or not."

The idea, in the user's own words: a Dow-Theory Higher-High/Higher-Low
swing structure on 5-min candles, preceded by a tightening consolidation
("coil"), is a higher-quality continuation setup than a bare Supertrend
crossover alone - conceptually the same thing trading literature calls a
flag/pennant continuation (see trading-skills/learnings/technical-
patterns/classic-chart-patterns.md - already researched, not a novel
idea). This is meant as an ADDITIONAL SOFT RE-RANKING SIGNAL layered on
top of Swing's existing entry trigger (5-min/1-min Supertrend crossover +
price-confirmation gate, `_evaluate_watchlist_entry_signal`) - it never
gates an entry on its own, only changes which of several simultaneously-
firing candidates gets picked first when `MAX_LIVE_BASKETS` can't take
them all (same role `_entry_candidate_rank_key`'s freshness+volume
already plays).

Nothing here does any I/O - same convention `_compute_supertrend`/
`_compute_ema` already follow (trading_engine.py, dhan_client.py). Every
function takes plain lists and returns plain values so it's cheap to
unit-test against hand-built synthetic candle data (see
tests/test_swing_momentum_signal.py) independent of any live fetch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


def find_fractal_swing_highs(highs: List[float], k: int) -> List[int]:
    """Returns the indices of every fractal swing high: a bar strictly
    higher than each of the k bars immediately before AND after it.
    `k` is deliberately a free parameter (user's own note: "this can
    further be optimized also") - a smaller k confirms faster (less lag)
    but produces noisier/smaller swings; a larger k requires a more
    significant high but takes longer to confirm. A bar within k of
    either end of the series can never be confirmed - not an oversight,
    this is the real, unavoidable confirmation LAG any live system
    reading the same rule would also have (you cannot know bar i was a
    swing high until k bars later have actually printed without
    exceeding it)."""
    n = len(highs)
    out = []
    for i in range(k, n - k):
        h = highs[i]
        if all(h > highs[j] for j in range(i - k, i)) and all(h > highs[j] for j in range(i + 1, i + 1 + k)):
            out.append(i)
    return out


def find_fractal_swing_lows(lows: List[float], k: int) -> List[int]:
    """Mirror of find_fractal_swing_highs for swing lows."""
    n = len(lows)
    out = []
    for i in range(k, n - k):
        l = lows[i]
        if all(l < lows[j] for j in range(i - k, i)) and all(l < lows[j] for j in range(i + 1, i + 1 + k)):
            out.append(i)
    return out


@dataclass
class BreakoutEvent:
    index: int  # bar where close first crossed above the pivot
    pivot_price: float  # the confirmed swing high that was broken
    pivot_index: int  # bar index of that swing high
    prior_swing_high: float  # the swing high before the pivot (must be lower - the "HH" half)
    swing_low_price: float  # most recent confirmed swing low (must be higher than the one before it - the "HL" half)
    prior_swing_low: float


def detect_hh_hl_breakouts(
    highs: List[float], lows: List[float], closes: List[float], k: int,
) -> List[BreakoutEvent]:
    """Walks the series in chronological order and returns every bar
    where price breaks out above a confirmed Higher-High/Higher-Low
    structure for the first time since that structure formed - i.e. the
    Dow Theory definition of "the uptrend just resumed" (Stage C from
    the design conversation): the last two confirmed swing highs are
    ascending (a genuine HH), the last two confirmed swing lows are also
    ascending (a genuine HL), and price now closes above the most recent
    swing high (the "pivot").

    A swing point at index i only becomes usable starting at bar i+k
    (see find_fractal_swing_highs/_lows's own docstring on confirmation
    lag) - this function respects that at every step, so an event's own
    `index` never implicitly assumes knowledge the market didn't
    actually have yet at that bar. Each pivot only fires ONCE (the exact
    crossing bar, not every bar price happens to sit above it
    afterwards) - re-arming only once a NEW, higher pivot is confirmed."""
    swing_high_idx = find_fractal_swing_highs(highs, k)
    swing_low_idx = find_fractal_swing_lows(lows, k)
    n = len(closes)

    events: List[BreakoutEvent] = []
    last_fired_pivot_index: Optional[int] = None

    for t in range(1, n):
        # Swings knowable as of bar t are those confirmed (index + k) by t.
        known_highs = [i for i in swing_high_idx if i + k <= t]
        known_lows = [i for i in swing_low_idx if i + k <= t]
        if len(known_highs) < 2 or len(known_lows) < 2:
            continue
        h_prev_idx, h_last_idx = known_highs[-2], known_highs[-1]
        l_prev_idx, l_last_idx = known_lows[-2], known_lows[-1]
        if not (highs[h_last_idx] > highs[h_prev_idx]):
            continue
        if not (lows[l_last_idx] > lows[l_prev_idx]):
            continue

        pivot_price = highs[h_last_idx]
        if h_last_idx == last_fired_pivot_index:
            continue  # already fired for this exact pivot
        if closes[t - 1] <= pivot_price < closes[t]:
            events.append(BreakoutEvent(
                index=t, pivot_price=pivot_price, pivot_index=h_last_idx,
                prior_swing_high=highs[h_prev_idx],
                swing_low_price=lows[l_last_idx], prior_swing_low=lows[l_prev_idx],
            ))
            last_fired_pivot_index = h_last_idx

    return events


def true_range_series(highs: List[float], lows: List[float], closes: List[float]) -> List[float]:
    """Plain True Range per bar (no smoothing) - bar 0 falls back to a
    plain high-low range since there's no previous close yet."""
    n = len(closes)
    tr = [0.0] * n
    for i in range(n):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
    return tr


def coil_tightness_score(
    highs: List[float], lows: List[float], closes: List[float],
    breakout_index: int, coil_bars: int = 12, baseline_bars: int = 60,
) -> Optional[float]:
    """How tightly price was coiled in the `coil_bars` immediately before
    the breakout, relative to a longer `baseline_bars` window ending at
    the same point - the intraday-scale analogue of VCP's "volume/range
    dry-up" (trading-skills/learnings/technical-patterns/vcp.md), just
    measured via ATR contraction instead of Minervini's own multi-week
    contraction-depth sequence (that pattern is explicitly daily-chart-
    only per that file's own "adapting this for DhanBoy" section - this
    is the intraday-scale equivalent, closer to a flag/pennant).

    Returns a score in [0, 1] - 1.0 means the immediate pre-breakout ATR
    was near zero relative to the baseline (maximally tight coil), 0.0
    means it was AS wide as the baseline or wider (no real contraction
    at all). None if there isn't enough history yet for the baseline
    window (early in the series)."""
    if breakout_index < baseline_bars:
        return None
    tr = true_range_series(highs, lows, closes)
    baseline_window = tr[breakout_index - baseline_bars:breakout_index]
    coil_window = tr[breakout_index - coil_bars:breakout_index]
    baseline_atr = sum(baseline_window) / len(baseline_window)
    coil_atr = sum(coil_window) / len(coil_window)
    if baseline_atr <= 0:
        return None
    ratio = coil_atr / baseline_atr
    return max(0.0, min(1.0, 1.0 - ratio))


def rvol_score(
    volumes: List[float], breakout_index: int, bars_per_day: int, min_days: int = 5,
) -> Optional[float]:
    """Relative Volume on the breakout bar - its own volume divided by
    the historical AVERAGE volume for that exact same bar-of-day (time-
    of-day normalized, per the sourced approach in trading-skills/
    learnings/intraday-options-trading/momentum-entry-conditions-
    research.md - RVOL >= 2.0 cited as a genuine momentum threshold).
    Returns the raw ratio (not clamped to [0,1] - callers decide how to
    fold it into a composite score) so the caller can distinguish a 1.5x
    day from a 4x day. None if fewer than `min_days` prior same-slot
    bars exist yet to average."""
    slot = breakout_index % bars_per_day
    same_slot_volumes = [volumes[i] for i in range(slot, breakout_index, bars_per_day)]
    if len(same_slot_volumes) < min_days:
        return None
    avg = sum(same_slot_volumes) / len(same_slot_volumes)
    if avg <= 0:
        return None
    return volumes[breakout_index] / avg


def freshness_score(breakout_index: int, current_index: int, decay_bars: int = 12) -> float:
    """1.0 at the exact breakout bar, decaying linearly to 0.0 by
    `decay_bars` bars later - same "just happened beats an hour ago"
    principle Swing's own `_entry_candidate_rank_key` already uses for
    the live Supertrend crossover (freshness of the crossover candle),
    just continuous instead of a plain sort key since this feeds a
    weighted composite score. Never negative - a candidate scored long
    after its own breakout just contributes 0 to this component rather
    than penalizing below zero."""
    bars_since = current_index - breakout_index
    if bars_since <= 0:
        return 1.0
    return max(0.0, 1.0 - bars_since / decay_bars)


def extension_score(pivot_price: float, current_price: float, atr_at_breakout: float, cap_atr_multiples: float = 2.0) -> float:
    """Penalizes a candidate that's already run far above its own pivot
    by the time it's being scored - chasing an extended move is lower-
    quality than catching it right at the breakout. Measured in ATR
    multiples (not a flat %) so it scales sensibly across both cheap and
    expensive stocks. 1.0 right at the pivot, decaying to 0.0 by
    `cap_atr_multiples` ATRs above it; a price at or below the pivot
    (shouldn't normally happen right after a breakout, but a wide
    scoring window could see it) also scores 1.0 rather than a
    meaningless >1 value."""
    if atr_at_breakout <= 0:
        return 1.0
    extension_in_atrs = (current_price - pivot_price) / atr_at_breakout
    if extension_in_atrs <= 0:
        return 1.0
    return max(0.0, 1.0 - extension_in_atrs / cap_atr_multiples)


DEFAULT_WEIGHTS = {"coil": 0.30, "freshness": 0.25, "rvol": 0.25, "extension": 0.20}


def composite_momentum_score(
    coil: Optional[float], freshness: float, rvol: Optional[float], extension: float,
    weights: Optional[dict] = None,
) -> Optional[float]:
    """Combines the four components into one [0, 1]-ish score for
    ranking candidates against each other. `rvol` is folded in via a
    simple 1.0-at-RVOL=2.0 saturating scale (clamped to [0,1]) so it
    lives on the same footing as the other three components rather than
    an unbounded ratio dominating the sum. Returns None if `coil` or
    `rvol` themselves are None (not enough history yet to score this
    candidate at all) - callers should treat that the same as "no score
    available", never as a 0.

    Weights are a plain dict so the backtest script can sweep them
    without touching this function - defaults are a starting point
    (equal-ish, mild lean toward coil tightness since that's the
    highest-conviction VCP-style signal), not a tuned result."""
    if coil is None or rvol is None:
        return None
    w = weights or DEFAULT_WEIGHTS
    rvol_component = max(0.0, min(1.0, rvol / 2.0))
    return (
        w["coil"] * coil
        + w["freshness"] * freshness
        + w["rvol"] * rvol_component
        + w["extension"] * extension
    )
