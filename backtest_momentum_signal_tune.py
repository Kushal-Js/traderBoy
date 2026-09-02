"""
Parameter sweep for the HH/HL momentum-continuation composite score -
user request 2 Sep 2026 follow-up: "tune the parameters and re-test."

Reuses the exact same real Dhan candle data already cached by
backtest_momentum_signal.py's first pass (no new network calls - the
entry-signal set itself, which candles fired Swing's real production
Supertrend-crossover entry rule and what each one's forward return was,
is entirely independent of the momentum-score's own tunable parameters,
so it's computed ONCE per symbol here and reused across every
configuration in the sweep). Run backtest_momentum_signal.py first if
the cache under CACHE_DIR doesn't exist yet.

Sweeps:
  - fractal k (2, 3, 4) - how many bars on each side confirm a swing
    point (user's own note: "this can further be optimized also").
  - (coil_bars, baseline_bars) - the pre-breakout contraction window vs
    its baseline.
  - four weight profiles (default / coil-heavy / RVOL-heavy /
    freshness-heavy) - which component the composite score leans on.

For each of the 3*3*4=36 configurations: recomputes HH/HL breakouts and
every score component fresh (k changes which bars even ARE swing points,
so breakouts genuinely differ per k - not just a re-weighting exercise),
then reports the score-vs-forward-return correlation, the same-day
horse-race win rate, and (new in this pass, per the first backtest's own
recommended next step) each tercile's WINNER/LOSER magnitude split, not
just win rate and mean - a trend-following signal can carry real edge a
bare win-rate comparison hides.

Run from the repo root: `uv run python backtest_momentum_signal_tune.py`
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from Options.dhan_client import _compute_supertrend  # noqa: E402
from Swing import config as swing_config  # noqa: E402
import Swing.momentum_signal as ms  # noqa: E402

CACHE_DIR = Path("/private/tmp/claude-501/-Users-kushalgaur-Desktop-projects-trading-traderBoy/"
                  "60a0e686-2110-4e20-bad5-fe817a57a72a/scratchpad/momentum_backtest_cache")

ST_PERIOD = swing_config.SUPERTREND_PERIOD
ST_MULT = swing_config.SUPERTREND_MULTIPLIER
ENTRY_TF = swing_config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES
CONFIRM_TF = swing_config.SUPERTREND_CONFIRM_TIMEFRAME_MINUTES
BARS_PER_DAY_5MIN = 75
MAX_HOLD_BARS = 150

K_GRID = [2, 3, 4]
COIL_BASELINE_GRID = [(12, 60), (8, 40), (16, 80)]
WEIGHT_PROFILES = {
    "default":       {"coil": 0.30, "freshness": 0.25, "rvol": 0.25, "extension": 0.20},
    "coil_heavy":    {"coil": 0.50, "freshness": 0.15, "rvol": 0.25, "extension": 0.10},
    "rvol_heavy":    {"coil": 0.20, "freshness": 0.15, "rvol": 0.50, "extension": 0.15},
    "fresh_heavy":   {"coil": 0.20, "freshness": 0.45, "rvol": 0.20, "extension": 0.15},
}


def load_watchlist_symbols() -> list[str]:
    path = REPO_ROOT / "data" / "watchlist"
    return [line.strip().partition(",")[0].strip().upper()
            for line in path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")]


def load_cached(symbol: str, interval: int) -> dict | None:
    f = CACHE_DIR / f"{symbol}_{interval}min.json"
    if not f.exists():
        return None
    return json.loads(f.read_text())


def find_entry_events(symbol: str) -> tuple[dict, list[dict]] | None:
    """Independent of any momentum-score parameter - the real Swing
    production entry rule (price-confirmation + 5-min crossed-above ST +
    1-min at/crossed-above ST) plus forward return to the Supertrend-
    reversal exit. Returns (candle_data_for_scoring, entry_events) or
    None if this symbol's cache is missing/too small."""
    d5 = load_cached(symbol, ENTRY_TF)
    d1 = load_cached(symbol, CONFIRM_TF)
    if not d5 or not d1:
        return None
    highs5, lows5, closes5, vols5, ts5 = d5["highs"], d5["lows"], d5["closes"], d5["volumes"], d5["timestamps"]
    highs1, lows1, closes1, ts1 = d1["highs"], d1["lows"], d1["closes"], d1["timestamps"]
    if len(closes5) < ST_PERIOD + 90 or len(closes1) < ST_PERIOD + 2:
        return None

    st5 = _compute_supertrend(highs5, lows5, closes5, period=ST_PERIOD, multiplier=ST_MULT)
    st1 = _compute_supertrend(highs1, lows1, closes1, period=ST_PERIOD, multiplier=ST_MULT)

    day_last_close: dict[str, float] = {}
    order: list[str] = []
    for ts, close in zip(ts5, closes5):
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if day not in day_last_close:
            order.append(day)
        day_last_close[day] = close
    prev_close_for_day = {order[i]: day_last_close[order[i - 1]] for i in range(1, len(order))}

    import bisect

    def nearest_1min(target_ts):
        idx = bisect.bisect_right(ts1, target_ts) - 1
        return idx if idx >= 0 else None

    latch: dict[str, bool] = {}
    n = len(closes5)
    events = []
    for t in range(max(ST_PERIOD + 2, 81), n):
        if st5[t] is None or st5[t - 1] is None:
            continue
        if not (closes5[t - 1] <= st5[t - 1] and closes5[t] > st5[t]):
            continue
        day = datetime.fromtimestamp(ts5[t]).strftime("%Y-%m-%d")
        prev_close = prev_close_for_day.get(day)
        if prev_close is None:
            continue
        latched = latch.get(day, False) or closes5[t] >= prev_close
        latch[day] = latched
        if not latched:
            continue
        one_min_idx = nearest_1min(ts5[t])
        if one_min_idx is None or st1[one_min_idx] is None:
            continue
        if not (closes1[one_min_idx] > st1[one_min_idx]):
            continue

        exit_idx = None
        for j in range(t + 1, min(n, t + 1 + MAX_HOLD_BARS)):
            if st5[j] is not None and st5[j - 1] is not None and closes5[j - 1] >= st5[j - 1] and closes5[j] < st5[j]:
                exit_idx = j
                break
        if exit_idx is None:
            exit_idx = min(n - 1, t + MAX_HOLD_BARS)
        forward_return_pct = (closes5[exit_idx] - closes5[t]) / closes5[t] * 100.0
        events.append({"symbol": symbol, "index": t, "entry_date": day, "forward_return_pct": forward_return_pct})

    candle_data = {"highs": highs5, "lows": lows5, "closes": closes5, "volumes": vols5}
    return candle_data, events


