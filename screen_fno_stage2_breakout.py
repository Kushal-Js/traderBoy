"""
Screens the full NSE F&O universe for "Stage 2 breakout after tightness/
consolidation" candidates, using the exact scanner criteria the user
supplied (v4 - two changes from v3, both explicit user requests: tightness
threshold loosened 7%->9%, and the 20/50 EMA cross condition from v1 is
back, now scoped to a 5-day lookback instead of v1's 10-day one). Replaces
the looser Phase1->Phase2 logic in screen_fno_phase2_breakout.py, kept
as-is for reference/comparison - not deleted, per this repo's own "don't
silently discard a prior attempt" convention.

USER-SUPPLIED CRITERIA (v4, quoted, then mapped to code below):

Stage 2 Trend Filter:
  - "20 EMA crossed above 50 EMA in last 5 days" - same genuine-cross-
    event check as v1 (ema20 at-or-below ema50 on some earlier bar in the
    window, strictly above on a later bar in the same window), just a
    tighter 5-day window instead of v1's 10-day one.
  - "Close > 50-day EMA"
  - "50-day EMA > 200-day EMA"
  - "Close > 1.25 * 52-week Low" AND "Close >= 0.80 * 52-week High"

Consolidation / Tightness Range:
  - "Highest(High, 20) - Lowest(Low, 20) / Close < 0.07"
  - Alternative: Bollinger Band Squeeze: "Upper Band - Lower Band < 2 * ATR"
    (implemented as an OR alongside the range test - either one confirms
    tightness, per the user's own "Alternatively")

Breakout & Volume Confirmation:
  - "Close > Max High (10 days ago, offset by 1 day)" - the rolling
    10-day high computed AS OF YESTERDAY (excludes today's own bar, so
    today's own high can't trivially satisfy its own max) - the standard
    Chartink `Max(High,10)[1]` idiom.
  - "Volume > 1.5 * 10-day Average Volume" - the 10-day average EXCLUDES
    today's own volume (an average that included today would already be
    inflated by the very surge being tested for).

Methodology (real daily data, no synthetic pricing):
  1. F&O universe from the real instrument master (same fetch_fno_universe
     as the other screeners this session).
  2. ~420 calendar days of real daily OHLCV per stock (Dhan's
     historical_daily_data - confirmed this session it DOES return
     'volume', not just OHLC). Same SEM_SERIES=="EQ" equity resolution as
     every other screener this session (guards the MOTHERSON bond-
     collision bug - see Options/dhan_client.py's _equity_security_id).
  3. 52-week high/low use the trailing 252-trading-day window.

v1 (kept in git history via this same file's prior version, and
independently in screen_fno_phase2_breakout.py's own separate approach)
required a FRESH 20/50 EMA cross and a 15-day/2x-volume breakout -
zero hits on 202 screened stocks, with the breakout+volume combination
being the rare bottleneck (~1% and ~5% pass rates respectively). This v2
loosens exactly those two levers (15d->10d breakout window, 2x->1.5x
volume) per explicit user instruction, and drops the EMA-cross
requirement entirely (a stock already established above its 50-EMA for a
while can still be a valid Stage 2 breakout - the cross itself was too
strict a "freshness" gate for what's actually being asked).

Read-only: historical_daily_data only, no order placement.

HOW TO RUN:
    DHAN_AUTH_MODE=access_token DHAN_ACCESS_TOKEN=<handoff token> \\
        uv run python screen_fno_stage2_breakout.py
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

IST = ZoneInfo("Asia/Kolkata")
LOOKBACK_DAYS = 420  # calendar days -> comfortably covers the 252-trading-day 52w window + holidays

EMA_FAST = 20
EMA_MID = 50
EMA_LONG = 200
CROSSOVER_LOOKBACK_DAYS = 5    # "20 EMA crossed above 50 EMA in last 5 days"
WEEK52_TRADING_DAYS = 252

TIGHTNESS_WINDOW_DAYS = 20
TIGHTNESS_MAX_RANGE_RATIO = 0.09   # loosened from 0.07 per user request
BB_PERIOD = 20
BB_STDDEV = 2.0
ATR_PERIOD = 14
BB_SQUEEZE_ATR_MULTIPLIER = 2.0    # (Upper-Lower) < 2*ATR

BREAKOUT_LOOKBACK_DAYS = 10    # Max High(10 days ago, offset by 1 day)
VOLUME_AVG_DAYS = 10
VOLUME_SURGE_MULTIPLIER = 1.5


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


def compute_sma(values: list[float], period: int) -> list[Optional[float]]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def compute_bollinger_bands(closes: list[float], period: int, stddev_mult: float):
    n = len(closes)
    sma = compute_sma(closes, period)
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        m = sma[i]
        variance = sum((c - m) ** 2 for c in closes[i - period + 1:i + 1]) / period
        std = variance ** 0.5
        upper[i] = m + stddev_mult * std
        lower[i] = m - stddev_mult * std
    return upper, lower


def compute_atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[Optional[float]]:
    n = len(closes)
    tr = [0.0] * n
    for i in range(n):
        tr[i] = (highs[i] - lows[i]) if i == 0 else max(
            highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    atr: list[Optional[float]] = [None] * n
    if n <= period:
        return atr
    atr[period] = sum(tr[1:period + 1]) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def rolling_max(values: list[float], window: int) -> list[Optional[float]]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    for i in range(window - 1, n):
        out[i] = max(values[i - window + 1:i + 1])
    return out


def check_stage2_breakout(closes, highs, lows, volumes) -> Optional[dict]:
    n = len(closes)
    ema20 = _compute_ema(closes, EMA_FAST)
    ema50 = _compute_ema(closes, EMA_MID)
    ema200 = _compute_ema(closes, EMA_LONG)
    if ema50[-1] is None:
        return None

    # --- Stage 2 Trend Filter ---
    latest_close = closes[-1]
    close_above_ema50 = latest_close > ema50[-1]
    ema50_above_ema200 = (ema200[-1] is not None) and (ema50[-1] > ema200[-1])

    crossed_recently = False
    for i in range(max(1, n - CROSSOVER_LOOKBACK_DAYS), n):
        prev20, prev50 = ema20[i - 1], ema50[i - 1]
        cur20, cur50 = ema20[i], ema50[i]
        if None in (prev20, prev50, cur20, cur50):
            continue
        if prev20 <= prev50 and cur20 > cur50:
            crossed_recently = True
            break

    week52_highs = highs[-WEEK52_TRADING_DAYS:]
    week52_lows = lows[-WEEK52_TRADING_DAYS:]
    high_52w = max(week52_highs)
    low_52w = min(week52_lows)
    above_low_floor = latest_close > 1.25 * low_52w
    near_high_ceiling = latest_close >= 0.80 * high_52w

    stage2_trend_ok = (close_above_ema50 and ema50_above_ema200 and above_low_floor
                       and near_high_ceiling and crossed_recently)

    # --- Consolidation / Tightness Range ---
    window_highs = highs[-TIGHTNESS_WINDOW_DAYS:]
    window_lows = lows[-TIGHTNESS_WINDOW_DAYS:]
    range_ratio = (max(window_highs) - min(window_lows)) / latest_close if latest_close else None
    range_tight = range_ratio is not None and range_ratio < TIGHTNESS_MAX_RANGE_RATIO

    bb_upper, bb_lower = compute_bollinger_bands(closes, BB_PERIOD, BB_STDDEV)
    atr = compute_atr(highs, lows, closes, ATR_PERIOD)
    bb_squeeze = (bb_upper[-1] is not None and bb_lower[-1] is not None and atr[-1] is not None
                  and (bb_upper[-1] - bb_lower[-1]) < BB_SQUEEZE_ATR_MULTIPLIER * atr[-1])

    tightness_ok = range_tight or bb_squeeze

    # --- Breakout & Volume Confirmation ---
    rolling_10_high = rolling_max(highs, BREAKOUT_LOOKBACK_DAYS)
    breakout_ref = rolling_10_high[-2] if n >= 2 else None  # "offset by 1 day" - yesterday's value
    broke_above_ceiling = breakout_ref is not None and latest_close > breakout_ref

    avg_volume_10 = sum(volumes[-(VOLUME_AVG_DAYS + 1):-1]) / VOLUME_AVG_DAYS if n > VOLUME_AVG_DAYS else None
    latest_volume = volumes[-1]
    volume_surge = (avg_volume_10 is not None and avg_volume_10 > 0
                     and latest_volume > VOLUME_SURGE_MULTIPLIER * avg_volume_10)

    breakout_ok = broke_above_ceiling and volume_surge

    is_stage2_breakout = stage2_trend_ok and tightness_ok and breakout_ok

    return {
        "latest_close": latest_close, "ema50": ema50[-1], "ema200": ema200[-1],
        "high_52w": high_52w, "low_52w": low_52w,
        "close_above_ema50": close_above_ema50, "ema50_above_ema200": ema50_above_ema200,
        "crossed_recently": crossed_recently,
        "above_low_floor": above_low_floor, "near_high_ceiling": near_high_ceiling,
        "stage2_trend_ok": stage2_trend_ok,
        "range_ratio": range_ratio, "range_tight": range_tight, "bb_squeeze": bb_squeeze,
        "tightness_ok": tightness_ok,
        "breakout_ref": breakout_ref, "broke_above_ceiling": broke_above_ceiling,
        "avg_volume_10": avg_volume_10, "latest_volume": latest_volume, "volume_surge": volume_surge,
        "breakout_ok": breakout_ok, "is_stage2_breakout": is_stage2_breakout,
    }


def main() -> None:
    dhan_wrapper.authenticate()

    universe = fetch_fno_universe()
    print(f"F&O universe: {len(universe)} stocks. Screening for Stage 2 breakouts (v2: "
          f"20/50 EMA cross(5d), Close>50EMA, 50EMA>200EMA, 52w low/high position, "
          f"20-day range<9% or BB squeeze, "
          f"breakout above 10-day high[1] with 1.5x volume)...\n")

    results = []
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

        r = check_stage2_breakout(data["close"], data["high"], data["low"], data["volume"])
        if r is None:
            failed.append((sym, "insufficient history for EMA/SMA warm-up"))
            continue
        r["symbol"] = sym
        results.append(r)
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(universe)} done")

    print(f"\nScreened {len(results)}/{len(universe)} stocks successfully ({len(failed)} failed/skipped).\n")

    hits = [r for r in results if r["is_stage2_breakout"]]
    hits.sort(key=lambda r: (r["latest_volume"] / r["avg_volume_10"]) if r["avg_volume_10"] else 0, reverse=True)

    print("=" * 130)
    print("STAGE 2 BREAKOUT CANDIDATES (ALL criteria met)")
    print("=" * 130)
    print(f"{'Symbol':12s} {'Last':>9s} {'EMA50':>9s} {'SMA200':>9s} "
          f"{'Range%':>7s} {'VolxAvg':>8s} {'52wLow':>9s} {'52wHigh':>9s}")
    for r in hits:
        vol_x = r["latest_volume"] / r["avg_volume_10"] if r["avg_volume_10"] else 0
        print(f"{r['symbol']:12s} {r['latest_close']:9.2f} {r['ema50']:9.2f} "
              f"{r['ema200'] if r['ema200'] is not None else 0:9.2f} "
              f"{(r['range_ratio'] or 0)*100:6.2f}% {vol_x:7.2f}x {r['low_52w']:9.2f} {r['high_52w']:9.2f}")
    if not hits:
        print("(none found - see below)")

    conditions_map = [
        ("crossed_recently", "20/50 EMA cross(5d)"), ("close_above_ema50", "Close>EMA50"),
        ("ema50_above_ema200", "EMA50>EMA200"),
        ("above_low_floor", ">1.25x 52wLow"), ("near_high_ceiling", ">=0.80x 52wHigh"),
        ("tightness_ok", "Tight/Squeeze"), ("broke_above_ceiling", "Broke 10d high[1]"),
        ("volume_surge", "Vol>1.5x avg"),
    ]

    print("\n" + "=" * 60)
    print("PER-CONDITION PASS RATE ACROSS THE SCREENED UNIVERSE")
    print("=" * 60)
    for key, label in conditions_map:
        passed = sum(1 for r in results if r[key])
        print(f"{label:20s} {passed:4d}/{len(results)} ({passed/len(results)*100:5.1f}%)")

    ranked = []
    for r in results:
        passed = sum(1 for key, _ in conditions_map if r[key])
        ranked.append((passed, r))
    ranked.sort(key=lambda pr: (pr[0], (pr[1]["latest_volume"] / pr[1]["avg_volume_10"]) if pr[1]["avg_volume_10"] else 0), reverse=True)

    print("\n" + "=" * 130)
    print("TOP 20 BY CONDITION COUNT (includes full hits at the top if any)")
    print("=" * 130)
    header = f"{'Symbol':12s} {'Last':>9s} {'#Pass':>6s}"
    for _, label in conditions_map:
        header += f" | {label:>16s}"
    print(header)
    for passed, r in ranked[:20]:
        line = f"{r['symbol']:12s} {r['latest_close']:9.2f} {passed:6d}"
        for key, label in conditions_map:
            line += f" | {'YES':>16s}" if r[key] else f" | {'no':>16s}"
        print(line)

    if failed:
        print(f"\n{len(failed)} symbols skipped (no equity match, insufficient history, or fetch error).")


if __name__ == "__main__":
    main()
