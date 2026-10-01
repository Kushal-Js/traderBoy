"""Final grid for the unified strategy (1 Oct 2026): per-engine slot caps (A pullback calls / B momentum, PUTs or
both), the market chop gate (NIFTY 2 h efficiency ratio below th -> no new entries), S1/S2 on A, premium filters.
Out-of-sample protocol: pick the best config on AUGUST (profit per Aug drawdown) -> report its SEPTEMBER, and the
reverse; count configs positive in both months. Cache only after research_unified_super_strategy.py ran."""
import itertools
from collections import defaultdict
from pathlib import Path
import research_unified_super_strategy as U

OUT = Path("research_results/2026-10-01_unified_super_strategy_final.txt")
lines = []
def say(s=""):
    print(s, flush=True); lines.append(s)
days = sorted(U.rsim.allowed)
by_sym_days = defaultdict(set)
for d, syms in U.rsim.allowed.items():
    for s in syms:
        by_sym_days[s].add(d)
cand_a = U.engine_a()
cand_b = [t for sym in sorted(by_sym_days) for t in U.engine_b(sym, by_sym_days[sym], gate=True)]
ner = U.nifty_er()
cands = sorted(cand_a + cand_b, key=lambda x: (x["t0"], x["engine"]))
base = {"budget": U.BUDGET, "min_premium": U.MIN_PREMIUM, "max_leg": U.MAX_LEG, "daily_stop": None, "addons": "s1s2"}
def run(name, cfg):
    r = U.portfolio(cands, {**base, **cfg}, ner)
    return name, cfg, r, U.stats(r, days)
def fmt(name, s):
    return (f"{name:66s} {s['total']:>+9,.0f} {s['aug']:>+8,.0f} {s['sep']:>+8,.0f} {s['dd']:>+8,.0f} {s['worst']:>+8,.0f} "
            f"{s['win_days']:>3d}/{len(days):<3d} {s['A']:>3d}/{s['B']:<3d} {s['total'] / max(1, -s['dd']):>5.1f}")
HDR = f"{'setup':66s} {'total':>9s} {'Aug':>8s} {'Sep':>8s} {'max dd':>8s} {'worst':>8s} {'win d':>7s} {'A/B':>7s} {'P/DD':>5s}"
say("UNIFIED STRATEGY - FINAL GRID. Rs 1.2L, 3 Aug - 29 Sep 2026, walk-forward picks, modelled P&L")
say("\n1) Premium filters on A alone (2 slots, hedge + S1 + S2):")
say(HDR)
for mp, ml in ((5.0, 1e9), (10.0, 1e9), (5.0, 45000), (10.0, 45000)):
    name, _c, r, s = run(f"A 2 slots s1s2, premium >= {mp:g}, leg <= {'none' if ml > 1e8 else f'{ml:,.0f}'}",
                         {"engines": "A", "slots": 2, "min_premium": mp, "max_leg": ml, "market_gate": False})
    say(fmt(name, s))
grid = []
for sa, sb, sides, th in itertools.product((2, 3), (0, 1, 2), (("PUT",), ("CALL", "PUT")), (None, 0.10, 0.12, 0.15)):
    if sb == 0 and sides != ("PUT",):
        continue
    name = (f"A{sa}+B{sb}{'' if sb == 0 else ('(puts)' if sides == ('PUT',) else '(both)')}"
            f"{'' if th is None else f' gate {th:.2f}'}")
    grid.append(run(name, {"engines": "AB" if sb else "A", "slots": sa + sb, "slots_a": sa, "slots_b": sb,
                           "b_sides": sides, "market_gate": th is not None, "gate_th": th or 0.15}))
say("\n2) Grid: A slots x B slots x B sides x chop gate (all with hedge + S1 + S2 on A, premium >= 10, leg <= 45k):")
say(HDR)
for name, _c, _r, s in sorted(grid, key=lambda g: -g[3]["total"] / max(1, -g[3]["dd"])):
    say(fmt(name, s))
by_aug = max(grid, key=lambda g: g[3]["aug"] / max(1.0, -g[3]["dd_aug"]))
by_sep = max(grid, key=lambda g: g[3]["sep"] / max(1.0, -g[3]["dd_sep"]))
pos = [g for g in grid if g[3]["aug"] > 0 and g[3]["sep"] > 0]
say(f"\nOut-of-sample: picked on AUGUST -> {by_aug[0]}: Aug {by_aug[3]['aug']:+,.0f} | SEPTEMBER (unseen) {by_aug[3]['sep']:+,.0f}")
say(f"               picked on SEPTEMBER -> {by_sep[0]}: Sep {by_sep[3]['sep']:+,.0f} | AUGUST (unseen) {by_sep[3]['aug']:+,.0f}")
say(f"Configs positive in both months: {len(pos)}/{len(grid)}; median total {sorted(g[3]['total'] for g in grid)[len(grid)//2]:+,.0f}")
OUT.write_text("\n".join(lines) + "\n")
import pickle
pickle.dump({"grid": [(n, c, dict(r["day"]), [ {k: v for k, v in t.items()} for t in r["trades"]], s) for n, c, r, s in grid],
             "days": days}, open("history/bt_unified_super_strategy/final_grid.pkl", "wb"))
