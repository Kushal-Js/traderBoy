"""
Shadow-mode reversal-prevention filters (prototype, added 16 Sep 2026).

LOG-ONLY - this module NEVER blocks a real entry and NEVER raises into
its caller. It only records what each candidate filter WOULD have done
for every REAL entry Options/Futures/Luxury actually places, so the
results can be reviewed against what really happened (win/loss/exit
reason, already logged separately via trade_history's real_trades log)
before any of this is trusted to actually gate a live entry.

Built directly on these backtest rounds against real trades:
  - backtest_reversal_filters_sep15_16.py (37 trades, 2 days)
  - backtest_reversal_filters_15day.py (139-147 trades, 15 days, loaded
    programmatically from history/*_real_trades.log)
  - backtest_trend_strength_indicators.py / backtest_er_vs_volume_floor.py /
    backtest_er_daywise_with_swing_check.py (16 Sep 2026 - Kaufman
    Efficiency Ratio research, see ER's own note below)
All rounds are the evidence behind every threshold below - see those
files' own docstrings for the full methodology and per-trade findings.

Filters:
  - ADX(14) < 20: choppy/non-trending market (logged for reference only -
    excluded from the recommended combo, its benefit overlaps the
    cooldown filter almost entirely in both backtest rounds).
  - RSI(14) exhaustion alone (>70 for CE, <30 for PE): logged for
    reference only - backtested NET NEGATIVE both rounds (too blunt, also
    blocks genuine trend-continuation entries that simply have a strong
    RSI reading from being in a real trend).
  - Volume floor: entry-candle volume < 1.2x the 20-bar rolling average -
    the single strongest filter in both backtests (+Rs.7,131.50 on 37
    trades, +Rs.17,123.00 on 139 trades). IN the recommended combo.
  - Climax combo (the key finding): RSI-extreme AND volume > 20x average.
    An abnormal volume SPIKE at an already-extreme RSI reading is a blow-
    off/exhaustion signature, not a breakout - this is what actually
    explains the two worst single-trade disasters found in this
    analysis (PAYTM -Rs.4,603.75/22 seconds and YESBANK-Options
    -Rs.4,043/1 minute - both had RSI>77 AND volume 24-98x average).
    Only fired 4 times across the full 15-day/139-trade backtest, 2 of
    those 4 were exactly those two disasters - high precision, small
    sample. IN the recommended combo.
  - Cooldown: a SUPERTREND_EXIT for this exact (strategy, symbol,
    option_type) within the last COOLDOWN_MINUTES - directly targets the
    whipsaw pattern (flat-exit via SUPERTREND_EXIT, immediately re-enter
    the same direction, stop out). 10 minutes was the backtested sweet
    spot - shorter windows (e.g. 3 min) missed real whipsaws that re-
    entered 3-8+ minutes later; every window tested had zero forgone
    gains, but returns clearly diminish past 10-15 min. IN the
    recommended combo.
  - Kaufman Efficiency Ratio (ER, period 10) < 0.3: |net price
    displacement| / (sum of |bar-to-bar price changes|) over the last 10
    closes - unlike ADX/Choppiness Index, NOT built from ATR/true-range,
    so it doesn't just re-flag what Supertrend/cooldown already encode
    (confirmed: ADX(14)<20 overlaps almost entirely with cooldown, ER
    does not). Backtested net POSITIVE (+Rs.10,925.75 alone,
    +Rs.5,561.25 INCREMENTAL on top of the already-live volume floor,
    143 trades/15 days) but the evidence is thin and lumpy, not yet
    trustworthy enough to gate anything: 85% of that incremental benefit
    came from a single day (11 Sep - three different symbols/asset-
    classes, MCX/Futures/Options, all with healthy-to-high volume but
    ER<0.15, all genuine losers) while two OTHER days had ER blocking
    real winners for a net cost (-Rs.390 on 10 Sep, -Rs.980 on 15 Sep) -
    contrast with volume floor's own evidence, whose best single day was
    only 32% of its 15-day total, a much more evenly-distributed
    (trustworthy) signal at the same stage. LOGGED FOR REFERENCE ONLY -
    excluded from the recommended combo until more days of shadow data
    confirm the 11 Sep cluster wasn't a fluke (target: re-review at EOD
    17 Sep 2026). Threshold intentionally left at the conservative 0.3
    (not the in-sample-best 0.45 found while backtesting - that number
    was optimized on the same data it was scored against, see
    backtest_trend_strength_indicators.py's own overfitting caveat).

RECOMMENDED_COMBO_BLOCKS = volume floor OR climax combo OR cooldown.
This is what's reported as the headline "would this have been blocked"
verdict; ADX/RSI-alone/ER are logged purely for comparison.

Evaluated once per REAL entry (hooked into each package's own
PositionStore.add_position, right alongside record_opened_position - see
Options/Futures/Luxury's position_store.py) and once per real
SUPERTREND_EXIT (hooked into close_position). The one added REST call
per real entry (fetch_continuous_intraday) is the same cost class as the
several other entry-time-only calls already on that path
(get_atm_option/get_option_ltp/place_market_order) - it fires once per
real entry, never per monitor-loop tick, so it does not add the kind of
per-tick rate-limit pressure that caused K01 to be kept disabled.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from Options.dhan_client import IST, dhan_wrapper
from trade_history import append_jsonl

logger = logging.getLogger("reversal_filters")

SHADOW_LOG_NAME = "reversal_filter_shadow"

ADX_MIN = 20.0
RSI_OVERBOUGHT, RSI_OVERSOLD = 70.0, 30.0
VOL_RATIO_MIN = 1.2
SPIKE_RATIO = 20.0
COOLDOWN_MINUTES = 10
ER_PERIOD = 10
ER_THRESHOLD = 0.3

# In-memory only, per-process - (strategy, symbol, option_type) -> last
# SUPERTREND_EXIT time. Resets on restart, same as every other in-process
# cache in this codebase (dhan_client's own Supertrend/EMA caches included)
# - acceptable for a shadow/logging-only prototype; a restart just means a
# short window right after where the cooldown filter has nothing to look
# back on yet, exactly like the live Supertrend signal's own cold start.
_last_supertrend_exit: dict[tuple[str, str, str], datetime] = {}


def record_supertrend_exit(strategy: str, symbol: str, option_type: str) -> None:
    """Call synchronously from close_position (while its lock is already
    held) whenever reason == "SUPERTREND_EXIT" - trivial in-memory write,
    no I/O, safe to call without a lock/thread concern of its own."""
    try:
        _last_supertrend_exit[(strategy, symbol, option_type)] = datetime.now()
    except Exception:  # noqa: BLE001
        logger.exception("record_supertrend_exit failed for %s %s %s - shadow cooldown filter "
                          "just won't fire for this one, no other effect", strategy, symbol, option_type)


# --------------------------------------------------------------------- #
# Indicators - identical formulas to the two backtest scripts (kept as a
# third copy here rather than importing from either backtest file, same
# per-package independence convention this codebase already uses for
# _compute_rsi/_compute_atr across K01/IndexScalping/CopperOptions).
# --------------------------------------------------------------------- #
def _compute_rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    n = len(closes)
    rsi: list[Optional[float]] = [None] * n
    if n < period + 1:
        return rsi
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        gain, loss = gains[i - 1], losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return rsi


def _compute_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[Optional[float]]:
    n = len(closes)
    adx: list[Optional[float]] = [None] * n
    if n < period * 2 + 1:
        return adx
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    smoothed_tr = sum(tr[1:period + 1])
    smoothed_plus_dm = sum(plus_dm[1:period + 1])
    smoothed_minus_dm = sum(minus_dm[1:period + 1])
    dx_values: list[Optional[float]] = [None] * n

    def _di_dx(s_tr: float, s_pdm: float, s_mdm: float) -> Optional[float]:
        if s_tr == 0:
            return None
        plus_di = 100 * s_pdm / s_tr
        minus_di = 100 * s_mdm / s_tr
        if plus_di + minus_di == 0:
            return 0.0
        return 100 * abs(plus_di - minus_di) / (plus_di + minus_di)

    dx_values[period] = _di_dx(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm)
    for i in range(period + 1, n):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm[i]
        dx_values[i] = _di_dx(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm)

    first_dx_idx = period
    last_seed_idx = min(first_dx_idx + period, n) - 1
    seed_dx = [d for d in dx_values[first_dx_idx:last_seed_idx + 1] if d is not None]
    if len(seed_dx) < period:
        return adx
    adx[last_seed_idx] = sum(seed_dx) / len(seed_dx)
    for i in range(last_seed_idx + 1, n):
        if dx_values[i] is None or adx[i - 1] is None:
            continue
        adx[i] = (adx[i - 1] * (period - 1) + dx_values[i]) / period
    return adx


def _volume_ratio_at(volumes: list[float], idx: int, lookback: int = 20) -> Optional[float]:
    if idx < lookback:
        return None
    window = volumes[idx - lookback:idx]
    avg = sum(window) / len(window) if window else 0.0
    if avg == 0:
        return None
    return volumes[idx] / avg


def _efficiency_ratio_at(closes: list[float], idx: int, period: int = ER_PERIOD) -> Optional[float]:
    """Kaufman Efficiency Ratio: |net displacement| / (sum of |bar-to-bar
    moves|) over the last `period` closes ending at idx. 1.0 = every bar
    contributed to net direction (efficient/trending), 0.0 = pure noise
    (lots of motion, no net progress). Unlike ADX/Choppiness Index, this
    is NOT built from ATR/true-range - see this module's own docstring
    for why that's exactly what makes it worth logging separately."""
    if idx < period:
        return None
    net_change = abs(closes[idx] - closes[idx - period])
    path_sum = sum(abs(closes[j] - closes[j - 1]) for j in range(idx - period + 1, idx + 1))
    if path_sum <= 0:
        return None
    return net_change / path_sum


