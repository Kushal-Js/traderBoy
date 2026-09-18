"""
MA-ribbon-expansion scoring (added 18 Sep 2026, user request after
reviewing a SBILIFE chart that rose ~5% yesterday while being alerted to
Luxury ~28 times and never selected - see trading-skills' own note on why
that happened: rank_and_pick_top_stocks() only compares candidates within
ONE alert batch by raw day-change%, so a stock that's structurally in a
much stronger position (a clean multi-EMA breakout) than its batch-mates
can still lose the ranking on a given minute's %change alone).

This module scores a SINGLE stock's current 5-min chart structure against
the "textbook momentum breakout" shape discussed with the user: a tight
multi-EMA ribbon (compression) that recently broke out (trigger), is now
fanning apart in bullish order (fan-out), and - if a pullback is
currently under way - is holding its structure rather than reversing.
It is designed to run on EVERY candidate in EVERY incoming alert, all day,
not just the top-N/bottom-N slice rank_and_pick_top_stocks already keeps -
producing one comparable score per candidate so a "materially better
setup came in" decision can be made against whatever is currently held.

STATUS: a scoring/backtest PROTOTYPE only. Nothing in this file is wired
into any package's live trading_engine.py - see decide_switch()'s own
docstring for exactly what would need to change (and be explicitly
approved) before this could ever cause a real exit/entry. Building this
and backtesting it against real data is the current, explicitly-scoped
ask; wiring it live is a separate decision the user will make after
reviewing backtest results.

Every threshold below (lookback window, tightness/expansion cutoffs, the
switch margin, the 15-minute grace period) is a first-pass, explicitly
documented judgment call, not a fitted/optimized parameter - flag them for
review rather than treating them as settled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from Options.dhan_client import _compute_ema
from reversal_filters import _compute_adx, _compute_rsi, _efficiency_ratio_at  # noqa: F401 (RSI kept for future use)

# Fastest-to-slowest EMA periods forming the "ribbon" - a GMMA-style short-
# term-trader group (9/20) plus a medium/slow pair (50/100) wide enough to
# show real compression-vs-expansion structure on 5-min bars. Not the same
# thing as Options.config's own fast/slow EMA-cross pair (used for exits,
# just two EMAs) - this is a genuinely separate, wider ribbon built to see
# the *shape* of a move, not just a single crossover.
RIBBON_PERIODS: tuple[int, ...] = (9, 20, 50, 100)

# How many bars back to look for a compression trough / breakout trigger.
# 15 bars of 5-min data = 75 minutes - long enough to catch a base that
# formed this morning, short enough that a "recent" squeeze isn't actually
# from hours ago.
DEFAULT_LOOKBACK_BARS = 15

# A ribbon width (max EMA - min EMA, as % of price) at or below this counts
# as "fully tight" for the compression score. 1% is a judgment call: on a
# ~100-500 Rs F&O underlying, a sub-1% spread across 4 EMAs is a genuinely
# tangled ribbon, not just an ordinary quiet moment.
FULLY_TIGHT_WIDTH_PCT = 0.01

# The ribbon must have widened at least this much off its own recent
# trough before "compression is over" counts for anything - otherwise
# every bar in a slow drift would score as "just came out of a squeeze."
MIN_EXPANSION_OFF_TROUGH = 0.5

# Reuses reversal_filters.check_trend_strength's own ADX/ER cutoffs (see
# that function's docstring) rather than inventing a second, inconsistent
# trend-strength bar in the same codebase.
ADX_CONFIRM_THRESHOLD = 20.0
ER_CONFIRM_THRESHOLD = 0.3

# A candidate scoring below this on the 0-100 scale isn't worth entering
# at all, regardless of how it compares to its alert-batch-mates - the
# real, backtest-derived quality bar rank_and_pick_top_stocks' day-
# change%-only ranking never had (that ranking always returns top_n
# candidates no matter how weak the whole batch is). Validated against
# 2026-09-17's real alerts in backtest_ribbon_expansion_sep17.py.
MIN_ENTRY_SCORE = 50.0


@dataclass
class RibbonScore:
    total: float
    compression: float
    trigger: float
    fanout: float
    pullback: float
    confirmation: float
    bars_since_trigger: Optional[int]
    detail: dict = field(default_factory=dict)


def _ribbon_width_pct(ribbon_values: list[Optional[float]], close: float) -> Optional[float]:
    """(max EMA - min EMA) / close at one bar - how tangled (small) or
    spread out (large) the ribbon is. None if any EMA isn't warm yet."""
    if close <= 0 or any(v is None for v in ribbon_values):
        return None
    return (max(ribbon_values) - min(ribbon_values)) / close