def score_events(all_symbol_data: dict, k: int, coil_bars: int, baseline_bars: int, weights: dict) -> list[dict]:
    """Re-detects HH/HL breakouts (k affects WHICH bars are swing points,
    so this genuinely differs per k, not just re-weighting the same
    breakouts) and re-scores every entry event for one configuration."""
    scored = []
    for symbol, (candle_data, events) in all_symbol_data.items():
        highs, lows, closes, vols = candle_data["highs"], candle_data["lows"], candle_data["closes"], candle_data["volumes"]
        breakouts = ms.detect_hh_hl_breakouts(highs, lows, closes, k=k)
        breakout_by_index = {b.index: b for b in breakouts}
        sorted_idx = sorted(breakout_by_index.keys())
        import bisect

        def most_recent_breakout(t):
            pos = bisect.bisect_right(sorted_idx, t) - 1
            return breakout_by_index[sorted_idx[pos]] if pos >= 0 else None

        tr = ms.true_range_series(highs, lows, closes)
        for e in events:
            t = e["index"]
            breakout = most_recent_breakout(t)
            if breakout is None:
                continue
            coil = ms.coil_tightness_score(highs, lows, closes, breakout.index, coil_bars=coil_bars, baseline_bars=baseline_bars)
            rvol = ms.rvol_score(vols, breakout.index, bars_per_day=BARS_PER_DAY_5MIN)
            fresh = ms.freshness_score(breakout.index, t, decay_bars=BARS_PER_DAY_5MIN)
            atr_at_breakout = mean(tr[max(0, breakout.index - ST_PERIOD):breakout.index]) if breakout.index > 0 else 0.0
            ext = ms.extension_score(breakout.pivot_price, closes[t], atr_at_breakout)
            score = ms.composite_momentum_score(coil, fresh, rvol, ext, weights=weights)
            if score is None:
                continue
            scored.append({**e, "score": score})
    return scored


