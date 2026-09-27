"""
Screens the full NSE F&O universe for stocks transitioning from a tight,
non-trending "Phase 1" base into a confirmed, rising "Phase 2" trend with
positive momentum - user request: "phase 2 stock which have broken from
their consolidation or tightness range after phase 1 and now have started
move towards trending phase and in momentum also."

This is a simpler, more direct implementation of the same Weinstein/
Minervini "Stage 1 -> Stage 2 transition" idea covered in trading-skills'
learnings/technical-patterns/vcp.md and minervini-trend-template.md - NOT
full VCP detection (sequential-contraction counting with volume dry-up),
which that doc explicitly flags as "meaningfully more complex... worth its
own design pass before building." This screen instead directly tests the
two things the user actually asked for: (1) was there a recent tight,
non-trending base, and (2) has price now broken above it with ADX
confirming a freshly-rising trend and positive momentum. A full VCP
detector (multi-contraction sequence, volume dry-up) can be layered on
top of this later if wanted - see that doc for why it's a separate effort.

Methodology (real daily data, no synthetic pricing):
  1. F&O universe from the real instrument master (same fetch_fno_universe
     as the other three screeners this session).
  2. ~420 calendar days of real daily OHLC per stock (Dhan's
     historical_daily_data), same SEM_SERIES=="EQ" equity resolution as
     screen_fno_ath_multi_timeframe.py (guards the MOTHERSON bond-
     collision bug - see Options/dhan_client.py's _equity_security_id).
  3. PHASE 1 ("the base"): the BASE_LOOKBACK_DAYS window ending
     RECENT_EXCLUDE_DAYS bars ago (i.e. excluding the most recent bars,
     so the base is measured BEFORE any breakout, not including it).
     Tight/non-trending is confirmed by BOTH:
       - base_range_pct = (max(high) - min(low)) / avg(close) over that
         window <= BASE_MAX_RANGE_PCT (a genuinely narrow price range)
       - min(ADX) over that same window <= BASE_MAX_ADX (ADX was
         actually low somewhere in the base - confirms it was non-
         trending, not just narrow-but-still-drifting)
  4. PHASE 2 ("the breakout"), all of the following on the LATEST bar:
       - latest_close > base_high (price has broken above the Phase-1
         base's own high - the actual breakout)
       - latest ADX >= BREAKOUT_MIN_ADX (now confirmed trending, not just
         drifted marginally above the base)
       - latest ADX > ADX at the start of the recent window (ADX is
         RISING right now - catches a FRESH transition, not a stock
         that's already been trending for a while)
       - momentum_pct_robust over the 1M window > 0 (real, current
         positive momentum - reuses ATH() from screen_fno_ath_multi_
         timeframe.py directly, the robust/regression-based version so a
         single noisy day doesn't flip the read)
       - latest close above its 20-EMA (same trend-confirmation flag
         used throughout this session's other screens)

Read-only: historical_daily_data only, no order placement.

HOW TO RUN:
    DHAN_AUTH_MODE=access_token DHAN_ACCESS_TOKEN=<handoff token> \\
        uv run python screen_fno_phase2_breakout.py
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
from screen_fno_ath_multi_timeframe import ATH

IST = ZoneInfo("Asia/Kolkata")
LOOKBACK_DAYS = 420  # calendar days -> comfortably covers everything below + holidays
EMA_PERIOD = 20
ADX_PERIOD = 14

BASE_LOOKBACK_DAYS = 40     # Phase 1 "base" window, in trading days
RECENT_EXCLUDE_DAYS = 5     # most recent bars excluded from the base (the breakout candidate window)
BASE_MAX_RANGE_PCT = 15.0   # Phase 1 must be at least this tight (base range as % of avg close)
BASE_MAX_ADX = 20.0         # Phase 1 must have had at least one genuinely non-trending reading
BREAKOUT_MIN_ADX = 25.0     # Phase 2 requires ADX at/above this now (conventional "trending" threshold)


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
    needed = BASE_LOOKBACK_DAYS + RECENT_EXCLUDE_DAYS + ADX_PERIOD * 2 + 21  # + 1M window for ATH()
    if len(closes) < needed:
        return None
    return {"close": closes, "high": highs, "low": lows, "bars": len(closes)}


def check_phase2_breakout(closes, highs, lows) -> Optional[dict]:
    """Returns a dict with the full diagnostic read whether or not the
    stock actually passes, so a near-miss is still visible in the report
    (only the caller decides pass/fail on `is_phase2_breakout`)."""
    n = len(closes)
    base_start = n - (BASE_LOOKBACK_DAYS + RECENT_EXCLUDE_DAYS)
    base_end = n - RECENT_EXCLUDE_DAYS  # exclusive
    if base_start < 0:
        return None

    base_highs = highs[base_start:base_end]
    base_lows = lows[base_start:base_end]
    base_closes = closes[base_start:base_end]
    base_high = max(base_highs)
    base_low = min(base_lows)
    avg_close = sum(base_closes) / len(base_closes)
    base_range_pct = (base_high - base_low) / avg_close * 100.0 if avg_close else None

    adx_series = _compute_adx(highs, lows, closes, period=ADX_PERIOD)
    base_adx_vals = [a for a in adx_series[base_start:base_end] if a is not None]
    min_base_adx = min(base_adx_vals) if base_adx_vals else None

    latest_close = closes[-1]
    latest_adx = adx_series[-1]
    recent_start_adx = adx_series[base_end] if base_end < n and adx_series[base_end] is not None else None

    ema20 = _compute_ema(closes, EMA_PERIOD)
    latest_ema20 = ema20[-1]
    above_20ema = (latest_ema20 is not None) and (latest_close > latest_ema20)

    mom_1m = ATH(closes, highs, 21)
    momentum_1m_robust = mom_1m["momentum_pct_robust"] if mom_1m else None

    was_tight_base = (base_range_pct is not None and base_range_pct <= BASE_MAX_RANGE_PCT
                       and min_base_adx is not None and min_base_adx <= BASE_MAX_ADX)
    broke_above_base = latest_close > base_high
    adx_now_trending = latest_adx is not None and latest_adx >= BREAKOUT_MIN_ADX
    adx_rising = (latest_adx is not None and recent_start_adx is not None
                  and latest_adx > recent_start_adx)
    positive_momentum = momentum_1m_robust is not None and momentum_1m_robust > 0

    is_phase2_breakout = (was_tight_base and broke_above_base and adx_now_trending
                           and adx_rising and positive_momentum and above_20ema)

    return {
        "base_high": base_high, "base_low": base_low, "base_range_pct": base_range_pct,
        "min_base_adx": min_base_adx, "latest_close": latest_close, "latest_adx": latest_adx,
        "recent_start_adx": recent_start_adx, "above_20ema": above_20ema, "ema20": latest_ema20,
        "momentum_1m_robust": momentum_1m_robust,
        "was_tight_base": was_tight_base, "broke_above_base": broke_above_base,
        "adx_now_trending": adx_now_trending, "adx_rising": adx_rising,
        "positive_momentum": positive_momentum, "is_phase2_breakout": is_phase2_breakout,
    }


def main() -> None:
    dhan_wrapper.authenticate()

    universe = fetch_fno_universe()
    print(f"F&O universe: {len(universe)} stocks. Screening for Phase 1 -> Phase 2 breakouts "
          f"(base={BASE_LOOKBACK_DAYS}d, recent={RECENT_EXCLUDE_DAYS}d, "
          f"base_max_range={BASE_MAX_RANGE_PCT}%, base_max_adx={BASE_MAX_ADX}, "
          f"breakout_min_adx={BREAKOUT_MIN_ADX})...\n")

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

        r = check_phase2_breakout(data["close"], data["high"], data["low"])
        if r is None:
            failed.append((sym, "insufficient history for base window"))
            continue
        r["symbol"] = sym
        results.append(r)
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(universe)} done")

    print(f"\nScreened {len(results)}/{len(universe)} stocks successfully ({len(failed)} failed/skipped).\n")

    hits = [r for r in results if r["is_phase2_breakout"]]
    hits.sort(key=lambda r: r["momentum_1m_robust"], reverse=True)

    print("=" * 130)
    print("PHASE 1 -> PHASE 2 BREAKOUT CANDIDATES (all conditions met)")
    print("=" * 130)
    print(f"{'Symbol':12s} {'Last':>9s} {'BaseHigh':>9s} {'BaseRange%':>10s} {'MinBaseADX':>10s} "
          f"{'ADXnow':>7s} {'ADXrising':>9s} {'Mom1M%':>8s} {'Above20EMA':>11s}")
    for r in hits:
        print(f"{r['symbol']:12s} {r['latest_close']:9.2f} {r['base_high']:9.2f} "
              f"{r['base_range_pct']:9.2f}% {r['min_base_adx']:10.1f} {r['latest_adx']:7.1f} "
              f"{'YES' if r['adx_rising'] else 'no':>9s} {r['momentum_1m_robust']:7.2f}% "
              f"{'YES' if r['above_20ema'] else 'no':>11s}")
    if not hits:
        print("(none found - see near-misses below)")

    # Near-misses: passed everything except one condition - useful context for tuning thresholds
    near_misses = []
    for r in results:
        if r["is_phase2_breakout"]:
            continue
        conditions = [r["was_tight_base"], r["broke_above_base"], r["adx_now_trending"],
                      r["adx_rising"], r["positive_momentum"], r["above_20ema"]]
        passed = sum(1 for c in conditions if c)
        if passed >= 5:
            near_misses.append(r)
    near_misses.sort(key=lambda r: (r["is_phase2_breakout"], r["momentum_1m_robust"] or -999), reverse=True)

    print("\n" + "=" * 130)
    print("NEAR-MISSES (5 of 6 conditions met - one gap away from a full Phase 2 breakout)")
    print("=" * 130)
    print(f"{'Symbol':12s} {'Last':>9s} {'TightBase':>9s} {'BrokeBase':>9s} {'ADX>=25':>8s} "
          f"{'ADXRising':>9s} {'Mom>0':>6s} {'Above20EMA':>11s}")
    for r in near_misses[:20]:
        print(f"{r['symbol']:12s} {r['latest_close']:9.2f} "
              f"{'Y' if r['was_tight_base'] else 'n':>9s} {'Y' if r['broke_above_base'] else 'n':>9s} "
              f"{'Y' if r['adx_now_trending'] else 'n':>8s} {'Y' if r['adx_rising'] else 'n':>9s} "
              f"{'Y' if r['positive_momentum'] else 'n':>6s} {'Y' if r['above_20ema'] else 'n':>11s}")

    if failed:
        print(f"\n{len(failed)} symbols skipped (no equity match, insufficient history, or fetch error).")


if __name__ == "__main__":
    main()
