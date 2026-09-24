"""
Multi-timeframe "structure break" detector (user request 22 Sep 2026) - a
Python port of the BOSWaves "Smart Money Flow Cloud" Pine Script indicator
(see .claude/skills/structure-break/SKILL.md for the original source).

WHAT "STRUCTURE BREAK" MEANS HERE: this indicator is a money-flow-weighted
adaptive ATR band around a smoothed EMA/ALMA baseline. A "structure break"
is a fully-closed candle's close crossing OUTSIDE that band - the same
event the Pine indicator plots as its Buy/Sell labels (st.switchUp /
st.switchDown). This is deliberately NOT classic swing-structure analysis
(higher-high/higher-low breaks) - it's this specific indicator's
band-cross regime flip, per the user's own framing when this was built.

READ-ONLY, NOT WIRED TO ANY LIVE STRATEGY: this module only fetches
candles and reports a regime per timeframe. It never places an order and
is not imported by Options/Futures/Luxury/Swing. Building/running it
carries none of [[feedback-live-trading-safety]]'s real-money risk - that
gate is about starting/restarting the live bot or running order-placement
code, neither of which happens here.

DATA: uses the same DhanClient candle-fetch path every real signal in this
repo uses (Options.dhan_client.dhan_wrapper.fetch_continuous_intraday /
historical_daily_data) - a continuous multi-session series per
[[feedback-continuous-candles]], never a today-only fragment. Equities
only (NSE_EQ/EQUITY, via dhan_wrapper._equity_security_id) - indices and
options aren't resolved by that lookup.

EMA/ATR CONVENTION: deliberately reuses this repo's existing SMA-seeded
EMA/Wilder-ATR convention (Options/dhan_client.py's _compute_ema /
_compute_supertrend's inline ATR), not Pine's own bar-0-seeded ta.ema. This
means values won't match TradingView bar-for-bar during the first `period`
bars of a freshly fetched window, but converge after that - the same
accepted tradeoff already documented for the real-money Supertrend/
EMA-cross signals elsewhere in dhan_client.py. Fetching enough warm-up
history (see _LOOKBACK_DAYS below) keeps the actually-reported last bar
well past that warm-up window.
"""
from __future__ import annotations

import argparse
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Valid Dhan intraday intervals: 1, 5, 15, 25, 60 minutes.
TIMEFRAMES = {
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "1d": None,  # daily candles come from historical_daily_data, not intraday
}

# Calendar-day lookback per timeframe, chosen so the reported last bar sits
# well past this module's EMA/ATR warm-up window (len=34, mfLen=24,
# atrLen=14 by default) rather than right at the cold-start edge -
# analogous to Swing/signals.py's REGIME_SLOW_INTERVAL_MINUTES 15-min
# lookback and dhan_client.py's fetch_continuous_intraday
# lookback_days_override.
_INTRADAY_LOOKBACK_DAYS = {5: 15, 15: 30, 60: 90}
_DAILY_LOOKBACK_DAYS = 400


@dataclass
class StructureBreakParams:
    """Mirrors the Pine indicator's inputs 1:1 (defaults = the indicator's
    own defaults)."""
    length: int = 34
    basis_type: str = "EMA"          # "EMA" or "ALMA"
    alma_offset: float = 0.85
    alma_sigma: float = 6.0
    basis_smooth: int = 3
    mf_len: int = 24
    mf_smooth: int = 5
    mf_power: float = 1.2
    atr_len: int = 14
    min_mult: float = 0.9
    max_mult: float = 2.2
    dot_cooldown: int = 12
    tick_size: float = 0.05          # syminfo.mintick stand-in for the gauge


@dataclass
class StructureBreakResult:
    """One fully-computed timeframe's worth of output. Arrays are one entry
    per input bar (None where a value isn't warm yet); the `last_*` fields
    summarize the most recently fully-closed bar, which is what callers
    actually care about."""
    n: int
    basis: list
    upper: list
    lower: list
    regime: list           # 1 = bullish regime, -1 = bearish, 0 = not yet warm
    switch_up: list         # True on the bar a bullish structure break happened
    switch_down: list
    bull_retest: list
    bear_retest: list
    strength: list          # signed trend-strength gauge, -1..1

    # Raw per-bar series, carried through for multi-series/backtest
    # consumers (e.g. aligning 5m/15m/1h regimes by timestamp) - empty on
    # the error-path constructors below, which never had bars to carry.
    close: list = field(default_factory=list)
    open: list = field(default_factory=list)
    timestamps: list = field(default_factory=list)   # epoch seconds, one per bar

    last_close: Optional[float] = None
    last_regime: int = 0
    last_upper: Optional[float] = None
    last_lower: Optional[float] = None
    last_basis: Optional[float] = None
    last_strength_pct: Optional[float] = None
    broke_this_bar: Optional[str] = None      # "up" / "down" / None
    bars_since_break: Optional[int] = None
    retest_this_bar: Optional[str] = None      # "bull" / "bear" / None
    warm: bool = False
    error: Optional[str] = None


