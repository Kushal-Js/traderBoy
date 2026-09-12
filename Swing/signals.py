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
from datetime import datetime, timedelta
from typing import Optional

from Options.dhan_client import dhan_wrapper, _compute_ema, _compute_supertrend, IST
from . import config

logger = logging.getLogger("swing_signals")


def _now_ist() -> datetime:
    return datetime.now(IST)


# --------------------------------------------------------------------------- #
# Regime: 5-min EMA(200) vs 15-min EMA(200)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegimeState:
    fast_ema: float
    slow_ema: float
    is_bullish: bool          # fast_ema > slow_ema
    fast_candle_start: Optional[datetime]
    slow_candle_start: Optional[datetime]


_regime_cache: dict[str, tuple[datetime, Optional[RegimeState]]] = {}


def _ema200_on(closes: list[float], timestamps: list[int], interval_minutes: int) -> tuple[Optional[float], Optional[datetime]]:
    """Drops a still-forming last candle, requires at least REGIME_EMA_
    PERIOD closed bars, returns (last EMA(200) value, that candle's own
    start) or (None, None)."""
    if timestamps:
        last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
        if _now_ist() < last_candle_start + timedelta(minutes=interval_minutes):
            closes, timestamps = closes[:-1], timestamps[:-1]
    if len(closes) < config.REGIME_EMA_PERIOD:
        return None, None
    ema = _compute_ema(closes, config.REGIME_EMA_PERIOD)
    if ema[-1] is None:
        return None, None
    return ema[-1], (datetime.fromtimestamp(timestamps[-1], tz=IST) if timestamps else None)


def _fetch_regime_state_once(symbol: str) -> Optional[RegimeState]:
    """Blocking - always call via run_in_executor. Two continuous-candle
    fetches (5-min and 15-min), each with the longer REGIME_EMA_LOOKBACK_
    DAYS override (see fetch_continuous_intraday's own docstring for why
    the shared 7-day global can't warm up a 200-period EMA at all)."""
    security_id = dhan_wrapper._equity_security_id(symbol)

    fast_data = dhan_wrapper.fetch_continuous_intraday(
        security_id, "NSE_EQ", "EQUITY", config.REGIME_FAST_INTERVAL_MINUTES,
        lookback_days_override=config.REGIME_EMA_LOOKBACK_DAYS,
    )
    fast_ema, fast_start = _ema200_on(
        fast_data.get("close") or [], fast_data.get("timestamp") or [], config.REGIME_FAST_INTERVAL_MINUTES,
    )
    if fast_ema is None:
        return None

    slow_data = dhan_wrapper.fetch_continuous_intraday(
        security_id, "NSE_EQ", "EQUITY", config.REGIME_SLOW_INTERVAL_MINUTES,
        lookback_days_override=config.REGIME_EMA_LOOKBACK_DAYS,
    )
    slow_ema, slow_start = _ema200_on(
        slow_data.get("close") or [], slow_data.get("timestamp") or [], config.REGIME_SLOW_INTERVAL_MINUTES,
    )
    if slow_ema is None:
        return None

    return RegimeState(
        fast_ema=fast_ema, slow_ema=slow_ema, is_bullish=fast_ema > slow_ema,
        fast_candle_start=fast_start, slow_candle_start=slow_start,
    )


def peek_regime_state(symbol: str) -> Optional[RegimeState]:
    """Cache-only, no fetch - for GET /swing/signals (the rollout's main
    observe-before-you-trade tool). Returns whatever was last computed,
    or None if nothing has run for this symbol yet."""
    cached = _regime_cache.get(symbol)
    return cached[1] if cached else None


async def get_regime_state(symbol: str) -> Optional[RegimeState]:
    """Cached, throttled (config.REGIME_REFRESH_SECONDS), fail-open - see
    this module's own docstring for why a fetch exception here keeps the
    last good cached value rather than writing None over it."""
    cached = _regime_cache.get(symbol)
    if cached and (_now_ist() - cached[0]).total_seconds() < config.REGIME_REFRESH_SECONDS:
        return cached[1]
    loop = asyncio.get_running_loop()
    try:
        state = await loop.run_in_executor(None, _fetch_regime_state_once, symbol)
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch regime state - keeping last cached value", symbol)
        return cached[1] if cached else None
    _regime_cache[symbol] = (_now_ist(), state)
    return state


# --------------------------------------------------------------------------- #
# 5-min Supertrend crossover - lifted from the old Swing/trading_engine.py's
# own SupertrendState/_fetch_supertrend_state (already correct, already
# proven, unchanged in substance - only the interval is no longer a
# parameter, since Swing v2 only ever needs the one 5-min series).
# --------------------------------------------------------------------------- #
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

    @property
    def crossed_above(self) -> bool:
        """True only on the candle where price flips from AT-OR-BELOW to
        ABOVE the Supertrend line - a real transition, not "is above right
        now" (true for every candle of an established uptrend)."""
        return (not self.prev_is_above) and self.is_above

    @property
    def crossed_below(self) -> bool:
        return self.prev_is_above and not self.is_above


_supertrend_cache: dict[str, tuple[datetime, Optional[SupertrendState]]] = {}


def _fetch_supertrend_state_once(symbol: str) -> Optional[SupertrendState]:
    """Blocking - always call via run_in_executor. Returns None only if
    the fetch genuinely came back with too little data - callers treat
    that as "no signal," never as a false crossover."""
    security_id = dhan_wrapper._equity_security_id(symbol)
    data = dhan_wrapper.fetch_continuous_intraday(
        security_id, "NSE_EQ", "EQUITY", config.SUPERTREND_INTERVAL_MINUTES,
    )
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    volumes = data.get("volume") or []
    timestamps = data.get("timestamp") or []

    period = config.SUPERTREND_PERIOD
    if timestamps:
        last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
        if _now_ist() < last_candle_start + timedelta(minutes=config.SUPERTREND_INTERVAL_MINUTES):
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
    )


def peek_supertrend_state(symbol: str) -> Optional[SupertrendState]:
    """Cache-only counterpart to peek_regime_state above."""
    cached = _supertrend_cache.get(symbol)
    return cached[1] if cached else None


async def get_supertrend_state(symbol: str) -> Optional[SupertrendState]:
    """Cached, throttled (config.SUPERTREND_REFRESH_SECONDS), fail-open -
    same "keep the last good value" discipline as get_regime_state above."""
    cached = _supertrend_cache.get(symbol)
    if cached and (_now_ist() - cached[0]).total_seconds() < config.SUPERTREND_REFRESH_SECONDS:
        return cached[1]
    loop = asyncio.get_running_loop()
    try:
        state = await loop.run_in_executor(None, _fetch_supertrend_state_once, symbol)
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch Supertrend state - keeping last cached value", symbol)
        return cached[1] if cached else None
    _supertrend_cache[symbol] = (_now_ist(), state)
    return state