def _is_stacked_bullish(ribbon_values: list[Optional[float]]) -> bool:
    """True if every EMA is strictly above the next-slower one, fastest
    first (RIBBON_PERIODS is already ordered fast->slow) - the "ribbon in
    clean bullish order" condition a fanned-out uptrend produces."""
    if any(v is None for v in ribbon_values):
        return False
    return all(ribbon_values[i] > ribbon_values[i + 1] for i in range(len(ribbon_values) - 1))


def _is_stacked_bearish(ribbon_values: list[Optional[float]]) -> bool:
    """Mirror of _is_stacked_bullish - every EMA strictly BELOW the next-
    slower one, fastest first: the "ribbon in clean bearish order"
    condition a fanned-out downtrend produces."""
    if any(v is None for v in ribbon_values):
        return False
    return all(ribbon_values[i] < ribbon_values[i + 1] for i in range(len(ribbon_values) - 1))


def score_ribbon_expansion(
    highs: list[float], lows: list[float], closes: list[float],
    *, lookback_bars: int = DEFAULT_LOOKBACK_BARS, symbol: str = "", as_of_label: str = "",
) -> Optional[RibbonScore]:
    """Scores the LAST bar in `closes` (caller must already have dropped
    any still-forming candle - same convention as every other indicator in
    this codebase). Returns None if there isn't enough warmed-up history
    (needs RIBBON_PERIODS' slowest EMA - 100 bars - fully seeded, plus
    `lookback_bars` of margin) rather than guessing on partial data.

    Combines five components into one 0-100 score:
      - compression (25%): was there a genuinely tight squeeze recently,
        and has the ribbon since started re-opening off it?
      - trigger (25%): how many bars ago did price last close above the
        WHOLE ribbon for the first time since that squeeze (a full-ribbon
        breakout, not just a single fast-EMA poke)?
      - fanout (25%): is the ribbon currently stacked in bullish order,
        and still widening bar-over-bar (not plateaued/reversing)?
      - pullback (15%, can go NEGATIVE): if the last bar is a down bar,
        is price still holding above the fastest EMA with the ribbon still
        stacked (a healthy buy-the-dip moment - scores +100) or has that
        structure broken (a real reversal warning - scores -100)? Zero if
        the last bar isn't a pullback at all (nothing to reward or punish).
      - confirmation (10%): ADX/ER/bullish-fast-over-next-EMA, the same
        bar reversal_filters.check_trend_strength already uses elsewhere.

    The final total is clamped to [0, 100] for a clean leaderboard number;
    `detail["structure_broken"]` is where the pullback penalty actually
    surfaces if the clamp hides it."""
    n = len(closes)
    if n < 100 + lookback_bars:
        return None
    idx = n - 1

    emas = {p: _compute_ema(closes, p) for p in RIBBON_PERIODS}
    if any(emas[p][idx] is None for p in RIBBON_PERIODS):
        return None
    adx = _compute_adx(highs, lows, closes)

    def ribbon_at(i: int) -> list[Optional[float]]:
        return [emas[p][i] for p in RIBBON_PERIODS]

    window = range(max(0, idx - lookback_bars), idx + 1)
    widths = [(i, _ribbon_width_pct(ribbon_at(i), closes[i])) for i in window]
    widths = [(i, w) for i, w in widths if w is not None]
    if len(widths) < 5:
        return None

    # --- 1. compression -------------------------------------------------
    tightest_i, tightest_w = min(widths, key=lambda t: t[1])
    bars_since_tightest = idx - tightest_i
    current_w = widths[-1][1]
    expanded_since = (current_w - tightest_w) / tightest_w if tightest_w > 0 else float("inf")

    compression_score = 0.0
    if expanded_since >= MIN_EXPANSION_OFF_TROUGH:
        recency_factor = max(0.0, 1 - bars_since_tightest / lookback_bars)
        tightness_factor = max(0.0, 1 - min(tightest_w / FULLY_TIGHT_WIDTH_PCT, 1.0))
        compression_score = 100 * 0.5 * (recency_factor + tightness_factor)

    # --- 2. trigger -------------------------------------------------------
    # Latest bar, at/after the tightest point, where price closed above the
    # WHOLE ribbon having not been above it the bar before - a genuine
    # first-breakout-through-the-ribbon event, not just re-testing highs
    # already made after breaking out once.
    trigger_i: Optional[int] = None
    for i in range(tightest_i, idx + 1):
        vals = ribbon_at(i)
        if any(v is None for v in vals):
            continue
        was_above = i > 0 and all(v is not None for v in ribbon_at(i - 1)) and closes[i - 1] > max(ribbon_at(i - 1))
        if closes[i] > max(vals) and not was_above:
            trigger_i = i
    bars_since_trigger = (idx - trigger_i) if trigger_i is not None else None
    trigger_score = 100 * max(0.0, 1 - bars_since_trigger / lookback_bars) if bars_since_trigger is not None else 0.0

    # --- 3. fan-out -------------------------------------------------------
    stacked = _is_stacked_bullish(ribbon_at(idx))
    widening = len(widths) >= 3 and widths[-1][1] > widths[-3][1]
    fanout_score = 0.0
    if stacked:
        fanout_score = 100.0 if widening else 60.0

    # --- 4. pullback-without-structure-break -------------------------------
    recent_down = idx >= 1 and closes[idx] < closes[idx - 1]
    fastest_ema_now = emas[RIBBON_PERIODS[0]][idx]
    above_fast_ema = fastest_ema_now is not None and closes[idx] >= fastest_ema_now
    pullback_score = 0.0
    structure_broken = False
    if recent_down:
        if stacked and above_fast_ema:
            pullback_score = 100.0
        else:
            structure_broken = True
            pullback_score = -100.0

    # --- 5. technical-filter confirmation ---------------------------------
    adx_now = adx[idx]
    er_now = _efficiency_ratio_at(closes, idx)
    bullish_cross = emas[RIBBON_PERIODS[0]][idx] > emas[RIBBON_PERIODS[1]][idx]
    confirms = sum([
        adx_now is not None and adx_now >= ADX_CONFIRM_THRESHOLD,
        er_now is not None and er_now >= ER_CONFIRM_THRESHOLD,
        bullish_cross,
    ])
    confirmation_score = 100 * confirms / 3

    raw_total = (
        0.25 * compression_score + 0.25 * trigger_score + 0.25 * fanout_score
        + 0.15 * pullback_score + 0.10 * confirmation_score
    )
    total = max(0.0, min(100.0, raw_total))

    return RibbonScore(
        total=round(total, 2), compression=round(compression_score, 2),
        trigger=round(trigger_score, 2), fanout=round(fanout_score, 2),
        pullback=round(pullback_score, 2), confirmation=round(confirmation_score, 2),
        bars_since_trigger=bars_since_trigger,
        detail={
            "symbol": symbol, "as_of": as_of_label, "stacked": stacked, "widening": widening,
            "structure_broken": structure_broken,
            "adx": round(adx_now, 2) if adx_now is not None else None,
            "er": round(er_now, 3) if er_now is not None else None,
            "ribbon_width_pct_now": round(current_w * 100, 3),
            "ribbon_width_pct_tightest": round(tightest_w * 100, 3),
            "bars_since_tightest": bars_since_tightest,
        },
    )


