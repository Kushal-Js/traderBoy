"""
Swing v2's own signal computation: the 5-min-vs-15-min 200-EMA regime
filter and the 5-min Supertrend crossover (see Swing/config.py's module
docstring for the full user request this implements).

Deliberately kept Swing-local rather than added to Options/dhan_client.py's
shared signal cache - matching this package's own pre-existing precedent
(its old Supertrend implementation, lifted into this file essentially
unchanged, was ALSO always kept independent of Options' shared cache,
specifically because that shared cache is live real-money exit protection
for Options/Futures/Luxury, and no other package wants a 200-EMA regime
signal anyway). The only thing genuinely shared is the pure indicator
math (_compute_ema/_compute_supertrend) and the continuous-candle fetch
(fetch_continuous_intraday) - both imported from Options/dhan_client.py,
zero duplicated indicator logic.

Both public functions below are fail-open: a fetch failure or not-enough-
data condition returns None and LEAVES THE PREVIOUS CACHED VALUE IN
PLACE - a None read must always mean "skip this tick," never "exit now"
or "regime flipped." Callers must never treat None as a signal.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from Options.dhan_client import dhan_wrapper, _compute_ema, _compute_supertrend, IST
from . import config

logger = logging.getLogger("swing_signals")


def _now_ist() -> datetime:
    return datetime.now(IST)


# Resolved MCX futures contract's security_id, cached per calendar day
# (added 12 Sep 2026, Swing v2's Copper support) - re-resolving on every
# tick would be wasteful and the contract only changes when a monthly
# expiry cycle actually rolls, which get_mcx_futures_contract's own
# roll-forward logic already only needs to check once a day for.
_mcx_contract_cache: dict[str, tuple[date, str]] = {}


def _underlying_reference(symbol: str) -> tuple[str, str, str]:
    """Returns (security_id, exchange_segment, instrument_type) for the
    underlying series both regime and Supertrend fetches read. A normal
    watchlist symbol resolves its NSE cash-segment security_id, unchanged
    from before. A symbol in config.MCX_SYMBOLS instead resolves the
    CURRENT MCX futures contract (there's no continuous "spot" for an MCX
    commodity - see Swing/config.py's MCX_SYMBOLS docstring), independent
    of whatever BASKET_TYPE is actually configured - the regime/Supertrend
    signal always needs a real continuous price series regardless of
    which instrument ends up traded."""
    if symbol in config.MCX_SYMBOLS:
        today = _now_ist().date()
        cached = _mcx_contract_cache.get(symbol)
        if not cached or cached[0] != today:
            contract = dhan_wrapper.get_mcx_futures_contract(symbol)
            _mcx_contract_cache[symbol] = (today, contract.security_id)
            logger.info("%s: resolved MCX futures contract for today's signal reference: security_id=%s",
                        symbol, contract.security_id)
        return _mcx_contract_cache[symbol][1], "MCX_COMM", "FUTCOM"
    return dhan_wrapper._equity_security_id(symbol), "NSE_EQ", "EQUITY"


# --------------------------------------------------------------------------- #
# Regime: 5-min EMA(200) vs 15-min EMA(200)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegimeState:
    """fast_ema/slow_ema/is_bullish are the plain LEVEL comparison,
    UNCHANGED in meaning since this file's own original design - still
    what v1's own simpler entry rule and GET /swing/signals read.

    crossed_above/crossed_below (added 17 Sep 2026, user request) are a
    SEPARATE, EDGE-based reading of the exact same two EMAs - the 5-min
    EMA200 crossing above/below the 15-min EMA200 between the last two
    closed 5-min candles, mirroring Supertrend's own crossed_above/
    crossed_below (a state CHANGE, not "currently above/below"). v2's own
    combined entry rule uses THIS as its "Regime Bullish/Bearish" leg -
    is_bullish is a different, coarser signal, kept as-is for v1/
    observability.

    gap_widened (added 17 Sep 2026, user request) - whether the |fast_ema
    - slow_ema| gap has grown (not shrunk) over the last config.REGIME_
    GAP_WIDENING_LOOKBACK_CANDLES 5-min candles, in whichever direction it
    currently sits - a strengthening-trend confirmation, not just "still
    on the same side." Real motivating case (COPPER, same night): a
    regime reading can be technically bullish/bearish by sign while the
    two EMAs are actually CONVERGING (about to flip) - a hairline,
    weakening gap is a much lower-conviction signal than a genuinely
    widening one. v2's own "Trend-aware Filter Bullish/Bearish" leg =
    (is_bullish/not is_bullish) AND gap_widened. None (not False) when
    there isn't yet enough history to judge - callers must treat None the
    same as False (fails closed, never fires this leg on missing data)."""
    fast_ema: float
    slow_ema: float
    is_bullish: bool          # fast_ema > slow_ema (LEVEL - unchanged meaning)
    fast_candle_start: Optional[datetime]
    slow_candle_start: Optional[datetime]
    prev_is_bullish: Optional[bool] = None
    gap_widened: Optional[bool] = None

    @property
    def crossed_above(self) -> bool:
        return bool(self.prev_is_bullish is False and self.is_bullish is True)

    @property
    def crossed_below(self) -> bool:
        return bool(self.prev_is_bullish is True and self.is_bullish is False)


# Consecutive-failure backoff (added 22 Sep 2026, real incident) - the
# throttle-on-failure fix earlier today (stamping the cache even on
# failure) stopped a persistently-failing symbol from being retried on
# EVERY 5s monitor tick, but it still retried at the same fixed cadence
# forever regardless of how many times in a row it had already failed.
# MCX symbols hit this hardest: confirmed live, 113 DH-904 rate-limit
# failures in the 16 minutes right after a restart, 60 of them on
# NATURALGAS alone - Supertrend's own 15s base interval means up to 4
# retry attempts/minute per (symbol, interval) pair, on an endpoint
# that's failing for account-wide rate-limit reasons having nothing to
# do with that specific symbol. Doubling the effective wait on each
# consecutive failure (capped at MAX_FETCH_BACKOFF_SECONDS) means a
# genuinely rate-limited symbol backs off and stops adding to that same
# account-wide pressure, while resetting to the base interval the moment
# a fetch succeeds again means a recovered symbol goes straight back to
# full-speed refresh - no lingering penalty once Dhan is responding again.
MAX_FETCH_BACKOFF_SECONDS = 300
_regime_fail_streak: dict[str, int] = {}
_supertrend_fail_streak: dict[tuple[str, int], int] = {}

_regime_cache: dict[str, tuple[datetime, Optional[RegimeState]]] = {}


def _last_closed_idx(timestamps: list[int], cutoff_ts: float, interval_minutes: int) -> Optional[int]:
    """Index of the last candle that had FULLY CLOSED by cutoff_ts (its
    own start + interval <= cutoff) - not merely started by then. Shared
    by the regime gap-alignment lookback below (a 5-min candle's own
    close instant -> the most recently closed 15-min candle as of that
    same moment)."""
    interval_s = interval_minutes * 60
    best = None
    for i, ts in enumerate(timestamps):
        if ts + interval_s <= cutoff_ts:
            best = i
        else:
            break
    return best


def _aligned_gap(
    fast_ema: list[Optional[float]], fast_ts: list[int], fast_idx: int,
    slow_ema: list[Optional[float]], slow_ts: list[int],
) -> Optional[float]:
    """fast_ema[fast_idx] minus the slow EMA value from the most recently
    CLOSED slow candle as of that fast candle's own close instant -
    generalizes the "15-min state as of this 5-min candle" alignment
    _evaluate_entry_signal's v2 filter already uses, to any historical
    fast_idx (not only the latest), so the crossover/gap-widening checks
    below can look back multiple 5-min candles at the correctly-aligned
    slow value for each one. None if fast_idx is out of range or either
    side doesn't have a real value there yet (not enough history)."""
    if fast_idx < 0 or fast_idx >= len(fast_ema) or fast_ema[fast_idx] is None:
        return None
    cutoff = fast_ts[fast_idx] + config.REGIME_FAST_INTERVAL_MINUTES * 60
    idx = _last_closed_idx(slow_ts, cutoff, config.REGIME_SLOW_INTERVAL_MINUTES)
    if idx is None or slow_ema[idx] is None:
        return None
    return fast_ema[fast_idx] - slow_ema[idx]


def _fetch_regime_state_once(symbol: str) -> Optional[RegimeState]:
    """Blocking - always call via run_in_executor. Two continuous-candle
    fetches (5-min and 15-min), each with the longer REGIME_EMA_LOOKBACK_
    DAYS override (see fetch_continuous_intraday's own docstring for why
    the shared 7-day global can't warm up a 200-period EMA at all).

    Raises if either fetch comes back COMPLETELY empty (real incident,
    17 Sep 2026: a live Dhan API instability window made
    fetch_continuous_intraday silently return {} - its own documented
    failure mode - for every watchlist symbol; since that reads as
    "0 candles" rather than an exception, this function used to just
    compute None and return normally, which get_regime_state's caller
    then cached as a legitimate reading, silently overwriting whatever
    good regime value was already cached with None - the exact "goes
    silently null with zero logged errors" pattern from an earlier,
    still-unexplained watchlist anomaly this same night). A genuinely
    too-new symbol (never has REGIME_EMA_PERIOD bars of real history)
    still returns None normally below - Dhan serves however many bars
    actually exist for a real, currently-listed instrument, so a
    COMPLETELY empty response from an already-established watchlist
    symbol is a fetch failure, not a warm-up state, and must be treated
    as one so get_regime_state's own try/except (which already knows how
    to keep the last good cached value) actually gets to run."""
    security_id, exchange_segment, instrument_type = _underlying_reference(symbol)

    fast_data = dhan_wrapper.fetch_continuous_intraday(
        security_id, exchange_segment, instrument_type, config.REGIME_FAST_INTERVAL_MINUTES,
        lookback_days_override=config.REGIME_EMA_LOOKBACK_DAYS,
    )
    if not fast_data.get("close"):
        raise RuntimeError(
            f"{symbol}: fetch_continuous_intraday returned no data at all for the "
            f"{config.REGIME_FAST_INTERVAL_MINUTES}-min regime series - treating as a fetch "
            f"failure, not genuinely insufficient history"
        )
    fast_closes = fast_data.get("close") or []
    fast_ts = fast_data.get("timestamp") or []
    if fast_ts:
        last_candle_start = datetime.fromtimestamp(fast_ts[-1], tz=IST)
        if _now_ist() < last_candle_start + timedelta(minutes=config.REGIME_FAST_INTERVAL_MINUTES):
            fast_closes, fast_ts = fast_closes[:-1], fast_ts[:-1]
    if len(fast_closes) < config.REGIME_EMA_PERIOD:
        return None
    fast_ema_arr = _compute_ema(fast_closes, config.REGIME_EMA_PERIOD)
    if fast_ema_arr[-1] is None:
        return None

    slow_data = dhan_wrapper.fetch_continuous_intraday(
        security_id, exchange_segment, instrument_type, config.REGIME_SLOW_INTERVAL_MINUTES,
        lookback_days_override=config.REGIME_EMA_LOOKBACK_DAYS,
    )
    if not slow_data.get("close"):
        raise RuntimeError(
            f"{symbol}: fetch_continuous_intraday returned no data at all for the "
            f"{config.REGIME_SLOW_INTERVAL_MINUTES}-min regime series - treating as a fetch "
            f"failure, not genuinely insufficient history"
        )
    slow_closes = slow_data.get("close") or []
    slow_ts = slow_data.get("timestamp") or []
    if slow_ts:
        last_candle_start = datetime.fromtimestamp(slow_ts[-1], tz=IST)
        if _now_ist() < last_candle_start + timedelta(minutes=config.REGIME_SLOW_INTERVAL_MINUTES):
            slow_closes, slow_ts = slow_closes[:-1], slow_ts[:-1]
    if len(slow_closes) < config.REGIME_EMA_PERIOD:
        return None
    slow_ema_arr = _compute_ema(slow_closes, config.REGIME_EMA_PERIOD)
    if slow_ema_arr[-1] is None:
        return None

    # "Current" reading is byte-identical to the pre-17-Sep-2026 code path
    # (plain last-value comparison) - only the NEW historical lookups
    # below (prev candle, N candles back) need the generalized alignment
    # helper, since they must look at an EARLIER 5-min candle against
    # whatever 15-min candle was actually closed at THAT moment in time.
    fast_ema = fast_ema_arr[-1]
    slow_ema = slow_ema_arr[-1]
    current_gap = fast_ema - slow_ema
    is_bullish = current_gap > 0

    fast_idx_now = len(fast_ema_arr) - 1
    prev_gap = _aligned_gap(fast_ema_arr, fast_ts, fast_idx_now - 1, slow_ema_arr, slow_ts)
    prev_is_bullish = (prev_gap > 0) if prev_gap is not None else None

    gap_n_ago = _aligned_gap(
        fast_ema_arr, fast_ts, fast_idx_now - config.REGIME_GAP_WIDENING_LOOKBACK_CANDLES, slow_ema_arr, slow_ts,
    )
    if gap_n_ago is None:
        gap_widened = None
    elif is_bullish:
        gap_widened = current_gap > gap_n_ago
    else:
        gap_widened = current_gap < gap_n_ago

    return RegimeState(
        fast_ema=fast_ema, slow_ema=slow_ema, is_bullish=is_bullish,
        fast_candle_start=datetime.fromtimestamp(fast_ts[-1], tz=IST) if fast_ts else None,
        slow_candle_start=datetime.fromtimestamp(slow_ts[-1], tz=IST) if slow_ts else None,
        prev_is_bullish=prev_is_bullish, gap_widened=gap_widened,
    )


def peek_regime_state(symbol: str) -> Optional[RegimeState]:
    """Cache-only, no fetch - for GET /swing/signals (the rollout's main
    observe-before-you-trade tool). Returns whatever was last computed,
    or None if nothing has run for this symbol yet."""
    cached = _regime_cache.get(symbol)
    return cached[1] if cached else None


async def get_regime_state(symbol: str) -> Optional[RegimeState]:
    """Cached, throttled (config.REGIME_REFRESH_SECONDS, doubling on each
    consecutive failure up to MAX_FETCH_BACKOFF_SECONDS - see that
    constant's own comment), fail-open - see this module's own docstring
    for why a fetch exception here keeps the last good cached value
    rather than writing None over it."""
    cached = _regime_cache.get(symbol)
    streak = _regime_fail_streak.get(symbol, 0)
    effective_refresh = min(config.REGIME_REFRESH_SECONDS * (2 ** streak), MAX_FETCH_BACKOFF_SECONDS)
    if cached and (_now_ist() - cached[0]).total_seconds() < effective_refresh:
        return cached[1]
    loop = asyncio.get_running_loop()
    try:
        state = await loop.run_in_executor(None, _fetch_regime_state_once, symbol)
        _regime_fail_streak[symbol] = 0
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch regime state - keeping last cached value", symbol)
        state = cached[1] if cached else None
        _regime_fail_streak[symbol] = streak + 1
    # Stamp the cache even on failure - otherwise a persistent fetch failure
    # never satisfies the "fresh enough" check above and every 5s monitor
    # tick retries immediately instead of waiting REGIME_REFRESH_SECONDS,
    # turning one bad fetch into a continuous full-speed hammering of
    # Dhan's REST endpoint (confirmed live 21 Sep 2026: ~22k failed calls
    # across market hours after one early failure never got throttled).
    _regime_cache[symbol] = (_now_ist(), state)
    return state


# --------------------------------------------------------------------------- #
# 5-min Supertrend crossover - lifted from the old Swing/trading_engine.py's
# own SupertrendState/_fetch_supertrend_state (already correct, already
# proven, unchanged in substance - only the interval is no longer a
# parameter, since Swing v2 only ever needs the one 5-min series).
# --------------------------------------------------------------------------- #
def _volume_ratio_at(volumes: list[float], idx: int, lookback: int = 20) -> Optional[float]:
    """Entry candle's volume vs the average of the prior `lookback` bars -
    same formula as reversal_filters.py's identical helper (kept as its
    own copy here per this codebase's per-package indicator-duplication
    convention)."""
    if idx < lookback or idx >= len(volumes):
        return None
    window = volumes[idx - lookback:idx]
    avg = sum(window) / len(window) if window else 0.0
    return (volumes[idx] / avg) if avg else None


@dataclass
class SupertrendState:
    """The last TWO fully-closed candles' relationship to the Supertrend
    line - enough to detect an actual crossover (a state CHANGE), not
    just a current side."""
    candle_start: Optional[datetime]
    close: float
    supertrend: float
    is_above: bool
    prev_close: float
    prev_supertrend: float
    prev_is_above: bool
    volume: float = 0.0
    # Entry candle's volume vs its own 20-bar rolling average - added 16
    # Sep 2026 for the MCX volume-floor entry gate (see config.MCX_
    # VOLUME_FLOOR_GATE_ENABLED). None if there wasn't enough history to
    # compute yet - callers must treat None as "gate fails open," never
    # "block."
    volume_ratio: Optional[float] = None

    @property
    def crossed_above(self) -> bool:
        """True only on the candle where price flips from AT-OR-BELOW to
        ABOVE the Supertrend line - a real transition, not "is above right
        now" (true for every candle of an established uptrend)."""
        return (not self.prev_is_above) and self.is_above

    @property
    def crossed_below(self) -> bool:
        return self.prev_is_above and not self.is_above


_supertrend_cache: dict[tuple[str, int], tuple[datetime, Optional[SupertrendState]]] = {}


def _fetch_supertrend_state_once(symbol: str, interval_minutes: int) -> Optional[SupertrendState]:
    """Blocking - always call via run_in_executor. Returns None only if
    the fetch genuinely came back with too little data - callers treat
    that as "no signal," never as a false crossover.

    interval_minutes was hardcoded to config.SUPERTREND_INTERVAL_MINUTES
    (5) until 14 Sep 2026, when the "v2" combined entry strategy (see
    Swing/config.py's ENTRY_STRATEGY_VERSION) needed a SECOND Supertrend
    instance on the 15-min timeframe as an additional entry filter -
    parameterized here rather than duplicating this whole function, since
    period/multiplier/the crossover math are identical for either
    timeframe, only the candle interval differs."""
    security_id, exchange_segment, instrument_type = _underlying_reference(symbol)
    data = dhan_wrapper.fetch_continuous_intraday(
        security_id, exchange_segment, instrument_type, interval_minutes,
    )
    if not data.get("close"):
        # A COMPLETELY empty response for an already-established watchlist
        # symbol is a fetch failure (see _fetch_regime_state_once's own
        # docstring for the real incident this guards against - the same
        # "fetch silently returns {}, gets cached as a legitimate None"
        # pattern applies here identically), not genuinely insufficient
        # history - raise so get_supertrend_state's try/except keeps the
        # last good cached value instead of overwriting it.
        raise RuntimeError(
            f"{symbol}: fetch_continuous_intraday returned no data at all for the "
            f"{interval_minutes}-min Supertrend series - treating as a fetch failure, "
            f"not genuinely insufficient history"
        )
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    volumes = data.get("volume") or []
    timestamps = data.get("timestamp") or []

    period = config.SUPERTREND_PERIOD
    if timestamps:
        last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
        if _now_ist() < last_candle_start + timedelta(minutes=interval_minutes):
            highs, lows, closes, volumes, timestamps = highs[:-1], lows[:-1], closes[:-1], volumes[:-1], timestamps[:-1]

    # period+1 candles for the first computable bar, one more on top for a
    # PREVIOUS bar to compare against (period+2 total).
    if len(closes) < period + 2:
        return None

    supertrend = _compute_supertrend(highs, lows, closes, period=period, multiplier=config.SUPERTREND_MULTIPLIER)
    if supertrend[-1] is None or supertrend[-2] is None:
        return None

    return SupertrendState(
        candle_start=datetime.fromtimestamp(timestamps[-1], tz=IST) if timestamps else None,
        close=closes[-1], supertrend=supertrend[-1], is_above=closes[-1] > supertrend[-1],
        prev_close=closes[-2], prev_supertrend=supertrend[-2], prev_is_above=closes[-2] > supertrend[-2],
        volume=volumes[-1] if volumes else 0.0,
        volume_ratio=_volume_ratio_at(volumes, len(volumes) - 1) if volumes else None,
    )


def peek_supertrend_state(symbol: str, interval_minutes: Optional[int] = None) -> Optional[SupertrendState]:
    """Cache-only counterpart to peek_regime_state above. interval_minutes
    defaults to config.SUPERTREND_INTERVAL_MINUTES (5) so every pre-14-Sep
    call site (which only ever wants the one 5-min series) is unaffected
    by this function now supporting a second timeframe."""
    interval_minutes = interval_minutes if interval_minutes is not None else config.SUPERTREND_INTERVAL_MINUTES
    cached = _supertrend_cache.get((symbol, interval_minutes))
    return cached[1] if cached else None


async def get_supertrend_state(symbol: str, interval_minutes: Optional[int] = None) -> Optional[SupertrendState]:
    """Cached, throttled (config.SUPERTREND_REFRESH_SECONDS, doubling on
    each consecutive failure up to MAX_FETCH_BACKOFF_SECONDS - see that
    constant's own comment), fail-open - same "keep the last good value"
    discipline as get_regime_state above. interval_minutes defaults to
    config.SUPERTREND_INTERVAL_MINUTES (5), same backward-compatibility
    note as peek_supertrend_state above - the v2 combined entry strategy
    is the only caller that ever passes a different value
    (config.REGIME_SLOW_INTERVAL_MINUTES, 15)."""
    interval_minutes = interval_minutes if interval_minutes is not None else config.SUPERTREND_INTERVAL_MINUTES
    cache_key = (symbol, interval_minutes)
    cached = _supertrend_cache.get(cache_key)
    streak = _supertrend_fail_streak.get(cache_key, 0)
    effective_refresh = min(config.SUPERTREND_REFRESH_SECONDS * (2 ** streak), MAX_FETCH_BACKOFF_SECONDS)
    if cached and (_now_ist() - cached[0]).total_seconds() < effective_refresh:
        return cached[1]
    loop = asyncio.get_running_loop()
    try:
        state = await loop.run_in_executor(None, _fetch_supertrend_state_once, symbol, interval_minutes)
        _supertrend_fail_streak[cache_key] = 0
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch Supertrend state (%smin) - keeping last cached value",
                          symbol, interval_minutes)
        state = cached[1] if cached else None
        _supertrend_fail_streak[cache_key] = streak + 1
    # Stamp the cache even on failure - see the matching comment in
    # get_regime_state above; same bug, same fix, same live incident.
    _supertrend_cache[cache_key] = (_now_ist(), state)
    return state