# --------------------------------------------------------------------- #
# Pure indicator math - no I/O, matches this repo's existing
# dhan_client.py _compute_ema/_compute_supertrend style so it's testable
# and reviewable the same way.
# --------------------------------------------------------------------- #
def _ema(values: list, period: int) -> list:
    n = len(values)
    out: list = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def _ema_of_series(values: list, period: int) -> list:
    """Like _ema, but tolerates None gaps at the front (skips them and
    starts warming up from the first non-None run) - used for smoothing
    already-derived series (money flow, basis) that are None during their
    own warm-up."""
    n = len(values)
    out: list = [None] * n
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or period <= 0:
        return out
    clean = values[start:]
    smoothed = _ema(clean, period)
    for i, v in enumerate(smoothed):
        out[start + i] = v
    return out


def _alma(values: list, period: int, offset: float, sigma: float) -> list:
    n = len(values)
    out: list = [None] * n
    if n < period or period <= 0:
        return out
    m = offset * (period - 1)
    s = period / sigma
    weights = [math.exp(-((j - m) ** 2) / (2 * s * s)) for j in range(period)]
    wsum = sum(weights)
    for i in range(period - 1, n):
        window = values[i - period + 1: i + 1]
        out[i] = sum(w * v for w, v in zip(weights, window)) / wsum
    return out


def _basis_from(values: list, params: StructureBreakParams) -> list:
    raw = (
        _alma(values, params.length, params.alma_offset, params.alma_sigma)
        if params.basis_type == "ALMA"
        else _ema(values, params.length)
    )
    return _ema_of_series(raw, params.basis_smooth) if params.basis_smooth > 1 else raw


def _true_range(highs: list, lows: list, closes: list) -> list:
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