def score_ribbon_breakdown(
    highs: list[float], lows: list[float], closes: list[float],
    *, lookback_bars: int = DEFAULT_LOOKBACK_BARS, symbol: str = "", as_of_label: str = "",
) -> Optional[RibbonScore]:
    """PE/bearish mirror of score_ribbon_expansion (added 18 Sep 2026, user
    request to extend ranking-only to PE alerts). Same five components,
    same weights, same 0-100 scale - every comparison is simply inverted:
    a tight ribbon that breaks DOWN through the whole ribbon, fans out in
    DESCENDING (bearish) order, and - if a bounce is under way - holds
    below the fastest EMA rather than reclaiming it.

    IMPORTANT CAVEAT: unlike score_ribbon_expansion, this function has NOT
    been backtested against real data - the 2026-09-17 backtest (see
    backtest_ribbon_expansion_sep17.py) was explicitly CE-only, because
    shadow_evaluator.py's own simulated fills are always CE-ATM regardless
    of alert direction. This is built on the reasonable, standard
    assumption that a ribbon-compression/expansion pattern is direction-
    symmetric (a well-established idea in technical analysis - the same
    reasoning already applied when reusing check_trend_strength's ADX/ER
    thresholds here), not on its own empirical confirmation. Treat its
    real-world performance as unproven until it has its own trade history
    to look back on."""
    n = len(closes)
    if n < 100 + lookback_bars:
        return None
    idx = n - 1

    emas = {p: _compute_ema(closes, p) for p in RIBBON_PERIODS}
    if any(emas[p][idx] is None for p in RIBBON_PERIODS):
        return None
    adx = _compute_adx(highs, lows, closes)

    def ribbon_at(i: int) -> list[Optional[float]]:
        return [emas[p][i] for p in RIBBON_PERIODS]

    window = range(max(0, idx - lookback_bars), idx + 1)
    widths = [(i, _ribbon_width_pct(ribbon_at(i), closes[i])) for i in window]
    widths = [(i, w) for i, w in widths if w is not None]
    if len(widths) < 5:
        return None

    # --- 1. compression (direction-agnostic - identical to the bullish version) ---
    tightest_i, tightest_w = min(widths, key=lambda t: t[1])
    bars_since_tightest = idx - tightest_i
    current_w = widths[-1][1]
    expanded_since = (current_w - tightest_w) / tightest_w if tightest_w > 0 else float("inf")

    compression_score = 0.0
    if expanded_since >= MIN_EXPANSION_OFF_TROUGH:
        recency_factor = max(0.0, 1 - bars_since_tightest / lookback_bars)
        tightness_factor = max(0.0, 1 - min(tightest_w / FULLY_TIGHT_WIDTH_PCT, 1.0))
        compression_score = 100 * 0.5 * (recency_factor + tightness_factor)

    # --- 2. trigger - latest bar price closed BELOW the whole ribbon, having
    # not been below it the bar before (a genuine first-breakdown event) ---
    trigger_i: Optional[int] = None
    for i in range(tightest_i, idx + 1):
        vals = ribbon_at(i)
        if any(v is None for v in vals):
            continue
        was_below = i > 0 and all(v is not None for v in ribbon_at(i - 1)) and closes[i - 1] < min(ribbon_at(i - 1))
        if closes[i] < min(vals) and not was_below:
            trigger_i = i
    bars_since_trigger = (idx - trigger_i) if trigger_i is not None else None
    trigger_score = 100 * max(0.0, 1 - bars_since_trigger / lookback_bars) if bars_since_trigger is not None else 0.0

    # --- 3. fan-out - ribbon stacked bearish, still widening ---
    stacked = _is_stacked_bearish(ribbon_at(idx))
    widening = len(widths) >= 3 and widths[-1][1] > widths[-3][1]
    fanout_score = 0.0
    if stacked:
        fanout_score = 100.0 if widening else 60.0

    # --- 4. pullback (bounce)-without-structure-break ---
    # A "pullback" against a downtrend is an UP bar; healthy if price stays
    # BELOW the fastest EMA with the ribbon still bearishly stacked.
    recent_up = idx >= 1 and closes[idx] > closes[idx - 1]
    fastest_ema_now = emas[RIBBON_PERIODS[0]][idx]
    below_fast_ema = fastest_ema_now is not None and closes[idx] <= fastest_ema_now
    pullback_score = 0.0
    structure_broken = False
    if recent_up:
        if stacked and below_fast_ema:
            pullback_score = 100.0
        else:
            structure_broken = True
            pullback_score = -100.0

    # --- 5. technical-filter confirmation - ADX/ER thresholds unchanged
    # (trend strength/efficiency are direction-agnostic); the EMA-cross
    # check inverts to fastest-below-next (a bearish cross). ---
    adx_now = adx[idx]
    er_now = _efficiency_ratio_at(closes, idx)
    bearish_cross = emas[RIBBON_PERIODS[0]][idx] < emas[RIBBON_PERIODS[1]][idx]
    confirms = sum([
        adx_now is not None and adx_now >= ADX_CONFIRM_THRESHOLD,
        er_now is not None and er_now >= ER_CONFIRM_THRESHOLD,
        bearish_cross,
    ])
    confirmation_score = 100 * confirms / 3

    raw_total = (
        0.25 * compression_score + 0.25 * trigger_score + 0.25 * fanout_score
        + 0.15 * pullback_score + 0.10 * confirmation_score
    )
    total = max(0.0, min(100.0, raw_total))

    return RibbonScore(
        total=round(total, 2), compression=round(compression_score, 2),
        trigger=round(trigger_score, 2), fanout=round(fanout_score, 2),
        pullback=round(pullback_score, 2), confirmation=round(confirmation_score, 2),
        bars_since_trigger=bars_since_trigger,
        detail={
            "symbol": symbol, "as_of": as_of_label, "stacked": stacked, "widening": widening,
            "structure_broken": structure_broken,
            "adx": round(adx_now, 2) if adx_now is not None else None,
            "er": round(er_now, 3) if er_now is not None else None,
            "ribbon_width_pct_now": round(current_w * 100, 3),
            "ribbon_width_pct_tightest": round(tightest_w * 100, 3),
            "bars_since_tightest": bars_since_tightest,
        },
    )


