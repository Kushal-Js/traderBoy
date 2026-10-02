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
from datetime import date, datetime, timedelta
from typing import Optional

from Options.dhan_client import dhan_wrapper, IST
from Swing import candle_feed
from . import config

logger = logging.getLogger("bollinger_signals")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _symbol_market_open(symbol: str) -> bool:
    """Same per-symbol market-hours gate reasoning as Swing/signals.py's
    own _symbol_market_open (26 Sep 2026: extended to match it exactly,
    now that this package also trades MCX/index - see config.py's own
    module docstring). An index (config.INDEX_SYMBOLS) shares NSE cash-
    market hours with a plain equity, same as Swing's own version - only
    MCX gets its own, longer session."""
    now = _now_ist()
    if now.weekday() >= 5:  # Saturday/Sunday - neither exchange trades
        return False
    segment = "MCX_COMM" if dhan_wrapper.is_mcx_commodity(symbol) else "NSE_EQ"
    return dhan_wrapper.is_market_open(exchange_segment=segment)


# Resolved MCX futures contract's security_id, cached per calendar day -
# same caching rationale as Swing/signals.py's own _mcx_contract_cache
# (added 26 Sep 2026 alongside this package's MCX support).
_mcx_contract_cache: dict[str, tuple[date, str]] = {}


def _underlying_reference(symbol: str) -> tuple[str, str, str]:
    """Underlying reference for the regime series - extended 26 Sep 2026
    (see config.py's own module docstring) to mirror Swing/signals.py's
    own _underlying_reference exactly: an MCX commodity resolves its
    current futures contract (no continuous "spot" exists for one), an
    index (config.INDEX_SYMBOLS) resolves its own index security_id
    (IDX_I/INDEX - no SEM_INSTRUMENT_NAME=="EQUITY" row exists for an
    index), everything else keeps the original NSE-equity path unchanged.
    WS-subscribes via Swing.candle_feed (shared feed, confirmed idempotent/
    safe for a second package to call - see this package's own
    architecture plan) for all three branches when config.USE_WS_CANDLES
    is on."""
    is_index = symbol in config.INDEX_SYMBOLS
    if dhan_wrapper.is_mcx_commodity(symbol):
        today = _now_ist().date()
        cached = _mcx_contract_cache.get(symbol)
        if not cached or cached[0] != today:
            contract = dhan_wrapper.get_mcx_futures_contract(symbol)
            _mcx_contract_cache[symbol] = (today, contract.security_id)
            logger.info("%s: resolved MCX futures contract for today's Bollinger signal reference: security_id=%s",
                        symbol, contract.security_id)
        security_id, exchange_segment, instrument_type = _mcx_contract_cache[symbol][1], "MCX_COMM", "FUTCOM"
    elif is_index:
        security_id, exchange_segment, instrument_type = dhan_wrapper.index_security_id(symbol), "IDX_I", "INDEX"
    else:
        security_id, exchange_segment, instrument_type = dhan_wrapper._equity_security_id(symbol), "NSE_EQ", "EQUITY"

    if config.USE_WS_CANDLES:
        try:
            candle_feed.ensure_subscribed(symbol, security_id, exchange_segment)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not WS-subscribe for candle feed - REST fallback continues", symbol)
    return security_id, exchange_segment, instrument_type


_SERIES_KEYS = ("open", "high", "low", "close", "volume", "timestamp")

# Last good REST_LOOKBACK_DAYS series per symbol - reused until a newer
# closed bar should exist, so the full-history base costs ~one REST call
# per symbol per bar instead of one per SIGNAL_REFRESH_SECONDS.
_rest_series_cache: dict[str, dict] = {}
# When each symbol's base was last requested (successful or not). A base still
# behind after this bar's first request is re-requested at most every
# REST_BASE_MIN_REFETCH_SECONDS (2 Oct 2026, same floor as Swing/signals): in
# the session's first bar the "newest closed bar" (09:10) never exists, and
# every call used to refetch the 60-day series - several per second at the
# open (UM + SB tick listeners). WS bars still extend the base meanwhile.
_rest_requested_at: dict[str, datetime] = {}
REST_BASE_MIN_REFETCH_SECONDS = 60


def _fetch_rest_series(security_id: str, exchange_segment: str, instrument_type: str) -> dict:
    data = dhan_wrapper.fetch_continuous_intraday(
        security_id, exchange_segment, instrument_type, config.SIGNAL_INTERVAL_MINUTES,
        lookback_days_override=config.REST_LOOKBACK_DAYS,
    )
    timestamps = data.get("timestamp") or []
    if timestamps:
        last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
        if _now_ist() < last_candle_start + timedelta(minutes=config.SIGNAL_INTERVAL_MINUTES):
            for key in _SERIES_KEYS:
                if data.get(key):
                    data[key] = data[key][:-1]
    return data


