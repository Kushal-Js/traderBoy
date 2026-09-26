"""
Screens the full NSE F&O universe (every OPTSTK underlying, same pattern
as screen_clean_trending_fno_stocks.py's fetch_fno_universe) for stocks
whose latest daily close is at/near their trailing 52-week high.

Methodology (real daily data, no synthetic pricing):
  1. F&O universe from the real instrument master (K01/paper_engine.py's
     own _fetch_fno_universe pattern - every NSE OPTSTK underlying).
  2. ~370 calendar days of real daily OHLC per stock (Dhan's
     historical_daily_data) -> gives >=52 trading weeks even after
     weekends/holidays.
  3. 52-week high = max(high) over the trailing ~252 trading days
     available in that window.
  4. Flag a stock if its latest close is within NEAR_HIGH_PCT of that
     52-week high (default 1.0%), and separately flag exact new highs
     (latest high >= prior max(high)).

This does NOT claim true "all-time" high - Dhan's historical endpoint
window here only covers ~1 year, so for stocks listed long before that,
"52-week high" and "all-time high" may differ. Flagged as such in output.

Read-only: historical_daily_data only, no order placement.

HOW TO RUN:
    DHAN_AUTH_MODE=access_token DHAN_ACCESS_TOKEN=<handoff token> \\
        uv run python screen_fno_52week_high.py
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

from Options.dhan_client import dhan_wrapper

IST = ZoneInfo("Asia/Kolkata")
LOOKBACK_DAYS = 370  # calendar days -> >=252 trading days (52 weeks) with buffer
NEAR_HIGH_PCT = 1.0  # flag if latest close within this % of the 52w high


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
    if len(closes) < 30:
        return None
    return {"close": closes, "high": highs, "bars": len(closes)}


def main() -> None:
    dhan_wrapper.authenticate()

    universe = fetch_fno_universe()
    print(f"F&O universe: {len(universe)} stocks. Screening for 52-week highs...\n")

    hits = []
    all_results = []
    failed = []
    for i, sym in enumerate(universe):
        try:
            data = fetch_daily_series(sym)
        except Exception as e:  # noqa: BLE001
            failed.append((sym, str(e)))
            continue
        time.sleep(0.15)  # own pacing on top of the account-wide 0.5s floor already in fetch path
        if data is None:
            failed.append((sym, "no equity match or insufficient history"))
            continue

        closes = data["close"]
        highs = data["high"]
        latest_close = closes[-1]
        latest_high = highs[-1]
        week52_high = max(highs)
        prior_max_high = max(highs[:-1]) if len(highs) > 1 else week52_high
        pct_from_high = (week52_high - latest_close) / week52_high * 100.0 if week52_high else None
        is_new_high = latest_high >= prior_max_high
        row = {
            "symbol": sym, "latest_close": latest_close, "week52_high": week52_high,
            "pct_from_high": pct_from_high, "is_new_high": is_new_high, "bars": data["bars"],
        }
        all_results.append(row)
        if pct_from_high is not None and pct_from_high <= NEAR_HIGH_PCT:
            hits.append(row)
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(universe)} done")

    print(f"\nScreened {len(universe) - len(failed)}/{len(universe)} stocks successfully "
          f"({len(failed)} failed/skipped).\n")

    hits.sort(key=lambda r: r["pct_from_high"])

    print("=" * 90)
    print(f"NSE F&O STOCKS AT/NEAR 52-WEEK HIGH (within {NEAR_HIGH_PCT}% of trailing-year high)")
    print("=" * 90)
    print(f"{'Symbol':14s} {'LastClose':>10s} {'52wHigh':>10s} {'%FromHigh':>10s} {'NewHigh?':>9s} {'Bars':>6s}")
    for r in hits:
        print(f"{r['symbol']:14s} {r['latest_close']:10.2f} {r['week52_high']:10.2f} "
              f"{r['pct_from_high']:9.2f}% {'YES' if r['is_new_high'] else 'no':>9s} {r['bars']:6d}")

    if not hits:
        print("(none found)")

    all_results.sort(key=lambda r: r["pct_from_high"])
    print("\n" + "=" * 90)
    print("TOP 15 CLOSEST TO 52-WEEK HIGH (for context, regardless of threshold)")
    print("=" * 90)
    print(f"{'Symbol':14s} {'LastClose':>10s} {'52wHigh':>10s} {'%FromHigh':>10s} {'NewHigh?':>9s} {'Bars':>6s}")
    for r in all_results[:15]:
        print(f"{r['symbol']:14s} {r['latest_close']:10.2f} {r['week52_high']:10.2f} "
              f"{r['pct_from_high']:9.2f}% {'YES' if r['is_new_high'] else 'no':>9s} {r['bars']:6d}")

    print(f"\nNote: 'Bars' < ~250 means the trailing window doesn't cover a full 52 weeks of "
          f"history for that symbol (recent listing or data gap) - 52w high there is a lower bound.")

    if failed:
        print(f"\n{len(failed)} symbols skipped (no equity match, insufficient history, or fetch error).")


if __name__ == "__main__":
    main()