# --------------------------------------------------------------------- #
# Position-switching decision (prototype - see this module's own header)
# --------------------------------------------------------------------- #

# "wait if it's showing momentum for 15 mins" (user's own wording) - never
# switch a position out within its first N minutes, regardless of how much
# a competing candidate scores, so a fresh entry always gets a fair chance
# to develop before being pre-empted.
MIN_HOLD_MINUTES_BEFORE_SWITCH = 15.0

# A candidate must beat the WEAKEST open position's current score by at
# least this many points before a switch fires - without a margin, two
# nearly-tied scores would cause constant back-and-forth churn (exit fees,
# missed re-entries) for no real edge.
SWITCH_SCORE_MARGIN = 15.0


def held_position_health(score: RibbonScore) -> float:
    """A currently-open position's ongoing health, deliberately NOT the
    same as score_ribbon_expansion()'s full `.total` (added 18 Sep 2026,
    after backtesting against real 2026-09-17 data showed the full score
    causing near-constant switching - e.g. SRF went 81->69 over 25 minutes
    with its ribbon still perfectly stacked and widening the whole time,
    purely because `compression`/`trigger` decay bar-by-bar as "time since
    the breakout" grows. That decay is exactly what you want when scoring
    a fresh CANDIDATE (a breakout from 3 bars ago is a better entry than
    one from 20 bars ago) - it's wrong for judging whether an ALREADY-HELD
    position is still worth holding, which should only ask "is the trend
    still intact right now," not "how long ago did it start."

    So this uses only `fanout` (still stacked + still widening) and
    `confirmation` (ADX/ER/EMA-cross) - the two components that describe
    the CURRENT state of the trend rather than how long ago it began."""
    return 0.7 * score.fanout + 0.3 * score.confirmation


