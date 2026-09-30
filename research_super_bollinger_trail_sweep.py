"""
Trailing-stop sweeps on the Sep HYBRID Super Bollinger trades (30 Sep 2026, user: "we need a trailing stop
for both sides - the APLAPOLLO hedge was up 3k and we kept 1.5k"). Cache-only (imports
research_super_bollinger_scale_in_out, which blocks every Dhan call).
  - hedge PE: arm / giveback grid + stepped trails (live = arm 1000, give back 40%);
  - main CE trade: a profit trail added on top of G' (breakeven after +1.5k, max loss 4.5k).
Finding: hedge - giveback 20-30% or a stepped trail +16-18k vs +14.1k live, both halves; arming at 500 or
50% giveback is worse. CE - every trail but one cell loses (-6k to -51k): the CE edge is a few big winners.

Run: uv run python research_super_bollinger_trail_sweep.py
"""
import bisect, contextlib, io, json
from collections import defaultdict
with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_scale_in_out as m
priced = m.priced
mod = m.mod_pnl
STOP = 1500

def stats(extra_by_day, base=True):
    daily = defaultdict(float)
    if base:
        for ev in m.events:
            daily[ev["r"]["day"]] += float(ev["r"]["pnl_modeled"])
    for d, v in extra_by_day:
        daily[d] += v
    eq = pk = dd = 0.0
    for k in sorted(daily):
        eq += daily[k]; pk = max(pk, eq); dd = min(dd, eq - pk)
    h1 = sum(v for k, v in daily.items() if k < m.HALF_SPLIT); h2 = sum(v for k, v in daily.items() if k >= m.HALF_SPLIT)
    return round(sum(daily.values())), round(h1), round(h2), round(dd), round(min(daily.values()))

# ---------------- hedge trail sweep ----------------
def hedge_run(ev, tiers, touch):
    """tiers: [(peak_rs, giveback)] ascending; the tightest tier reached applies. touch=True: resting-stop
    style (level from the peak BEFORE this minute; filled at the level or the open if gapped) - conservative."""
    h = ev["hedge"]; pm, i0, q0, qty, sq = h["pm"], h["i0"], h["q0"], ev["qty"], ev["sq"]
    peak = 0.0
    for j in range(i0 + 1, len(pm.ts)):
        t = pm.ts[j]
        if t >= sq:
            return mod(q0, pm.o[j], qty), peak
        if (q0 - pm.l[j]) * qty >= STOP:
            return mod(q0, min(q0 - STOP / qty, pm.o[j]), qty), peak
        gb = None
        for p_rs, g in tiers:
            if peak >= p_rs:
                gb = g
        if touch and gb is not None:
            lvl = q0 + peak * (1 - gb) / qty
            if pm.l[j] <= lvl:
                return mod(q0, min(lvl, pm.o[j]), qty), peak
        peak = max(peak, (pm.h[j] - q0) * qty)
        if not touch:
            gb = None
            for p_rs, g in tiers:
                if peak >= p_rs:
                    gb = g
            if gb is not None and (pm.c[j] - q0) * qty <= peak * (1 - gb):
                return mod(q0, pm.c[j], qty), peak
    return mod(q0, pm.c[-1], qty), peak

hedged = [ev for ev in priced if ev["hedge"]]
print(f"== HEDGE PE trail (live rule T2000+ATR, stop 1500) - {len(hedged)} hedges, Sep HYBRID. 'close' = checked on 1-min closes (as the earlier research); 'touch' = resting stop at the trail level ==")
variants = {"LIVE arm 1000, give back 40%": [(1000, .40)]}
for g in (.20, .25, .30, .50):
    variants[f"arm 1000, give back {int(g*100)}%"] = [(1000, g)]
variants["arm 500, give back 40%"] = [(500, .40)]
variants["arm 500, give back 30%"] = [(500, .30)]
variants["arm 1500, give back 30%"] = [(1500, .30)]
variants["stepped: 40% from 1k, 30% from 2k, 20% from 3k"] = [(1000, .40), (2000, .30), (3000, .20)]
variants["stepped: 40% from 1k, 25% from 2k"] = [(1000, .40), (2000, .25)]
variants["stepped: 30% from 1k, 20% from 2.5k"] = [(1000, .30), (2500, .20)]
for name, tiers in variants.items():
    row = []
    for touch in (False, True):
        res = [(ev["r"]["day"], *hedge_run(ev, tiers, touch)) for ev in hedged]
        tot = sum(x[1] for x in res); wins = sum(x[1] > 0 for x in res)
        peaks = sum(x[2] for x in res if x[2] > 0)
        h1 = sum(x[1] for x in res if x[0] < m.HALF_SPLIT); h2 = tot - h1
        row.append(f"{'touch' if touch else 'close'}: hedge {tot:>+8,.0f} (H1 {h1:>+7,.0f} H2 {h2:>+7,.0f}) wins {wins:2d}/{len(res)} captured {100*max(tot,0)/peaks:4.0f}% of peaks")
    print(f"{name:48s} | " + " | ".join(row))

# ---------------- CE trade trail sweep ----------------
def ce_run(ev, tiers):
    """Extra profit-lock on top of G' (the CSV exit): once peak profit >= tier, a resting stop keeping
    (1-giveback) of the peak; conservative touch fill. Returns modeled pnl of the CE trade."""
    cm, p0, qty, t0, t1 = ev["cm"], ev["p0"], ev["qty"], ev["t0"], ev["t1"]
    peak = 0.0
    for j, t in enumerate(cm.ts):
        if t <= t0:
            continue
        if t >= t1:
            break
        gb = None
        for p_rs, g in tiers:
            if peak >= p_rs:
                gb = g
        if gb is not None:
            lvl = p0 + peak * (1 - gb) / qty
            if cm.l[j] <= lvl:
                return mod(p0, min(lvl, cm.o[j]), qty)
        peak = max(peak, (cm.h[j] - p0) * qty)
    return float(ev["r"]["pnl_modeled"])

print(f"\n== MAIN CE trade: profit trail added on top of the current rules (G') - {len(priced)} trades ==")
base = stats([])
print(f"{'CURRENT rules (no trail)':48s} total {base[0]:>+8,} H1 {base[1]:>+8,} H2 {base[2]:>+8,} dd {base[3]:>8,} worst {base[4]:>+8,}")
ce_variants = {}
for arm in (2000, 3000, 4000, 6000):
    for g in (.30, .40, .50):
        ce_variants[f"after +{arm//1000}k, give back {int(g*100)}%"] = [(arm, g)]
ce_variants["stepped: 50% from 3k, 35% from 6k, 25% from 10k"] = [(3000, .50), (6000, .35), (10000, .25)]
ce_variants["stepped: 50% from 4k, 30% from 8k"] = [(4000, .50), (8000, .30)]
ce_variants["only big winners: 30% from 8k"] = [(8000, .30)]
ce_variants["only big winners: 25% from 10k"] = [(10000, .25)]
for name, tiers in ce_variants.items():
    extra = [(ev["r"]["day"], ce_run(ev, tiers) - float(ev["r"]["pnl_modeled"])) for ev in priced]
    changed = sum(1 for _d, v in extra if abs(v) > 1)
    s = stats(extra)
    print(f"{name:48s} total {s[0]:>+8,} H1 {s[1]:>+8,} H2 {s[2]:>+8,} dd {s[3]:>8,} worst {s[4]:>+8,}  ({changed} trades exit earlier, {s[0]-base[0]:>+8,} vs current)")
