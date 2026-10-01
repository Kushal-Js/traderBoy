"""
Swing: momentum vs sideways/choppy regime filters, 1 Sep - 1 Oct 2026 (1 Oct 2026, user: "To improve performance
we have to skip trading on choppy days of a stock, create a logic to identify when a stock is in momentum and when
it is sideways or choppy with clear signals").

Same Swing engine, data and caveats as research_swing_hedge_variants_30day.py (live 1 Oct rules, 15-stock list of 1
Oct - hindsight-picked, real option prices, modelled slippage). Two parts:

  A  DIAGNOSTIC (hindsight, not tradable): Swing's trades bucketed by how their stock's WHOLE day behaved - day
     efficiency ratio |close - open| / path length of the 5-min closes, and the number of 5-min Supertrend flips that
     day. Shows how much of Swing's loss sits on choppy stock-days, i.e. the most a perfect filter could save.
  B  ENTRY GATES (tradable - only data up to the entry candle's close): the simulation re-run with each gate, so
     freed slots can take other trades. Standard thresholds, not tuned here:
       CHOP(14) on 15-min (Choppiness Index; > 61.8 choppy, < 38.2-50 trending), ADX(14) on 15-min (>= 20/25 trend),
       Kaufman efficiency ratio on 5-min closes (12 = last hour, 24 = last 2 h), 5-min Supertrend flips in the 2 h
       before the entry candle, today's efficiency ratio so far, yesterday's whole-day efficiency ratio, daily
       ADX(14), and two composites (MOMENTUM state; NOT-CHOPPY state).
     Halves: trades/days 1-15 Sep (H1) vs 16 Sep - 1 Oct (H2) - a gate that only helps one half is noise.

Run after 15:30 IST (new trades a gate lets in may need option data):
    HANDOFF_DHAN_ACCESS_TOKEN=<token> .venv/bin/python research_swing_chop_regime_30day.py
"""
from __future__ import annotations

import bisect
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import research_swing_hedge_scale_actual_trades as R
import research_swing_hedge_variants_30day as H

IST = R.IST
OUTFILE = Path("research_results/2026-10-01_swing_chop_regime_30day.txt")
SPLIT = date(2026, 9, 16)


# --------------------------------------------------------------------------- #
# Indicators (pure; index k = a 5-min bar, known at its close)
# --------------------------------------------------------------------------- #
def true_range(h, l, c):
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def chop(h, l, c, n=14):
    tr, out = true_range(h, l, c), [None] * len(c)
    for k in range(n - 1, len(c)):
        rng = max(h[k - n + 1:k + 1]) - min(l[k - n + 1:k + 1])
        if rng > 0:
            out[k] = 100 * math.log10(sum(tr[k - n + 1:k + 1]) / rng) / math.log10(n)
    return out


def adx(h, l, c, n=14):
    """Wilder's ADX."""
    m = len(c)
    out = [None] * m
    if m < 2 * n + 1:
        return out
    tr = true_range(h, l, c)
    pdm = [0.0] + [max(h[i] - h[i - 1], 0) if h[i] - h[i - 1] > l[i - 1] - l[i] else 0.0 for i in range(1, m)]
    ndm = [0.0] + [max(l[i - 1] - l[i], 0) if l[i - 1] - l[i] > h[i] - h[i - 1] else 0.0 for i in range(1, m)]
    atr_, p, q = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(ndm[1:n + 1])
    dx = []
    for i in range(n + 1, m):
        atr_, p, q = atr_ - atr_ / n + tr[i], p - p / n + pdm[i], q - q / n + ndm[i]
        pdi, ndi = 100 * p / atr_ if atr_ else 0, 100 * q / atr_ if atr_ else 0
        dx.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0)
        if len(dx) == n:
            a = sum(dx) / n
            out[i] = a
        elif len(dx) > n:
            a = (a * (n - 1) + dx[-1]) / n
            out[i] = a
    return out


def er(c, k, n):
    if k < n:
        return None
    path = sum(abs(c[i] - c[i - 1]) for i in range(k - n + 1, k + 1))
    return abs(c[k] - c[k - n]) / path if path else 0.0


# --------------------------------------------------------------------------- #
# Per-symbol feature tables
# --------------------------------------------------------------------------- #
FEAT: dict[str, dict] = {}


