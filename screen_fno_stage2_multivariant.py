"""
Multi-combo extension of screen_fno_stage2_breakout.py - user request:
"try this stage 2 with more variables, I think it should capture more
stocks like MOTHERSON, try with different combos and what does markmini
method suggests?"

Two things this script does, both reusing a SINGLE daily-OHLCV fetch per
stock (no extra Dhan API calls beyond the one fetch - all the combos
below are just different arithmetic over the same cached arrays):

1. A battery of named parameter combos around the same "Stage 2 breakout"
   shape as screen_fno_stage2_breakout.py's v4 (20/50 EMA cross, Close>
   50EMA, 50EMA>200EMA, 52w low/high position, tightness, breakout+volume)
   - each combo loosens/drops exactly one lever from v4, so it's clear
   which specific relaxation is what let a given stock (like MOTHERSON)
   through.

2. A FAITHFUL implementation of Mark Minervini's own published Trend
   Template (see trading-skills/learnings/technical-patterns/minervini-
   trend-template.md, researched 30 Aug 2026, sourced from *Think & Trade
   Like a Champion* and cross-referenced across TraderLion/Deepvue/
   ChartMill/sharpely.in) - genuinely different from our own "Stage 2"
   criteria above in three ways worth calling out explicitly:
     - Uses 150-day AND 200-day (Minervini's own two long MAs, we'd only
       been using 200), both as plain SMAs (Minervini's original method,
       not EMA - our own Stage2 screener uses EMA for 50/200 throughout,
       a deliberate simplification, not what Minervini actually teaches).
     - Requires the 200-day MA to be "trending up for at least 1 month" -
       a condition our own Stage2 screener never checks at all (a flat or
       declining 200-MA can still pass our EMA50>EMA200 test if the two
       just happen to be crossed, even if neither is really rising).
     - Uses 30%-above-low / within-25%-of-high (1.30x/0.75x) - LOOSER on
       the low-floor side than our own 1.25x, and LOOSER on the high-
       ceiling side than our own 0.80x. Our Stage2 screener's thresholds
       were never meant to reproduce Minervini's own numbers exactly.
   Minervini's own 8th criterion (relative strength vs the broader
   market) is a SOFT/supporting criterion per the source material, not a
   hard gate - skipped here, same as our own doc's framing: "often
   included as supporting context rather than a hard gate."
   IMPORTANT FRAMING DIFFERENCE: Minervini's Trend Template is a
   "worth watching at all" PRE-FILTER, not a breakout-day trigger - it
   says nothing about tightness, a breakout print, or volume. Expect it
   to pass MANY more stocks than the Stage2 breakout combos above; that's
   by design, not a looser version of the same test.

Read-only: historical_daily_data only, no order placement.

HOW TO RUN:
    DHAN_AUTH_MODE=access_token DHAN_ACCESS_TOKEN=<handoff token> \\
        uv run python screen_fno_stage2_multivariant.py
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
from screen_fno_stage2_breakout import compute_sma, compute_bollinger_bands, compute_atr, rolling_max

IST = ZoneInfo("Asia/Kolkata")
LOOKBACK_DAYS = 420
WEEK52_TRADING_DAYS = 252
BB_PERIOD = 20
BB_STDDEV = 2.0
ATR_PERIOD = 14
BB_SQUEEZE_ATR_MULTIPLIER = 2.0
TIGHTNESS_WINDOW_DAYS = 20
MONTH_TRADING_DAYS = 21


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


# --------------------------------------------------------------------------- #
# Combo 1: parameterized Stage2-breakout evaluator (same shape as
# screen_fno_stage2_breakout.py's v4, but every threshold is a parameter)
# --------------------------------------------------------------------------- #
COMBOS: dict[str, dict] = {
    "v4 (current, baseline)":     dict(require_cross=True,  cross_days=5,  tight_pct=0.09, breakout_days=10, vol_mult=1.5, vol_days=10),
    "No cross requirement":       dict(require_cross=False, cross_days=5,  tight_pct=0.09, breakout_days=10, vol_mult=1.5, vol_days=10),
    "Cross window 10d":           dict(require_cross=True,  cross_days=10, tight_pct=0.09, breakout_days=10, vol_mult=1.5, vol_days=10),
    "Looser volume 1.2x":         dict(require_cross=True,  cross_days=5,  tight_pct=0.09, breakout_days=10, vol_mult=1.2, vol_days=10),
    "No volume requirement":      dict(require_cross=True,  cross_days=5,  tight_pct=0.09, breakout_days=10, vol_mult=None, vol_days=10),
    "Wider breakout window 15d":  dict(require_cross=True,  cross_days=5,  tight_pct=0.09, breakout_days=15, vol_mult=1.5, vol_days=10),
    "Looser tightness 12%":       dict(require_cross=True,  cross_days=5,  tight_pct=0.12, breakout_days=10, vol_mult=1.5, vol_days=10),
    "No cross + no volume":       dict(require_cross=False, cross_days=5,  tight_pct=0.09, breakout_days=10, vol_mult=None, vol_days=10),
    "No cross + looser vol 1.2x": dict(require_cross=False, cross_days=5,  tight_pct=0.09, breakout_days=10, vol_mult=1.2, vol_days=10),
    "Everything loosened":        dict(require_cross=False, cross_days=10, tight_pct=0.12, breakout_days=15, vol_mult=1.2, vol_days=10),
}


def evaluate_stage2(closes, highs, lows, volumes, params: dict) -> Optional[dict]:
    n = len(closes)
    ema20 = _compute_ema(closes, 20)
    ema50 = _compute_ema(closes, 50)
    ema200 = _compute_ema(closes, 200)
    if ema50[-1] is None:
        return None

    latest_close = closes[-1]
    close_above_ema50 = latest_close > ema50[-1]
    ema50_above_ema200 = (ema200[-1] is not None) and (ema50[-1] > ema200[-1])

    crossed_recently = True
    if params["require_cross"]:
        crossed_recently = False
        cd = params["cross_days"]
        for i in range(max(1, n - cd), n):
            p20, p50, c20, c50 = ema20[i - 1], ema50[i - 1], ema20[i], ema50[i]
            if None in (p20, p50, c20, c50):
                continue
            if p20 <= p50 and c20 > c50:
                crossed_recently = True
                break

    week52_highs = highs[-WEEK52_TRADING_DAYS:]
    week52_lows = lows[-WEEK52_TRADING_DAYS:]
    high_52w = max(week52_highs)
    low_52w = min(week52_lows)
    above_low_floor = latest_close > 1.25 * low_52w
    near_high_ceiling = latest_close >= 0.80 * high_52w

    window_highs = highs[-TIGHTNESS_WINDOW_DAYS:]
    window_lows = lows[-TIGHTNESS_WINDOW_DAYS:]
    range_ratio = (max(window_highs) - min(window_lows)) / latest_close if latest_close else None
    range_tight = range_ratio is not None and range_ratio < params["tight_pct"]

    bb_upper, bb_lower = compute_bollinger_bands(closes, BB_PERIOD, BB_STDDEV)
    atr = compute_atr(highs, lows, closes, ATR_PERIOD)
    bb_squeeze = (bb_upper[-1] is not None and bb_lower[-1] is not None and atr[-1] is not None
                  and (bb_upper[-1] - bb_lower[-1]) < BB_SQUEEZE_ATR_MULTIPLIER * atr[-1])
    tightness_ok = range_tight or bb_squeeze

    bd = params["breakout_days"]
    rolling_high = rolling_max(highs, bd)
    breakout_ref = rolling_high[-2] if n >= 2 else None
    broke_above_ceiling = breakout_ref is not None and latest_close > breakout_ref

    volume_surge = True
    if params["vol_mult"] is not None:
        vd = params["vol_days"]
        avg_volume = sum(volumes[-(vd + 1):-1]) / vd if n > vd else None
        latest_volume = volumes[-1]
        volume_surge = (avg_volume is not None and avg_volume > 0
                         and latest_volume > params["vol_mult"] * avg_volume)

    is_hit = (crossed_recently and close_above_ema50 and ema50_above_ema200 and above_low_floor
              and near_high_ceiling and tightness_ok and broke_above_ceiling and volume_surge)

    return {"is_hit": is_hit, "latest_close": latest_close}


# --------------------------------------------------------------------------- #
# Combo 2: faithful Minervini Trend Template (see module docstring)
# --------------------------------------------------------------------------- #
def evaluate_minervini(closes, highs, lows) -> Optional[dict]:
    n = len(closes)
    sma50 = compute_sma(closes, 50)
    sma150 = compute_sma(closes, 150)
    sma200 = compute_sma(closes, 200)
    if sma150[-1] is None or sma200[-1] is None:
        return None

    latest_close = closes[-1]
    price_above_150 = latest_close > sma150[-1]
    price_above_200 = latest_close > sma200[-1]
    sma150_above_200 = sma150[-1] > sma200[-1]

    sma200_trending_up = False
    if n > MONTH_TRADING_DAYS and sma200[-1 - MONTH_TRADING_DAYS] is not None:
        sma200_trending_up = sma200[-1] > sma200[-1 - MONTH_TRADING_DAYS]

    price_above_50 = sma50[-1] is not None and latest_close > sma50[-1]
    sma50_above_150 = sma50[-1] is not None and sma50[-1] > sma150[-1]
    sma50_above_200 = sma50[-1] is not None and sma50[-1] > sma200[-1]

    week52_highs = highs[-WEEK52_TRADING_DAYS:]
    week52_lows = lows[-WEEK52_TRADING_DAYS:]
    high_52w = max(week52_highs)
    low_52w = min(week52_lows)
    above_30pct_of_low = latest_close > 1.30 * low_52w
    within_25pct_of_high = latest_close >= 0.75 * high_52w

    all_conditions = [price_above_150, price_above_200, sma150_above_200, sma200_trending_up,
                       price_above_50, sma50_above_150, sma50_above_200,
                       above_30pct_of_low, within_25pct_of_high]
    passes_all = all(all_conditions)

    return {
        "latest_close": latest_close, "passes_all": passes_all,
        "num_passed": sum(all_conditions), "num_total": len(all_conditions),
        "price_above_150": price_above_150, "price_above_200": price_above_200,
        "sma150_above_200": sma150_above_200, "sma200_trending_up": sma200_trending_up,
        "price_above_50": price_above_50, "sma50_above_150": sma50_above_150,
        "sma50_above_200": sma50_above_200, "above_30pct_of_low": above_30pct_of_low,
        "within_25pct_of_high": within_25pct_of_high,
        "high_52w": high_52w, "low_52w": low_52w,
    }


def main() -> None:
    dhan_wrapper.authenticate()

    universe = fetch_fno_universe()
    print(f"F&O universe: {len(universe)} stocks. Fetching daily data once, then evaluating "
          f"{len(COMBOS)} Stage2 combos + Minervini Trend Template on the same cached data...\n")

    all_data: dict[str, dict] = {}
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
        all_data[sym] = data
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(universe)} fetched")

    print(f"\nFetched {len(all_data)}/{len(universe)} stocks successfully ({len(failed)} failed/skipped).\n")

    # --- Stage2 combo grid ---
    print("=" * 100)
    print("STAGE 2 BREAKOUT - COMBO GRID (each row loosens/drops ONE lever from the v4 baseline)")
    print("=" * 100)
    print(f"{'Combo':30s} {'#Hits':>6s}  Hit symbols")
    for combo_name, params in COMBOS.items():
        hits = []
        for sym, data in all_data.items():
            r = evaluate_stage2(data["close"], data["high"], data["low"], data["volume"], params)
            if r is not None and r["is_hit"]:
                hits.append(sym)
        hit_str = ", ".join(sorted(hits)) if hits else "(none)"
        print(f"{combo_name:30s} {len(hits):6d}  {hit_str}")

    # --- Minervini Trend Template ---
    print("\n" + "=" * 100)
    print("MINERVINI TREND TEMPLATE (faithful implementation - a 'worth watching' PRE-FILTER,")
    print("NOT a breakout trigger - no tightness/breakout/volume check at all, see docstring)")
    print("=" * 100)
    minervini_results = []
    for sym, data in all_data.items():
        r = evaluate_minervini(data["close"], data["high"], data["low"])
        if r is None:
            continue
        r["symbol"] = sym
        minervini_results.append(r)

    full_passes = [r for r in minervini_results if r["passes_all"]]
    full_passes.sort(key=lambda r: r["latest_close"] / r["high_52w"], reverse=True)
    print(f"\nFull Trend Template passes (all 8 hard criteria, RS vs market skipped - soft/context only): "
          f"{len(full_passes)}/{len(minervini_results)}")
    print(f"{'Symbol':12s} {'Last':>9s} {'52wLow':>9s} {'52wHigh':>9s} {'%ofHigh':>8s}")
    for r in full_passes[:30]:
        print(f"{r['symbol']:12s} {r['latest_close']:9.2f} {r['low_52w']:9.2f} {r['high_52w']:9.2f} "
              f"{r['latest_close']/r['high_52w']*100:7.1f}%")

    print(f"\n{len([r for r in minervini_results if r['num_passed'] == r['num_total'] - 1])} more stocks "
          f"pass 8 of 9 criteria (one gap away):")
    near = [r for r in minervini_results if not r["passes_all"] and r["num_passed"] >= r["num_total"] - 1]
    near.sort(key=lambda r: r["latest_close"] / r["high_52w"], reverse=True)
    cond_labels = [
        ("price_above_150", "Px>150SMA"), ("price_above_200", "Px>200SMA"),
        ("sma150_above_200", "150>200"), ("sma200_trending_up", "200SMA rising"),
        ("price_above_50", "Px>50SMA"), ("sma50_above_150", "50>150"), ("sma50_above_200", "50>200"),
        ("above_30pct_of_low", ">1.30x Low"), ("within_25pct_of_high", ">=0.75x High"),
    ]
    header = f"{'Symbol':12s} {'Last':>9s}"
    for _, label in cond_labels:
        header += f" | {label:>13s}"
    print(header)
    for r in near[:20]:
        line = f"{r['symbol']:12s} {r['latest_close']:9.2f}"
        for key, label in cond_labels:
            line += f" | {'YES':>13s}" if r[key] else f" | {'no':>13s}"
        print(line)

    # MOTHERSON specifically, since the user asked about it by name
    if "MOTHERSON" in all_data:
        d = all_data["MOTHERSON"]
        mr = evaluate_minervini(d["close"], d["high"], d["low"])
        print(f"\nMOTHERSON specifically vs Minervini Trend Template: "
              f"{mr['num_passed']}/{mr['num_total']} criteria passed"
              + (" - FULL PASS" if mr["passes_all"] else ""))
        for key, label in cond_labels:
            print(f"    {label:16s} {'YES' if mr[key] else 'no'}")

    if failed:
        print(f"\n{len(failed)} symbols skipped (no equity match, insufficient history, or fetch error).")


if __name__ == "__main__":
    main()