def _fetch_raw_candles_sync(symbol: str, min_bars: int) -> Optional[dict]:
    """Shared blocking fetch - 7 days of the underlying's own 5-min
    candles (NSE_EQ). Extracted from _fetch_indicators_sync (18 Sep 2026)
    so the ribbon-ranking path below can reuse the identical fetch/auth-
    guard instead of duplicating it a second time. Returns None (never
    raises) whenever the real answer isn't available - not yet
    authenticated, fetch failure, or fewer than `min_bars` candles of
    history - callers treat that as "skip this row."

    CRITICAL: dhan_wrapper.client is a LAZY property that calls
    self.authenticate() (a real, live Dhan login) the first time anything
    touches it if dhan_wrapper._client is still None. In production this
    is always already set by option_main.lifespan's own authenticate()
    call at startup, long before any real entry can happen - a no-op
    check here. In a test process, NOTHING has authenticated, so without
    this guard every test that calls add_position() would trigger a real
    login attempt from a background thread (found live during this
    module's own rollout - a real incident, not theoretical: it hung the
    test suite for minutes and would have made genuine Dhan API calls
    from unit tests, exactly the class of accidental-real-call risk this
    codebase's test suite has had incidents from before). Checking the
    private attribute directly (never the `client` property) is what
    avoids triggering the lazy auth just to check whether it already
    happened."""
    if dhan_wrapper._client is None:
        return None
    try:
        # Deliberately NOT dhan_wrapper.fetch_continuous_intraday - that
        # helper bakes in _retry (2 retries, 1.5s sleep between each), the
        # right choice for a signal the live strategy actually needs, but
        # wrong here: this shadow path is purely diagnostic, runs on the
        # SAME shared executor thread pool real order-placement and exit-
        # monitoring calls depend on (EXECUTOR_MAX_WORKERS=5 on the
        # droplet), and a slow/failing Dhan call has no business holding
        # one of those 5 workers for up to ~4.5 extra seconds on a retry
        # loop just to produce a log line. One attempt, fail fast, skip
        # this one row - exactly the K01-style shared-executor-contention
        # risk this codebase has already been deliberately careful about.
        security_id = dhan_wrapper._equity_security_id(symbol)
        now_ist = datetime.now(IST)
        from_date = (now_ist - timedelta(days=7)).strftime("%Y-%m-%d")
        to_date = now_ist.strftime("%Y-%m-%d")
        resp = dhan_wrapper.client.Dhan.intraday_minute_data(
            security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
            from_date=from_date, to_date=to_date, interval=5,
        )
        data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
        highs, lows, closes, volumes = (data.get("high") or []), (data.get("low") or []), (data.get("close") or []), (data.get("volume") or [])
        if len(closes) < min_bars:
            return None
        return {"high": highs, "low": lows, "close": closes, "volume": volumes}
    except Exception:  # noqa: BLE001
        logger.exception("%s: candle fetch failed - skipping, no other effect", symbol)
        return None