def _atr(highs: list, lows: list, closes: list, period: int) -> list:
    n = len(closes)
    out: list = [None] * n
    if n < period:
        return out
    tr = _true_range(highs, lows, closes)
    out[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def _rolling_sum(values: list, window: int) -> list:
    n = len(values)
    out = [0.0] * n
    running = 0.0
    for i in range(n):
        running += values[i]
        if i >= window:
            running -= values[i - window]
        out[i] = running
    return out


def _tanh(x: float) -> float:
    ex, emx = math.exp(x), math.exp(-x)
    return (ex - emx) / (ex + emx)


def compute_structure_break(
    opens: list, highs: list, lows: list, closes: list, volumes: list,
    timestamps: Optional[list] = None,
    params: Optional[StructureBreakParams] = None,
) -> StructureBreakResult:
    """Pure function - takes plain OHLCV lists (already trimmed of any
    still-forming candle by the caller), returns one StructureBreakResult.
    No network I/O, so this is independently unit-testable exactly like
    dhan_client.py's _compute_supertrend. `timestamps` is optional (epoch
    seconds, one per bar) - carried through unchanged for callers that
    need to align this timeframe's regime against another's (e.g. a
    multi-timeframe backtest); the math itself never reads it."""
    params = params or StructureBreakParams()
    n = len(closes)
    if n == 0:
        return StructureBreakResult(
            n=0, basis=[], upper=[], lower=[], regime=[], switch_up=[], switch_down=[],
            bull_retest=[], bear_retest=[], strength=[], error="no candles",
        )

    # ---- Smart-money-flow strength -> adaptive multiplier ----
    clv = [0.0 if highs[i] == lows[i] else ((closes[i] - lows[i]) - (highs[i] - closes[i])) / (highs[i] - lows[i])
           for i in range(n)]
    raw = [clv[i] * volumes[i] for i in range(n)]
    num = _rolling_sum(raw, params.mf_len)
    den = _rolling_sum([abs(r) for r in raw], params.mf_len)
    mf = [0.0 if den[i] == 0 else num[i] / den[i] for i in range(n)]
    mf_sm = _ema(mf, params.mf_smooth) if params.mf_smooth > 1 else mf
    strength01 = [max(0.0, min(1.0, abs(v) ** params.mf_power)) if v is not None else 0.0 for v in mf_sm]
    mult = [params.min_mult + (params.max_mult - params.min_mult) * s for s in strength01]

    # ---- Baseline + adaptive bands ----
    basis_open = _basis_from(opens, params)
    basis_close = _basis_from(closes, params)
    atr = _atr(highs, lows, closes, params.atr_len)

    upper = [None] * n
    lower = [None] * n
    for i in range(n):
        if basis_close[i] is not None and atr[i] is not None:
            upper[i] = basis_close[i] + atr[i] * mult[i]
            lower[i] = basis_close[i] - atr[i] * mult[i]

    # ---- Regime / structure-break signal (stateful, matches Pine's
    # st.lastSignal carried across bars) ----
    regime = [0] * n
    switch_up = [False] * n
    switch_down = [False] * n
    last_signal = 0
    for i in range(n):
        if upper[i] is None or lower[i] is None or basis_close[i] is None:
            regime[i] = last_signal
            continue
        if last_signal == 0:
            last_signal = 1 if closes[i] >= basis_close[i] else -1
        prev_signal = last_signal
        long_cond = (
            i > 0 and upper[i - 1] is not None
            and closes[i - 1] <= upper[i - 1] and closes[i] > upper[i]
        )
        short_cond = (
            i > 0 and lower[i - 1] is not None
            and closes[i - 1] >= lower[i - 1] and closes[i] < lower[i]
        )
        if long_cond:
            last_signal = 1
        elif short_cond:
            last_signal = -1
        regime[i] = last_signal
        switch_up[i] = last_signal == 1 and prev_signal == -1
        switch_down[i] = last_signal == -1 and prev_signal == 1

    # ---- Retest signals (cooldown in bars, same as Pine's dotCooldown) ----
    bull_retest = [False] * n
    bear_retest = [False] * n
    last_bull_bar: Optional[int] = None
    last_bear_bar: Optional[int] = None
    for i in range(n):
        if basis_close[i] is None:
            continue
        bear_cond = regime[i] == -1 and highs[i] > basis_close[i]
        bull_cond = regime[i] == 1 and lows[i] < basis_close[i]
        bear_ok = bear_cond and (
            params.dot_cooldown == 0 or last_bear_bar is None or (i - last_bear_bar) >= params.dot_cooldown
        )
        bull_ok = bull_cond and (
            params.dot_cooldown == 0 or last_bull_bar is None or (i - last_bull_bar) >= params.dot_cooldown
        )
        if bear_ok:
            last_bear_bar = i
            bear_retest[i] = True
        if bull_ok:
            last_bull_bar = i
            bull_retest[i] = True

    # ---- Signed trend-strength gauge (-1..1), matches f_trendStrengthSigned ----
    raw_strength = [0.0] * n
    for i in range(n):
        if upper[i] is None or lower[i] is None or basis_close[i] is None:
            continue
        up_span = max(upper[i] - basis_close[i], params.tick_size)
        dn_span = max(basis_close[i] - lower[i], params.tick_size)
        raw_strength[i] = (
            (closes[i] - basis_close[i]) / up_span if regime[i] == 1
            else -(basis_close[i] - closes[i]) / dn_span
        )
    tanh_v = [_tanh(v * 1.5) for v in raw_strength]
    strength = _ema(tanh_v, 3)
    strength = [s if s is not None else tanh_v[i] for i, s in enumerate(strength)]

    result = StructureBreakResult(
        n=n, basis=basis_close, upper=upper, lower=lower, regime=regime,
        switch_up=switch_up, switch_down=switch_down,
        bull_retest=bull_retest, bear_retest=bear_retest, strength=strength,
        close=list(closes), open=list(opens),
        timestamps=list(timestamps) if timestamps else [],
    )

    last = n - 1
    result.last_close = closes[last]
    result.last_regime = regime[last]
    result.last_upper = upper[last]
    result.last_lower = lower[last]
    result.last_basis = basis_close[last]
    result.warm = upper[last] is not None
    if result.warm:
        result.last_strength_pct = round(abs(strength[last]) * 100.0, 1)
    if switch_up[last]:
        result.broke_this_bar = "up"
    elif switch_down[last]:
        result.broke_this_bar = "down"
    if bull_retest[last]:
        result.retest_this_bar = "bull"
    elif bear_retest[last]:
        result.retest_this_bar = "bear"

    # bars since the last regime flip, for "how fresh is this break" context
    for i in range(last, -1, -1):
        if switch_up[i] or switch_down[i]:
            result.bars_since_break = last - i
            break

    return result


# --------------------------------------------------------------------- #
# Live data fetch - same DhanClient path every real signal in this repo
# uses. Blocking (REST); call from a sync context or run_in_executor.
# --------------------------------------------------------------------- #
def _drop_forming_candle(data: dict, interval_minutes: int) -> tuple[list, list, list, list, list, list]:
    opens = list(data.get("open") or [])
    highs = list(data.get("high") or [])
    lows = list(data.get("low") or [])
    closes = list(data.get("close") or [])
    volumes = list(data.get("volume") or [])
    timestamps = list(data.get("timestamp") or [])
    if timestamps:
        last_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
        if datetime.now(IST) < last_start + timedelta(minutes=interval_minutes):
            opens, highs, lows, closes, volumes, timestamps = (
                opens[:-1], highs[:-1], lows[:-1], closes[:-1], volumes[:-1], timestamps[:-1]
            )
    return opens, highs, lows, closes, volumes, timestamps


def _ensure_authenticated() -> None:
    """Same hand-off pattern backtest_all_fno_breakout_signal_15day.py's
    _authenticate_avoiding_session_collision uses (this module deliberately
    doesn't duplicate that function's own name/behavior, just its
    reasoning): if HANDOFF_DHAN_ACCESS_TOKEN is set, validate that existing
    token in access_token mode instead of letting a lazy authenticate() run
    pin_totp, which would mint a brand-new Dhan session and kick out the
    live droplet bot's current one (see trading-skills' incidents/2026-09-
    21-local-backtest-dhan-session-collision.md). No-op once dhan_wrapper
    is already authenticated (checked first so this never re-authenticates
    on every timeframe call)."""
    from Options.dhan_client import dhan_wrapper
    import Options.config as ocfg
    if dhan_wrapper._client is not None:
        return
    handoff = os.environ.get("HANDOFF_DHAN_ACCESS_TOKEN")
    if handoff:
        ocfg.DHAN_AUTH_MODE = "access_token"
        ocfg.DHAN_ACCESS_TOKEN = handoff
    dhan_wrapper.authenticate()


_mcx_contract_cache: dict = {}   # symbol -> (date, security_id), day-cached like Swing/signals.py's own


def _underlying_reference(symbol: str, mcx: bool) -> tuple:
    """(security_id, exchange_segment, instrument_type) for `symbol`.
    Mirrors Swing/signals.py's own _underlying_reference exactly (same
    reasoning: an MCX commodity has no continuous NSE cash-segment
    series, so its regime reference is its current MCX futures contract
    instead) - duplicated rather than imported to keep structure_break.py
    usable standalone, outside the Swing package."""
    from Options.dhan_client import dhan_wrapper
    if not mcx:
        return dhan_wrapper._equity_security_id(symbol), "NSE_EQ", "EQUITY"
    today = datetime.now(IST).date()
    cached = _mcx_contract_cache.get(symbol)
    if not cached or cached[0] != today:
        contract = dhan_wrapper.get_mcx_futures_contract(symbol)
        _mcx_contract_cache[symbol] = (today, contract.security_id)
    return _mcx_contract_cache[symbol][1], "MCX_COMM", "FUTCOM"


def fetch_timeframe(symbol: str, timeframe: str, params: Optional[StructureBreakParams] = None,
                     mcx: bool = False, lookback_days_override: Optional[int] = None,
                     ws_candles_fn: Optional[Callable[[str, int], Optional[dict]]] = None) -> StructureBreakResult:
    """Fetches `symbol`'s candles for one timeframe ("5m"/"15m"/"1h"/"1d")
    and returns its StructureBreakResult. NSE equities by default; pass
    `mcx=True` for an MCX commodity (e.g. COPPER) - resolves its current
    futures contract instead of an equity security_id, same as Swing's
    own regime/Supertrend fetch does. `lookback_days_override` widens the
    default per-interval history window (useful for a backtest spanning
    more trading days than the default warm-up buffer comfortably covers -
    see backtest_swing_structure_break_mtf.py). Blocking REST call(s) via
    the shared dhan_wrapper - never call this from the WebSocket tick
    path.

    `ws_candles_fn` (added 24 Sep 2026, user request - "add WS feeds for
    COPPER should also use in structure_break.py... just switch from REST
    to WS (REST for fallback)") - an OPTIONAL (symbol, interval_minutes)
    -> candles-dict-or-None callable, tried BEFORE the REST fetch for any
    intraday (non-daily) timeframe. Deliberately a caller-injected hook,
    not an import of Swing/candle_feed.py's WS mechanism directly - this
    module stays usable standalone, outside the Swing package (see this
    module's own docstring), which owns the actual WS subscription/local-
    bar-reconstruction state; Swing/signals.py's own _fetch_one_structure_
    break_timeframe is the one live caller that supplies it. None (the
    default) preserves the exact prior REST-only behavior unchanged - the
    CLI (`main()`) and the backtest script never pass one. Falls straight
    through to the existing REST fetch below whenever the hook returns
    None or raises, OR whenever the candles it DOES return, once actually
    run through compute_structure_break, come back not-warm or errored -
    a bare bar-count check is NOT trusted as a proxy for "enough to warm
    the indicator" (a real regression from an earlier version of this
    fix: a thin freshly-subscribed WS series passed a naive count check,
    got used instead of REST's much deeper history, and came back not
    warm - silently worse than skipping the hook entirely). REST is
    always the fallback, per the same fail-open discipline every other
    hybrid fetch in this repo follows."""
    if timeframe not in TIMEFRAMES:
        return StructureBreakResult(n=0, basis=[], upper=[], lower=[], regime=[], switch_up=[],
                                     switch_down=[], bull_retest=[], bear_retest=[], strength=[],
                                     error=f"unknown timeframe {timeframe!r}, expected one of {list(TIMEFRAMES)}")
    from Options.dhan_client import dhan_wrapper

    params = params or StructureBreakParams()
    try:
        _ensure_authenticated()
        security_id, exchange_segment, instrument_type = _underlying_reference(symbol, mcx)
        interval = TIMEFRAMES[timeframe]
        if interval is None:  # daily
            now = datetime.now(IST)
            resp = dhan_wrapper.client.Dhan.historical_daily_data(
                security_id=security_id, exchange_segment=exchange_segment, instrument_type=instrument_type,
                from_date=(now - timedelta(days=_DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
                # yesterday - today's daily bar isn't closed yet, same
                # look-ahead-avoidance breakout_signal.py's _fetch_daily_sync uses.
                to_date=(now - timedelta(days=1)).strftime("%Y-%m-%d"),
            )
            data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
            opens = list(data.get("open") or [])
            highs = list(data.get("high") or [])
            lows = list(data.get("low") or [])
            closes = list(data.get("close") or [])
            volumes = list(data.get("volume") or [])
            timestamps = list(data.get("timestamp") or [])
        else:
            # ws_candles_fn's result is only TRUSTED once it's actually run
            # through compute_structure_break and reports warm=True/no
            # error - a bare bar-count check (e.g. ">= atr_len+1") is NOT
            # a reliable proxy for "enough to warm the indicator" (the
            # basis's own `length`, 34 by default, is what typically
            # gates `warm`, not atr_len 14) and a real regression was
            # caught exactly this way in testing: a freshly-subscribed
            # WS series with, say, 16 bars passed a naive ">=15" gate,
            # got used instead of REST's much deeper history, and came
            # back not-warm - silently WORSE than never having the WS
            # hook at all, since REST alone would have succeeded. This
            # computes the real indicator on the WS candles FIRST and
            # only returns that result if it's genuinely usable; any
            # other outcome falls straight through to the untouched REST
            # path below, exactly as if ws_candles_fn had returned None.
            if ws_candles_fn is not None:
                try:
                    ws_data = ws_candles_fn(symbol, interval)
                except Exception:  # noqa: BLE001
                    ws_data = None
                if ws_data and (ws_data.get("close") or []):
                    ws_o, ws_h, ws_l, ws_c, ws_v, ws_t = _drop_forming_candle(ws_data, interval)
                    if len(ws_c) >= params.atr_len + 1:
                        candidate = compute_structure_break(ws_o, ws_h, ws_l, ws_c, ws_v, ws_t, params)
                        if not candidate.error and candidate.warm:
                            return candidate
            # Retry once on an EMPTY-but-not-raised response, not just on a
            # raised exception - dhan_client.py's own _retry only retries
            # when the underlying call THROWS, but Dhan's documented
            # back-to-back-unpaced-calls failure mode is a soft "failure"
            # JSON body with no "data" key, which fetch_continuous_intraday
            # turns into a plain empty dict rather than an exception. Real
            # symptom this fixes: fetching 5m then immediately 15m then 1h
            # for the same symbol with no gap between calls - the 2nd/3rd
            # call would occasionally come back with 0 candles and no error
            # message at all (confirmed live, 22 Sep 2026, COPPER 15m).
            data = dhan_wrapper.fetch_continuous_intraday(
                security_id, exchange_segment, instrument_type, interval,
                lookback_days_override=lookback_days_override or _INTRADAY_LOOKBACK_DAYS[interval],
            )
            if not (data.get("close") or []):
                time.sleep(2.0)
                data = dhan_wrapper.fetch_continuous_intraday(
                    security_id, exchange_segment, instrument_type, interval,
                    lookback_days_override=lookback_days_override or _INTRADAY_LOOKBACK_DAYS[interval],
                )
            opens, highs, lows, closes, volumes, timestamps = _drop_forming_candle(data, interval)

        if len(closes) < params.atr_len + 1:
            return StructureBreakResult(n=0, basis=[], upper=[], lower=[], regime=[], switch_up=[],
                                         switch_down=[], bull_retest=[], bear_retest=[], strength=[],
                                         error=f"not enough {timeframe} candles ({len(closes)})")
        return compute_structure_break(opens, highs, lows, closes, volumes, timestamps, params)
    except Exception as exc:  # noqa: BLE001
        return StructureBreakResult(n=0, basis=[], upper=[], lower=[], regime=[], switch_up=[],
                                     switch_down=[], bull_retest=[], bear_retest=[], strength=[],
                                     error=str(exc))


def analyze_symbol(symbol: str, timeframes: tuple = ("5m", "15m", "1h", "1d"),
                    params: Optional[StructureBreakParams] = None) -> dict:
    """Runs fetch_timeframe for each requested timeframe independently - one
    timeframe's failure (thin history, a rate limit) doesn't take down the
    others. Returns {timeframe: StructureBreakResult}."""
    return {tf: fetch_timeframe(symbol, tf, params) for tf in timeframes}


def _format_report(symbol: str, results: dict) -> str:
    lines = [f"Structure break - {symbol} (BOSWaves Smart Money Flow Cloud port)"]
    for tf, r in results.items():
        if r.error:
            lines.append(f"  {tf:>3}: error - {r.error}")
            continue
        if not r.warm:
            lines.append(f"  {tf:>3}: not warm yet")
            continue
        regime_str = "BULLISH" if r.last_regime == 1 else "BEARISH" if r.last_regime == -1 else "flat"
        break_str = f", BROKE {r.broke_this_bar.upper()} this bar" if r.broke_this_bar else ""
        since_str = f", {r.bars_since_break} bars since last break" if r.bars_since_break is not None else ""
        retest_str = f", {r.retest_this_bar} retest this bar" if r.retest_this_bar else ""
        lines.append(
            f"  {tf:>3}: {regime_str:>7}  close={r.last_close:.2f}  "
            f"band=[{r.last_lower:.2f}, {r.last_upper:.2f}]  "
            f"strength={r.last_strength_pct}%{break_str}{since_str}{retest_str}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-timeframe structure-break check (BOSWaves Smart Money Flow Cloud port)")
    parser.add_argument("symbol", help="NSE trading symbol, e.g. RELIANCE")
    parser.add_argument("--timeframes", default="5m,15m,1h,1d", help="comma-separated subset of 5m,15m,1h,1d")
    args = parser.parse_args()
    timeframes = tuple(t.strip() for t in args.timeframes.split(",") if t.strip())
    results = analyze_symbol(args.symbol.upper(), timeframes)
    print(_format_report(args.symbol.upper(), results))


if __name__ == "__main__":
    main()