def build_features(data: dict) -> None:
    for sym, d in data.items():
        f = d["fast"]
        slow = H.resample(d["spot"], 15)
        n = len(f["ts"])
        st = d["sig"]["st"]
        above = [None if st[k] is None else f["c"][k] > st[k] for k in range(n)]
        flip = [0] + [1 if above[k] is not None and above[k - 1] is not None and above[k] != above[k - 1] else 0
                      for k in range(1, n)]
        days = [datetime.fromtimestamp(t, IST).date() for t in f["ts"]]
        first_of_day, day_bars = {}, defaultdict(list)
        for k, dd in enumerate(days):
            first_of_day.setdefault(dd, k)
            day_bars[dd].append(k)
        day_list = sorted(day_bars)
        # whole-day stats (hindsight, diagnostic only) and the PREVIOUS day's (tradable at the open)
        day_er, day_flips = {}, {}
        for dd, ks in day_bars.items():
            path = sum(abs(f["c"][k] - f["c"][k - 1]) for k in ks[1:])
            day_er[dd] = abs(f["c"][ks[-1]] - f["o"][ks[0]]) / path if path else 0.0
            day_flips[dd] = sum(flip[k] for k in ks[1:])
        prev_day = {day_list[i]: day_list[i - 1] for i in range(1, len(day_list))}
        # daily bars -> daily ADX(14), known for days BEFORE today
        dh = [max(f["h"][k] for k in day_bars[dd]) for dd in day_list]
        dl = [min(f["l"][k] for k in day_bars[dd]) for dd in day_list]
        dc = [f["c"][day_bars[dd][-1]] for dd in day_list]
        dadx = dict(zip(day_list, adx(dh, dl, dc, 14)))
        slow_end = [t + 900 for t in slow["ts"]]
        chop15, adx15 = chop(slow["h"], slow["l"], slow["c"]), adx(slow["h"], slow["l"], slow["c"])
        FEAT[sym] = {"f": f, "flip": flip, "days": days, "first": first_of_day, "day_er": day_er,
                     "day_flips": day_flips, "prev_day": prev_day, "dadx": dadx, "slow_end": slow_end,
                     "chop15": chop15, "adx15": adx15}


def at(sym: str, k: int) -> dict:
    """Every feature for the entry candle k (only data known at its close)."""
    F = FEAT[sym]
    f, t_close = F["f"], F["f"]["ts"][k] + 300
    j = bisect.bisect_right(F["slow_end"], t_close) - 1
    day = F["days"][k]
    k0 = F["first"][day]
    so_far = k - k0
    path = sum(abs(f["c"][i] - f["c"][i - 1]) for i in range(k0 + 1, k + 1))
    pd = F["prev_day"].get(day)
    return {
        "chop15": F["chop15"][j] if j >= 0 else None, "adx15": F["adx15"][j] if j >= 0 else None,
        "er12": er(f["c"], k, 12), "er24": er(f["c"], k, 24),
        "flips2h": sum(F["flip"][max(1, k - 24):k]),                 # flips in the 2 h BEFORE the entry candle
        "today_er": (abs(f["c"][k] - f["o"][k0]) / path if path else 0.0) if so_far >= 6 else None,
        "today_flips": sum(F["flip"][k0 + 1:k]),
        "prev_er": F["day_er"].get(pd) if pd else None,
        "dadx": F["dadx"].get(pd) if pd else None,                  # daily ADX as of yesterday's close
        "day_er": F["day_er"][day], "day_flips": F["day_flips"][day],   # hindsight (diagnostic only)
    }


def ok(v, test) -> bool:
    return True if v is None else test(v)      # missing history -> fail open (as the live gates do)


