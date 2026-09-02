"""
Backtest: does Relative-Strength-vs-NIFTY ranking pick better-performing
candidates than Swing's existing entry trigger alone? User request 2 Sep
2026, after putting the HH/HL momentum-continuation signal on hold:
"explore a different signal idea" - "Relative Strength ranking vs Nifty."

Read-only, real Dhan data, no order placement - same discipline every
other backtest script in this repo follows. Reuses the SAME real Swing
entry-signal events already found and cached by backtest_momentum_
signal.py's first pass (find_entry_events, imported from backtest_
momentum_signal_tune.py - identical entry rule, identical forward-return
definition, so this result is directly comparable to the HH/HL signal's
own numbers on an apples-to-apples basis) - only ONE new fetch is needed
here: NIFTY's own daily OHLC, to serve as the benchmark.

Methodology:
1. Same 792 real Swing entry-signal events as the HH/HL backtest (5-min/
   1-min Supertrend crossover + price-confirmation gate, replicated
   exactly from production).
2. At each entry event, score the underlying stock's own trailing
   N-trading-day return MINUS NIFTY's own trailing N-day return over the
   SAME window, ending at the trading day immediately BEFORE the entry's
   own calendar date (never today's own still-forming day - same
   point-in-time discipline production's _fetch_daily_closes_once
   already follows for its "yesterday's close" gate).
3. No lookahead: only closes up to and including "yesterday" (relative
   to the entry) are ever used.
4. Sweeps lookback_days in {10, 20, 40} - ONE parameter, not four -
   deliberately simpler than the HH/HL composite (see Swing/
   relative_strength.py's own docstring for why).
5. Same evaluation as the HH/HL backtest for direct comparability:
   population correlation (score vs forward return), score-tercile
   breakdown (avg/median return, win rate, winner/loser magnitude), and
   the same-day "horse race" (does the higher-RS candidate actually
   outperform the lower-RS one on days both fire).

Run from the repo root: `uv run python backtest_relative_strength.py`
(assumes backtest_momentum_signal.py has already been run at least once
so its candle cache exists under CACHE_DIR - this script reuses that
cache rather than re-fetching the 22 watchlist symbols' own candles).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from Options.dhan_client import dhan_wrapper, _retry  # noqa: E402
import Swing.relative_strength as rs  # noqa: E402
from backtest_momentum_signal_tune import find_entry_events, load_watchlist_symbols, pearson  # noqa: E402

CACHE_DIR = Path("/private/tmp/claude-501/-Users-kushalgaur-Desktop-projects-trading-traderBoy/"
                  "60a0e686-2110-4e20-bad5-fe817a57a72a/scratchpad/momentum_backtest_cache")
NIFTY_CACHE_FILE = CACHE_DIR / "NIFTY_daily.json"
DAYS_BACK = 90
LOOKBACK_GRID = [10, 20, 40]


def fetch_nifty_daily_cached() -> dict:
    if NIFTY_CACHE_FILE.exists():
        return json.loads(NIFTY_CACHE_FILE.read_text())
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    resp = _retry(
        dhan_wrapper.client.Dhan.historical_daily_data,
        security_id="13", exchange_segment="IDX_I", instrument_type="INDEX",
        from_date=from_date, to_date=to_date,
    )
    data = resp.get("data") or {}
    result = {"closes": data.get("close") or [], "timestamps": data.get("timestamp") or []}
    NIFTY_CACHE_FILE.write_text(json.dumps(result))
    time.sleep(1.0)
    return result


def resample_daily_closes(timestamps: list[int], closes: list[float]) -> tuple[list[str], list[float]]:
    """Same-day-last-close resample from 5-min candles into a plain
    (dates, closes) daily series, sorted chronologically - shared shape
    with backtest_momentum_signal.py's own resample_daily_prev_close,
    just returning the full series here instead of only "yesterday's"
    value per day, since Relative Strength needs the whole trailing
    window, not just one prior value."""
    day_last_close: dict[str, float] = {}
    order: list[str] = []
    for ts, close in zip(timestamps, closes):
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if day not in day_last_close:
            order.append(day)
        day_last_close[day] = close
    return order, [day_last_close[d] for d in order]


def load_cached_5min(symbol: str) -> dict | None:
    f = CACHE_DIR / f"{symbol}_5min.json"
    return json.loads(f.read_text()) if f.exists() else None


def score_events_for_lookback(all_events_by_symbol: dict, nifty_dates: list[str], nifty_closes: list[float],
                               lookback_days: int) -> list[dict]:
    import bisect
    scored = []
    for symbol, events in all_events_by_symbol.items():
        d5 = load_cached_5min(symbol)
        if d5 is None:
            continue
        stock_dates, stock_closes = resample_daily_closes(d5["timestamps"], d5["closes"])
        dates, stock_aligned, nifty_aligned = rs.align_series_by_date(stock_dates, stock_closes, nifty_dates, nifty_closes)
        if len(dates) < lookback_days + 2:
            continue
        for e in events:
            # "Yesterday" relative to the entry's own calendar date - the
            # last index in `dates` strictly before entry_date.
            idx = bisect.bisect_left(dates, e["entry_date"]) - 1
            if idx < 0:
                continue
            score = rs.relative_strength_score(stock_aligned, nifty_aligned, as_of_index=idx, lookback_days=lookback_days)
            if score is None:
                continue
            scored.append({**e, "score": score})
    return scored


def evaluate(scored: list[dict]) -> dict:
    if len(scored) < 30:
        return {"n": len(scored)}
    ranked = sorted(scored, key=lambda e: e["score"])
    tercile = len(ranked) // 3
    low, mid, high = ranked[:tercile], ranked[tercile:2 * tercile], ranked[2 * tercile:]

    def bucket_stats(bucket):
        rets = [e["forward_return_pct"] for e in bucket]
        winners = [r for r in rets if r > 0]
        losers = [r for r in rets if r <= 0]
        return {
            "n": len(bucket), "avg_return": mean(rets), "median_return": median(rets),
            "win_rate": 100 * len(winners) / len(bucket) if bucket else 0.0,
            "avg_winner": mean(winners) if winners else 0.0,
            "avg_loser": mean(losers) if losers else 0.0,
        }

    scores = [e["score"] for e in scored]
    returns = [e["forward_return_pct"] for e in scored]
    corr = pearson(scores, returns)

    by_date: dict[str, list[dict]] = {}
    for e in scored:
        by_date.setdefault(e["entry_date"], []).append(e)
    pair_wins = pair_total = 0
    for es in by_date.values():
        if len(es) < 2:
            continue
        es_sorted = sorted(es, key=lambda e: e["score"], reverse=True)
        best, worst = es_sorted[0], es_sorted[-1]
        if best["symbol"] == worst["symbol"]:
            continue
        pair_total += 1
        if best["forward_return_pct"] >= worst["forward_return_pct"]:
            pair_wins += 1

    return {
        "n": len(scored), "corr": corr,
        "low": bucket_stats(low), "mid": bucket_stats(mid), "high": bucket_stats(high),
        "horse_race_win_pct": 100 * pair_wins / pair_total if pair_total else None,
        "horse_race_n": pair_total,
    }


def main():
    symbols = load_watchlist_symbols()
    print(f"[rs-backtest] {len(symbols)} watchlist symbols\n")

    all_events_by_symbol = {}
    for sym in symbols:
        result = find_entry_events(sym)
        if result is not None:
            _, events = result
            all_events_by_symbol[sym] = events
    total = sum(len(v) for v in all_events_by_symbol.values())
    print(f"[rs-backtest] {len(all_events_by_symbol)}/{len(symbols)} symbols with cached data, "
          f"{total} total entry-signal events (identical set to the HH/HL backtest)\n")

    nifty = fetch_nifty_daily_cached()
    nifty_dates, nifty_closes = resample_daily_closes(nifty["timestamps"], nifty["closes"])
    print(f"[rs-backtest] NIFTY daily series: {len(nifty_dates)} trading days "
          f"({nifty_dates[0] if nifty_dates else '?'} to {nifty_dates[-1] if nifty_dates else '?'})\n")

    print(f"{'lookback_days':<16}{'n':<6}{'corr':<9}{'Low avg%':<10}{'Low win%':<10}"
          f"{'High avg%':<11}{'High win%':<11}{'HorseRace%':<12}{'(n days)':<9}")
    results = []
    for lookback_days in LOOKBACK_GRID:
        scored = score_events_for_lookback(all_events_by_symbol, nifty_dates, nifty_closes, lookback_days)
        stats = evaluate(scored)
        stats["lookback_days"] = lookback_days
        results.append(stats)
        if "corr" not in stats:
            print(f"{lookback_days:<16}{stats['n']:<6} -- too few scored entries (n<30) --")
            continue
        hr = f"{stats['horse_race_win_pct']:.1f}" if stats["horse_race_win_pct"] is not None else "n/a"
        print(f"{lookback_days:<16}{stats['n']:<6}{stats['corr']:<+9.3f}"
              f"{stats['low']['avg_return']:<+10.2f}{stats['low']['win_rate']:<10.1f}"
              f"{stats['high']['avg_return']:<+11.2f}{stats['high']['win_rate']:<11.1f}{hr:<12}{stats['horse_race_n']:<9}")

    valid = [r for r in results if "corr" in r]
    if valid:
        best = max(valid, key=lambda r: r["corr"])
        print(f"\n=== Best by correlation: lookback_days={best['lookback_days']} (corr={best['corr']:+.3f}) ===")
        for name in ("low", "mid", "high"):
            b = best[name]
            print(f"{name.capitalize():<6} tercile: n={b['n']:<4} avg_return={b['avg_return']:+.2f}%  "
                  f"median_return={b['median_return']:+.2f}%  win_rate={b['win_rate']:.1f}%  "
                  f"avg_winner={b['avg_winner']:+.2f}%  avg_loser={b['avg_loser']:+.2f}%")
        if best["horse_race_win_pct"] is not None:
            print(f"Same-day horse race: {best['horse_race_win_pct']:.1f}% (n={best['horse_race_n']} days)")

        best_hr = max(valid, key=lambda r: (r["horse_race_win_pct"] or -1, r["horse_race_n"]))
        if best_hr["lookback_days"] != best["lookback_days"]:
            print(f"\n=== Best by same-day horse race: lookback_days={best_hr['lookback_days']} "
                  f"(horse_race={best_hr['horse_race_win_pct']:.1f}%, corr={best_hr['corr']:+.3f}) ===")


if __name__ == "__main__":
    main()
