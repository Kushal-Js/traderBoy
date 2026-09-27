"""
GLOBAL COMMON FUNCTION - the composite Minervini+ATH+Stage2 breakout/
momentum screener, extracted from screen_fno_top15_composite.py so it can
be imported by anything that needs it (the manual analysis script, and
weekly_watchlist_refresh.py's scheduled job) without duplicating the
scoring logic in two places - this repo has been burned by that exact
kind of drift before (see trading-skills' backtest-methodology.md's
"Keeping this in sync" section: a backtest script copied from another
backtest script silently carried forward a bug the original had already
fixed). This module is the ONE place the composite score is computed;
both the manual screener and the scheduled job call get_top_n_candidates()
rather than each keeping their own copy.

User request (27 Sep 2026): "Create and deploy this ATH method as a
global common function with this combo logic and setup we have done now."

COMPOSITE SCORE (100 points) - see screen_fno_top15_composite.py's git
history for the original, fuller reasoning per component; summarized here:

  1. Minervini structural score (25 pts) = (criteria_passed/9) * 25.
     Faithful Trend Template (150/200-day SMA, 200-SMA rising over the
     last month, 50-SMA alignment, 30%-above-low/25%-of-high) - see
     screen_fno_stage2_multivariant.py's evaluate_minervini(), imported
     here unmodified.
  2. 52-week-high proximity band (20 pts) - full points at/above the 52w
     high, linearly down to 0 at 10% below it, per the user's own "1% to
     10% from the high is fine" framing.
  3. Loosened Stage 2 criteria (25 pts) = (passed/7) * 25 - EMA50/EMA200
     alignment, 52w low/high position, tightness (range or BB squeeze),
     breakout above a rolling 15-day high, 1.2x volume - the cross
     requirement is excluded from this gate and scored separately (#4)
     since requiring both simultaneously produced zero hits in this
     session's own combo grid (screen_fno_stage2_multivariant.py).
  4. Fresh 20/50 EMA cross bonus (10 pts flat).
  5. ADX(14) trend-strength bonus (10 pts, scaled) = min(ADX,25)/25*10.
  6. Momentum-consistency bonus (10 pts) = 2 pts per timeframe (of
     1W/1M/3M/6M/1Y) where ATH()'s regression-based robust momentum is
     positive - reuses ATH() from screen_fno_ath_multi_timeframe.py
     directly, unmodified.

Methodology: F&O universe from the real instrument master (every NSE
OPTSTK underlying), ~420 calendar days of real daily OHLCV per stock
(Dhan's historical_daily_data), SEM_SERIES=="EQ" equity resolution (see
Options/dhan_client.py's _equity_security_id docstring for the real
MOTHERSON bond-collision bug this guards against).

Read-only: historical_daily_data only, no order placement. Callers are
responsible for their own Dhan authentication (this module never calls
dhan_wrapper.authenticate() itself) - see weekly_watchlist_refresh.py for
the droplet-safe pattern (reuses the cached token, no pin_totp collision
risk since it runs on the droplet itself) and screen_fno_top15_composite.
py for the local/handoff-token pattern.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from Options.dhan_client import dhan_wrapper, _compute_ema
from reversal_filters import _compute_adx
from screen_fno_ath_multi_timeframe import ATH, PERIODS
from screen_fno_stage2_breakout import compute_bollinger_bands, compute_atr, rolling_max
from screen_fno_stage2_multivariant import evaluate_minervini

IST = ZoneInfo("Asia/Kolkata")
LOOKBACK_DAYS = 420
WEEK52_TRADING_DAYS = 252
ADX_PERIOD = 14
EMA_PERIOD_20 = 20

TIGHTNESS_WINDOW_DAYS = 20
LOOSENED_TIGHT_PCT = 0.12
LOOSENED_BREAKOUT_DAYS = 15
LOOSENED_VOL_MULT = 1.2
LOOSENED_VOL_DAYS = 10
CROSS_LOOKBACK_DAYS = 5

BB_PERIOD = 20
BB_STDDEV = 2.0
ATR_PERIOD = 14
BB_SQUEEZE_ATR_MULTIPLIER = 2.0

DEFAULT_TOP_N = 15
DEFAULT_PACE_SECONDS = 0.15


def fetch_fno_universe() -> list[str]:
    df = dhan_wrapper.instruments()
    optstk = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")]
    underlyings = set()
    for trading_symbol in optstk["SEM_TRADING_SYMBOL"]:
        try:
            underlyings.add(dhan_wrapper._underlying_from_trading_symbol(str(trading_symbol)))
        except Exception:  # noqa: BLE001
            continue
    return sorted(underlyings)


def fetch_daily_series(symbol: str) -> Optional[dict]:
    df = dhan_wrapper.instruments()
    row = df[(df["SEM_TRADING_SYMBOL"] == symbol) & (df["SEM_EXM_EXCH_ID"] == "NSE")
             & (df["SEM_INSTRUMENT_NAME"] == "EQUITY") & (df["SEM_SERIES"] == "EQ")]
    if row.empty:
        return None
    security_id = str(int(row.iloc[0]["SEM_SMST_SECURITY_ID"]))
    to_date = datetime.now(IST).strftime("%Y-%m-%d")
    from_date = (datetime.now(IST) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    resp = dhan_wrapper.client.Dhan.historical_daily_data(
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=from_date, to_date=to_date,
    )
    data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
    closes = data.get("close") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    volumes = data.get("volume") or []
    if len(closes) < WEEK52_TRADING_DAYS + 1 or len(volumes) != len(closes):
        return None
    return {"close": closes, "high": highs, "low": lows, "volume": volumes, "bars": len(closes)}


def evaluate_loosened_stage2(closes, highs, lows, volumes) -> dict:
    """The 7 core Stage2 conditions (cross excluded - scored separately
    as a bonus, see module docstring #4), at the loosened thresholds."""
    n = len(closes)
    ema50 = _compute_ema(closes, 50)
    ema200 = _compute_ema(closes, 200)
    latest_close = closes[-1]

    close_above_ema50 = ema50[-1] is not None and latest_close > ema50[-1]
    ema50_above_ema200 = (ema50[-1] is not None and ema200[-1] is not None
                           and ema50[-1] > ema200[-1])

    week52_highs = highs[-WEEK52_TRADING_DAYS:]
    week52_lows = lows[-WEEK52_TRADING_DAYS:]
    high_52w = max(week52_highs)
    low_52w = min(week52_lows)
    above_low_floor = latest_close > 1.25 * low_52w
    near_high_ceiling = latest_close >= 0.80 * high_52w

    window_highs = highs[-TIGHTNESS_WINDOW_DAYS:]
    window_lows = lows[-TIGHTNESS_WINDOW_DAYS:]
    range_ratio = (max(window_highs) - min(window_lows)) / latest_close if latest_close else None
    range_tight = range_ratio is not None and range_ratio < LOOSENED_TIGHT_PCT

    bb_upper, bb_lower = compute_bollinger_bands(closes, BB_PERIOD, BB_STDDEV)
    atr = compute_atr(highs, lows, closes, ATR_PERIOD)
    bb_squeeze = (bb_upper[-1] is not None and bb_lower[-1] is not None and atr[-1] is not None
                  and (bb_upper[-1] - bb_lower[-1]) < BB_SQUEEZE_ATR_MULTIPLIER * atr[-1])
    tightness_ok = range_tight or bb_squeeze

    rolling_high = rolling_max(highs, LOOSENED_BREAKOUT_DAYS)
    breakout_ref = rolling_high[-2] if n >= 2 else None
    broke_above_ceiling = breakout_ref is not None and latest_close > breakout_ref

    avg_volume = (sum(volumes[-(LOOSENED_VOL_DAYS + 1):-1]) / LOOSENED_VOL_DAYS
                  if n > LOOSENED_VOL_DAYS else None)
    latest_volume = volumes[-1]
    volume_surge = (avg_volume is not None and avg_volume > 0
                     and latest_volume > LOOSENED_VOL_MULT * avg_volume)

    conditions = {
        "close_above_ema50": close_above_ema50, "ema50_above_ema200": ema50_above_ema200,
        "above_low_floor": above_low_floor, "near_high_ceiling": near_high_ceiling,
        "tightness_ok": tightness_ok, "broke_above_ceiling": broke_above_ceiling,
        "volume_surge": volume_surge,
    }
    num_passed = sum(1 for v in conditions.values() if v)
    return {**conditions, "num_passed": num_passed, "num_total": len(conditions)}


def check_fresh_cross(closes) -> bool:
    ema20 = _compute_ema(closes, 20)
    ema50 = _compute_ema(closes, 50)
    n = len(closes)
    for i in range(max(1, n - CROSS_LOOKBACK_DAYS), n):
        p20, p50, c20, c50 = ema20[i - 1], ema50[i - 1], ema20[i], ema50[i]
        if None in (p20, p50, c20, c50):
            continue
        if p20 <= p50 and c20 > c50:
            return True
    return False


def score_stock(symbol: str, data: dict) -> Optional[dict]:
    closes, highs, lows, volumes = data["close"], data["high"], data["low"], data["volume"]
    latest_close = closes[-1]

    minervini = evaluate_minervini(closes, highs, lows)
    if minervini is None:
        return None
    minervini_score = (minervini["num_passed"] / minervini["num_total"]) * 25.0

    week52_high = max(highs[-WEEK52_TRADING_DAYS:])
    pct_below_52w_high = (week52_high - latest_close) / week52_high * 100.0 if week52_high else 100.0
    if pct_below_52w_high <= 0:
        high_proximity_score = 20.0
    elif pct_below_52w_high >= 10:
        high_proximity_score = 0.0
    else:
        high_proximity_score = 20.0 * (1 - pct_below_52w_high / 10.0)

    loosened = evaluate_loosened_stage2(closes, highs, lows, volumes)
    stage2_score = (loosened["num_passed"] / loosened["num_total"]) * 25.0

    fresh_cross = check_fresh_cross(closes)
    cross_score = 10.0 if fresh_cross else 0.0

    adx_series = _compute_adx(highs, lows, closes, period=ADX_PERIOD)
    latest_adx = adx_series[-1] if adx_series else None
    adx_score = (min(latest_adx, 25.0) / 25.0 * 10.0) if latest_adx is not None else 0.0

    ema20 = _compute_ema(closes, EMA_PERIOD_20)
    above_20ema = ema20[-1] is not None and latest_close > ema20[-1]

    momentum_by_period = {}
    positive_periods = 0
    for label, days in PERIODS.items():
        r = ATH(closes, highs, days)
        if r is not None:
            momentum_by_period[label] = r["momentum_pct_robust"]
            if r["momentum_pct_robust"] is not None and r["momentum_pct_robust"] > 0:
                positive_periods += 1
    momentum_score = positive_periods * 2.0

    total_score = (minervini_score + high_proximity_score + stage2_score
                   + cross_score + adx_score + momentum_score)

    return {
        "symbol": symbol, "latest_close": latest_close, "week52_high": week52_high,
        "pct_below_52w_high": pct_below_52w_high,
        "minervini": minervini, "minervini_score": minervini_score,
        "high_proximity_score": high_proximity_score,
        "loosened": loosened, "stage2_score": stage2_score,
        "fresh_cross": fresh_cross, "cross_score": cross_score,
        "adx": latest_adx, "adx_score": adx_score,
        "above_20ema": above_20ema,
        "momentum_by_period": momentum_by_period, "positive_periods": positive_periods,
        "momentum_score": momentum_score,
        "total_score": total_score,
    }


def explain(r: dict) -> str:
    parts = []
    m = r["minervini"]
    parts.append(f"Minervini {m['num_passed']}/{m['num_total']}")
    if r["pct_below_52w_high"] <= 0:
        parts.append("at/above 52w high")
    else:
        parts.append(f"{r['pct_below_52w_high']:.1f}% below 52w high")
    loosened = r["loosened"]
    missing = [k for k, v in loosened.items() if k not in ("num_passed", "num_total") and not v]
    parts.append(f"loosened-Stage2 {loosened['num_passed']}/{loosened['num_total']}"
                 + (f" (missing: {', '.join(missing)})" if missing else " (all pass)"))
    parts.append("fresh 20/50 cross" if r["fresh_cross"] else "no fresh cross")
    adx_str = f"ADX {r['adx']:.1f}" if r["adx"] is not None else "ADX n/a"
    parts.append(adx_str + (" (trending)" if (r["adx"] or 0) >= 25 else ""))
    parts.append(f"positive momentum in {r['positive_periods']}/5 timeframes")
    parts.append("above 20-EMA" if r["above_20ema"] else "below 20-EMA")
    return "; ".join(parts)


def get_top_n_candidates(
    n: int = DEFAULT_TOP_N,
    universe: Optional[list[str]] = None,
    pace_seconds: float = DEFAULT_PACE_SECONDS,
    progress_callback=None,
) -> tuple[list[dict], list[tuple[str, str]]]:
    """THE global common function. Scores the full F&O universe (or a
    caller-supplied subset) with the composite Minervini+ATH+Stage2 score
    and returns (top_n_scored_descending, failed[(symbol, reason)]).

    Caller must already be authenticated (dhan_wrapper.client usable) -
    this function never calls authenticate() itself, so it works the same
    way whether the caller is a local script with a handoff token or a
    droplet-side job reusing the bot's own cached session.

    `progress_callback(done, total)`, if given, is invoked periodically
    (every 25 symbols) so a caller can log/print progress without this
    function depending on any particular logging setup.
    """
    if universe is None:
        universe = fetch_fno_universe()

    scored: list[dict] = []
    failed: list[tuple[str, str]] = []
    for i, sym in enumerate(universe):
        try:
            data = fetch_daily_series(sym)
        except Exception as e:  # noqa: BLE001
            failed.append((sym, str(e)))
            continue
        time.sleep(pace_seconds)
        if data is None:
            failed.append((sym, "no equity match or insufficient history"))
            continue
        r = score_stock(sym, data)
        if r is None:
            failed.append((sym, "insufficient history for scoring"))
            continue
        scored.append(r)
        if progress_callback is not None and (i + 1) % 25 == 0:
            progress_callback(i + 1, len(universe))

    scored.sort(key=lambda r: r["total_score"], reverse=True)
    return scored[:n], failed