def _fetch_indicators_sync(symbol: str) -> Optional[dict]:
    """RSI/ADX/VolRatio/ER as of the last closed 5-min bar. Needs at least
    41 candles (idx>=40) of history - a much lower bar than ribbon-ranking's
    own 115-bar requirement, since RSI/ADX only need ~14-20 bars to warm up.
    See _fetch_raw_candles_sync's own docstring for the fetch/auth-guard
    details this reuses."""
    raw = _fetch_raw_candles_sync(symbol, min_bars=41)
    if raw is None:
        return None
    try:
        closes, highs, lows, volumes = raw["close"], raw["high"], raw["low"], raw["volume"]
        idx = len(closes) - 1
        return {
            "rsi": _compute_rsi(closes)[idx],
            "adx": _compute_adx(highs, lows, closes)[idx],
            "vol_ratio": _volume_ratio_at(volumes, idx),
            "er": _efficiency_ratio_at(closes, idx),
        }
    except Exception:  # noqa: BLE001
        logger.exception("%s: indicator computation failed - shadow logging skips this one row, no other effect", symbol)
        return None


def _evaluate_and_log_sync(strategy: str, symbol: str, option_type: str, entry_price: float, order_id: str) -> None:
    """Blocking - must be called via run_in_executor, never directly from
    async code. Never raises - any failure here just means this one
    entry has no shadow-filter row logged, the real entry itself is
    always already fully placed and unaffected by the time this runs."""
    indicators = _fetch_indicators_sync(symbol)
    if indicators is None:
        logger.info("%s %s: shadow filters skipped - not authenticated, fetch failed, or insufficient history",
                    strategy, symbol)
        return
    try:
        rsi, adx, vol_ratio, er = indicators["rsi"], indicators["adx"], indicators["vol_ratio"], indicators["er"]

        rsi_extreme = rsi is not None and (rsi > RSI_OVERBOUGHT if option_type == "CE" else rsi < RSI_OVERSOLD)
        adx_blocks = adx is not None and adx < ADX_MIN
        volume_blocks = vol_ratio is not None and vol_ratio < VOL_RATIO_MIN
        climax_combo_blocks = rsi_extreme and vol_ratio is not None and vol_ratio > SPIKE_RATIO
        last_exit = _last_supertrend_exit.get((strategy, symbol, option_type))
        cooldown_blocks = last_exit is not None and (datetime.now() - last_exit).total_seconds() <= COOLDOWN_MINUTES * 60
        er_blocks = er is not None and er < ER_THRESHOLD

        recommended_combo_blocks = volume_blocks or climax_combo_blocks or cooldown_blocks

        record = {
            "strategy": strategy, "symbol": symbol, "option_type": option_type,
            "entry_price": entry_price, "order_id": order_id,
            "rsi": round(rsi, 2) if rsi is not None else None,
            "adx": round(adx, 2) if adx is not None else None,
            "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "er": round(er, 3) if er is not None else None,
            "adx_blocks": adx_blocks, "rsi_extreme_alone_blocks": rsi_extreme, "volume_blocks": volume_blocks,
            "climax_combo_blocks": climax_combo_blocks, "cooldown_blocks": cooldown_blocks, "er_blocks": er_blocks,
            "recommended_combo_blocks": recommended_combo_blocks,
            "entry_time": datetime.now().isoformat(),
        }
        append_jsonl(SHADOW_LOG_NAME, record)
        logger.info(
            "%s %s %s: shadow filters logged - RSI=%s ADX=%s VolRatio=%s ER=%s -> recommended_combo_blocks=%s "
            "(volume=%s climax=%s cooldown=%s er=%s [reference only, not yet in combo])",
            strategy, symbol, option_type, record["rsi"], record["adx"], record["vol_ratio"], record["er"],
            recommended_combo_blocks, volume_blocks, climax_combo_blocks, cooldown_blocks, er_blocks,
        )
    except Exception:  # noqa: BLE001
        logger.exception("%s %s: shadow filter evaluation failed - real entry unaffected, this is logging-only",
                          strategy, symbol)