GATES = {
    "CHOP15 < 61.8 (skip choppy)": lambda x: ok(x["chop15"], lambda v: v < 61.8),
    "CHOP15 < 50 (trending only)": lambda x: ok(x["chop15"], lambda v: v < 50),
    "ADX15 >= 20": lambda x: ok(x["adx15"], lambda v: v >= 20),
    "ADX15 >= 25": lambda x: ok(x["adx15"], lambda v: v >= 25),
    "ER last 1h >= 0.3": lambda x: ok(x["er12"], lambda v: v >= 0.3),
    "ER last 2h >= 0.25": lambda x: ok(x["er24"], lambda v: v >= 0.25),
    "ST flips in last 2h <= 2": lambda x: x["flips2h"] <= 2,
    "ST flips in last 2h <= 1": lambda x: x["flips2h"] <= 1,
    "today's ER so far >= 0.3": lambda x: ok(x["today_er"], lambda v: v >= 0.3),
    "today's ST flips so far <= 3": lambda x: x["today_flips"] <= 3,
    "yesterday's day ER >= 0.3": lambda x: ok(x["prev_er"], lambda v: v >= 0.3),
    "daily ADX >= 20": lambda x: ok(x["dadx"], lambda v: v >= 20),
    "MOMENTUM: (ADX15>=20 or CHOP15<50) & flips2h<=2 & ER1h>=0.3":
        lambda x: (ok(x["adx15"], lambda v: v >= 20) or ok(x["chop15"], lambda v: v < 50))
        and x["flips2h"] <= 2 and ok(x["er12"], lambda v: v >= 0.3),
    "NOT CHOPPY: not (CHOP15>61.8 or flips2h>=3 or today ER<0.15)":
        lambda x: not ((x["chop15"] is not None and x["chop15"] > 61.8) or x["flips2h"] >= 3
                       or (x["today_er"] is not None and x["today_er"] < 0.15)),
}


# --------------------------------------------------------------------------- #
def stats(trades: list[dict]) -> dict:
    by_day = defaultdict(float)
    for t in trades:
        by_day[datetime.fromtimestamp(t["t1"] or t["t0"], IST).date()] += t["pnl"]
    cum = peak = dd = 0.0
    for d in sorted(by_day):
        cum += by_day[d]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return {"n": len(trades), "won": sum(1 for t in trades if t["pnl"] > 0), "mod": sum(t["pnl"] for t in trades),
            "raw": sum(t["pnl_raw"] for t in trades), "dd": dd, "win_days": sum(1 for v in by_day.values() if v > 0),
            "days": len(by_day), "h1": sum(v for d, v in by_day.items() if d < SPLIT),
            "h2": sum(v for d, v in by_day.items() if d >= SPLIT), "by_day": by_day}