# ------------------------------------------------------------------ #
# COPPER-only structure-break signal (config.COPPER_STRUCTURE_BREAK_
# ENABLED) - see that flag's own docstring in Swing/config.py for the
# full backtest/rationale. Combines structure_break.py's 5m/15m/1h regime
# into one +1/-1/0 agreement state, exactly mirroring
# backtest_swing_structure_break_mtf.py's own agree_bull/agree_bear
# combination logic - this IS the signal that was backtested, not a
# reimplementation of it.
# ------------------------------------------------------------------ #
@dataclass
class StructureBreakSignal:
    combined: int   # +1 = all 3 timeframes agree bullish, -1 = agree bearish, 0 = no agreement
    computed_at: datetime


_structure_break_cache: dict[str, tuple[datetime, Optional[StructureBreakSignal]]] = {}
_structure_break_fail_streak: dict[str, int] = {}
_STRUCTURE_BREAK_FETCH_PACE_SECONDS = 1.6   # same back-to-back-REST-call pacing bt_fetch.py/the backtest script use


def _fetch_structure_break_signal_once(symbol: str) -> StructureBreakSignal:
    """Blocking - always call via run_in_executor. Raises on any fetch
    failure or not-yet-warm timeframe (never returns a partial/best-
    effort signal) so get_structure_break_signal's try/except keeps the
    last good cached value instead of trusting an incomplete read - same
    fail-open discipline as every other function in this file. mcx=True
    unconditionally: this signal is currently only ever evaluated for
    COPPER (see config.COPPER_STRUCTURE_BREAK_ENABLED's own docstring on
    why it's hardcoded to that one symbol)."""
    import structure_break as sb
    import time as _time

    results = {}
    for i, tf in enumerate(("5m", "15m", "1h")):
        if i:
            _time.sleep(_STRUCTURE_BREAK_FETCH_PACE_SECONDS)
        r = sb.fetch_timeframe(symbol, tf, mcx=True)
        if r.error or not r.warm:
            raise RuntimeError(f"{symbol}: structure-break {tf} fetch failed/not warm: {r.error}")
        results[tf] = r

    agree_bull = all(results[tf].last_regime == 1 for tf in ("5m", "15m", "1h"))
    agree_bear = all(results[tf].last_regime == -1 for tf in ("5m", "15m", "1h"))
    combined = 1 if agree_bull else -1 if agree_bear else 0
    return StructureBreakSignal(combined=combined, computed_at=_now_ist())


