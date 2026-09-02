"""
Backtest: does the HH/HL momentum-continuation composite score
(Swing/momentum_signal.py) actually pick better-performing candidates
than Swing's existing entry trigger alone? User request 2 Sep 2026:
"build and backtest a logic... additional pre-filter/context layer...
re-rank them (soft signal)... show me results first."

Read-only. Only ever calls Dhan's historical intraday-candle REST
endpoint via the already-authenticated Options.dhan_client.dhan_wrapper
(the same single connection every package in this codebase shares) -
never touches order_placement/place_market_order/cancel_order, same
discipline bt_common.py already follows.

--------------------------------------------------------------------------
Methodology (read before trusting any number this prints)
--------------------------------------------------------------------------
1. Universe: every symbol currently in data/watchlist (22 as of 2 Sep
   2026), the exact set Swing actually watches live.
2. Data: Dhan's real 5-min AND 1-min intraday candles, ~90 calendar days
   back (Dhan's own actual limit for this account, confirmed empirically
   - the SDK's own docstring claim of "last 5 trading days" for 1-min
   data is WRONG, it goes back the same ~90 days as 5-min). Cached to
   local JSON on first fetch so re-running this script doesn't re-hit
   Dhan or burn the rate limit a second time.
3. "Daily" context (previous close, for the price-confirmation gate) is
   DERIVED by resampling the fetched 5-min candles into daily OHLC,
   NOT fetched from Dhan's own historical_daily_data endpoint. Initially
   avoided because that endpoint appeared to reject every request during
   this backtest's own development - CORRECTED same day: that was a
   stale local access token producing a misleading DH-905 "bad
   parameters" error rather than a genuine endpoint problem: re-verified
   directly against the live traderBoy droplet with a fresh token and
   it returned a clean, correct daily-candle result, and Swing's own
   live daily watchlist prune (_fetch_daily_closes_once, same endpoint)
   is unaffected - see trading-skills' learnings/dhan-charts-historical-
   endpoint-broken.md for the full corrected writeup. Resampling from
   5-min data sidesteps the question entirely either way and is
   arguably more consistent (one data source, ticks
   line up exactly) - not a compromise made to save time.
4. Entry trigger replicated EXACTLY as production defines it
   (Swing/trading_engine.py._evaluate_watchlist_entry_signal): price
   confirmed >= previous day's close (latches once true for the day),
   5-min close CROSSED ABOVE the 5-min Supertrend, AND the most recent
   1-min bar is at-or-crossed-above its own 1-min Supertrend. Same
   SUPERTREND_PERIOD/MULTIPLIER Swing actually runs live.
5. At every such entry event, this script separately checks: was there
   a confirmed HH/HL breakout (Swing/momentum_signal.detect_hh_hl_
   breakouts) at or before this same bar, and if so how long ago? If
   yes, the full composite score (coil tightness, freshness, RVOL,
   extension) is computed from that breakout's own context. A LOT of
   real Supertrend crossovers will have NO recent HH/HL breakout behind
   them at all - that's expected and itself an interesting result (how
   much of live entries this signal can even speak to), not a bug.
6. "Forward return" = the underlying's own % price move from the entry
   bar's close to the bar where the SAME Supertrend-reversal exit rule
   fires (5-min close crosses below Supertrend), capped at
   MAX_HOLD_BARS bars. This is a proxy for setup quality via the
   UNDERLYING's own move, not a full options/futures P&L simulation
   (margin, ATM strike selection, slippage, the PE-hedge swap mechanics
   basket_hedge mode actually uses) - deliberately kept simple for this
   first exploratory pass, per the user's own "show me results first,
   then we'll think about deploying." A promising signal here justifies
   the heavier work of simulating real option/futures P&L before any
   live decision; a null result here saves that work.
7. No lookahead: every score is computed using only bars up to and
   including the entry bar itself. The k-bar fractal confirmation lag is
   respected throughout (Swing/momentum_signal.py's own docstrings cover
   this in detail).

Run from the repo root: `uv run python backtest_momentum_signal.py`
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median, pstdev

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from Options.dhan_client import dhan_wrapper, _compute_supertrend, _retry  # noqa: E402
from Swing import config as swing_config  # noqa: E402
import Swing.momentum_signal as ms  # noqa: E402

CACHE_DIR = Path("/private/tmp/claude-501/-Users-kushalgaur-Desktop-projects-trading-traderBoy/"
                  "60a0e686-2110-4e20-bad5-fe817a57a72a/scratchpad/momentum_backtest_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DAYS_BACK = 90
ST_PERIOD = swing_config.SUPERTREND_PERIOD
ST_MULT = swing_config.SUPERTREND_MULTIPLIER
ENTRY_TF = swing_config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES  # 5
CONFIRM_TF = swing_config.SUPERTREND_CONFIRM_TIMEFRAME_MINUTES  # 1
FRACTAL_K = 2
COIL_BARS = 12
BASELINE_BARS = 60
BARS_PER_DAY_5MIN = 75  # 09:15-15:30 = 375 min / 5
MAX_HOLD_BARS = 150  # ~2 trading days at 5-min bars - bounds outliers, still lets a real swing move play out

print(f"[backtest] ST_PERIOD={ST_PERIOD} ST_MULT={ST_MULT} ENTRY_TF={ENTRY_TF} CONFIRM_TF={CONFIRM_TF} "
      f"FRACTAL_K={FRACTAL_K} COIL_BARS={COIL_BARS} BASELINE_BARS={BASELINE_BARS} MAX_HOLD_BARS={MAX_HOLD_BARS}")


def load_watchlist_symbols() -> list[str]:
    path = REPO_ROOT / "data" / "watchlist"
    symbols = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        symbols.append(line.partition(",")[0].strip().upper())
    return symbols


def fetch_candles_cached(symbol: str, interval: int) -> dict:
    cache_file = CACHE_DIR / f"{symbol}_{interval}min.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    security_id = dhan_wrapper._equity_security_id(symbol)
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    resp = _retry(
        dhan_wrapper.client.Dhan.intraday_minute_data,
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=from_date, to_date=to_date, interval=interval,
    )
    data = resp.get("data") or {}
    result = {
        "highs": data.get("high") or [], "lows": data.get("low") or [],
        "closes": data.get("close") or [], "volumes": data.get("volume") or [],
        "timestamps": data.get("timestamp") or [],
    }
    cache_file.write_text(json.dumps(result))
    time.sleep(1.5)  # be gentle with Dhan's rate limit across ~44 calls
    return result


def resample_daily_prev_close(timestamps: list[int], closes: list[float]) -> dict[str, float]:
    """Maps each trading-day date string -> that day's PREVIOUS day's
    close, derived purely from the 5-min series itself (see module
    docstring point 3 for why). The very first day in the series has no
    prior day and is simply absent from the returned dict - callers
    treat that day as "not entry-eligible yet" (same "not enough
    history" fails-open shape used everywhere else in this codebase)."""
    day_last_close: dict[str, float] = {}
    order: list[str] = []
    for ts, close in zip(timestamps, closes):
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if day not in day_last_close:
            order.append(day)
        day_last_close[day] = close
    prev_close_for_day = {}
    for i in range(1, len(order)):
        prev_close_for_day[order[i]] = day_last_close[order[i - 1]]
    return prev_close_for_day


def nearest_1min_index_at_or_before(one_min_ts: list[int], target_ts: int) -> int | None:
    """1-min timestamps are sorted ascending - binary search would be
    faster but this dataset is small enough (~23k bars) that a simple
    scan-from-cache-pointer per symbol is plenty fast in practice; kept
    as a plain bisect for clarity and correctness."""
    import bisect
    idx = bisect.bisect_right(one_min_ts, target_ts) - 1
    return idx if idx >= 0 else None


def backtest_symbol(symbol: str) -> list[dict]:
    d5 = fetch_candles_cached(symbol, ENTRY_TF)
    d1 = fetch_candles_cached(symbol, CONFIRM_TF)
    highs5, lows5, closes5, vols5, ts5 = d5["highs"], d5["lows"], d5["closes"], d5["volumes"], d5["timestamps"]
    highs1, lows1, closes1, ts1 = d1["highs"], d1["lows"], d1["closes"], d1["timestamps"]
    if len(closes5) < ST_PERIOD + BASELINE_BARS + 10 or len(closes1) < ST_PERIOD + 2:
        print(f"  {symbol}: not enough data (5min={len(closes5)}, 1min={len(closes1)}) - skipping")
        return []

    st5 = _compute_supertrend(highs5, lows5, closes5, period=ST_PERIOD, multiplier=ST_MULT)
    st1 = _compute_supertrend(highs1, lows1, closes1, period=ST_PERIOD, multiplier=ST_MULT)
    prev_close_for_day = resample_daily_prev_close(ts5, closes5)
    breakouts = ms.detect_hh_hl_breakouts(highs5, lows5, closes5, k=FRACTAL_K)
    breakout_by_index = {b.index: b for b in breakouts}
    sorted_breakout_indices = sorted(breakout_by_index.keys())

    def most_recent_breakout_at_or_before(t: int):
        import bisect
        pos = bisect.bisect_right(sorted_breakout_indices, t) - 1
        if pos < 0:
            return None
        return breakout_by_index[sorted_breakout_indices[pos]]

    price_confirmed_latch: dict[str, bool] = {}
    entries = []
    n = len(closes5)

    for t in range(max(ST_PERIOD + 2, BASELINE_BARS + 1), n):
        if st5[t] is None or st5[t - 1] is None:
            continue
        crossed_above_5min = closes5[t - 1] <= st5[t - 1] and closes5[t] > st5[t]
        if not crossed_above_5min:
            continue

        day = datetime.fromtimestamp(ts5[t]).strftime("%Y-%m-%d")
        prev_close = prev_close_for_day.get(day)
        if prev_close is None:
            continue
        latched = price_confirmed_latch.get(day, False) or closes5[t] >= prev_close
        price_confirmed_latch[day] = latched
        if not latched:
            continue

        one_min_idx = nearest_1min_index_at_or_before(ts1, ts5[t])
        if one_min_idx is None or st1[one_min_idx] is None:
            continue
        confirm_ok = closes1[one_min_idx] > st1[one_min_idx]
        if not confirm_ok:
            continue

        # --- Real Swing entry signal fires at bar t. Now score it. ---
        breakout = most_recent_breakout_at_or_before(t)
        score = coil = fresh = rvol = ext = None
        if breakout is not None:
            coil = ms.coil_tightness_score(highs5, lows5, closes5, breakout.index,
                                            coil_bars=COIL_BARS, baseline_bars=BASELINE_BARS)
            rvol = ms.rvol_score(vols5, breakout.index, bars_per_day=BARS_PER_DAY_5MIN)
            fresh = ms.freshness_score(breakout.index, t, decay_bars=BARS_PER_DAY_5MIN)
            tr = ms.true_range_series(highs5, lows5, closes5)
            atr_at_breakout = mean(tr[max(0, breakout.index - ST_PERIOD):breakout.index]) if breakout.index > 0 else 0.0
            ext = ms.extension_score(breakout.pivot_price, closes5[t], atr_at_breakout)
            score = ms.composite_momentum_score(coil, fresh, rvol, ext)

        # --- Forward return: hold until Supertrend-reversal exit or cap ---
        exit_idx = None
        for j in range(t + 1, min(n, t + 1 + MAX_HOLD_BARS)):
            if st5[j] is not None and st5[j - 1] is not None and closes5[j - 1] >= st5[j - 1] and closes5[j] < st5[j]:
                exit_idx = j
                break
        if exit_idx is None:
            exit_idx = min(n - 1, t + MAX_HOLD_BARS)
            exit_reason = "max_hold_cap"
        else:
            exit_reason = "supertrend_reversal"
        forward_return_pct = (closes5[exit_idx] - closes5[t]) / closes5[t] * 100.0

        entries.append({
            "symbol": symbol, "entry_index": t, "entry_time": datetime.fromtimestamp(ts5[t]).isoformat(),
            "entry_date": day, "has_signal": score is not None,
            "coil": coil, "freshness": fresh, "rvol": rvol, "extension": ext, "score": score,
            "forward_return_pct": forward_return_pct, "hold_bars": exit_idx - t, "exit_reason": exit_reason,
        })
    return entries


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = mean(xs), mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    if sx == 0 or sy == 0:
        return 0.0
    return cov / (sx * sy)


def report(all_entries: list[dict]) -> None:
    total = len(all_entries)
    scored = [e for e in all_entries if e["has_signal"]]
    unscored = [e for e in all_entries if not e["has_signal"]]
    print(f"\n=== RESULTS ===")
    print(f"Total real Swing-style entry signals across the watchlist: {total}")
    print(f"  - with a fresh HH/HL breakout context (scoreable):  {len(scored)} ({100*len(scored)/total:.1f}%)")
    print(f"  - with NO recent HH/HL breakout (score=None):       {len(unscored)} ({100*len(unscored)/total:.1f}%)")

    if unscored:
        print(f"\nAvg forward return, NO HH/HL context:  {mean(e['forward_return_pct'] for e in unscored):+.2f}% "
              f"(n={len(unscored)}, win rate {100*sum(1 for e in unscored if e['forward_return_pct']>0)/len(unscored):.1f}%)")
    if scored:
        print(f"Avg forward return, WITH HH/HL context: {mean(e['forward_return_pct'] for e in scored):+.2f}% "
              f"(n={len(scored)}, win rate {100*sum(1 for e in scored if e['forward_return_pct']>0)/len(scored):.1f}%)")

    if len(scored) >= 5:
        scores = [e["score"] for e in scored]
        returns = [e["forward_return_pct"] for e in scored]
        corr = pearson(scores, returns)
        print(f"\nPearson correlation (composite score vs forward return), n={len(scored)}: {corr:+.3f}")

        ranked = sorted(scored, key=lambda e: e["score"])
        tercile = len(ranked) // 3
        low, mid, high = ranked[:tercile], ranked[tercile:2 * tercile], ranked[2 * tercile:]
        print("\nScore-tercile breakdown:")
        print(f"{'Bucket':<8}{'n':<6}{'Avg score':<12}{'Avg return%':<14}{'Median return%':<16}{'Win rate%':<10}")
        for name, bucket in [("Low", low), ("Mid", mid), ("High", high)]:
            if not bucket:
                continue
            avg_ret = mean(e["forward_return_pct"] for e in bucket)
            med_ret = median(e["forward_return_pct"] for e in bucket)
            win = 100 * sum(1 for e in bucket if e["forward_return_pct"] > 0) / len(bucket)
            avg_score = mean(e["score"] for e in bucket)
            print(f"{name:<8}{len(bucket):<6}{avg_score:<12.3f}{avg_ret:<+14.2f}{med_ret:<+16.2f}{win:<10.1f}")

        # Same-day "horse race": on dates with 2+ scored entries across
        # different symbols, does the higher-scored one actually win?
        by_date: dict[str, list[dict]] = {}
        for e in scored:
            by_date.setdefault(e["entry_date"], []).append(e)
        multi_dates = {d: es for d, es in by_date.items() if len(es) >= 2}
        pair_wins, pair_total = 0, 0
        for d, es in multi_dates.items():
            es_sorted = sorted(es, key=lambda e: e["score"], reverse=True)
            best, worst = es_sorted[0], es_sorted[-1]
            if best["symbol"] == worst["symbol"]:
                continue
            pair_total += 1
            if best["forward_return_pct"] >= worst["forward_return_pct"]:
                pair_wins += 1
        if pair_total:
            print(f"\nSame-day horse race (highest-score vs lowest-score candidate that day), "
                  f"n={pair_total} days with 2+ scored candidates:")
            print(f"  Highest-scored candidate had >= return than lowest-scored: {pair_wins}/{pair_total} "
                  f"({100*pair_wins/pair_total:.1f}%)")

        print("\nTop 5 highest-scored setups found (for eyeballing plausibility):")
        for e in sorted(scored, key=lambda e: e["score"], reverse=True)[:5]:
            print(f"  {e['symbol']:<12} {e['entry_time']}  score={e['score']:.3f}  "
                  f"coil={e['coil']:.2f} rvol={e['rvol']:.2f} fresh={e['freshness']:.2f} ext={e['extension']:.2f}  "
                  f"-> forward_return={e['forward_return_pct']:+.2f}% ({e['exit_reason']}, {e['hold_bars']} bars)")
    else:
        print("\nToo few scored entries for a meaningful bucket/correlation breakdown.")


def main():
    symbols = load_watchlist_symbols()
    print(f"[backtest] {len(symbols)} watchlist symbols: {symbols}\n")
    all_entries = []
    for sym in symbols:
        print(f"Backtesting {sym}...")
        try:
            entries = backtest_symbol(sym)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: FAILED ({e})")
            continue
        print(f"  {sym}: {len(entries)} entry signals found")
        all_entries.extend(entries)

    out_path = CACHE_DIR / "all_entries.json"
    out_path.write_text(json.dumps(all_entries, indent=2))
    print(f"\nSaved {len(all_entries)} total entries to {out_path}")
    report(all_entries)


if __name__ == "__main__":
    main()