def main() -> None:
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    data = {}
    for sym in H.SYMS:
        spot = H.load_spot(sym)
        fast, slow = H.resample(spot, 5), H.resample(spot, 15)
        und = R.Series(spot)
        data[sym] = {"spot": spot, "idx": {t: i for i, t in enumerate(spot["ts"])}, "fast": fast,
                     "sig": H.S.signals(fast, slow, 5), "close_at": {t + 300: k for k, t in enumerate(fast["ts"])},
                     "und": und, "b5": R.five_min(und)}
    build_features(data)

    H.ENTRY_GATE = None
    base = H.simulate(data)
    sb = stats(base)
    say("SWING REGIME FILTERS, 1 Sep - 1 Oct 2026 (15-stock list of 1 Oct; modelled P&L after slippage; H1 = 1-15 Sep, "
        "H2 = 16 Sep - 1 Oct)")
    say(f"Swing as live: {sb['n']} trades, {sb['won']} won, modelled {sb['mod']:+,.0f} (raw {sb['raw']:+,.0f}), "
        f"H1 {sb['h1']:+,.0f}, H2 {sb['h2']:+,.0f}, max drawdown {sb['dd']:+,.0f}, winning days {sb['win_days']}/{sb['days']}")

    # ---- A: diagnostic ----
    say("\nA. HINDSIGHT - Swing's trades by how their stock's whole day behaved (not tradable; the ceiling)")
    feats = [(t, at(t["sym"], t["k_in"])) for t in base]

    def bucket(name, key, edges, labels):
        say(f"  {name}")
        for lo, hi, lab in zip(edges[:-1], edges[1:], labels):
            sel = [t for t, x in feats if x[key] is not None and lo <= x[key] < hi]
            if sel:
                s = stats(sel)
                say(f"    {lab:28s} trades {s['n']:>4d}  won {s['won']:>3d} ({100 * s['won'] / s['n']:>3.0f}%)  "
                    f"modelled {s['mod']:>+9,.0f}  raw {s['raw']:>+9,.0f}  per trade {s['mod'] / s['n']:>+7,.0f}")

    bucket("day efficiency ratio (|close-open| / path)", "day_er", [0, 0.15, 0.3, 0.5, 1.01],
           ["choppy  ER < 0.15", "mixed   0.15-0.30", "trend   0.30-0.50", "strong  >= 0.50"])
    bucket("5-min Supertrend flips that day", "day_flips", [0, 3, 6, 9, 100],
           ["0-2 flips", "3-5 flips", "6-8 flips", "9+ flips"])

    say("\n   ... and by the regime AT ENTRY (tradable features, same trades, no re-simulation)")
    for name, key, edges, labels in [
        ("CHOP(14) 15-min", "chop15", [0, 38.2, 50, 61.8, 101], ["< 38.2 trending", "38.2-50", "50-61.8", "> 61.8 choppy"]),
        ("ADX(14) 15-min", "adx15", [0, 20, 25, 35, 101], ["< 20 sideways", "20-25", "25-35", ">= 35 strong"]),
        ("efficiency ratio, last 1 h (5-min)", "er12", [0, 0.15, 0.3, 0.5, 1.01], ["< 0.15", "0.15-0.30", "0.30-0.50", ">= 0.50"]),
        ("Supertrend flips in the 2 h before entry", "flips2h", [0, 1, 2, 3, 100], ["0", "1", "2", "3+"]),
        ("today's efficiency ratio so far", "today_er", [0, 0.15, 0.3, 0.5, 1.01], ["< 0.15", "0.15-0.30", "0.30-0.50", ">= 0.50"]),
        ("yesterday's whole-day efficiency ratio", "prev_er", [0, 0.15, 0.3, 0.5, 1.01], ["< 0.15", "0.15-0.30", "0.30-0.50", ">= 0.50"]),
    ]:
        bucket(name, key, edges, labels)

    # ---- B: gates, re-simulated ----
    say("\nB. ENTRY GATES (re-simulated: a skipped signal frees the slot for another stock)")
    say(f"  {'gate':62s} {'trades':>6s} {'won%':>5s} {'modelled':>10s} {'raw':>9s} {'H1':>9s} {'H2':>9s} "
        f"{'max dd':>9s} {'win days':>8s}")
    say(f"  {'(none) Swing as live':62s} {sb['n']:>6d} {100 * sb['won'] / sb['n']:>4.0f}% {sb['mod']:>+10,.0f} "
        f"{sb['raw']:>+9,.0f} {sb['h1']:>+9,.0f} {sb['h2']:>+9,.0f} {sb['dd']:>+9,.0f} {sb['win_days']:>3d}/{sb['days']}")
    results = {}
    for name, gate in GATES.items():
        H.ENTRY_GATE = lambda sym, side, k, now, g=gate: g(at(sym, k))
        tr = H.simulate(data)
        s = stats(tr)
        results[name] = (s, tr)
        both = "  <- helps both halves" if s["h1"] > sb["h1"] and s["h2"] > sb["h2"] else ""
        say(f"  {name:62s} {s['n']:>6d} {100 * s['won'] / max(1, s['n']):>4.0f}% {s['mod']:>+10,.0f} {s['raw']:>+9,.0f} "
            f"{s['h1']:>+9,.0f} {s['h2']:>+9,.0f} {s['dd']:>+9,.0f} {s['win_days']:>3d}/{s['days']}{both}")
    H.ENTRY_GATE = None

    best = sorted(results.items(), key=lambda kv: kv[1][0]["mod"], reverse=True)[:3]
    say("\nDay-wise, Swing as live vs the three best gates (modelled):")
    say(f"  {'day':10s} {'Swing':>9s} " + " ".join(f"{n[:22]:>23s}" for n, _ in best))
    days = sorted(set(sb["by_day"]) | {d for _n, (s, _t) in best for d in s["by_day"]})
    for d in days:
        say(f"  {d.isoformat():10s} {sb['by_day'].get(d, 0):>+9,.0f} " +
            " ".join(f"{s['by_day'].get(d, 0):>+23,.0f}" for _n, (s, _t) in best))
    say(f"\nDhan calls this run: {H.CALLS['n']}; signals left unpriced: {len(H.MISSING)}")
    say("Caveats: one month, 15 hindsight-picked stocks, simulated (about half the live entries match on 30 Sep/1 Oct); "
        "thresholds are textbook values, not tuned - a gate counts only if it helps BOTH halves.")
    OUTFILE.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
