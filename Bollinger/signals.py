"""
Bollinger strategy signal computation - a Bollinger-ribbon (5 Bollinger
Bands, period=20, deviations 0.1/0.2/0.3/0.4/0.5) trend filter combined
with a Vortex Indicator (period=14) confirmation, entering via a
software-simulated pending stop order once a genuine multi-candle
pullback forms against a confirmed trend. See Bollinger/config.py's own
module docstring and backtest_bollinger_vortex_9symbols_30day.py's
INTERPRETATION-CALLS docstring for the full derivation - the pure
indicator/state-machine functions below are ported VERBATIM from that
backtest script, not re-derived.

CRITICAL DESIGN NOTE (found during plan review, do not shortcut this):
the pullback-arming state machine is genuinely stateful ACROSS THE ENTIRE
CANDLE HISTORY - a swing point reference (last_swing_high_since_bullish/
last_swing_low_since_bearish) can anchor arbitrarily far back during a
long uninurrupted trend, and the pullback streak calculation reads all
the way back to that anchor. This module therefore REPLAYS THE FULL
RETAINED SERIES from index 0 every refresh cycle (never maintains
incremental state across cycles/restarts) - a short recent window would
silently truncate the anchor mid-trend and diverge from the backtest with
no error. This is still cheap: pure O(n) Python over a few thousand bars,
dispatched via run_in_executor, same order of magnitude as Swing's own
200-EMA-over-600-bar computation.

Only ever treats a "fire" as actionable if it happens on the NEWEST
confirmed bar of the current replay - a fire from several bars ago (which
would already have been acted on when it first appeared) must never be
re-attempted on a later cycle.

Fail-open, same discipline as Swing/signals.py: a fetch failure or
not-enough-data condition returns None and callers must treat that as
"skip this tick," never as a signal of any kind.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from Options.dhan_client import dhan_wrapper, IST
from Swing import candle_feed
from . import config

logger = logging.getLogger("bollinger_signals")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _symbol_market_open(symbol: str) -> bool:
    """Same per-symbol market-hours gate reasoning as Swing/signals.py's
    own _symbol_market_open - v1 scope is NSE equity only, so this is
    always the NSE_EQ segment (no MCX branch needed here)."""
    now = _now_ist()
    if now.weekday() >= 5:
        return False
    return dhan_wrapper.is_market_open(exchange_segment="NSE_EQ")


def _underlying_reference(symbol: str) -> tuple[str, str, str]:
    """NSE-equity-only underlying reference - see config.py's own v1 scope
    note. WS-subscribes via Swing.candle_feed (shared feed, confirmed
    idempotent/safe for a second package to call - see this package's own
    architecture plan) when config.USE_WS_CANDLES is on, exactly mirroring
    Swing/signals.py's own _underlying_reference."""
    security_id = dhan_wrapper._equity_security_id(symbol)
    exchange_segment, instrument_type = "NSE_EQ", "EQUITY"
    if config.USE_WS_CANDLES:
        try:
            candle_feed.ensure_subscribed(symbol, security_id, exchange_segment)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not WS-subscribe for candle feed - REST fallback continues", symbol)
    return security_id, exchange_segment, instrument_type


def _get_intraday_series(symbol: str, security_id: str, exchange_segment: str, instrument_type: str) -> dict:
    """Hybrid WS-then-REST fetch, direct port of Swing/signals.py's own
    _get_intraday_series pattern, simplified for Bollinger's single
    interval/single caller (no per-caller min_bars/require_prior_day
    parameterization needed - this module has exactly one consumer of
    this series). Trims a still-forming trailing bar from the REST path
    the same way Swing's own Supertrend fetch does - candle_feed.py's own
    get_candles_dict already only ever returns completed bars, so no
    trim is needed on that path."""
    if config.USE_WS_CANDLES and candle_feed.is_fresh(symbol, config.WS_STALE_AFTER_SECONDS):
        ws_data = candle_feed.get_candles_dict(symbol, config.SIGNAL_INTERVAL_MINUTES)
        if len(ws_data.get("close") or []) >= config.BB_PERIOD + 2:
            return ws_data
    data = dhan_wrapper.fetch_continuous_intraday(
        security_id, exchange_segment, instrument_type, config.SIGNAL_INTERVAL_MINUTES,
        lookback_days_override=config.REST_LOOKBACK_DAYS,
    )
    timestamps = data.get("timestamp") or []
    if timestamps:
        last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
        if _now_ist() < last_candle_start + timedelta(minutes=config.SIGNAL_INTERVAL_MINUTES):
            for key in ("open", "high", "low", "close", "volume", "timestamp"):
                if data.get(key):
                    data[key] = data[key][:-1]
    return data


