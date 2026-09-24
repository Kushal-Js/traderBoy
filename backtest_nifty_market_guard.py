"""
Backtests nifty_market_guard.py against every real Options/Futures/
Luxury trade in history/*_real_trades.log (17 trading days available,
31 Aug - 24 Sep 2026 - real trade history doesn't go back further than
31 Aug, so this is the full window that exists, not a chosen 30).

Methodology:
  1. Fetch NIFTY50's own real daily OHLC (Dhan's historical_daily_data,
     the actual end-of-day bars, not an intraday reconstruction) for the
     real-trades window.
  2. For each trading day: prev_close/open from that real daily series,
     nifty_market_guard.classify_day() -> ALLOW_ALL / BLOCK_ALL - gap-
     down-only now (see that module's own docstring for the two 24 Sep
     corrections: first dropped a 3-tier "Luxury PE-only" middle ground,
     then dropped the 3-consecutive-falling-day trigger entirely - a gap
     UP never blocks anything, only a >=100pt gap DOWN does, nothing
     else considered).
  4. For every real trade opened on a BLOCK_ALL day, simulated pnl = 0
     (no trade ever placed, any strategy, any CE/PE). Allowed -> simulated
     pnl = real pnl (unchanged; this backtest does NOT re-simulate a
     different entry/exit, only block/no-block, same scope as
     backtest_reversal_filters_15day.py).

Read-only: dhan_wrapper.fetch... calls only, no order placement.

HOW TO RUN:
    DHAN_AUTH_MODE=access_token DHAN_ACCESS_TOKEN=<handoff token> \\
        uv run python backtest_nifty_market_guard.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from Options.dhan_client import dhan_wrapper
import nifty_market_guard as guard

IST = ZoneInfo("Asia/Kolkata")
UTC_TO_IST = timedelta(hours=5, minutes=30)


@dataclass
class Trade:
    strategy: str
    symbol: str
    option_type: str
    entry_price: float
    exit_price: float
    exit_reason: str
    pnl: float
    opened_at: datetime  # IST, naive
    trading_date: date


def load_trades() -> list[Trade]:
    trades: list[Trade] = []
    for path in sorted((REPO_ROOT / "history").glob("*_real_trades.log")):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("strategy") not in ("Options", "Futures", "Luxury"):
                continue
            if r.get("option_type") not in ("CE", "PE"):
                continue
            opened_at = datetime.fromisoformat(r["opened_at"]) + UTC_TO_IST
            trades.append(Trade(
                strategy=r["strategy"], symbol=r["underlying_symbol"], option_type=r["option_type"],
                entry_price=r["entry_price"], exit_price=r["exit_price"], exit_reason=r["exit_reason"],
                pnl=r["pnl"], opened_at=opened_at, trading_date=opened_at.date(),
            ))
    return trades


def fetch_nifty_daily(lookback_days: int = 60) -> dict[date, tuple[float, float]]:
    """{trading_date: (open, close)} per day, from Dhan's real daily bars
    for NIFTY (security_id=13, IDX_I/INDEX)."""
    dhan_wrapper.authenticate()
    to_date = datetime.now(IST).strftime("%Y-%m-%d")
    from_date = (datetime.now(IST) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    resp = dhan_wrapper.client.Dhan.historical_daily_data(
        security_id=dhan_wrapper.NIFTY_SECURITY_ID, exchange_segment="IDX_I", instrument_type="INDEX",
        from_date=from_date, to_date=to_date,
    )
    data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
    timestamps, opens, closes = data.get("timestamp") or [], data.get("open") or [], data.get("close") or []
    if not timestamps:
        raise RuntimeError(f"No NIFTY daily data returned: {resp}")
    out: dict[date, tuple[float, float]] = {}
    for ts, o, c in zip(timestamps, opens, closes):
        d = datetime.fromtimestamp(ts, tz=IST).date()
        out[d] = (o, c)
    return out


def build_day_states(daily: dict[date, tuple[float, float]]) -> dict[date, guard.NiftyDayState]:
    trading_days = sorted(daily.keys())
    states: dict[date, guard.NiftyDayState] = {}
    for i, d in enumerate(trading_days):
        if i == 0:
            continue  # no prior close to compute a gap from
        prev_close = daily[trading_days[i - 1]][1]
        today_open, _today_close = daily[d]
        states[d] = guard.NiftyDayState(trading_date=d, prev_close=prev_close, open=today_open)
    return states


def main() -> None:
    trades = load_trades()
    trade_dates = sorted(set(t.trading_date for t in trades))
    print(f"Loaded {len(trades)} real Options/Futures/Luxury trades across {len(trade_dates)} trading days "
          f"({trade_dates[0]} to {trade_dates[-1]}).\n")

    print("Fetching NIFTY's real daily OHLC...")
    daily = fetch_nifty_daily(lookback_days=60)
    print(f"Got {len(daily)} daily bars.\n")

    day_states = build_day_states(daily)

    print(f"{'Date':12s} {'PrevClose':>10s} {'Open':>10s} {'Gap':>8s} {'TIER':>10s}")
    print("-" * 55)
    tiers: dict[date, str] = {}
    for d in trade_dates:
        st = day_states.get(d)
        if st is None:
            print(f"{d} - no NIFTY daily bar available (holiday/data gap?) - treating as ALLOW_ALL")
            tiers[d] = "ALLOW_ALL"
            continue
        tier = guard.classify_day(st)
        tiers[d] = tier
        print(f"{str(d):12s} {st.prev_close:10.2f} {st.open:10.2f} {st.gap_points:+8.1f} {tier:>10s}")

    print("\n" + "=" * 100)
    print("TRADE-WISE RESULTS (only BLOCKED trades shown individually)")
    print("=" * 100)
    results = []
    for t in trades:
        tier = tiers.get(t.trading_date, "ALLOW_ALL")
        allowed = guard.allowed_for_trade(tier)
        sim_pnl = t.pnl if allowed else 0.0
        results.append({"trade": t, "tier": tier, "allowed": allowed, "sim_pnl": sim_pnl})

    blocked = [r for r in results if not r["allowed"]]
    for r in sorted(blocked, key=lambda r: r["trade"].trading_date):
        t = r["trade"]
        print(f"  BLOCKED  {str(t.trading_date):12s} {t.strategy:8s} {t.symbol:12s} {t.option_type} "
              f"real_pnl={t.pnl:9.2f}  {t.exit_reason}")

    print(f"\n{len(blocked)}/{len(results)} real trades would have been blocked by this guard.")

    print("\n" + "=" * 100)
    print("DAY-WISE PNL: real vs simulated (with guard applied)")
    print("=" * 100)
    print(f"{'Date':12s} {'Tier':>10s} {'#Trades':>8s} {'#Blocked':>9s} {'Real PnL':>12s} {'Sim PnL':>12s} {'Delta':>12s}")
    total_real, total_sim = 0.0, 0.0
    for d in trade_dates:
        day_results = [r for r in results if r["trade"].trading_date == d]
        real_pnl = sum(r["trade"].pnl for r in day_results)
        sim_pnl = sum(r["sim_pnl"] for r in day_results)
        n_blocked = sum(1 for r in day_results if not r["allowed"])
        total_real += real_pnl
        total_sim += sim_pnl
        marker = " <-- guard active (BLOCK_ALL)" if tiers.get(d, "ALLOW_ALL") == "BLOCK_ALL" else ""
        print(f"{str(d):12s} {tiers.get(d, 'ALLOW_ALL'):>10s} {len(day_results):8d} {n_blocked:9d} "
              f"{real_pnl:12.2f} {sim_pnl:12.2f} {sim_pnl - real_pnl:+12.2f}{marker}")

    print("-" * 90)
    print(f"{'TOTAL':12s} {'':>10s} {len(results):8d} {len(blocked):9d} {total_real:12.2f} {total_sim:12.2f} "
          f"{total_sim - total_real:+12.2f}")

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Real total P&L ({len(trade_dates)} days, {len(results)} trades): Rs.{total_real:.2f}")
    print(f"Simulated total P&L (guard applied):                    Rs.{total_sim:.2f}")
    print(f"Delta:                                                   {'+ ' if total_sim - total_real >= 0 else '- '}Rs.{abs(total_sim - total_real):.2f}")

    block_days = [d for d in trade_dates if tiers.get(d, "ALLOW_ALL") == "BLOCK_ALL"]
    print(f"\nDays where the guard would have activated (BLOCK_ALL): {len(block_days)}/{len(trade_dates)}")
    for d in block_days:
        st = day_states[d]
        print(f"  {d}: gap down {st.gap_down_abs:.1f}pts >= {guard.GAP_BLOCK_ABS:.0f}")


if __name__ == "__main__":
    main()