def pearson(xs, ys) -> float:
    mx, my = mean(xs), mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    return cov / (sx * sy) if sx and sy else 0.0


def evaluate_config(scored: list[dict]) -> dict:
    if len(scored) < 30:
        return {"n": len(scored)}
    ranked = sorted(scored, key=lambda e: e["score"])
    tercile = len(ranked) // 3
    low, high = ranked[:tercile], ranked[2 * tercile:]

    def bucket_stats(bucket):
        rets = [e["forward_return_pct"] for e in bucket]
        winners = [r for r in rets if r > 0]
        losers = [r for r in rets if r <= 0]
        return {
            "n": len(bucket), "avg_return": mean(rets),
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
        "low": bucket_stats(low), "high": bucket_stats(high),
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
    total_events = sum(len(events) for _, events in all_symbol_data.values())
    print(f"[tune] Loaded cached data for {len(all_symbol_data)}/{len(symbols)} symbols, "
          f"{total_events} total entry-signal events (same set for every configuration below)\n")

    results = []
    for k in K_GRID:
        for coil_bars, baseline_bars in COIL_BASELINE_GRID:
            for weight_name, weights in WEIGHT_PROFILES.items():
                scored = score_events(all_symbol_data, k, coil_bars, baseline_bars, weights)
                stats = evaluate_config(scored)
                stats.update({"k": k, "coil_bars": coil_bars, "baseline_bars": baseline_bars, "weights": weight_name})
                results.append(stats)

    print(f"{'k':<3}{'coil/base':<11}{'weights':<14}{'n':<6}{'corr':<9}"
          f"{'Low avg%':<10}{'Low win%':<10}{'High avg%':<11}{'High win%':<11}{'HorseRace%':<12}{'(n days)':<9}")
    for r in sorted(results, key=lambda r: r.get("corr", -999), reverse=True):
        coil_base = f"{r['coil_bars']}/{r['baseline_bars']}"
        if "corr" not in r:
            print(f"{r['k']:<3}{coil_base:<11}{r['weights']:<14}{r['n']:<6} -- too few scored entries (n<30) --")
            continue
        hr = f"{r['horse_race_win_pct']:.1f}" if r["horse_race_win_pct"] is not None else "n/a"
        print(f"{r['k']:<3}{coil_base:<11}{r['weights']:<14}{r['n']:<6}"
              f"{r['corr']:<+9.3f}{r['low']['avg_return']:<+10.2f}{r['low']['win_rate']:<10.1f}"
              f"{r['high']['avg_return']:<+11.2f}{r['high']['win_rate']:<11.1f}{hr:<12}{r['horse_race_n']:<9}")

    def print_best(label, chosen):
        print(f"\n=== Best configuration by {label}: k={chosen['k']}, "
              f"coil/baseline={chosen['coil_bars']}/{chosen['baseline_bars']}, "
              f"weights={chosen['weights']} (corr={chosen['corr']:+.3f}) ===")
        print(f"Low tercile:  n={chosen['low']['n']}  avg_return={chosen['low']['avg_return']:+.2f}%  "
              f"win_rate={chosen['low']['win_rate']:.1f}%  avg_winner={chosen['low']['avg_winner']:+.2f}%  "
              f"avg_loser={chosen['low']['avg_loser']:+.2f}%")
        print(f"High tercile: n={chosen['high']['n']}  avg_return={chosen['high']['avg_return']:+.2f}%  "
              f"win_rate={chosen['high']['win_rate']:.1f}%  avg_winner={chosen['high']['avg_winner']:+.2f}%  "
              f"avg_loser={chosen['high']['avg_loser']:+.2f}%")
        if chosen["horse_race_win_pct"] is not None:
            print(f"Same-day horse race: {chosen['horse_race_win_pct']:.1f}% (n={chosen['horse_race_n']} days)")

    valid = [r for r in results if "corr" in r]
    print_best("correlation", max(valid, key=lambda r: r["corr"]))
    print_best("same-day horse race", max(valid, key=lambda r: (r["horse_race_win_pct"] or -1, r["horse_race_n"])))


if __name__ == "__main__":
    main()