# --------------------------------------------------------------------------- #
# Pure indicator/state-machine math - ported verbatim from
# backtest_bollinger_vortex_9symbols_30day.py. See that file's own
# INTERPRETATION-CALLS docstring for what each parameter/rule means.
# --------------------------------------------------------------------------- #
def _compute_sma(values: list[float], period: int) -> list[Optional[float]]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def _true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    n = len(closes)
    tr = [0.0] * n
    for i in range(n):
        tr[i] = (highs[i] - lows[i]) if i == 0 else max(
            highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    return tr


def _compute_vortex(highs: list[float], lows: list[float], closes: list[float], period: int):
    n = len(closes)
    tr = _true_range(highs, lows, closes)
    vm_plus = [0.0] * n
    vm_minus = [0.0] * n
    for i in range(1, n):
        vm_plus[i] = abs(highs[i] - lows[i - 1])
        vm_minus[i] = abs(lows[i] - highs[i - 1])
    vi_plus: list[Optional[float]] = [None] * n
    vi_minus: list[Optional[float]] = [None] * n
    for i in range(period, n):
        sum_tr = sum(tr[i - period + 1:i + 1])
        if sum_tr > 0:
            vi_plus[i] = sum(vm_plus[i - period + 1:i + 1]) / sum_tr
            vi_minus[i] = sum(vm_minus[i - period + 1:i + 1]) / sum_tr
    return vi_plus, vi_minus


def _compute_fractal_swings(highs: list[float], lows: list[float], lookback: int):
    n = len(highs)
    swing_high = [False] * n
    swing_low = [False] * n
    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback:i + lookback + 1]
        window_l = lows[i - lookback:i + lookback + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            swing_high[i] = True
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            swing_low[i] = True
    return swing_high, swing_low


class PendingOrder:
    __slots__ = ("side", "trigger_price", "stop_price")

    def __init__(self, side: str, trigger_price: float, stop_price: float):
        self.side = side  # "BULLISH" or "BEARISH"
        self.trigger_price = trigger_price
        self.stop_price = stop_price


@dataclass(frozen=True)
class BollingerSignalState:
    """Full replay outcome as of the newest confirmed bar - see this
    module's own docstring for why this is always a fresh full-history
    replay, never incremental state. `fired` is only non-None when the
    pending order's trigger crossed on THIS newest bar specifically -
    never a stale historical fire."""
    valid_bullish: bool
    valid_bearish: bool
    pending_side: Optional[str]         # "BULLISH"/"BEARISH" if an order is currently armed, else None
    pending_trigger_price: Optional[float]
    pending_stop_price: Optional[float]
    fired: Optional[str]                # "BULLISH"/"BEARISH" if the pending order fired on the newest bar, else None
    fired_trigger_price: Optional[float]
    fired_stop_price: Optional[float]
    last_close: float                   # the underlying's own most recent closed-candle price - used to normalize stop_pct at entry
    candle_start: Optional[datetime]


def _replay(highs: list[float], lows: list[float], closes: list[float],
            timestamps: list[float]) -> Optional[BollingerSignalState]:
    """The actual port of backtest_bollinger_vortex_9symbols_30day.py's
    compute_signals + run_backtest's own entry_signal_for_bar - see that
    file for the byte-for-byte source of truth this must keep matching.
    Replays from i=0 every call (fresh local state, never nonlocal/
    persisted across calls) - see this module's own docstring."""
    n = len(closes)
    min_bars = config.BB_PERIOD + config.VORTEX_PERIOD + (2 * config.SWING_FRACTAL_LOOKBACK) + config.MIN_PULLBACK_CANDLES + 5
    if n < min_bars:
        return None

    sma = _compute_sma(closes, config.BB_PERIOD)
    vi_plus, vi_minus = _compute_vortex(highs, lows, closes, config.VORTEX_PERIOD)
    swing_high, swing_low = _compute_fractal_swings(highs, lows, config.SWING_FRACTAL_LOOKBACK)

    valid_bullish = [False] * n
    valid_bearish = [False] * n
    for i in range(n):
        if sma[i] is None or vi_plus[i] is None or vi_minus[i] is None:
            continue
        bb_side_bullish = closes[i] > sma[i]
        bb_side_bearish = closes[i] < sma[i]
        vortex_bullish = vi_plus[i] > vi_minus[i]
        vortex_bearish = vi_minus[i] > vi_plus[i]
        valid_bullish[i] = bb_side_bullish and vortex_bullish
        valid_bearish[i] = bb_side_bearish and vortex_bearish

    pending, fired_at_last_bar = _replay_pending_order_loop(
        highs, lows, closes, valid_bullish, valid_bearish, swing_high, swing_low,
        config.SWING_FRACTAL_LOOKBACK, config.MIN_PULLBACK_CANDLES,
    )

    last = n - 1
    fired_side = fired_trigger = fired_stop = None
    if fired_at_last_bar is not None:
        fired_side, fired_trigger, fired_stop = fired_at_last_bar

    return BollingerSignalState(
        valid_bullish=valid_bullish[last], valid_bearish=valid_bearish[last],
        pending_side=pending.side if pending else None,
        pending_trigger_price=pending.trigger_price if pending else None,
        pending_stop_price=pending.stop_price if pending else None,
        fired=fired_side, fired_trigger_price=fired_trigger, fired_stop_price=fired_stop,
        last_close=closes[last],
        candle_start=datetime.fromtimestamp(timestamps[last], tz=IST) if timestamps else None,
    )


def _replay_pending_order_loop(
    highs: list[float], lows: list[float], closes: list[float],
    valid_bullish: list[bool], valid_bearish: list[bool],
    swing_high: list[bool], swing_low: list[bool],
    lookback: int, min_pullback: int,
) -> tuple[Optional[PendingOrder], Optional[tuple[str, float, float]]]:
    """The actual pullback-arming/fire/cancel state machine - split out
    from _replay so it can be unit-tested directly against hand-crafted
    valid_bullish/valid_bearish/swing_high/swing_low arrays (see
    tests/test_bollinger_signals.py), independent of the BB/Vortex math
    that derives those arrays in real use (already validated via
    backtest_bollinger_vortex_9symbols_30day.py's own reviewed 30-day
    results). Direct, byte-for-byte port of that same backtest script's
    entry_signal_for_bar - see this module's own top-of-file docstring
    for why this always runs as a FRESH replay from i=0, never persisted
    state across calls. Returns (final pending order or None, the fire
    that happened on the LAST bar specifically, or None if no fire
    happened there)."""
    n = len(closes)
    pending: Optional[PendingOrder] = None
    last_swing_high_since_bullish: Optional[tuple[int, float]] = None
    last_swing_low_since_bearish: Optional[tuple[int, float]] = None
    running_pullback_extreme: Optional[float] = None
    fired_at_last_bar: Optional[tuple[str, float, float]] = None

    for i in range(lookback, n):
        j = i - lookback
        fired_at_last_bar = None  # only the LAST loop iteration's value survives past the loop

        if swing_high[j] and valid_bullish[j]:
            last_swing_high_since_bullish = (j, highs[j])
        if swing_low[j] and valid_bearish[j]:
            last_swing_low_since_bearish = (j, lows[j])
        if not valid_bullish[i]:
            last_swing_high_since_bullish = None
        if not valid_bearish[i]:
            last_swing_low_since_bearish = None

        if pending is not None:
            still_valid = valid_bullish[i] if pending.side == "BULLISH" else valid_bearish[i]
            if not still_valid:
                pending = None
                running_pullback_extreme = None

        if valid_bullish[i] and last_swing_high_since_bullish is not None:
            swing_idx, swing_price = last_swing_high_since_bullish
            if i > swing_idx:
                closes_since = closes[swing_idx:i + 1]
                down_streak = 0
                for k in range(1, len(closes_since)):
                    down_streak = down_streak + 1 if closes_since[k] < closes_since[k - 1] else 0
                if down_streak >= min_pullback:
                    if pending is None or pending.side != "BULLISH":
                        pending = PendingOrder("BULLISH", swing_price, lows[i])
                        running_pullback_extreme = lows[i]
                    else:
                        if swing_price > pending.trigger_price:
                            pending.trigger_price = swing_price
                        running_pullback_extreme = min(running_pullback_extreme, lows[i])
                        pending.stop_price = running_pullback_extreme

        if valid_bearish[i] and last_swing_low_since_bearish is not None:
            swing_idx, swing_price = last_swing_low_since_bearish
            if i > swing_idx:
                closes_since = closes[swing_idx:i + 1]
                up_streak = 0
                for k in range(1, len(closes_since)):
                    up_streak = up_streak + 1 if closes_since[k] > closes_since[k - 1] else 0
                if up_streak >= min_pullback:
                    if pending is None or pending.side != "BEARISH":
                        pending = PendingOrder("BEARISH", swing_price, highs[i])
                        running_pullback_extreme = highs[i]
                    else:
                        if swing_price < pending.trigger_price:
                            pending.trigger_price = swing_price
                        running_pullback_extreme = max(running_pullback_extreme, highs[i])
                        pending.stop_price = running_pullback_extreme

        if pending is not None:
            if pending.side == "BULLISH" and highs[i] >= pending.trigger_price:
                fired_at_last_bar = ("BULLISH", pending.trigger_price, pending.stop_price)
                pending = None
                running_pullback_extreme = None
                last_swing_high_since_bullish = None
            elif pending.side == "BEARISH" and lows[i] <= pending.trigger_price:
                fired_at_last_bar = ("BEARISH", pending.trigger_price, pending.stop_price)
                pending = None
                running_pullback_extreme = None
                last_swing_low_since_bearish = None

    return pending, fired_at_last_bar


# --------------------------------------------------------------------------- #
# Cached, throttled, fail-open public entry point - same discipline as
# Swing/signals.py's get_regime_state/get_supertrend_state.
# --------------------------------------------------------------------------- #
_signal_cache: dict[str, tuple[datetime, Optional[BollingerSignalState]]] = {}
_fail_streak: dict[str, int] = {}
MAX_FETCH_BACKOFF_SECONDS = 300


def _fetch_signal_state_once(symbol: str) -> Optional[BollingerSignalState]:
    """Blocking - always call via run_in_executor."""
    security_id, exchange_segment, instrument_type = _underlying_reference(symbol)
    data = _get_intraday_series(symbol, security_id, exchange_segment, instrument_type)
    closes = data.get("close") or []
    if not closes:
        raise RuntimeError(
            f"{symbol}: fetch_continuous_intraday returned no data at all for the "
            f"{config.SIGNAL_INTERVAL_MINUTES}-min Bollinger series - treating as a fetch failure, "
            f"not genuinely insufficient history"
        )
    return _replay(data.get("high") or [], data.get("low") or [], closes, data.get("timestamp") or [])


async def get_signal_state(symbol: str) -> Optional[BollingerSignalState]:
    """Cached, throttled (config.SIGNAL_REFRESH_SECONDS, doubling on each
    consecutive failure up to MAX_FETCH_BACKOFF_SECONDS), fail-open - a
    fetch failure or insufficient-history condition keeps the LAST good
    cached state rather than returning a fresh, possibly-wrong None that
    would read as "no signal" when really it's just "couldn't check right
    now". Callers must still treat every None (including the very first
    call before anything is cached) as "skip this tick"."""
    cache_key = symbol
    cached = _signal_cache.get(cache_key)
    streak = _fail_streak.get(cache_key, 0)
    effective_refresh = min(config.SIGNAL_REFRESH_SECONDS * (2 ** streak), MAX_FETCH_BACKOFF_SECONDS)
    if cached and (_now_ist() - cached[0]).total_seconds() < effective_refresh:
        return cached[1]
    loop = asyncio.get_running_loop()
    try:
        state = await loop.run_in_executor(None, _fetch_signal_state_once, symbol)
        _fail_streak[cache_key] = 0
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch Bollinger signal state - keeping last cached value", symbol)
        state = cached[1] if cached else None
        _fail_streak[cache_key] = streak + 1
    _signal_cache[cache_key] = (_now_ist(), state)
    return state


def peek_signal_state(symbol: str) -> Optional[BollingerSignalState]:
    """Cache-only counterpart for GET /bollinger/signals - no live fetch."""
    cached = _signal_cache.get(symbol)
    return cached[1] if cached else None