async def evaluate_and_log(strategy: str, symbol: str, option_type: str, entry_price: float, order_id: str) -> None:
    """Async wrapper - pass this (already-called, i.e. the coroutine
    object) to trade_history.fire_and_forget from add_position, exactly
    like record_opened_position already is."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _evaluate_and_log_sync, strategy, symbol, option_type, entry_price, order_id)


# --------------------------------------------------------------------- #
# Alert-candidate shadow logging (added 18 Sep 2026, user request after
# discussing whether a volume-weighted ranking might pick meaningfully
# different (and better-performing) stocks than the current pure
# %-change ranking - rank_and_pick_top_stocks() already fetches every
# candidate's day-change%, ranks them, then DISCARDS every candidate
# outside the selected top_n/bottom_n slice without a trace. That means
# there was no way to retroactively compare "what got picked" against
# "what got passed over" - the exact gap that made looking into today's
# real alerts impossible (confirmed live: a fresh Dhan login for a
# standalone analysis script fails outright while the bot's own session
# is active, so this can't be reconstructed after the fact either).
#
# This logs EVERY candidate in a multi-stock alert - selected or not -
# with the same RSI/ADX/VolRatio/ER already computed for real entries,
# plus the day-change% the CURRENT ranking actually uses, so a genuine
# "would a different ranking have done better" comparison becomes
# possible after enough days accumulate - the same 15-day shadow-mode
# bar every other filter in this file was held to before being trusted.
# Deliberately logging-only: never changes rank_and_pick_top_stocks'
# own return value or the entry flow that follows it.
# --------------------------------------------------------------------- #
ALERT_CANDIDATE_SHADOW_LOG_NAME = "alert_candidate_shadow"
CANDIDATE_FETCH_PACING_SECONDS = 0.35  # matches rank_and_pick_top_stocks' own pacing


def _log_alert_candidates_sync(
    strategy: str, scan_name: Optional[str], option_type: str,
    candidates: list[str], selected: set[str],
) -> None:
    """Blocking - must be called via run_in_executor. Paced the same way
    rank_and_pick_top_stocks already paces its own day-change% fetch
    (0.35s between candidates) - this doubles the REST calls a large
    multi-stock alert makes (one pass for ranking, one here for
    indicators), so pacing matters even more here to stay clear of
    Dhan's own rate limit and the shared executor pool real order-
    placement/monitoring depends on. Never raises - a failure logs
    nothing for that one candidate, no other effect."""
    if dhan_wrapper._client is None:
        return
    for i, symbol in enumerate(candidates):
        if i > 0:
            time.sleep(CANDIDATE_FETCH_PACING_SECONDS)
        try:
            day_change_pct = dhan_wrapper.get_day_change_pct(symbol)
        except Exception:  # noqa: BLE001
            day_change_pct = None
        indicators = _fetch_indicators_sync(symbol)
        record = {
            "strategy": strategy, "scan_name": scan_name, "symbol": symbol, "option_type": option_type,
            "was_selected": symbol in selected, "day_change_pct": day_change_pct,
            "rsi": None, "adx": None, "vol_ratio": None, "er": None,
            "logged_at": datetime.now().isoformat(),
        }
        if indicators is not None:
            rsi, adx, vol_ratio, er = indicators["rsi"], indicators["adx"], indicators["vol_ratio"], indicators["er"]
            record["rsi"] = round(rsi, 2) if rsi is not None else None
            record["adx"] = round(adx, 2) if adx is not None else None
            record["vol_ratio"] = round(vol_ratio, 2) if vol_ratio is not None else None
            record["er"] = round(er, 3) if er is not None else None
        try:
            append_jsonl(ALERT_CANDIDATE_SHADOW_LOG_NAME, record)
        except Exception:  # noqa: BLE001
            logger.exception("%s %s: could not log alert-candidate shadow row - no other effect", strategy, symbol)


async def log_alert_candidates(
    strategy: str, scan_name: Optional[str], option_type: str,
    candidates: list[str], selected: list[str],
) -> None:
    """Async wrapper - call via asyncio.create_task (fire-and-forget,
    never awaited) right after rank_and_pick_top_stocks returns, from
    each package's own webhook handler:
        asyncio.create_task(reversal_filters.log_alert_candidates(
            "Options", payload.scan_name, option_type, stocks, [s for s, _ in ranked]))
    Only worth calling when len(candidates) > 1 - a single-candidate
    alert has no ranking DECISION to compare against, so callers should
    skip it entirely rather than pay a fetch for nothing."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, _log_alert_candidates_sync, strategy, scan_name, option_type, candidates, set(selected),
    )


