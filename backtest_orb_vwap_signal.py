"""
Backtest: does an ORB (Opening Range Breakout) + VWAP + Volume confluence
score pick better-performing candidates than Swing's existing entry
trigger alone? User request 2 Sep 2026, the second of "explore a
different signal idea" after putting the HH/HL momentum-continuation
signal on hold (Relative Strength was the first, see backtest_relative_
strength.py - also no usable edge found).

Read-only, real Dhan data already cached (no new fetches needed - reuses
the same 5-min candle cache backtest_momentum_signal.py's first pass
already built, plus the SAME 782 real Swing entry-signal events already
found via find_entry_events, imported from backtest_momentum_signal_
tune.py - identical entry rule and forward-return definition to BOTH
prior backtests, so all three results are directly, apples-to-apples
comparable).

Methodology: at each real Swing entry-signal event, computes:
  - orb_breakout_score - does the entry bar's own close sit beyond that
    day's opening range high, and by how much (in ATR units)?
  - vwap_confluence_score - does the entry bar's own close sit above the
    day's own session VWAP so far, and by how much?
  - rvol_score (reused from momentum_signal.py) - the entry bar's own
    volume vs the historical average for that same time-of-day slot.
Combines via orb_vwap_composite_score, then runs the SAME evaluation as
both prior backtests: population correlation, score-tercile breakdown,
same-day horse race.

Sweeps OR_BARS (the opening-range window: 3, 6, 12 5-min bars = 15/30/60
minutes) since the sourced pattern didn't specify an exact window.

Run from the repo root: `uv run python backtest_orb_vwap_signal.py`
(assumes backtest_momentum_signal.py has already been run at least once
so its candle cache exists).
"""
from __future__ import annotations

import sys
from pathlib import Path
from statistics import mean, median

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

import Swing.orb_vwap_signal as ovs  # noqa: E402
import Swing.momentum_signal as ms  # noqa: E402
from backtest_momentum_signal_tune import find_entry_events, load_watchlist_symbols, pearson  # noqa: E402

OR_BARS_GRID = [3, 6, 12]  # 15 / 30 / 60 minutes of 5-min candles
ST_PERIOD = 10  # matches Swing's own SUPERTREND_PERIOD, only used here for ATR seeding length


def score_events_for_or_bars(all_symbol_data: dict, or_bars: int) -> list[dict]:
    scored = []
    for symbol, (candle_data, events) in all_symbol_data.items():
        highs, lows, closes, vols, timestamps = (
            candle_data["highs"], candle_data["lows"], candle_data["closes"],
            candle_data["volumes"], candle_data["timestamps"],
        )
        bounds = ovs.day_boundaries(timestamps)
        vwap = ovs.vwap_series(highs, lows, closes, vols, bounds)
        tr = ms.true_range_series(highs, lows, closes)

        # Map each bar index to its own day's start index, for opening-range lookup.
        day_start_for_index = {}
        for start, end in bounds:
            for i in range(start, end):
                day_start_for_index[i] = start

        for e in events:
            t = e["index"]
            day_start = day_start_for_index.get(t)
            if day_start is None:
                continue
            or_range = ovs.opening_range(highs, lows, day_start, or_bars)
            if or_range is None:
                continue
            or_high, _or_low = or_range
            atr = mean(tr[max(0, t - ST_PERIOD):t]) if t > 0 else 0.0
            orb = ovs.orb_breakout_score(closes[t], or_high, atr)
            vwap_score = ovs.vwap_confluence_score(closes[t], vwap[t], atr)
            rvol = ms.rvol_score(vols, t, bars_per_day=75)
            score = ovs.orb_vwap_composite_score(orb, vwap_score, rvol)
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
    all_symbol_data = {}
    for sym in symbols:
        result = find_entry_events(sym)
        if result is not None:
            all_symbol_data[sym] = result
    total = sum(len(events) for _, events in all_symbol_data.values())
    print(f"[orb-vwap-backtest] {len(all_symbol_data)}/{len(symbols)} symbols with cached data, "
          f"{total} total entry-signal events (identical set to the other two backtests)\n")

    print(f"{'OR_bars':<10}{'(mins)':<8}{'n':<6}{'corr':<9}{'Low avg%':<10}{'Low win%':<10}"
          f"{'High avg%':<11}{'High win%':<11}{'HorseRace%':<12}{'(n days)':<9}")
    results = []
    for or_bars in OR_BARS_GRID:
        scored = score_events_for_or_bars(all_symbol_data, or_bars)
        stats = evaluate(scored)
        stats["or_bars"] = or_bars
        results.append(stats)
        if "corr" not in stats:
            print(f"{or_bars:<10}{or_bars * 5:<8}{stats['n']:<6} -- too few scored entries (n<30) --")
            continue
        hr = f"{stats['horse_race_win_pct']:.1f}" if stats["horse_race_win_pct"] is not None else "n/a"
        print(f"{or_bars:<10}{or_bars * 5:<8}{stats['n']:<6}{stats['corr']:<+9.3f}"
              f"{stats['low']['avg_return']:<+10.2f}{stats['low']['win_rate']:<10.1f}"
              f"{stats['high']['avg_return']:<+11.2f}{stats['high']['win_rate']:<11.1f}{hr:<12}{stats['horse_race_n']:<9}")

    valid = [r for r in results if "corr" in r]
    if valid:
        best = max(valid, key=lambda r: r["corr"])
        print(f"\n=== Best by correlation: OR_bars={best['or_bars']} ({best['or_bars']*5} min) "
              f"(corr={best['corr']:+.3f}) ===")
        for name in ("low", "mid", "high"):
            b = best[name]
            print(f"{name.capitalize():<6} tercile: n={b['n']:<4} avg_return={b['avg_return']:+.2f}%  "
                  f"median_return={b['median_return']:+.2f}%  win_rate={b['win_rate']:.1f}%  "
                  f"avg_winner={b['avg_winner']:+.2f}%  avg_loser={b['avg_loser']:+.2f}%")
        if best["horse_race_win_pct"] is not None:
            print(f"Same-day horse race: {best['horse_race_win_pct']:.1f}% (n={best['horse_race_n']} days)")


if __name__ == "__main__":
    main()
