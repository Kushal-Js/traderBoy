"""
Composite "highest breakout/momentum probability" ranker - user request:
"Update ATH method to find top 15 stocks for highest possibility for
breakout after tightness/consolidation or continued momentum with the
combination of: (1) Markmini vini method, (2) All time high stocks (1%
to 10% range from highest under 52 weeks is fine), (3) Stage 2 criteria
with loosened parameter also, (4) some other criteria or combos ...
feel free to check out and add."

This extends screen_fno_ath_multi_timeframe.py's ATH() function (reused
here unmodified, imported directly - not reimplemented) with a scored
composite across everything built this session, rather than another hard
AND filter (every prior Stage2 attempt this session showed that a strict
AND of many conditions produces 0-2 hits on any given day - a SCORE lets
"how close/how good" rank the whole universe instead of a binary pass).

COMPOSITE SCORE (100 points, reasoning per component):

  1. Minervini structural score (25 pts) = (criteria_passed/9) * 25.
     Reuses evaluate_minervini() from screen_fno_stage2_multivariant.py
     unmodified (faithful 150/200-SMA, 200-SMA-rising, 50-SMA-alignment,
     30%-above-low/25%-of-high Trend Template - see that module's own
     docstring for the full citation and why it's a "worth watching"
     pre-filter, not a breakout trigger). This is the BASE structural
     soundness score - a stock failing most of Minervini's own template
     has no business ranking highly here regardless of anything else.

  2. 52-week-high proximity band (20 pts) - user's own "1% to 10% range
     from highest... is fine" framing, interpreted as a BAND, not a hard
     cutoff: full 20 pts at/above the 52w high (already broken out - the
     BEST case, not excluded), linearly down to 0 pts at 10% below it,
     zero beyond 10% below. Rewards both "already breaking out" and
     "closing in on the highs," matching the user's own "breakout ... OR
     continued momentum" framing (an already-extended breakout IS
     "continued momentum").

  3. Loosened Stage 2 criteria (25 pts) = (passed/7) * 25. Same shape as
     screen_fno_stage2_breakout.py's v4 MINUS the 20/50-cross requirement
     (that's scored separately below as a bonus, not a gate - see #4),
     using the loosened thresholds this session's own combo grid showed
     actually mattered: tightness <=12% (not 7%), breakout window 15
     days (not 10), volume >=1.2x 10-day average (not 1.5x/2x) - these
     are exactly the "Everything loosened" combo from screen_fno_stage2_
     multivariant.py's grid, which was the only combo that surfaced any
     hits (MOTHERSON, ZYDUSLIFE) besides the cross-and-volume-dropped one.

  4. Fresh 20/50 EMA cross BONUS (10 pts flat). Deliberately a bonus, not
     part of #3's gate - this session's combo grid showed requiring a
     fresh cross AND a loosened breakout simultaneously produces zero
     hits (a structural tension: a stock rarely both freshly crosses AND
     already breaks out in the same short window - see multivariant.py's
     own docstring). Scoring it as a bonus instead of a gate lets a
     genuinely fresh transition (rare - ~2-4% of the universe every run
     this session) add real weight without zeroing out every stock that
     doesn't have one.

  5. ADX(14) trend-strength bonus (10 pts, scaled) = min(ADX,25)/25 * 10.
     Same ADX this session's screen_fno_ath_multi_timeframe.py already
     computes - trend STRENGTH (direction-agnostic), distinguishing a
     clean trend from noisy chop that happens to net out directional.

  6. Momentum-consistency bonus (10 pts) = 2 pts per timeframe (of 1W/1M/
     3M/6M/1Y) where ATH()'s own momentum_pct_robust is positive. Reuses
     ATH() directly, unmodified, from screen_fno_ath_multi_timeframe.py -
     this IS "the ATH method updated," per the user's own phrasing:  the
     same function, now one input into a larger composite rather than
     the whole answer. Rewards genuinely broad-based momentum (positive
     across MOST timeframes) over a single cherry-picked window.

Methodology (real daily data, no synthetic pricing): same F&O universe
fetch, same ~420-day daily OHLCV fetch, same SEM_SERIES=="EQ" equity
resolution (guards the MOTHERSON bond-collision bug) as every other
screener this session. One fetch per stock feeds every component above -
no repeated API calls per criterion.

Read-only: historical_daily_data only, no order placement.

HOW TO RUN:
    DHAN_AUTH_MODE=access_token DHAN_ACCESS_TOKEN=<handoff token> \\
        uv run python screen_fno_top15_composite.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from Options.dhan_client import dhan_wrapper, _compute_ema
from reversal_filters import _compute_adx
from screen_fno_ath_multi_timeframe import ATH, PERIODS
from screen_fno_stage2_breakout import compute_sma, compute_bollinger_bands, compute_atr, rolling_max
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

TOP_N = 15


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
    l = r["loosened"]
    missing = [k for k, v in l.items() if k not in ("num_passed", "num_total") and not v]
    parts.append(f"loosened-Stage2 {l['num_passed']}/{l['num_total']}"
                 + (f" (missing: {', '.join(missing)})" if missing else " (all pass)"))
    parts.append("fresh 20/50 cross" if r["fresh_cross"] else "no fresh cross")
    adx_str = f"ADX {r['adx']:.1f}" if r["adx"] is not None else "ADX n/a"
    parts.append(adx_str + (" (trending)" if (r["adx"] or 0) >= 25 else ""))
    parts.append(f"positive momentum in {r['positive_periods']}/5 timeframes")
    parts.append("above 20-EMA" if r["above_20ema"] else "below 20-EMA")
    return "; ".join(parts)


def main() -> None:
    dhan_wrapper.authenticate()

    universe = fetch_fno_universe()
    print(f"F&O universe: {len(universe)} stocks. Fetching daily data + scoring composite "
          f"(Minervini 25pts, 52w-high-proximity 20pts, loosened-Stage2 25pts, "
          f"fresh-cross bonus 10pts, ADX bonus 10pts, momentum-consistency bonus 10pts)...\n")

    scored = []
    failed = []
    for i, sym in enumerate(universe):
        try:
            data = fetch_daily_series(sym)
        except Exception as e:  # noqa: BLE001
            failed.append((sym, str(e)))
            continue
        time.sleep(0.15)
        if data is None:
            failed.append((sym, "no equity match or insufficient history"))
            continue
        r = score_stock(sym, data)
        if r is None:
            failed.append((sym, "insufficient history for scoring"))
            continue
        scored.append(r)
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(universe)} done")

    print(f"\nScored {len(scored)}/{len(universe)} stocks successfully ({len(failed)} failed/skipped).\n")

    scored.sort(key=lambda r: r["total_score"], reverse=True)
    top15 = scored[:TOP_N]

    print("=" * 150)
    print(f"TOP {TOP_N} - HIGHEST BREAKOUT/MOMENTUM COMPOSITE SCORE")
    print("=" * 150)
    print(f"{'#':>2s} {'Symbol':12s} {'Last':>9s} {'Score':>6s} | {'Minervini':>9s} {'HighProx':>8s} "
          f"{'Stage2':>7s} {'Cross':>6s} {'ADX':>6s} {'Mom':>4s}")
    for i, r in enumerate(top15, 1):
        print(f"{i:2d} {r['symbol']:12s} {r['latest_close']:9.2f} {r['total_score']:6.1f} | "
              f"{r['minervini_score']:9.1f} {r['high_proximity_score']:8.1f} "
              f"{r['stage2_score']:7.1f} {r['cross_score']:6.1f} {r['adx_score']:6.1f} {r['momentum_score']:4.1f}")

    print("\n" + "=" * 150)
    print("EXPLANATIONS")
    print("=" * 150)
    for i, r in enumerate(top15, 1):
        print(f"{i:2d}. {r['symbol']} (score {r['total_score']:.1f}/100): {explain(r)}")

    if failed:
        print(f"\n{len(failed)} symbols skipped (no equity match, insufficient history, or fetch error).")


if __name__ == "__main__":
    main()