# --------------------------------------------------------------------- #
# Live gate (promoted from shadow-mode, 16 Sep 2026) - unlike
# evaluate_and_log above (diagnostic, called AFTER a real entry, never
# blocks anything), this is called BEFORE placing a real order and its
# return value actually decides whether the entry proceeds. Options-only
# (NSE equity underlying) - Swing's own MCX-aware volume floor reuses its
# own already-fetched SupertrendState.volume_ratio instead (see
# Swing/signals.py) rather than duplicating this NSE-specific fetch.
# --------------------------------------------------------------------- #
def check_volume_floor_sync(symbol: str, min_ratio: float) -> tuple[bool, Optional[float]]:
    """Blocking - must be called via run_in_executor. Returns (passes,
    vol_ratio) - passes=True means the entry should proceed. Fails OPEN
    (passes=True) whenever the real answer isn't confidently known (not
    yet authenticated, fetch failure, or insufficient candle history) -
    a diagnostic check's own failure or cold-start must never itself
    cause a missed entry; only a CONFIRMED thin candle actually blocks.
    Same single-attempt, no-retry, no-lazy-auth-trigger discipline as
    _evaluate_and_log_sync above, for the identical shared-executor-
    thread-pool-contention reason - see that function's own docstring."""
    if dhan_wrapper._client is None:
        return True, None
    try:
        security_id = dhan_wrapper._equity_security_id(symbol)
        now_ist = datetime.now(IST)
        from_date = (now_ist - timedelta(days=7)).strftime("%Y-%m-%d")
        to_date = now_ist.strftime("%Y-%m-%d")
        resp = dhan_wrapper.client.Dhan.intraday_minute_data(
            security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
            from_date=from_date, to_date=to_date, interval=5,
        )
        data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
        volumes = data.get("volume") or []
        if not volumes:
            return True, None
        vol_ratio = _volume_ratio_at(volumes, len(volumes) - 1)
        if vol_ratio is None:
            return True, None
        return vol_ratio >= min_ratio, vol_ratio
    except Exception:  # noqa: BLE001
        logger.exception("%s: volume floor gate check failed - failing OPEN (entry proceeds unaffected)", symbol)
        return True, None


async def check_volume_floor(symbol: str, min_ratio: float) -> tuple[bool, Optional[float]]:
    """Async wrapper for check_volume_floor_sync - call this from an
    entry path, e.g.:
        passes, vol_ratio = await reversal_filters.check_volume_floor(symbol, config.VOLUME_FLOOR_RATIO_MIN)
        if not passes: ... skip the entry ..."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, check_volume_floor_sync, symbol, min_ratio)


# --------------------------------------------------------------------- #
# Live loss-re-entry trend-strength gate (added 18 Sep 2026) - real
# incident: ATHERENERG 29 SEP 1540 PUT (Options, 17 Sep 2026) whipsawed
# out via SUPERTREND_EXIT just 95 seconds after entry (ADX=13.24, well
# below ADX_MIN - a genuinely choppy/non-trending reading, per this same
# module's own shadow-mode diagnostic already computed and logged at that
# exact entry) for a Rs 1,537.50 loss, then a same-day re-entry lost
# another Rs 2,325.00. Unlike check_volume_floor above (checked on EVERY
# entry), this is only meant to gate a RE-entry on a symbol that has
# already lost money for this strategy today - see each package's own
# _process_one_entry for where trade_history.loss_count_today decides
# whether to even call this.
#
# Passes if EITHER ADX or ER looks like a genuine trend (not both
# required) - reuses this module's own already-backtested ADX_MIN/
# ER_THRESHOLD rather than new, untested numbers. ADX alone was found to
# mostly overlap the cooldown filter's own benefit in the 15-day backtest
# (see this module's own docstring), and ER's evidence was "thin and
# lumpy, not yet trustworthy enough to gate anything" as of the 16 Sep
# review - but requiring only ONE of the two to pass, specifically for a
# symbol that has ALREADY cost real money today, is a materially
# different, more conservative bar than gating every single entry on
# either indicator alone.
def check_trend_strength_sync(symbol: str) -> tuple[bool, Optional[float], Optional[float]]:
    """Blocking - must be called via run_in_executor. Returns (passes,
    adx, er). Fails OPEN (passes=True) whenever the real answer isn't
    confidently known (not yet authenticated, fetch failure, or
    insufficient candle history) - same philosophy as check_volume_
    floor_sync: a diagnostic check's own failure or cold-start must never
    itself cause a missed entry, only a CONFIRMED choppy reading does."""
    if dhan_wrapper._client is None:
        return True, None, None
    try:
        security_id = dhan_wrapper._equity_security_id(symbol)
        now_ist = datetime.now(IST)
        from_date = (now_ist - timedelta(days=7)).strftime("%Y-%m-%d")
        to_date = now_ist.strftime("%Y-%m-%d")
        resp = dhan_wrapper.client.Dhan.intraday_minute_data(
            security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
            from_date=from_date, to_date=to_date, interval=5,
        )
        data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
        highs, lows, closes = (data.get("high") or []), (data.get("low") or []), (data.get("close") or [])
        idx = len(closes) - 1
        if idx < 40:
            return True, None, None
        adx = _compute_adx(highs, lows, closes)[idx]
        er = _efficiency_ratio_at(closes, idx)
        if adx is None and er is None:
            return True, None, None
        adx_ok = adx is not None and adx >= ADX_MIN
        er_ok = er is not None and er >= ER_THRESHOLD
        return (adx_ok or er_ok), adx, er
    except Exception:  # noqa: BLE001
        logger.exception("%s: trend-strength re-entry check failed - failing OPEN (entry proceeds unaffected)", symbol)
        return True, None, None


async def check_trend_strength(symbol: str) -> tuple[bool, Optional[float], Optional[float]]:
    """Async wrapper for check_trend_strength_sync - call this from a
    re-entry path once a symbol has already lost money today, e.g.:
        passes, adx, er = await reversal_filters.check_trend_strength(symbol)
        if not passes: ... skip the re-entry ..."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, check_trend_strength_sync, symbol)