def _get_intraday_series(symbol: str, security_id: str, exchange_segment: str, instrument_type: str) -> dict:
    """Full-history REST series (REST_LOOKBACK_DAYS, the same continuous
    multi-day window the backtest replays), with any NEWER completed WS
    bars appended on top.

    Fixed 28 Sep 2026 (live finding): this used to return the WS series
    on its own once it had BB_PERIOD + 2 bars. The WS feed only holds bars
    since the day's first subscribe, so (a) between BB_PERIOD + 2 and
    _replay's own min_bars it silently returned None - 14 of 17 symbols
    had no signal state at all after an 11:12 IST restart - and (b) past
    min_bars it replayed a today-only window, which the module docstring
    above explains diverges from the backtest with no error. WS is now
    only ever an extension of the REST base, never a replacement; an empty
    REST base returns {} (a fetch failure) rather than falling back to a
    today-only WS series."""
    interval = config.SIGNAL_INTERVAL_MINUTES
    now = _now_ist()
    bar_start = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % interval)
    newest_closed_start = bar_start - timedelta(minutes=interval)

    base = _rest_series_cache.get(symbol)
    base_ts = (base or {}).get("timestamp") or []
    if not base_ts or datetime.fromtimestamp(base_ts[-1], tz=IST) < newest_closed_start:
        requested = _rest_requested_at.get(symbol)
        if (not base_ts or requested is None or requested < bar_start
                or (now - requested).total_seconds() >= REST_BASE_MIN_REFETCH_SECONDS):
            _rest_requested_at[symbol] = now
            fetched = _fetch_rest_series(security_id, exchange_segment, instrument_type)
            if fetched.get("close"):
                _rest_series_cache[symbol] = fetched
                base = fetched
    if not base or not base.get("close"):
        return {}

    data = {key: list(base.get(key) or []) for key in _SERIES_KEYS}
    if config.USE_WS_CANDLES and candle_feed.is_fresh(symbol, config.WS_STALE_AFTER_SECONDS):
        ws_data = candle_feed.get_candles_dict(symbol, interval)
        last_ts = data["timestamp"][-1] if data["timestamp"] else None
        for i, ts in enumerate(ws_data.get("timestamp") or []):
            if last_ts is None or ts > last_ts:
                for key in _SERIES_KEYS:
                    data[key].append(ws_data[key][i])
    return data


def is_symbol_ws_fresh(symbol: str) -> bool:
    """True when `symbol`'s WS-reconstructed candle feed has ticked within
    config.WS_STALE_AFTER_SECONDS - same freshness check `_get_intraday_
    series` itself uses, exposed for `trading_engine.py`'s entry-scan loop
    (added 26 Sep 2026, user-flagged audit finding, same fix as Swing's own
    - see Swing/signals.py's own `is_symbol_ws_fresh` docstring for the
    full rationale: SYMBOL_PACING_SECONDS was being applied unconditionally
    even for a symbol about to be served entirely from this in-memory
    cache with no network call at all. Heuristic, not a guarantee - see
    that same docstring for why an occasional miss is acceptable here."""
    return config.USE_WS_CANDLES and candle_feed.is_fresh(symbol, config.WS_STALE_AFTER_SECONDS)


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
# One fetch per symbol at a time (1 Oct 2026): Super Bollinger (paper) and Unified Momentum both force a refresh
# of the same stock on the first tick of every new bar - a caller that finds another one's fetch of the same symbol
# in flight waits for it and takes its result instead of making a second, identical Dhan call.
_fetch_locks: dict[str, asyncio.Lock] = {}
MAX_FETCH_BACKOFF_SECONDS = 300


def resting_trigger_hit(state: Optional[BollingerSignalState], forming: Optional[dict],
                        interval_minutes: int, today: date) -> Optional[tuple[str, float, float, float]]:
    """Resting-stop-order entry check (added 28 Sep 2026 - see config.
    ENTRY_MODE's own comment for the full why). Pure function, no I/O.

    HOW IT WORKS, step by step:
      1. `state` is the replay as of the newest CLOSED 5-min bar. If it
         shows an armed pending order (e.g. BULLISH, trigger 1864.70), that
         is our resting buy-stop for the NEXT bar.
      2. `forming` is that next bar, still in progress, from the live tick
         feed (Swing.candle_feed.forming_bar). Its high/low cover every tick
         so far in the bar, so a touch between our 5-second polls still
         counts.
      3. If the forming bar's high has reached the trigger (low, for
         BEARISH), the resting order would have filled -> return an entry.

    Guards (each returns None = "no entry this tick"):
      - no armed pending order;
      - the pending order is from a previous trading day (the video's
        pending order is a same-session idea - never carried overnight);
      - the forming bar is not EXACTLY the bar right after the pending
        order's bar (the signal cache or the tick feed is out of step, so
        we can't be sure which order was resting when the touch happened).

    Returns (side, trigger_price, stop_price, reference_price). The trigger
    is the reference price for sizing the stop (the order fills at the
    trigger), matching bollinger_research.py's validated "resting" mode.
    The caller is responsible for acting on a given pending order only
    once - see trading_engine._resting_consumed."""
    if state is None or state.pending_side is None or state.pending_trigger_price is None:
        return None
    if state.candle_start is None or state.candle_start.date() != today:
        return None
    if forming is None or forming.get("candle_start") != state.candle_start + timedelta(minutes=interval_minutes):
        return None
    trigger = state.pending_trigger_price
    if state.pending_side == "BULLISH" and forming["high"] >= trigger:
        return "BULLISH", trigger, state.pending_stop_price, trigger
    if state.pending_side == "BEARISH" and forming["low"] <= trigger:
        return "BEARISH", trigger, state.pending_stop_price, trigger
    return None


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


async def get_signal_state(symbol: str, force: bool = False) -> Optional[BollingerSignalState]:
    """`force=True` (added 30 Sep 2026, Super Bollinger's tick-driven entries)
    skips the normal refresh interval - used once per symbol right after a
    new 5-min bar starts, so the new bar's pending order is known within a
    second instead of up to SIGNAL_REFRESH_SECONDS later. Never forces
    through a failure backoff.

    Cached, throttled (config.SIGNAL_REFRESH_SECONDS, doubling on each
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
    if cached and (_now_ist() - cached[0]).total_seconds() < effective_refresh and not (force and streak == 0):
        return cached[1]
    lock = _fetch_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        latest = _signal_cache.get(cache_key)
        if latest is not None and latest is not cached:
            return latest[1]          # another caller refreshed it while this one waited
        loop = asyncio.get_running_loop()
        try:
            state = await loop.run_in_executor(dhan_wrapper.history_executor(), _fetch_signal_state_once, symbol)
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
