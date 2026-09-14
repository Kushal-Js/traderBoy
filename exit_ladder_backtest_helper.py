"""
Shared backtest helper (added 14 Sep 2026, user request: "add it to the
shared methodology and re-verify... rather create a backtest plan and
keep it in sync with real entry exit conditions"). Reconstructs the FULL,
verified exit-priority ladder from Options/trading_engine.py's
_exit_reason_for - the single function every backtest script in this repo
should call from here on, instead of each one reimplementing its own
partial copy of the ladder inline. That per-script duplication is exactly
how EMA_CROSS_EXIT and the liquidity guard went unmodeled across every
backtest run this session (Options CE, Futures CE, Options PE, 4
different CSVs) despite both being live-enabled in production - each
script's author (this session) copied the same incomplete ladder forward
without re-deriving it from the real code each time.

Verified against Options/trading_engine.py._exit_reason_for and
Options/dhan_client.py's refresh_ema_cross_signal/refresh_liquidity_signal
on 14 Sep 2026. Live-deployed flags checked directly (not assumed from
code defaults) via the droplet's .env:
  ENABLE_TARGET_EXIT=true, FUTURES_ENABLE_TARGET_EXIT=true,
    LUXURY_ENABLE_TARGET_EXIT=true (all on, contrary to code's own
    default=on-but-off-for-Futures comment - .env overrides it uniformly)
  ENABLE_EMA_CROSS_EXIT=true, FUTURES_ENABLE_EMA_CROSS_EXIT=true,
    LUXURY_ENABLE_EMA_CROSS_EXIT=true (all on, contrary to code's own
    default=off-except-Futures comment)
  ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF=true, FUTURES_/LUXURY_ same (all on,
    contrary to code's own default=False comment)
  LIQUIDITY_GUARD_ENABLED - no per-package override in .env, so all three
    run on the code default (true)
This mismatch between code-comment defaults and actual deployed .env
values is exactly why this module reads flags from Options/config.py at
import time (which itself reads the real .env) rather than hardcoding
anything from a comment - see BACKTEST_PLAN.md for the standing
discipline this enforces.

The exit-priority order implemented here, verbatim from
_exit_reason_for's own docstring order:
  1. MAX_LOSS_HIT (gated by ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF pre-cutoff,
     unconditional post-cutoff)
  2. TARGET_HIT (gated by ENABLE_TARGET_EXIT)
  3. PROFIT_PROTECTION_HIT (rupee threshold + giveback %)
  4. TRAILING_SL_HIT / STOP_LOSS_HIT (dynamic SL)
  5. SUPERTREND_EXIT (gated by ENABLE_SUPERTREND_EXIT) - direction-aware:
     bearish crossover exits CE, bullish exits PE
  6. EMA_CROSS_EXIT (gated by ENABLE_EMA_CROSS_EXIT) - direction-aware,
     same sense as Supertrend, checked right after it
  7. LIQUIDITY_GUARD_ZERO_VOLUME (gated by LIQUIDITY_GUARD_ENABLED) -
     checked LAST, independent of the price-threshold checks above; N
     consecutive zero-volume 1-min bars on the OPTION's own contract

NOT modeled (same standing disclosure as ever - see BACKTEST_PLAN.md):
  - SL-L broker-side stop-loss (real effect can only tighten a loss,
    never worsen it).
  - cross_strategy_registry (Options/Futures/Luxury mutual symbol lock).
  - Nifty gap-down/sharp-fall CE delay is NOT part of this module - it's
    an ENTRY gate, not an exit condition, and already has its own shared
    helper (nifty_gap_down_backtest_helper.py). CE-only, never applies to
    PE, per should_delay_ce_entry's own contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from Options.dhan_client import _compute_ema  # noqa: E402
from Options import config as options_config  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def build_full_minute_grid(underlying_data_by_symbol: dict, market_open: str = "09:15",
                            market_close: str = "15:29") -> list:
    """Fixes the documented "sim-clock trap" (trading-skills repo,
    learnings/backtest-methodology.md, found 10 Sep 2026): Dhan's 5-min
    NSE_EQ candles stop at the 15:10 bar (verified live again 14 Sep 2026 -
    still true), so a simulation clock built by extending 5-min underlying
    timestamps (e.g. `ts + 60*k for k in range(5)`) never produces any
    minute past ~15:14 each day. Every backtest script this session built
    that way (before this fix) was blind to the last ~15 minutes of each
    trading day - end-of-day PP/MAX_LOSS/trailing-SL/target checks never
    ran on those minutes, and a position still open at that point "jumps"
    straight to the next trading day's first checked minute, which can
    misattribute an overnight/weekend price move as if it happened on a
    single tick (the documented HINDALCO/VEDL phantom-MAX_LOSS_HIT case).

    This builds the clock independently of any candle data: every trading
    DAY present in any symbol's 5-min series contributes a full 1-minute
    grid from market_open to market_close (inclusive), regardless of
    what timestamps that day's 5-min series actually contains. Entry-alert
    timing also becomes more precise as a side effect - alerts are checked
    at their exact minute rather than snapped to the nearest 5-min-derived
    grid point."""
    all_days = set()
    for d in underlying_data_by_symbol.values():
        for ts in d["timestamps"]:
            all_days.add(datetime.fromtimestamp(ts, tz=IST).date())
    open_h, open_m = (int(x) for x in market_open.split(":"))
    close_h, close_m = (int(x) for x in market_close.split(":"))
    grid = []
    for day in sorted(all_days):
        t = datetime.combine(day, dtime(open_h, open_m), tzinfo=IST)
        end = datetime.combine(day, dtime(close_h, close_m), tzinfo=IST)
        while t <= end:
            grid.append(int(t.timestamp()))
            t += timedelta(minutes=1)
    return grid


def idx_at_or_before(ts_list, target_ts):
    best = None
    for i, ts in enumerate(ts_list):
        if ts <= target_ts:
            best = i
        else:
            break
    return best


def idx_at_or_after(ts_list, target_ts):
    for i, ts in enumerate(ts_list):
        if ts >= target_ts:
            return i
    return None


def compute_ema_cross_series(closes: list[float], fast_period: int, slow_period: int) -> list:
    """Returns one entry per bar index: None until both EMAs are warm,
    otherwise (crossed_this_candle: bool, fast_below_slow: bool) - mirrors
    refresh_ema_cross_signal's own fast_ema[-1]<slow_ema[-1] /
    crossed_this_candle computation, just for every historical bar instead
    of only the latest one."""
    fast = _compute_ema(closes, fast_period)
    slow = _compute_ema(closes, slow_period)
    n = len(closes)
    result: list = [None] * n
    for i in range(1, n):
        if fast[i] is None or slow[i] is None or fast[i - 1] is None or slow[i - 1] is None:
            continue
        fast_below_slow = fast[i] < slow[i]
        prev_fast_below_slow = fast[i - 1] < slow[i - 1]
        result[i] = (fast_below_slow != prev_fast_below_slow, fast_below_slow)
    return result


def is_illiquid_at(volumes: list, option_ts_list: list, t: int, zero_volume_bars: int,
                    option_interval_seconds: int = 60) -> bool:
    """True if the zero_volume_bars fully-closed 1-min option bars at-or-
    before t are ALL zero volume - mirrors refresh_liquidity_signal's own
    `all(v == 0 for v in volumes[-n:])` check, evaluated at a historical
    point in time instead of "now".

    Uses t - option_interval_seconds, same fix and same reasoning as
    evaluate_exit_reason's Supertrend/EMA-cross lookups: the live code
    explicitly drops the still-forming candle before checking
    ("Drop the current, still-forming candle if Dhan included one" - see
    refresh_liquidity_signal's own comment) - a bar timestamped exactly at
    t hasn't closed yet at time t, so including it would let the guard
    fire on data from a bar that, in real time, doesn't exist yet."""
    idx = idx_at_or_before(option_ts_list, t - option_interval_seconds)
    if idx is None or idx + 1 < zero_volume_bars:
        return False
    window = volumes[idx + 1 - zero_volume_bars: idx + 1]
    return len(window) == zero_volume_bars and all(v == 0 for v in window)


class ExitLadderConfig:
    """Snapshots the live config values this ladder needs, read once at
    construction (not per-call).

    IMPORTANT: most of these flags are PER-PACKAGE, not shared from
    Options - Futures and Luxury each carry their own FUTURES_/LUXURY_-
    prefixed override for ENABLE_TARGET_EXIT, ENABLE_MAX_LOSS_HIT_BEFORE_
    CUTOFF, ENABLE_SUPERTREND_EXIT, ENABLE_EMA_CROSS_EXIT, LIQUIDITY_GUARD_
    ENABLED, and RISK_THRESHOLD_CUTOFF_TIME (verified by grepping
    Futures/config.py and Luxury/config.py directly on 14 Sep 2026, not
    assumed) - an earlier draft of this class hardcoded everything to
    Options/config.py, which would have silently used OPTIONS' flags for
    a Futures or Luxury backtest. Pass the correct package config module
    (Options.config / Futures.config / Luxury.config) as `pkg_config`.

    Only the numeric EMA-cross periods (EMA_CROSS_FAST_PERIOD/_SLOW_
    PERIOD) and the liquidity-guard bar count (LIQUIDITY_GUARD_ZERO_
    VOLUME_BARS) are genuinely shared, undeclared in Futures/Luxury's own
    config.py - always read from Options.config regardless of pkg_config,
    same reasoning as the RSI/Nifty-gap-down thresholds in
    nifty_gap_down_backtest_helper.py."""

    def __init__(self, pkg_config=options_config):
        self.enable_max_loss_before_cutoff = pkg_config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF
        self.enable_target_exit = pkg_config.ENABLE_TARGET_EXIT
        # giveback_pct deliberately NOT read here - every backtest script this
        # session supports a PP_GIVEBACK_PCT argv override for PP/giveback
        # sweeps, so it's passed into evaluate_exit_reason() directly by the
        # caller (same pattern as current_max_loss_rs/current_profit_
        # protection_rs below) rather than baked into this snapshot, which
        # would silently ignore that override.
        self.enable_supertrend_exit = pkg_config.ENABLE_SUPERTREND_EXIT
        self.enable_ema_cross_exit = pkg_config.ENABLE_EMA_CROSS_EXIT
        self.ema_cross_fast = options_config.EMA_CROSS_FAST_PERIOD
        self.ema_cross_slow = options_config.EMA_CROSS_SLOW_PERIOD
        self.liquidity_guard_enabled = pkg_config.LIQUIDITY_GUARD_ENABLED
        self.liquidity_guard_zero_volume_bars = options_config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS
        cutoff_h, cutoff_m = (int(x) for x in pkg_config.RISK_THRESHOLD_CUTOFF_TIME.split(":"))
        self.cutoff_time = dtime(cutoff_h, cutoff_m)


def evaluate_exit_reason(
    *, option_type: str, entry_price: float, highest_price: float, hard_stop_loss: float,
    target_price: float, trailing_sl: float, premium: float, qty: int, dt: datetime,
    current_max_loss_rs: float, current_profit_protection_rs: float, giveback_pct: float,
    underlying_ts_list: list, underlying_closes: list, supertrend_values: list,
    ema_cross_series: list, entry_candle_ts: int, t: int,
    option_volumes: list, option_ts_list: list,
    ladder_config: ExitLadderConfig, candle_interval_seconds: int = 300,
) -> str | None:
    """The one shared exit-reason evaluator every backtest script should
    call - a faithful port of Options/trading_engine.py's _exit_reason_for,
    verified against that function directly (see this module's own
    docstring for the verification date and the live-deployed flag values
    checked). option_type is "CE" or "PE" - flips the Supertrend/EMA-cross
    direction check the same way the real code does.

    Caller is responsible for computing current_max_loss_rs/
    current_profit_protection_rs (the before/after-RISK_THRESHOLD_CUTOFF_
    TIME split), giveback_pct (PP_GIVEBACK_PCT, argv-overridable per
    script), and trailing_sl (the dynamic-SL step logic) themselves, same
    as before - those are unchanged from every prior backtest this session
    and aren't part of what was missing."""
    loss_rs = (entry_price - premium) * qty
    before_cutoff = dt.time() < ladder_config.cutoff_time
    if (ladder_config.enable_max_loss_before_cutoff or not before_cutoff) and loss_rs >= current_max_loss_rs:
        return "MAX_LOSS_HIT"

    if ladder_config.enable_target_exit and premium >= target_price:
        return "TARGET_HIT"

    peak_profit_rs = (highest_price - entry_price) * qty
    giveback_floor = highest_price * (1 - giveback_pct)
    if peak_profit_rs > current_profit_protection_rs and premium < giveback_floor:
        return "PROFIT_PROTECTION_HIT"

    if premium <= trailing_sl:
        return "TRAILING_SL_HIT" if trailing_sl > hard_stop_loss else "STOP_LOSS_HIT"

    # FIXED 14 Sep 2026 (found while sanity-checking the "04" re-run's
    # results): this used to look up u_idx via idx_at_or_after(t), which
    # finds the NEXT candle boundary at-or-after t - almost always a
    # still-FORMING candle, not the last one that's actually closed. That
    # made "underlying_ts_list[u_idx] > entry_candle_ts" go true almost
    # immediately after entry (as soon as t ticks past the entry candle's
    # own start), defeating the entry-candle-skip protection within
    # ~1 minute and firing SUPERTREND_EXIT/EMA_CROSS_EXIT at premium
    # basically unchanged from entry - visible as a wave of pnl=0.0 exits
    # exactly 1 minute after entry once EMA_CROSS_EXIT/the liquidity guard
    # made the pattern common enough to notice.
    #
    # The live code reads a CACHED "last fully-closed candle" value,
    # refreshed periodically - it never sees a still-forming candle. The
    # backtest equivalent of "the last candle that has actually closed as
    # of time t" is the candle whose start is at-or-before (t - the
    # candle's own interval), not at-or-after t. Using idx_at_or_before on
    # (t - candle_interval_seconds) reproduces that correctly: at
    # t = entry+1min the answer is still the PRE-entry candle (the entry
    # candle itself hasn't closed yet), and the first candle that can ever
    # trigger an exit is the one immediately after the entry candle, and
    # only once IT has actually closed - matching the live code's own
    # entry-candle-skip semantics exactly instead of defeating it.
    if ladder_config.enable_supertrend_exit:
        u_idx = idx_at_or_before(underlying_ts_list, t - candle_interval_seconds)
        if (u_idx is not None and underlying_ts_list[u_idx] > entry_candle_ts
                and supertrend_values[u_idx] is not None):
            is_bearish = underlying_closes[u_idx] < supertrend_values[u_idx]
            against_position = is_bearish if option_type == "CE" else (not is_bearish)
            if against_position:
                return "SUPERTREND_EXIT"

    if ladder_config.enable_ema_cross_exit:
        u_idx = idx_at_or_before(underlying_ts_list, t - candle_interval_seconds)
        if (u_idx is not None and underlying_ts_list[u_idx] > entry_candle_ts
                and ema_cross_series[u_idx] is not None):
            crossed, fast_below_slow = ema_cross_series[u_idx]
            against_position = fast_below_slow if option_type == "CE" else (not fast_below_slow)
            if crossed and against_position:
                return "EMA_CROSS_EXIT"

    if ladder_config.liquidity_guard_enabled:
        if is_illiquid_at(option_volumes, option_ts_list, t, ladder_config.liquidity_guard_zero_volume_bars):
            return "LIQUIDITY_GUARD_ZERO_VOLUME"

    return None