# --------------------------------------------------------------------- #
# Live option-liquidity entry gate (added 18 Sep 2026) - real incident:
# SOLARINDS 29 SEP 18750 PUT (Options, 17 Sep 2026) lost Rs 3,995.00 via
# MAX_LOSS_HIT - the underlying moved only ~0.32% (18840 -> 18780) while
# the option premium collapsed ~15.5% (580 peak -> 490.05 exit), a pure
# option-side liquidity/gap event. Investigated at the time and concluded
# "not preventable by current filters" - every entry-time signal this
# codebase has (volume floor, ADX, ER via the shadow filters above)
# measures the UNDERLYING's own price/volume, never the OPTION's. This
# gate closes that gap by reusing dhan_wrapper.refresh_liquidity_signal/
# get_cached_illiquid - the option's OWN "last N completed 1-min bars all
# zero volume" check, already built, tested, and live for EXIT decisions
# (see that function's own docstring for the CHOLAFIN incident it exists
# for) - but never previously checked before an entry. Whether it would
# have caught SOLARINDS specifically is unverifiable after the fact (no
# option-level volume was captured at that exact entry moment), but this
# is the only real, targeted signal available for "is the option ITSELF
# already thin," as opposed to the underlying.
def check_option_liquidity_sync(option_trading_symbol: str) -> tuple[bool, Optional[bool]]:
    """Blocking - must be called via run_in_executor. Returns (passes,
    is_illiquid). Fails OPEN (passes=True) whenever the real answer isn't
    confidently known (not yet authenticated, fetch failure, or not
    enough completed bars yet) - same philosophy as check_volume_floor_
    sync/check_trend_strength_sync: a diagnostic check's own failure or
    cold-start must never itself cause a missed entry, only a CONFIRMED
    zero-volume streak does. refresh_liquidity_signal has no pre-existing
    cache entry for a symbol that isn't an open position yet, so this
    call always performs a fresh fetch rather than being throttled by a
    stale cached value."""
    if dhan_wrapper._client is None:
        return True, None
    try:
        dhan_wrapper.refresh_liquidity_signal(option_trading_symbol)
        is_illiquid = dhan_wrapper.get_cached_illiquid(option_trading_symbol)
        if is_illiquid is None:
            return True, None
        return (not is_illiquid), is_illiquid
    except Exception:  # noqa: BLE001
        logger.exception("%s: option-liquidity entry check failed - failing OPEN (entry proceeds unaffected)",
                          option_trading_symbol)
        return True, None


