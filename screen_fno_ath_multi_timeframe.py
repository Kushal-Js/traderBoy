"""
Standalone `ATH()` function + a full-F&O-universe screener built on it.

User request: "Create a standalone function named 'ATH' which captures
All Time High for FnO stocks for multiple time frames (1 week, 1 month,
3 months, 6 months, 1 year) and show a list with % change across. This
formula should also capture average %change over that period selected
and the momentum and whether above 20 EMA or not." Follow-up: "avg%change/
momentum should be calculated for all windows" (done), then "add that as
an alternative and consider ADX also" - referring to a flagged limitation
of the original momentum formula (a plain two-point Rate-of-Change: only
the window's FIRST and LAST close matter, so a single noisy/spiky day at
either endpoint can distort it) and a request to also weigh in ADX
(trend-strength, direction-agnostic - a real gap, since neither the
original momentum nor avg%change actually says whether a move is a clean
trend or noisy chop).

NOTE ON NAMING: "ATH" here means the highest price reached WITHIN each
selected lookback window (a "period high"), not the stock's genuine
multi-decade all-time high (see screen_fno_alltime_high_breakout.py,
built the prior session, for that - a different, stricter question).
Kept the user's own requested name/spirit; the per-window meaning is
made explicit in ATH()'s own docstring below to avoid confusion between
the two scripts.

Methodology (real daily data, no synthetic pricing):
  1. F&O universe from the real instrument master - every NSE OPTSTK
     underlying (same fetch_fno_universe as the other two screeners).
  2. ~420 calendar days of real daily OHLC per stock (Dhan's
     historical_daily_data) - enough trading days to cover the longest
     (1-year/252-trading-day) window plus holiday buffer.
  3. ATH(closes, highs, period_days) -> for the trailing `period_days`
     bars: the period's high, the latest close's % distance from it, the
     average day-over-day % change within the window, and the window's
     total/cumulative % change (start-of-window close -> latest close,
     i.e. Rate-of-Change over that period - this is "momentum").
  4. Above/below the 20-day EMA is a single, period-independent flag
     (always computed on the full daily-close series - reuses the same
     _compute_ema this repo already uses for Supertrend backtests).
  5. ATH() ALSO returns momentum_pct_robust - a linear-regression-based
     alternative to the plain two-point momentum_pct: fits an OLS trend
     line through EVERY close in the window (not just the first/last),
     then measures that fitted line's own start-to-end % change. This is
     far less sensitive to a single unusual spike/dip landing exactly on
     the window's first or last bar - the whole window's shape drives the
     answer, not two points. Genuinely differs from momentum_pct when a
     stock's move wasn't roughly linear (front/back-loaded, or ends on an
     outlier day) - the two calcs converging usually means the trend was
     already fairly steady across the window.
  6. ADX(14) (period-independent, same convention as the 20-EMA flag -
     always computed on the full daily H/L/C series, reusing this repo's
     own shared reversal_filters._compute_adx) - trend STRENGTH, not
     direction (that's what momentum/avg%change already answer). >=25 is
     the conventional "trending" threshold, <20 "no clear trend"/chop -
     shown alongside momentum so a big momentum number can be read
     against whether the underlying move was actually a clean trend or
     noisy chop that happened to net out directional.

Equity-instrument resolution filters SEM_SERIES=="EQ" (see Options/
dhan_client.py's _equity_security_id docstring for the real MOTHERSON
bond-collision bug this guards against).

Read-only: historical_daily_data only, no order placement.

HOW TO RUN:
    DHAN_AUTH_MODE=access_token DHAN_ACCESS_TOKEN=<handoff token> \\
        uv run python screen_fno_ath_multi_timeframe.py
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

IST = ZoneInfo("Asia/Kolkata")
LOOKBACK_DAYS = 420  # calendar days -> comfortably covers a 252-trading-day (1Y) window + holidays
EMA_PERIOD = 20
ADX_PERIOD = 14
ADX_TRENDING_THRESHOLD = 25.0

PERIODS: dict[str, int] = {
    "1W": 5,
    "1M": 21,
    "3M": 63,
    "6M": 126,
    "1Y": 252,
}


def _linreg_pct_change(values: list[float]) -> Optional[float]:
    """OLS trend line through every point in `values` (x = 0..n-1), then
    the FITTED line's own start-to-end % change - see ATH()'s docstring
    (point 5) for why this is a more robust momentum alternative than a
    plain two-point Rate-of-Change."""
    n = len(values)
    if n < 2:
        return None
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n)) / denom
    intercept = mean_y - slope * mean_x
    fitted_start = intercept
    fitted_end = intercept + slope * (n - 1)
    if not fitted_start:
        return None
    return (fitted_end - fitted_start) / fitted_start * 100.0


def ATH(closes: list[float], highs: list[float], period_days: int) -> Optional[dict]:
    """Standalone period-high capture, as requested.

    Given a stock's full daily close/high series (oldest -> newest) and a
    lookback window in TRADING days, returns:
      - period_high: max(high) over the trailing `period_days` bars
        (the window's own high - what the user is calling its "ATH" for
        that timeframe).
      - pct_from_high: latest close's % distance from that period high
        (negative = below it, the common case; positive would mean the
        latest bar itself set a new high inside its own window, which
        can't exceed the max by definition - this is always <= 0 here
        since period_high is computed INCLUDING the latest bar).
      - avg_daily_pct_change: mean of day-over-day close-to-close %
        changes within the window - the average daily move's size and
        direction over that period.
      - momentum_pct: total/cumulative % change from the start of the
        window to the latest close (Rate-of-Change over the period) -
        the big-picture trend strength over that same window, distinct
        from the day-to-day average above.
      - momentum_pct_robust: the same idea, but from an OLS trend line
        fitted through every close in the window rather than just its
        first/last point - see this module's docstring (point 5) and
        _linreg_pct_change for why. Prefer this one when a single
        endpoint day looks like an outlier; the two should be close for
        an already-steady trend.

    Returns None if there isn't enough history to cover period_days.
    """
    n = len(closes)
    if n < period_days + 1 or len(highs) != n:
        return None

    window_highs = highs[-period_days:]
    window_closes = closes[-period_days:]
    period_high = max(window_highs)
    latest_close = closes[-1]
    pct_from_high = (latest_close - period_high) / period_high * 100.0 if period_high else None

    daily_changes = []
    for i in range(1, len(window_closes)):
        prev, cur = window_closes[i - 1], window_closes[i]
        if prev:
            daily_changes.append((cur - prev) / prev * 100.0)
    avg_daily_pct_change = sum(daily_changes) / len(daily_changes) if daily_changes else None

    start_close = window_closes[0]
    momentum_pct = (latest_close - start_close) / start_close * 100.0 if start_close else None
    momentum_pct_robust = _linreg_pct_change(window_closes)

    return {
        "period_high": period_high,
        "pct_from_high": pct_from_high,
        "avg_daily_pct_change": avg_daily_pct_change,
        "momentum_pct": momentum_pct,
        "momentum_pct_robust": momentum_pct_robust,
    }


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
    if len(closes) < PERIODS["1Y"] + 1:
        return None
    return {"close": closes, "high": highs, "low": lows, "bars": len(closes)}


def main() -> None:
    dhan_wrapper.authenticate()

    universe = fetch_fno_universe()
    print(f"F&O universe: {len(universe)} stocks. Computing ATH() across {list(PERIODS)}...\n")

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
            failed.append((sym, "no equity match or <1Y of history"))
            continue

        closes, highs, lows = data["close"], data["high"], data["low"]
        per_period = {}
        ok = True
        for label, days in PERIODS.items():
            r = ATH(closes, highs, days)
            if r is None:
                ok = False
                break
            per_period[label] = r
        if not ok:
            failed.append((sym, "ATH() returned None for some period"))
            continue

        ema20 = _compute_ema(closes, EMA_PERIOD)
        latest_ema20 = ema20[-1]
        above_20ema = (latest_ema20 is not None) and (closes[-1] > latest_ema20)

        adx_series = _compute_adx(highs, lows, closes, period=ADX_PERIOD)
        latest_adx = adx_series[-1] if adx_series else None

        results.append({
            "symbol": sym, "latest_close": closes[-1], "periods": per_period,
            "above_20ema": above_20ema, "ema20": latest_ema20, "adx14": latest_adx,
            "bars": data["bars"],
        })
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(universe)} done")

    print(f"\nScreened {len(results)}/{len(universe)} stocks successfully ({len(failed)} failed/skipped).\n")

    # --- Table 1: multi-timeframe period-high comparison ---
    print("=" * 130)
    print("TABLE 1: PERIOD HIGH ('ATH' per timeframe) AND % FROM IT - sorted by closest to 1Y high")
    print("=" * 130)
    header = f"{'Symbol':12s} {'Last':>9s}"
    for label in PERIODS:
        header += f" | {label+'High':>9s} {label+'%':>7s}"
    print(header)
    results_sorted = sorted(results, key=lambda r: r["periods"]["1Y"]["pct_from_high"], reverse=True)
    for r in results_sorted[:25]:
        line = f"{r['symbol']:12s} {r['latest_close']:9.2f}"
        for label in PERIODS:
            p = r["periods"][label]
            line += f" | {p['period_high']:9.2f} {p['pct_from_high']:6.2f}%"
        print(line)

    # --- Table 2: avg daily %change, ALL periods ---
    results_by_momentum = sorted(results, key=lambda r: r["periods"]["1Y"]["momentum_pct"], reverse=True)

    print("\n" + "=" * 110)
    print("TABLE 2: AVG DAILY %CHANGE - ALL PERIODS (sorted by 1Y momentum, same order as Table 3)")
    print("=" * 110)
    header2 = f"{'Symbol':12s} {'Last':>9s}"
    for label in PERIODS:
        header2 += f" | {label+'Avg%':>9s}"
    print(header2)
    for r in results_by_momentum[:25]:
        line = f"{r['symbol']:12s} {r['latest_close']:9.2f}"
        for label in PERIODS:
            line += f" | {r['periods'][label]['avg_daily_pct_change']:8.3f}%"
        print(line)

    # --- Table 3: momentum (total %change, plain two-point), ALL periods ---
    print("\n" + "=" * 110)
    print("TABLE 3: MOMENTUM (plain two-point Rate-of-Change) - ALL PERIODS")
    print("=" * 110)
    header3 = f"{'Symbol':12s} {'Last':>9s}"
    for label in PERIODS:
        header3 += f" | {label+'Mom%':>9s}"
    print(header3)
    for r in results_by_momentum[:25]:
        line = f"{r['symbol']:12s} {r['latest_close']:9.2f}"
        for label in PERIODS:
            line += f" | {r['periods'][label]['momentum_pct']:8.2f}%"
        print(line)

    # --- Table 3b: momentum, ROBUST (linear-regression) alternative, ALL periods ---
    print("\n" + "=" * 110)
    print("TABLE 3b: MOMENTUM - ROBUST ALTERNATIVE (OLS trend-line fit, not just 2 endpoints) - ALL PERIODS")
    print("(compare against Table 3 - a big gap between the two means an endpoint day is an outlier)")
    print("=" * 110)
    header3b = f"{'Symbol':12s} {'Last':>9s}"
    for label in PERIODS:
        header3b += f" | {label+'Mom%':>9s}"
    print(header3b)
    for r in results_by_momentum[:25]:
        line = f"{r['symbol']:12s} {r['latest_close']:9.2f}"
        for label in PERIODS:
            v = r["periods"][label]["momentum_pct_robust"]
            line += f" | {v:8.2f}%" if v is not None else f" | {'n/a':>8s} "
        print(line)

    # --- Table 4: 20-EMA + ADX(14) status (both period-independent) ---
    print("\n" + "=" * 80)
    print(f"TABLE 4: 20-EMA + ADX({ADX_PERIOD}) STATUS (period-independent, always daily)")
    print(f"(ADX >= {ADX_TRENDING_THRESHOLD:.0f} = trending, direction-agnostic; below = weak/no trend, i.e. chop)")
    print("=" * 80)
    print(f"{'Symbol':12s} {'Last':>9s} {'Above20EMA':>11s} {'EMA20':>9s} {'ADX14':>7s} {'Trending?':>10s}")
    for r in results_by_momentum[:25]:
        ema_str = f"{r['ema20']:.2f}" if r["ema20"] is not None else "n/a"
        adx_val = r["adx14"]
        adx_str = f"{adx_val:.1f}" if adx_val is not None else "n/a"
        trending_str = ("YES" if adx_val >= ADX_TRENDING_THRESHOLD else "no") if adx_val is not None else "n/a"
        print(f"{r['symbol']:12s} {r['latest_close']:9.2f} {'YES' if r['above_20ema'] else 'no':>11s} "
              f"{ema_str:>9s} {adx_str:>7s} {trending_str:>10s}")

    if failed:
        print(f"\n{len(failed)} symbols skipped (no equity match, insufficient history, or fetch error).")


if __name__ == "__main__":
    main()
