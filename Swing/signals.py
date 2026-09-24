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

from Options.dhan_client import dhan_wrapper, _compute_ema, _compute_rsi, _compute_supertrend, IST
from . import candle_feed, config

logger = logging.getLogger("swing_signals")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _symbol_market_open(symbol: str) -> bool:
    """Per-symbol market-hours gate (22 Sep 2026 fix): before this, Swing's
    entry-evaluation path and this module's own structure-break refresh
    loop both polled Dhan for regime/Supertrend/structure-break data
    unconditionally, 24/7 - including nights and weekends, when no new
    candle can possibly form and no order could fill anyway. Confirmed
    live: 39 DH-904 rate-limit hits in under an hour, overnight, entirely
    from this. MCX's session runs materially longer than NSE's (see
    Options/config.py's MCX_MARKET_OPEN_TIME/_CLOSE_TIME), so this checks
    the RIGHT session per symbol - a blanket NSE-hours cutoff would wrongly
    block a live MCX symbol for hours it's genuinely still open, and a
    blanket MCX-hours cutoff would leave the same overnight polling problem
    for NSE symbols mostly unfixed. Weekday check first since is_market_
    open() itself only checks time-of-day, not day-of-week."""
    now = _now_ist()
    if now.weekday() >= 5:  # Saturday/Sunday - neither exchange trades
        return False
    segment = "MCX_COMM" if symbol in config.MCX_SYMBOLS else "NSE_EQ"
    return dhan_wrapper.is_market_open(exchange_segment=segment)


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
    is_index = symbol in config.INDEX_SYMBOLS
    if symbol in config.MCX_SYMBOLS:
        today = _now_ist().date()
        cached = _mcx_contract_cache.get(symbol)
        if not cached or cached[0] != today:
            contract = dhan_wrapper.get_mcx_futures_contract(symbol)
            _mcx_contract_cache[symbol] = (today, contract.security_id)
            logger.info("%s: resolved MCX futures contract for today's signal reference: security_id=%s",
                        symbol, contract.security_id)
        security_id, exchange_segment, instrument_type = _mcx_contract_cache[symbol][1], "MCX_COMM", "FUTCOM"
    elif is_index:
        # No SEM_INSTRUMENT_NAME=="EQUITY" row exists for an index - the
        # plain NSE-equity path below always raised "No NSE equity
        # instrument found" for NIFTY/BANKNIFTY (a real gap, confirmed
        # live before this fix). Same segment/instrument_type should_
        # delay_ce_entry already uses in production.
        #
        # WS coverage extended here 24 Sep 2026 (user request, direct
        # follow-up to confirming candle_feed.py was WS-live for COPPER/
        # NATURALGAS but REST-only for indices) - candle_feed.ensure_
        # subscribed is now called for index symbols too, same as every
        # other branch below. This used to be deliberately skipped (REST-
        # only) because candle_feed.py's WS subscribe path didn't have an
        # IDX_I-segment method yet - dhan_wrapper.subscribe_index_quote/
        # unsubscribe_index_quote (Options/dhan_client.py) close that gap,
        # confirmed against dhanhq's own MarketFeed.IDX segment constant.
        # UNVERIFIED as of this change whether Dhan's Quote-mode packet for
        # an index actually carries the "volume" key _on_market_tick's own
        # quote-tick routing requires (see that function's own comment on
        # the IDX_I branch) - watch GET /swing/debug/candle-feed/snapshot
        # for NIFTY/BANKNIFTY after deploy to confirm ticks actually
        # arrive, not just that the subscribe call succeeded. Fails safe
        # either way: _get_intraday_series below only trusts the WS series
        # once is_fresh() and a real bar count both check out, REST
        # fallback continues exactly as before if ticks never arrive.
        security_id, exchange_segment, instrument_type = dhan_wrapper.index_security_id(symbol), "IDX_I", "INDEX"
    else:
        security_id, exchange_segment, instrument_type = dhan_wrapper._equity_security_id(symbol), "NSE_EQ", "EQUITY"

    if config.USE_WS_CANDLES:
        try:
            candle_feed.ensure_subscribed(symbol, security_id, exchange_segment)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not WS-subscribe for candle feed - REST fallback continues", symbol)
    return security_id, exchange_segment, instrument_type


RAW_SERIES_DEDUP_SECONDS = 10
_raw_series_cache: dict[tuple, tuple[datetime, dict]] = {}