def peek_structure_break_signal(symbol: str) -> Optional[StructureBreakSignal]:
    cached = _structure_break_cache.get(symbol)
    return cached[1] if cached else None


async def get_structure_break_signal(symbol: str) -> Optional[StructureBreakSignal]:
    """Cached, throttled (config.STRUCTURE_BREAK_REFRESH_SECONDS, doubling
    on each consecutive failure up to MAX_FETCH_BACKOFF_SECONDS - identical
    pattern to get_supertrend_state above), fail-open."""
    cached = _structure_break_cache.get(symbol)
    streak = _structure_break_fail_streak.get(symbol, 0)
    effective_refresh = min(config.STRUCTURE_BREAK_REFRESH_SECONDS * (2 ** streak), MAX_FETCH_BACKOFF_SECONDS)
    if cached and (_now_ist() - cached[0]).total_seconds() < effective_refresh:
        return cached[1]
    loop = asyncio.get_running_loop()
    try:
        state = await loop.run_in_executor(None, _fetch_structure_break_signal_once, symbol)
        _structure_break_fail_streak[symbol] = 0
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch structure-break signal - keeping last cached value", symbol)
        state = cached[1] if cached else None
        _structure_break_fail_streak[symbol] = streak + 1
    _structure_break_cache[symbol] = (_now_ist(), state)
    return state