async def check_option_liquidity(option_trading_symbol: str) -> tuple[bool, Optional[bool]]:
    """Async wrapper for check_option_liquidity_sync - call this right
    after resolving the real contract to enter (e.g. get_atm_option),
    before placing the real order:
        passes, is_illiquid = await reversal_filters.check_option_liquidity(atm.trading_symbol)
        if not passes: ... skip the entry ..."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, check_option_liquidity_sync, option_trading_symbol)


# --------------------------------------------------------------------- #
# MA-ribbon-expansion ranking + switch-shadow monitoring (added 18 Sep
# 2026, see ribbon_score.py's own module docstring for what the score
# measures and design rationale - built after backtesting confirmed a
# real improvement over the day-change%-only ranking on 2026-09-17's real
# alerts). `import ribbon_score` is done LOCALLY inside each function
# below rather than at module level: ribbon_score.py itself imports
# _compute_adx/_compute_rsi/_efficiency_ratio_at FROM this module, so a
# top-level import the other way would be a circular import. Same local-
# import-to-avoid-a-cycle pattern already used in this codebase - see
# Luxury/luxury_main.py's manual_square_off endpoint.
# --------------------------------------------------------------------- #
RIBBON_SWITCH_SHADOW_LOG_NAME = "ribbon_switch_shadow"

# score_ribbon_expansion needs its slowest EMA (100) fully warm plus a
# lookback margin (ribbon_score.DEFAULT_LOOKBACK_BARS) - a materially
# higher bar than _fetch_indicators_sync's own 41-bar minimum, since RSI/
# ADX warm up far faster than a 100-period EMA.
def _ribbon_min_bars() -> int:
    import ribbon_score
    return 100 + ribbon_score.DEFAULT_LOOKBACK_BARS


def _rank_by_ribbon_score_sync(stock_symbols: list[str], top_n: int, min_score: Optional[float], score_fn_name: str) -> list[tuple[str, float]]:
    """Shared blocking worker behind rank_by_ribbon_expansion_sync (CE) and
    rank_by_ribbon_breakdown_sync (PE) - identical fetch/pacing/sort logic,
    parameterized only by which ribbon_score function actually scores each
    candidate. `score_fn_name` is a string (not the function object) so
    this can still do the module-level local import once and look the
    function up off it - see this section's own header note on why
    `ribbon_score` is imported locally rather than at module scope."""
    import ribbon_score  # local import - see this section's own header note

    score_fn = getattr(ribbon_score, score_fn_name)
    if min_score is None:
        min_score = ribbon_score.MIN_ENTRY_SCORE

    scored: list[tuple[str, float]] = []
    min_bars = _ribbon_min_bars()
    for i, symbol in enumerate(stock_symbols):
        if i > 0:
            time.sleep(CANDIDATE_FETCH_PACING_SECONDS)
        raw = _fetch_raw_candles_sync(symbol, min_bars=min_bars)
        if raw is None:
            continue
        try:
            score = score_fn(raw["high"], raw["low"], raw["close"], symbol=symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s: ribbon score computation failed - excluded from this ranking", symbol)
            continue
        if score is not None and score.total >= min_score:
            scored.append((symbol, score.total))

    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:top_n] if top_n > 0 else []


def rank_by_ribbon_expansion_sync(
    stock_symbols: list[str], top_n: int, min_score: Optional[float] = None,
) -> list[tuple[str, float]]:
    """Blocking - CE/bullish-alert drop-in alternative to trading_engine.
    rank_and_pick_top_stocks' day-change%-only ranking. Returns [(symbol,
    ribbon_score_total), ...], same shape so it's a drop-in swap at call
    sites - EXCEPT it also enforces a real minimum-quality bar (min_score,
    defaults to ribbon_score.MIN_ENTRY_SCORE) the day-change% ranking
    never had: a candidate scoring below this is simply never returned,
    even if that means returning fewer than top_n symbols (or none at all
    on a genuinely weak alert) - callers' existing "if not ranked:
    no_action" handling already covers this. See rank_by_ribbon_breakdown_
    sync for the PE/bearish counterpart (added 18 Sep 2026)."""
    return _rank_by_ribbon_score_sync(stock_symbols, top_n, min_score, "score_ribbon_expansion")


async def rank_by_ribbon_expansion(
    stock_symbols: list[str], top_n: int, min_score: Optional[float] = None,
) -> list[tuple[str, float]]:
    """Async wrapper - see rank_by_ribbon_expansion_sync's own docstring."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, rank_by_ribbon_expansion_sync, stock_symbols, top_n, min_score)


def rank_by_ribbon_breakdown_sync(
    stock_symbols: list[str], top_n: int, min_score: Optional[float] = None,
) -> list[tuple[str, float]]:
    """PE/bearish mirror of rank_by_ribbon_expansion_sync (added 18 Sep
    2026, user request to extend ranking-only to PE alerts) - scores each
    candidate with ribbon_score.score_ribbon_breakdown instead. See that
    function's own docstring for the important caveat: this direction has
    NOT been backtested against real data, unlike the CE side."""
    return _rank_by_ribbon_score_sync(stock_symbols, top_n, min_score, "score_ribbon_breakdown")


