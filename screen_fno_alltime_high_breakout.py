"""
Screens the full NSE F&O universe (every OPTSTK underlying, same pattern
as screen_fno_52week_high.py) for stocks whose latest daily close is AT
LEAST 1% ABOVE their prior all-time high - a genuine breakout past the
old ATH by a full percent, not just "near" it.

Methodology (real daily data, no synthetic pricing):
  1. F&O universe from the real instrument master - every NSE OPTSTK
     underlying (same fetch_fno_universe as screen_fno_52week_high.py).
  2. FULL available daily OHLC per stock (Dhan's historical_daily_data,
     from_date="2000-01-01" - confirmed empirically this session that
     Dhan actually serves genuinely long history, not just ~1 year:
     RELIANCE returned 6,150 daily bars back to 2002-01-01). Whatever a
     given stock's real listing date is, Dhan just returns from there -
     no error for a recently-listed name, it simply gets fewer bars.
  3. prior_ath = max(high) over every bar EXCEPT the most recent one (the
     all-time high price set BEFORE today's/the latest session's move).
     pct_above = (latest_close - prior_ath) / prior_ath * 100. Flagged if
     pct_above >= 1.0%.

Unlike screen_fno_52week_high.py (which only ever had ~1 year of history
and explicitly could NOT claim true all-time), this script's "ATH" is the
real thing for any stock with enough history to reach 2000. Equity-
instrument resolution filters SEM_SERIES=="EQ" (see Options/dhan_client.
py's _equity_security_id docstring for the real MOTHERSON bond-collision
bug this guards against).

Read-only: historical_daily_data only, no order placement.

HOW TO RUN:
    DHAN_AUTH_MODE=access_token DHAN_ACCESS_TOKEN=<handoff token> \\
        uv run python screen_fno_alltime_high_breakout.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from Options.dhan_client import dhan_wrapper

IST = ZoneInfo("Asia/Kolkata")
FROM_DATE = "2000-01-01"  # earlier than any current NSE F&O stock's listing
BREAKOUT_PCT = 1.0  # flag if latest close is at least this % above the prior ATH


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


def fetch_full_daily_series(symbol: str) -> Optional[dict]:
    df = dhan_wrapper.instruments()
    row = df[(df["SEM_TRADING_SYMBOL"] == symbol) & (df["SEM_EXM_EXCH_ID"] == "NSE")
             & (df["SEM_INSTRUMENT_NAME"] == "EQUITY") & (df["SEM_SERIES"] == "EQ")]
    if row.empty:
        return None
    security_id = str(int(row.iloc[0]["SEM_SMST_SECURITY_ID"]))
    to_date = datetime.now(IST).strftime("%Y-%m-%d")
    resp = dhan_wrapper.client.Dhan.historical_daily_data(
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=FROM_DATE, to_date=to_date,
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
    print(f"F&O universe: {len(universe)} stocks. Screening for >= {BREAKOUT_PCT}% all-time-high breakouts "
          f"(full history from {FROM_DATE})...\n")

    hits = []
    all_results = []
    failed = []
    for i, sym in enumerate(universe):
        try:
            data = fetch_full_daily_series(sym)
        except Exception as e:  # noqa: BLE001
            failed.append((sym, str(e)))
            continue
        time.sleep(0.15)
        if data is None:
            failed.append((sym, "no equity match or insufficient history"))
            continue

        closes, highs = data["close"], data["high"]
        latest_close = closes[-1]
        prior_ath = max(highs[:-1]) if len(highs) > 1 else None
        if not prior_ath:
            failed.append((sym, "insufficient prior history to establish an ATH"))
            continue
        pct_above = (latest_close - prior_ath) / prior_ath * 100.0
        row = {"symbol": sym, "latest_close": latest_close, "prior_ath": prior_ath,
               "pct_above": pct_above, "bars": data["bars"]}
        all_results.append(row)
        if pct_above >= BREAKOUT_PCT:
            hits.append(row)
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(universe)} done")

    print(f"\nScreened {len(universe) - len(failed)}/{len(universe)} stocks successfully "
          f"({len(failed)} failed/skipped).\n")

    hits.sort(key=lambda r: r["pct_above"], reverse=True)
    print("=" * 90)
    print(f"NSE F&O STOCKS >= {BREAKOUT_PCT}% ABOVE THEIR PRIOR ALL-TIME HIGH")
    print("=" * 90)
    print(f"{'Symbol':14s} {'LastClose':>10s} {'PriorATH':>10s} {'%AboveATH':>10s} {'Bars':>6s} {'HistoryFrom':>12s}")
    for r in hits:
        print(f"{r['symbol']:14s} {r['latest_close']:10.2f} {r['prior_ath']:10.2f} "
              f"{r['pct_above']:9.2f}% {r['bars']:6d}")
    if not hits:
        print("(none found)")

    all_results.sort(key=lambda r: r["pct_above"], reverse=True)
    print("\n" + "=" * 90)
    print("TOP 15 CLOSEST TO A NEW ALL-TIME HIGH (for context, regardless of threshold)")
    print("=" * 90)
    print(f"{'Symbol':14s} {'LastClose':>10s} {'PriorATH':>10s} {'%vsATH':>10s} {'Bars':>6s}")
    for r in all_results[:15]:
        print(f"{r['symbol']:14s} {r['latest_close']:10.2f} {r['prior_ath']:10.2f} "
              f"{r['pct_above']:9.2f}% {r['bars']:6d}")

    print(f"\nNote: 'Bars' shows how much real history each symbol actually has back to {FROM_DATE} - "
          f"a low bar count means a recent listing, not missing data.")
    if failed:
        print(f"\n{len(failed)} symbols skipped (no equity match, insufficient history, or fetch error).")


if __name__ == "__main__":
    main()