def _get_intraday_series(
    symbol: str, security_id: str, exchange_segment: str, instrument_type: str,
    interval_minutes: int, min_bars: int, lookback_days_override: Optional[int] = None,
) -> dict:
    """Tries the local WS-reconstructed candle feed first (candle_feed.py)
    when config.USE_WS_CANDLES is on, falling back to the existing REST
    fetch otherwise - zero REST calls once the local series is fresh and
    already has at least min_bars for THIS call's own need, byte-identical
    behavior to today whenever it isn't (never raises on an insufficient/
    empty WS series - just falls through). min_bars must match whatever
    the caller itself requires downstream (config.REGIME_EMA_PERIOD for
    regime, config.SUPERTREND_PERIOD + 2 for Supertrend) so a WS series
    is never accepted with LESS history than REST would have supplied -
    the hybrid switch can only make a symbol's signal faster to compute,
    never worse.

    REST fallback goes through a short-TTL (RAW_SERIES_DEDUP_SECONDS) raw-
    series cache, keyed by (security_id, exchange_segment, instrument_type,
    interval_minutes, lookback_days_override) - NOT by symbol, so any two
    callers asking about the same underlying instrument's same interval
    within the same dedup window share one REST call. Added 24 Sep 2026,
    real incident: get_regime_state's fast(5min) fetch, get_supertrend_
    state's own 5min fetch, and (for INDEX_SYMBOLS) get_day_range_state's
    5min fetch were each independently re-fetching the IDENTICAL series
    for NIFTY/BANKNIFTY within the same monitor tick - confirmed live via
    journalctl: 3 separate fetch_continuous_intraday calls for the same
    security_id+interval seconds apart, each hitting Dhan's account-wide
    DH-904 rate limit independently. This does NOT fix DH-904 itself (an
    account-wide budget shared across all 4 packages, see trading-skills'
    2026-09-22 incident writeup for that still-open question) - it only
    removes Swing's own redundant, same-tick duplicate calls, cutting a
    symbol needing all three signals from ~5 REST calls/tick down to ~2
    (one per distinct interval). Caches the OUTCOME either way (including
    an empty {} on failure) so a second caller within the window gets the
    same failure immediately rather than making its own doomed call -
    each caller's own existing fail-streak/backoff logic is unaffected,
    since it reads this function's return value exactly as before."""
    if config.USE_WS_CANDLES and candle_feed.is_fresh(symbol, config.WS_STALE_AFTER_SECONDS):
        ws_data = candle_feed.get_candles_dict(symbol, interval_minutes)
        if len(ws_data.get("close") or []) >= min_bars:
            return ws_data
    cache_key = (security_id, exchange_segment, instrument_type, interval_minutes, lookback_days_override)
    cached = _raw_series_cache.get(cache_key)
    if cached and (_now_ist() - cached[0]).total_seconds() < RAW_SERIES_DEDUP_SECONDS:
        return cached[1]
    data = dhan_wrapper.fetch_continuous_intraday(
        security_id, exchange_segment, instrument_type, interval_minutes,
        lookback_days_override=lookback_days_override,
    )
    _raw_series_cache[cache_key] = (_now_ist(), data)
    return data


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

    fast_data = _get_intraday_series(
        symbol, security_id, exchange_segment, instrument_type, config.REGIME_FAST_INTERVAL_MINUTES,
        min_bars=config.REGIME_EMA_PERIOD, lookback_days_override=config.REGIME_EMA_LOOKBACK_DAYS,
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

    slow_data = _get_intraday_series(
        symbol, security_id, exchange_segment, instrument_type, config.REGIME_SLOW_INTERVAL_MINUTES,
        min_bars=config.REGIME_EMA_PERIOD, lookback_days_override=config.REGIME_EMA_LOOKBACK_DAYS,
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
    data = _get_intraday_series(
        symbol, security_id, exchange_segment, instrument_type, interval_minutes,
        min_bars=config.SUPERTREND_PERIOD + 2,
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


# --------------------------------------------------------------------------- #
# Day Range Bull/Bear - v3, config.INDEX_SYMBOLS (NIFTY/BANKNIFTY) ONLY, user
# request 24 Sep 2026. See Swing/config.py's DAY_RANGE_RSI_PERIOD docstring
# for the full backtest this ports (backtest_nifty_options_swing_v2_1min.py -
# +Rs14,752/59.4% WR, NIFTY, 30-day window, 5-min fast layer) and Swing/
# trading_engine.py's _evaluate_entry_signal for the exact combined formula
# this feeds into. Never called for a non-index symbol - see that function.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DayRangeState:
    """today_open/yesterday_close are derived from the SAME continuous 5-min
    series close/open/candle_start already fetched here (bucketed by IST
    calendar date) - not a separate OHLC-quote REST call, deliberately
    mirroring exactly how the backtest itself derived these two values, and
    avoiding a second, independently-throttled fetch path with its own
    failure mode. close/supertrend/is_above_supertrend are this state's OWN
    5-min Supertrend read (a LEVEL, not an edge - the spec's "5 min candle
    close is greater than 5 min Super trend" is a standing condition, not a
    crossover) - intentionally NOT shared with SupertrendState's own
    independently-cached/throttled value, same reasoning st15 and st (5-min)
    already stay independent of each other in _evaluate_entry_signal.

    rsi/prev_rsi are RSI(config.DAY_RANGE_RSI_PERIOD) on the same 5-min
    close series, the last two closed bars only - enough to detect the
    level-CROSS the spec asks for ("buy when 5 min candle price cross above
    RSI value 60"), read as the RSI VALUE crossing level 60/40 (not price
    crossing an RSI value) - same interpretation call the backtest itself
    documented and used. None (not False) when there isn't yet enough
    history - callers must treat None the same as False, fails closed."""
    today_open: float
    yesterday_close: float
    close: float
    supertrend: float
    is_above_supertrend: bool
    rsi: Optional[float]
    prev_rsi: Optional[float]
    candle_start: Optional[datetime]

    @property
    def gap_up_day(self) -> bool:
        return self.today_open > self.yesterday_close

    @property
    def gap_down_day(self) -> bool:
        return self.today_open < self.yesterday_close

    @property
    def crossed_above_bull_level(self) -> bool:
        return bool(self.prev_rsi is not None and self.rsi is not None
                    and self.prev_rsi <= config.DAY_RANGE_RSI_BULL_LEVEL < self.rsi)

    @property
    def crossed_below_bear_level(self) -> bool:
        return bool(self.prev_rsi is not None and self.rsi is not None
                    and self.prev_rsi >= config.DAY_RANGE_RSI_BEAR_LEVEL > self.rsi)

    @property
    def bullish_entry(self) -> bool:
        """Day Range Bull, exactly as specified: today's open > yesterday's
        close, AND this candle's close is above today's open, AND this
        candle's close is above the 5-min Supertrend, AND RSI just crossed
        above the bull level (60)."""
        return bool(self.gap_up_day and self.close > self.today_open
                    and self.is_above_supertrend and self.crossed_above_bull_level)

    @property
    def bearish_entry(self) -> bool:
        return bool(self.gap_down_day and self.close < self.today_open
                    and (not self.is_above_supertrend) and self.crossed_below_bear_level)


_day_range_cache: dict[str, tuple[datetime, Optional[DayRangeState]]] = {}
_day_range_fail_streak: dict[str, int] = {}


def _fetch_day_range_state_once(symbol: str) -> Optional[DayRangeState]:
    """Blocking - always call via run_in_executor. Same fail-open/raise-on-
    completely-empty-response discipline as _fetch_regime_state_once/
    _fetch_supertrend_state_once above - see those for the real incident
    this guards against."""
    security_id, exchange_segment, instrument_type = _underlying_reference(symbol)
    min_bars = max(config.SUPERTREND_PERIOD + 2, config.DAY_RANGE_RSI_PERIOD + 2)
    data = _get_intraday_series(
        symbol, security_id, exchange_segment, instrument_type, config.SUPERTREND_INTERVAL_MINUTES,
        min_bars=min_bars,
    )
    if not data.get("close"):
        raise RuntimeError(
            f"{symbol}: fetch_continuous_intraday returned no data at all for the "
            f"{config.SUPERTREND_INTERVAL_MINUTES}-min Day Range series - treating as a fetch "
            f"failure, not genuinely insufficient history"
        )
    opens = data.get("open") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    timestamps = data.get("timestamp") or []
    if timestamps:
        last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
        if _now_ist() < last_candle_start + timedelta(minutes=config.SUPERTREND_INTERVAL_MINUTES):
            opens, highs, lows, closes, timestamps = opens[:-1], highs[:-1], lows[:-1], closes[:-1], timestamps[:-1]
    if len(closes) < min_bars:
        return None

    dts = [datetime.fromtimestamp(t, tz=IST) for t in timestamps]
    today = dts[-1].date()
    today_first_idx = next((i for i, dt in enumerate(dts) if dt.date() == today), None)
    if today_first_idx is None or today_first_idx == 0:
        return None  # no fully-formed prior trading day in this fetched window

    supertrend = _compute_supertrend(highs, lows, closes, period=config.SUPERTREND_PERIOD,
                                      multiplier=config.SUPERTREND_MULTIPLIER)
    if supertrend[-1] is None:
        return None
    rsi = _compute_rsi(closes, config.DAY_RANGE_RSI_PERIOD)
    if rsi[-1] is None:
        return None

    return DayRangeState(
        today_open=opens[today_first_idx], yesterday_close=closes[today_first_idx - 1],
        close=closes[-1], supertrend=supertrend[-1], is_above_supertrend=closes[-1] > supertrend[-1],
        rsi=rsi[-1], prev_rsi=rsi[-2] if len(rsi) > 1 else None,
        candle_start=dts[-1],
    )


def peek_day_range_state(symbol: str) -> Optional[DayRangeState]:
    """Cache-only, no fetch - for GET /swing/signals, same as peek_regime_
    state/peek_supertrend_state above."""
    cached = _day_range_cache.get(symbol)
    return cached[1] if cached else None


async def get_day_range_state(symbol: str) -> Optional[DayRangeState]:
    """Cached, throttled (reuses config.SUPERTREND_REFRESH_SECONDS - same
    5-min cadence as the Supertrend signal this shares a timeframe with),
    fail-open - same "keep the last good cached value" discipline as
    get_regime_state/get_supertrend_state above. Only ever called for
    symbols in config.INDEX_SYMBOLS - see Swing/trading_engine.py's
    _evaluate_entry_signal."""
    cached = _day_range_cache.get(symbol)
    streak = _day_range_fail_streak.get(symbol, 0)
    effective_refresh = min(config.SUPERTREND_REFRESH_SECONDS * (2 ** streak), MAX_FETCH_BACKOFF_SECONDS)
    if cached and (_now_ist() - cached[0]).total_seconds() < effective_refresh:
        return cached[1]
    loop = asyncio.get_running_loop()
    try:
        state = await loop.run_in_executor(None, _fetch_day_range_state_once, symbol)
        _day_range_fail_streak[symbol] = 0
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch Day Range state - keeping last cached value", symbol)
        state = cached[1] if cached else None
        _day_range_fail_streak[symbol] = streak + 1
    _day_range_cache[symbol] = (_now_ist(), state)
    return state


# ------------------------------------------------------------------ #
# COPPER-only structure-break signal (config.COPPER_STRUCTURE_BREAK_
# ENABLED) - see that flag's own docstring in Swing/config.py for the
# full backtest/rationale. Combines structure_break.py's 5m/15m/1h regime
# into one +1/-1/0 agreement state, exactly mirroring
# backtest_swing_structure_break_mtf.py's own agree_bull/agree_bear
# combination logic - this IS the signal that was backtested, not a
# reimplementation of it.
#
# ARCHITECTURE (rewritten 22 Sep 2026, real incident): the first version
# had _evaluate_entry_signal/_evaluate_exit_signal AWAIT the live fetch
# directly. Those two functions run INSIDE monitor_loop's own single,
# sequential tick (position exits and entry scans for every symbol, one
# after another, no concurrency). dhanhq's own HTTP timeout is 60s; the
# fetch needs 3 timeframes, each with structure_break.py's own internal
# retry-on-empty (up to 2 attempts) - a genuinely slow/rate-limited
# stretch could take up to ~6 minutes for ONE call. Confirmed live: the
# whole monitor loop went silent for ~9 minutes straight after enabling
# this - not just COPPER's own check, EVERY symbol's, because the loop
# never got past the single await that was stuck.
#
# Fix: the fetch now runs on its OWN independent background task
# (structure_break_refresh_loop, started once at Swing startup - see
# Swing/swing_main.py), decoupled entirely from monitor_loop's tick.
# _evaluate_entry_signal/_evaluate_exit_signal read the cache ONLY
# (peek_structure_break_signal - synchronous, instant, never awaits a
# live fetch) - a slow refresh now means a briefly stale signal, never a
# frozen monitor loop. Second, independent fix: each of the 3 timeframe
# fetches now gets its OWN run_in_executor dispatch, with the 1.6s
# inter-call pacing done via `await asyncio.sleep()` (releases the
# executor thread and yields the event loop) instead of a blocking
# time.sleep() held INSIDE one long-running executor call - this app's
# default executor has only 5 workers total, shared by every live-
# trading package's own blocking Dhan calls, so holding one for the
# whole multi-call sequence was its own, independent way to starve the
# pool under load.
# ------------------------------------------------------------------ #
@dataclass
class StructureBreakSignal:
    combined: int   # +1 = all 3 timeframes agree bullish, -1 = agree bearish, 0 = no agreement
    computed_at: datetime


_structure_break_cache: dict[str, tuple[datetime, Optional[StructureBreakSignal]]] = {}
_structure_break_fail_streak: dict[str, int] = {}
_STRUCTURE_BREAK_FETCH_PACE_SECONDS = 1.6   # same back-to-back-REST-call pacing bt_fetch.py/the backtest script use


def _structure_break_ws_candles(symbol: str, interval_minutes: int) -> Optional[dict]:
    """The ws_candles_fn hook structure_break.fetch_timeframe accepts
    (added 24 Sep 2026, user request - "add WS feeds for COPPER should
    also use in structure_break.py... just switch from REST to WS (REST
    for fallback)"). Thin wrapper over candle_feed.py's own is_fresh/
    get_candles_dict - the exact same WS-first/REST-fallback discipline
    _get_intraday_series already applies for regime/Supertrend/Day Range,
    just handed to structure_break.py as a callback instead of baked into
    it directly (keeps that module import-free of the Swing package - see
    its own fetch_timeframe docstring). None (not an empty dict) on
    anything not fresh/warm enough - fetch_timeframe's own fallback logic
    treats that as "try REST instead", never as "zero candles exist"."""
    if not (config.USE_WS_CANDLES and candle_feed.is_fresh(symbol, config.WS_STALE_AFTER_SECONDS)):
        return None
    return candle_feed.get_candles_dict(symbol, interval_minutes)


def _fetch_one_structure_break_timeframe(symbol: str, timeframe: str) -> int:
    """Blocking, ONE timeframe only - always call via run_in_executor.
    Deliberately split out from a combined multi-fetch function (see this
    section's own module-level comment above) so the executor thread is
    released back to the shared pool between calls, not held through the
    whole sequence. Raises on any fetch failure or not-yet-warm timeframe -
    never returns a partial/best-effort regime.

    _underlying_reference(symbol) below is called FIRST purely for its
    WS-subscribe side effect (added 24 Sep 2026) - COPPER never reaches
    get_regime_state/get_supertrend_state/get_day_range_state (this
    module's own COPPER-only structure-break branch bypasses all three
    entirely, see this section's own module-level comment), so nothing
    else in this file would otherwise ever call candle_feed.ensure_
    subscribed for it. Its own return value is unused here - structure_
    break.py resolves its OWN security_id independently (own _mcx_
    contract_cache, deliberately not shared - see that module's own
    _underlying_reference docstring on staying standalone); this call
    only needs to happen, not feed its result forward."""
    _underlying_reference(symbol)
    import structure_break as sb
    r = sb.fetch_timeframe(symbol, timeframe, mcx=True, ws_candles_fn=_structure_break_ws_candles)
    if r.error or not r.warm:
        raise RuntimeError(f"{symbol}: structure-break {timeframe} fetch failed/not warm: {r.error}")
    return r.last_regime


def peek_structure_break_signal(symbol: str) -> Optional[StructureBreakSignal]:
    """Cache-only, synchronous, instant - the ONLY way _evaluate_entry_
    signal/_evaluate_exit_signal are allowed to read this signal (see this
    section's own module-level comment for why awaiting a live fetch from
    there froze the whole monitor loop). None means no successful refresh
    has completed yet (e.g. right after startup, before
    structure_break_refresh_loop's first iteration) - callers must treat
    that as "no signal yet," never force anything off a missing read."""
    cached = _structure_break_cache.get(symbol)
    return cached[1] if cached else None


# "Wait for the agreement to break and reform, don't re-enter instantly on
# a still-persisting one" (user request 22 Sep 2026, after two real same-
# day incidents: a profit-protection exit immediately re-entering COPPER
# at a worse price 9 seconds later, and a stop-loss exit repeatedly
# re-attempting entry every ~5s for 15+ minutes, blocked only by the MCX
# volume-floor gate - neither incident involved the combined signal
# itself ever changing). Maps symbol -> the combined value (+1/-1) a
# position was already ENTERED for (not just signalled - see
# mark_structure_break_consumed's own docstring for why that distinction
# matters). structure_break_entry_signal suppresses returning that same
# value again until it's observed the signal leave it first.
_structure_break_consumed: dict[str, int] = {}


def structure_break_entry_signal(symbol: str) -> Optional[int]:
    """The gated read _evaluate_entry_signal's COPPER branch actually
    uses (peek_structure_break_signal underneath). Returns None when
    there's no signal yet, no agreement (combined==0), OR the agreement
    is the same one a position was already entered for and hasn't broken
    since. Clears the "consumed" mark the first time it observes the
    signal at anything other than the consumed value - from that point
    on, reaching that value again is a FRESH formation, not a repeat.

    The "did it break" check runs BEFORE the combined==0 early return
    (not after) - a real bug caught in testing: combined dropping to 0
    (agreement lapses without a clean reversal) IS the break signal, and
    clearing consumed must happen on that transition too, not only on a
    direct flip to the opposite side. Returning early before checking
    would leave a stale consumed value in place forever if the agreement
    only ever lapses to neutral rather than flipping cleanly."""
    sig = peek_structure_break_signal(symbol)
    if sig is None:
        return None
    consumed = _structure_break_consumed.get(symbol)
    if consumed is not None and sig.combined != consumed:
        _structure_break_consumed[symbol] = None  # observed it break - next match is a fresh formation
        consumed = None
    if sig.combined == 0:
        return None
    if consumed is not None and sig.combined == consumed:
        return None  # same agreement a position already used - still waiting for it to break
    return sig.combined


def mark_structure_break_consumed(symbol: str, side: int) -> None:
    """Called ONLY once a real position has actually been OPENED for
    `side` (see Swing/trading_engine.py's enter_position_for_stock, its
    one call site) - deliberately NOT called merely when the signal
    fires, since a signal can fire repeatedly while genuinely blocked by
    a downstream gate (the MCX volume-floor gate did exactly this for
    15+ minutes straight in the real incident this whole mechanism
    fixes) - marking it consumed at signal-time instead of entry-time
    would have wrongly suppressed every one of those legitimate retries."""
    _structure_break_consumed[symbol] = side


def _predict_structure_break_entry_signal(symbol: str) -> Optional[int]:
    """Read-only prediction of what structure_break_entry_signal would
    return right now - same logic, but WITHOUT that function's "clear
    the consumed marker the instant a break is observed" side effect.
    That mutation must only ever happen from the real entry-evaluation
    path (_evaluate_entry_signal, on monitor_tick's own cadence) - a
    debug/introspection read polled at an arbitrary, unrelated cadence
    must never be able to advance real gating state just by being
    called. Used only by structure_break_debug_snapshot below."""
    sig = peek_structure_break_signal(symbol)
    if sig is None:
        return None
    consumed = _structure_break_consumed.get(symbol)
    if consumed is not None and sig.combined != consumed:
        consumed = None  # would clear on the real path - predicted here, not applied
    if sig.combined == 0:
        return None
    if consumed is not None and sig.combined == consumed:
        return None
    return sig.combined


def structure_break_debug_snapshot(symbol: str) -> dict:
    """Cache-only introspection for a debug endpoint (added 23 Sep 2026,
    user request after asking whether COPPER's structure-break signal is
    actually working - unlike /swing/signals' classic regime/Supertrend
    state, there was no way to see this mechanism's own state without
    reading server logs and inferring). Lays out everything peek_
    structure_break_signal/structure_break_entry_signal read from, in one
    place - never used by real entry/exit logic itself (that stays on
    the narrower, purpose-built peek_structure_break_signal/structure_
    break_entry_signal - see their own docstrings for why), this exists
    only so a human can see WHY the gated entry read is returning what it
    returns, without guessing from log silence (this file's fail-open
    refresh only logs on failure, so "nothing logged" is ambiguous
    between "working fine" and "never ran" without this).

    Deliberately calls _predict_structure_break_entry_signal, NOT
    structure_break_entry_signal directly - the real function mutates
    _structure_break_consumed as a side effect (clearing it the instant
    it observes a break), which a read-only debug endpoint must never
    trigger on its own, unrelated polling cadence (see that predictor's
    own docstring).

    `cache_age_seconds` is time since the last refresh ATTEMPT (success
    or failure - refresh_structure_break_signal stamps the cache either
    way); `computed_at`/`combined` are from the last SUCCESSFUL
    computation specifically, which can be older than the last attempt
    if the symbol has been failing. All fields are None/0 in their
    natural empty state if nothing has ever run yet for this symbol
    (e.g. right after startup, or before market hours open the refresh
    loop's own gate)."""
    cached = _structure_break_cache.get(symbol)
    sig = cached[1] if cached else None
    return {
        "symbol": symbol,
        "combined": sig.combined if sig else None,
        "computed_at": sig.computed_at.isoformat() if sig else None,
        "cache_age_seconds": (_now_ist() - cached[0]).total_seconds() if cached else None,
        "fail_streak": _structure_break_fail_streak.get(symbol, 0),
        "consumed_side": _structure_break_consumed.get(symbol),
        "effective_entry_signal": _predict_structure_break_entry_signal(symbol),
    }


async def refresh_structure_break_signal(symbol: str) -> None:
    """Does the actual fetch + cache update - called ONLY from
    structure_break_refresh_loop's own independent background task, NEVER
    from the entry/exit evaluators directly. Fail-open, same discipline as
    every other refresh in this file: a failure keeps the last good cached
    value and stamps the cache anyway (so a persistently-failing symbol
    doesn't get retried every single loop iteration - see get_regime_
    state's own comment on this exact bug/fix elsewhere in this file)."""
    cached = _structure_break_cache.get(symbol)
    streak = _structure_break_fail_streak.get(symbol, 0)
    loop = asyncio.get_running_loop()
    try:
        regimes: dict[str, int] = {}
        for i, tf in enumerate(("5m", "15m", "1h")):
            if i:
                await asyncio.sleep(_STRUCTURE_BREAK_FETCH_PACE_SECONDS)
            regimes[tf] = await loop.run_in_executor(None, _fetch_one_structure_break_timeframe, symbol, tf)
        agree_bull = all(v == 1 for v in regimes.values())
        agree_bear = all(v == -1 for v in regimes.values())
        combined = 1 if agree_bull else -1 if agree_bear else 0
        state = StructureBreakSignal(combined=combined, computed_at=_now_ist())
        _structure_break_fail_streak[symbol] = 0
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not refresh structure-break signal - keeping last cached value", symbol)
        state = cached[1] if cached else None
        _structure_break_fail_streak[symbol] = streak + 1
    _structure_break_cache[symbol] = (_now_ist(), state)


async def structure_break_refresh_loop(symbols: list) -> None:
    """Independent background task, started once at Swing startup
    (Swing/swing_main.py, alongside monitor_loop itself) - keeps the
    structure-break cache warm on its own timer, completely decoupled
    from monitor_loop's own tick. Checks cache age every 5s (cheap, no
    I/O) but only actually FETCHES when a symbol's cache has aged past
    its effective refresh interval (config.STRUCTURE_BREAK_REFRESH_
    SECONDS, doubling on each consecutive failure up to MAX_FETCH_
    BACKOFF_SECONDS - identical backoff shape to get_supertrend_state's
    own, just applied here instead of inline in the read path)."""
    logger.info("Structure-break refresh loop started for: %s", symbols)
    while True:
        for symbol in symbols:
            if not _symbol_market_open(symbol):
                continue
            cached = _structure_break_cache.get(symbol)
            streak = _structure_break_fail_streak.get(symbol, 0)
            effective_refresh = min(config.STRUCTURE_BREAK_REFRESH_SECONDS * (2 ** streak), MAX_FETCH_BACKOFF_SECONDS)
            if not cached or (_now_ist() - cached[0]).total_seconds() >= effective_refresh:
                try:
                    await refresh_structure_break_signal(symbol)
                except Exception:  # noqa: BLE001
                    logger.exception("%s: structure-break refresh loop iteration failed", symbol)
        await asyncio.sleep(5)
