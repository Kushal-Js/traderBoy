"""
Shadow-mode reversal-prevention filters (prototype, added 16 Sep 2026).

LOG-ONLY - this module NEVER blocks a real entry and NEVER raises into
its caller. It only records what each candidate filter WOULD have done
for every REAL entry Options/Futures/Luxury actually places, so the
results can be reviewed against what really happened (win/loss/exit
reason, already logged separately via trade_history's real_trades log)
before any of this is trusted to actually gate a live entry.

Built directly on two backtest rounds against real trades:
  - backtest_reversal_filters_sep15_16.py (37 trades, 2 days)
  - backtest_reversal_filters_15day.py (139 trades, 15 days, loaded
    programmatically from history/*_real_trades.log)
Both rounds are the evidence behind every threshold below - see those
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

RECOMMENDED_COMBO_BLOCKS = volume floor OR climax combo OR cooldown.
This is what's reported as the headline "would this have been blocked"
verdict; ADX/RSI-alone are logged purely for comparison.

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


def _evaluate_and_log_sync(strategy: str, symbol: str, option_type: str, entry_price: float, order_id: str) -> None:
    """Blocking - must be called via run_in_executor, never directly from
    async code. Never raises - any failure here just means this one
    entry has no shadow-filter row logged, the real entry itself is
    always already fully placed and unaffected by the time this runs.

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
        logger.debug("%s %s: shadow filters skipped - dhan_wrapper not authenticated yet "
                     "(expected in tests; should never happen for a real live entry)", strategy, symbol)
        return
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
        idx = len(closes) - 1
        if idx < 40:
            logger.info("%s %s: shadow filters skipped - only %d candle(s) of history available",
                        strategy, symbol, idx + 1)
            return

        rsi = _compute_rsi(closes)[idx]
        adx = _compute_adx(highs, lows, closes)[idx]
        vol_ratio = _volume_ratio_at(volumes, idx)

        rsi_extreme = rsi is not None and (rsi > RSI_OVERBOUGHT if option_type == "CE" else rsi < RSI_OVERSOLD)
        adx_blocks = adx is not None and adx < ADX_MIN
        volume_blocks = vol_ratio is not None and vol_ratio < VOL_RATIO_MIN
        climax_combo_blocks = rsi_extreme and vol_ratio is not None and vol_ratio > SPIKE_RATIO
        last_exit = _last_supertrend_exit.get((strategy, symbol, option_type))
        cooldown_blocks = last_exit is not None and (datetime.now() - last_exit).total_seconds() <= COOLDOWN_MINUTES * 60

        recommended_combo_blocks = volume_blocks or climax_combo_blocks or cooldown_blocks

        record = {
            "strategy": strategy, "symbol": symbol, "option_type": option_type,
            "entry_price": entry_price, "order_id": order_id,
            "rsi": round(rsi, 2) if rsi is not None else None,
            "adx": round(adx, 2) if adx is not None else None,
            "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "adx_blocks": adx_blocks, "rsi_extreme_alone_blocks": rsi_extreme, "volume_blocks": volume_blocks,
            "climax_combo_blocks": climax_combo_blocks, "cooldown_blocks": cooldown_blocks,
            "recommended_combo_blocks": recommended_combo_blocks,
            "entry_time": datetime.now().isoformat(),
        }
        append_jsonl(SHADOW_LOG_NAME, record)
        logger.info(
            "%s %s %s: shadow filters logged - RSI=%s ADX=%s VolRatio=%s -> recommended_combo_blocks=%s "
            "(volume=%s climax=%s cooldown=%s)",
            strategy, symbol, option_type, record["rsi"], record["adx"], record["vol_ratio"],
            recommended_combo_blocks, volume_blocks, climax_combo_blocks, cooldown_blocks,
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
