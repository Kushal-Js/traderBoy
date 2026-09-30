"""
Re-simulation of the best anti-chop entry filters THROUGH the portfolio
simulator (30 Sep 2026), so the 5-slot limit is respected: a refused trade
frees a slot that another signal may take. HYBRID walk-forward picks, 3 Aug -
29 Sep, CE only, then the live hedge and S1 (re-add a call when the stock is
back at its trigger; each lot own 4,500; add exits with the original) on top.
CACHE ONLY: a newly admitted trade whose option prices are not cached is
skipped and counted ("no option data").

Run: uv run python research_super_bollinger_chop_filter_resim.py
"""
from __future__ import annotations

import contextlib
import io
from collections import defaultdict

with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_chop_filters as cf
rs, m = cf.rs, cf.m
u, r = m.u, m.r
SPLIT = "2026-08-31"
picks = m.load_picks("HYBRID")
days = r.trading_days()
as_of = r.weekly_as_of(days)
allowed = {d: picks[a] for d, a in as_of.items() if a in picks}
syms = sorted(set().union(*picks.values()))


def candle_gate(minutes, offset=0, rule="green"):
    cache = {}

    def gate(sym, t, _side):
        key = (sym, minutes, offset)
        if key not in cache:
            b = cf.series(sym)["b"]
            cache[key] = cf.resample(b, minutes) if offset == 0 else None
        h = cf.series(sym)["h1"] if (minutes == 60 and offset == 0) else cache[key]
        k = cf.last_closed(h["end"], t)
        if k < 1:
            return False
        if rule == "green":
            return h["c"][k] > h["o"][k]
        return h["c"][k] > h["c"][k - 1]
    return gate


def stats(trades, extra):
    daily = defaultdict(float)
    for t, e in zip(trades, extra):
        daily[t["day"]] += float(t["pnl_modeled"]) + e
    eq = pk = dd = 0.0
    for d in sorted(daily):
        eq += daily[d]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    a = sum(v for d, v in daily.items() if d < SPLIT)
    b = sum(v for d, v in daily.items() if d >= SPLIT)
    return f"{a + b:>+9,.0f} (Aug {a:>+8,.0f} Sep {b:>+8,.0f}) dd {dd:>8,.0f} worst {min(daily.values()):>+8,.0f}"


print("filter                                  trades  skipped(filter / no option data) | plain | + hedge + S1")
for name, gate in (("no filter", None), ("last 60-min candle green (09:15 anchor)", candle_gate(60)),
                   ("last 60-min close above the one before", candle_gate(60, rule="close_up")),
                   ("last 45-min candle green", candle_gate(45)), ("last 90-min candle green", candle_gate(90)),
                   ("last 30-min candle green", candle_gate(30))):
    with contextlib.redirect_stdout(io.StringIO()):
        trades, st = u.simulate(syms, r.CAP, m.pr, f"HYBRID_{name}", "long", allowed, symbol_gate=gate)
        evs = [rs.prep(t) for t in trades]
        hedge = [rs.put_side(ev, False, None)[0] for ev in evs]
        s1 = [rs.call_side(ev, confirm=True, lot2_exit="with", arm=0, pair_cap=False, reentry=False)[0] for ev in evs]
    unpriced_hedges = sum(1 for ev in evs if ev["th"] is not None and ev["hm"] is None)
    print(f"{name:40s} {len(trades):4d}   {st.get('skipped_symbol_gate', 0):3d} / {st.get('skipped_no_option_data', 0):3d}"
          f"  (hedges unpriced {unpriced_hedges})\n      plain        {stats(trades, [0.0] * len(trades))}\n      +hedge +S1   "
          f"{stats(trades, [a + b for a, b in zip(hedge, s1)])}", flush=True)
print(f"\nDhan calls made: {m.pr.calls} (cache-only: these are refused lookups, not network calls)")