async def rank_by_ribbon_breakdown(
    stock_symbols: list[str], top_n: int, min_score: Optional[float] = None,
) -> list[tuple[str, float]]:
    """Async wrapper - see rank_by_ribbon_breakdown_sync's own docstring."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, rank_by_ribbon_breakdown_sync, stock_symbols, top_n, min_score)


def _log_switch_shadow_sync(
    strategy: str, option_type: str, candidate_symbol: str, candidate_total: float,
    open_positions: list[dict],
) -> None:
    """Blocking - must be called via run_in_executor. SHADOW ONLY: scores
    the strategy's real currently-open positions' CURRENT trend health and
    asks ribbon_score.decide_switch() what it would recommend for a
    candidate that just won the (real, ribbon-based) ranking - then just
    logs the answer. NEVER calls any real exit/entry code and never
    affects the real webhook response either way.

    `open_positions`: [{"symbol": underlying_symbol, "opened_at": iso str
    from position_store.snapshot()}, ...] for this strategy/option_type's
    real live positions right now.

    Added 18 Sep 2026 per user request: ranking-only is going live behind
    config.RIBBON_RANKING_ENABLED, but the SWITCHING half of what was
    backtested (actively exiting a held position for a better one) stays
    shadow-only pending a full day's worth of real switch-decision data to
    evaluate - see ribbon_score.decide_switch's own docstring for exactly
    what real live wiring would still need (a new exit_reason, real-time
    re-scoring of open positions in the monitor loop, and the same
    explicit sign-off every other live-trading-behavior change here has
    gone through)."""
    import ribbon_score  # local import - see this section's own header note

    if dhan_wrapper._client is None or not open_positions:
        return
    # CE positions score on the bullish (expansion) ribbon; PE on the
    # bearish (breakdown) one - see ribbon_score.score_ribbon_breakdown's
    # own docstring for its own, still-unbacktested caveat.
    score_fn = ribbon_score.score_ribbon_expansion if option_type == "CE" else ribbon_score.score_ribbon_breakdown
    try:
        min_bars = _ribbon_min_bars()
        scored_open = []
        for p in open_positions:
            raw = _fetch_raw_candles_sync(p["symbol"], min_bars=min_bars)
            if raw is None:
                continue
            score = score_fn(raw["high"], raw["low"], raw["close"], symbol=p["symbol"])
            if score is None:
                continue
            opened_at = datetime.fromisoformat(p["opened_at"])
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=IST)
            held_minutes = (datetime.now(IST) - opened_at.astimezone(IST)).total_seconds() / 60.0
            scored_open.append(ribbon_score.OpenPositionForSwitch(p["symbol"], score, held_minutes))

        if not scored_open:
            return

        # Only .total is read off the candidate side by decide_switch - the
        # other fields are informational-only for a candidate, so a dummy
        # RibbonScore carrying just the real total is sufficient here.
        candidate_score = ribbon_score.RibbonScore(
            total=candidate_total, compression=0.0, trigger=0.0, fanout=0.0,
            pullback=0.0, confirmation=0.0, bars_since_trigger=None,
        )
        decision = ribbon_score.decide_switch(
            scored_open, candidate_symbol, candidate_score, max_positions=len(open_positions),
        )
        record = {
            "strategy": strategy, "option_type": option_type, "candidate_symbol": candidate_symbol,
            "candidate_score": round(candidate_total, 2),
            "open_positions": [{"symbol": p.symbol, "health": round(ribbon_score.held_position_health(p.score), 2),
                                 "held_minutes": round(p.held_minutes, 1)} for p in scored_open],
            "would_switch": decision.should_switch, "would_exit_symbol": decision.exit_symbol,
            "reason": decision.reason, "logged_at": datetime.now().isoformat(),
        }
        append_jsonl(RIBBON_SWITCH_SHADOW_LOG_NAME, record)
    except Exception:  # noqa: BLE001
        logger.exception("%s: switch-shadow logging failed - no other effect", candidate_symbol)


async def log_switch_shadow(
    strategy: str, option_type: str, candidate_symbol: str, candidate_total: float, open_positions: list[dict],
) -> None:
    """Async wrapper - call via asyncio.create_task, fire-and-forget, right
    after rank_by_ribbon_expansion returns a top candidate."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, _log_switch_shadow_sync, strategy, option_type, candidate_symbol, candidate_total, open_positions,
    )


async def log_ribbon_switch_shadow_for_alert(strategy: str, option_type: str, stocks: list[str], position_store) -> None:
    """Async, fire-and-forget, self-contained - computes its OWN ribbon-
    ranking top candidate for `stocks` (bullish/expansion ranking for a CE
    alert, bearish/breakdown for PE - extended to PE 18 Sep 2026, user
    request) and shadow-logs what decide_switch() would recommend against
    the strategy's REAL currently-open positions of that same option_type.

    Deliberately INDEPENDENT of config.RIBBON_RANKING_ENABLED/RIBBON_
    RANKING_PE_ENABLED and of whatever the real entry path actually does
    with `stocks` - call sites should invoke this unconditionally for
    every alert (CE and PE both) so switch-shadow data keeps accumulating
    even while ranking-only itself is off for that side. Never touches a
    real position; never raises into its caller - any failure here has
    zero effect on the real webhook response, which has typically already
    been returned by the time this runs anyway."""
    try:
        rank_fn = rank_by_ribbon_expansion if option_type == "CE" else rank_by_ribbon_breakdown
        ranked = await rank_fn(stocks, top_n=1)
        if not ranked:
            return
        top_symbol, top_score = ranked[0]
        snapshot = await position_store.snapshot()
        # position_store.snapshot() returns the Position dataclass's raw
        # __dict__ (vars(p)) when called in-process like this - opened_at
        # is a real datetime object here, NOT the ISO string it only
        # becomes after FastAPI's own JSON serialization on the HTTP
        # endpoints. Found live 18 Sep 2026 (15-minute post-deploy
        # monitoring check): datetime.fromisoformat(p["opened_at"]) inside
        # _log_switch_shadow_sync raised TypeError on every single call
        # since this morning's restart (caught and logged, zero effect on
        # real trading, but it meant switch-shadow never actually logged
        # anything all day). Converting explicitly here keeps _log_
        # switch_shadow_sync's own "opened_at is an ISO string" contract
        # true regardless of what shape the caller's own position objects
        # are in.
        open_positions = [
            {"symbol": p["underlying_symbol"], "opened_at": p["opened_at"].isoformat()}
            for p in snapshot["live_positions"] if p.get("option_type") == option_type
        ]
        await log_switch_shadow(strategy, option_type, top_symbol, top_score, open_positions)
    except Exception:  # noqa: BLE001
        logger.exception("%s: switch-shadow-for-alert failed - no other effect", strategy)