@dataclass
class OpenPositionForSwitch:
    symbol: str
    score: RibbonScore
    held_minutes: float


@dataclass
class SwitchDecision:
    should_switch: bool
    exit_symbol: Optional[str]
    reason: str


def decide_switch(
    open_positions: list[OpenPositionForSwitch], candidate_symbol: str,
    candidate_score: RibbonScore, max_positions: int,
) -> SwitchDecision:
    """Should a NEW higher-scoring candidate bump out an existing, weaker
    open position, given `max_positions` is this strategy/option-type's own
    real MAX_LIVE_POSITIONS_CE/_PE cap?

    Deliberately conservative: if there's still free capacity, this never
    recommends a switch - it only ever reallocates an already-full slot,
    never grows the strategy's real capital exposure past its existing cap.
    Only ever names the SINGLE weakest eligible open position as the one to
    exit, never more than one per candidate.

    NOT wired into any live trading_engine.py. To ever go live this would
    need, at minimum: real-time re-scoring of every open position (not just
    at entry), a new PositionStore.close_position() exit_reason (e.g.
    "SWITCHED_TO_HIGHER_SCORE"), and - per this repo's own live-trading
    safety practice - the user's explicit sign-off before deployment, the
    same as every other change to real trading behavior this session."""
    if len(open_positions) < max_positions:
        return SwitchDecision(False, None, "capacity_available_no_switch_needed")

    eligible = [p for p in open_positions if p.held_minutes >= MIN_HOLD_MINUTES_BEFORE_SWITCH]
    if not eligible:
        return SwitchDecision(False, None, "all_open_positions_still_within_15min_grace_period")

    weakest = min(eligible, key=lambda p: held_position_health(p.score))
    weakest_health = held_position_health(weakest.score)
    if candidate_score.total >= weakest_health + SWITCH_SCORE_MARGIN:
        return SwitchDecision(
            True, weakest.symbol,
            f"{candidate_symbol} scored {candidate_score.total} vs {weakest.symbol}'s "
            f"current health {round(weakest_health, 2)} (margin {SWITCH_SCORE_MARGIN})",
        )
    return SwitchDecision(False, None, "no_open_position_beaten_by_the_required_margin")
