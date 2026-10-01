"""Follow-up to research_unified_super_strategy.py (1 Oct 2026): does engine B add anything once the market chop gate
is on, how sensitive is the gate's threshold, and which side of B (CALL / PUT) carries it. Same data, cache only after
the main run; results appended to research_results/2026-10-01_unified_super_strategy_followup.txt."""
import contextlib, io, json
from collections import defaultdict
from datetime import date
from pathlib import Path
import research_unified_super_strategy as U

OUT = Path("research_results/2026-10-01_unified_super_strategy_followup.txt")
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
base = {"budget": U.BUDGET, "min_premium": U.MIN_PREMIUM, "max_leg": U.MAX_LEG, "market_gate": False, "daily_stop": None}
def run(name, cfg):
    r = U.portfolio(cands, {**base, **cfg}, ner)
    return name, r, U.stats(r, days)
rows = []
for slots in (2, 4, 5):
    for addons in ("hedge", "s1s2"):
        for mg in (False, True):
            rows.append(run(f"A only {slots} slots {addons}{' + chop gate' if mg else ''}",
                            {"engines": "A", "slots": slots, "addons": addons, "market_gate": mg}))
for mg in (False, True):
    rows.append(run(f"B only 5 slots{' + chop gate' if mg else ''}", {"engines": "B", "slots": 5, "addons": "none", "market_gate": mg}))
for th in (0.10, 0.12, 0.15, 0.18, 0.20, 0.25):
    rows.append(run(f"UNIFIED 5 slots s1s2 + chop gate {th:.2f}", {"engines": "AB", "slots": 5, "addons": "s1s2", "market_gate": True, "gate_th": th}))
    rows.append(run(f"A only 5 slots s1s2 + chop gate {th:.2f}", {"engines": "A", "slots": 5, "addons": "s1s2", "market_gate": True, "gate_th": th}))
for sides, nm in ((("CALL",), "B calls only"), (("PUT",), "B puts only")):
    for mg in (False, True):
        rows.append(run(f"UNIFIED 5 slots s1s2, {nm}{' + chop gate' if mg else ''}",
                        {"engines": "AB", "slots": 5, "addons": "s1s2", "market_gate": mg, "b_sides": sides}))
say("FOLLOW-UP - does engine B add value? chop-gate threshold; B sides. Rs 1.2L, 3 Aug - 29 Sep, modelled")
say(f"{'setup':52s} {'total':>9s} {'Aug':>8s} {'Sep':>8s} {'max dd':>8s} {'dd Aug':>8s} {'dd Sep':>8s} {'worst':>8s} {'win days':>8s} {'A/B':>7s} {'P/DD':>5s}")
for name, r, s in rows:
    say(f"{name:52s} {s['total']:>+9,.0f} {s['aug']:>+8,.0f} {s['sep']:>+8,.0f} {s['dd']:>+8,.0f} {s['dd_aug']:>+8,.0f} {s['dd_sep']:>+8,.0f} "
        f"{s['worst']:>+8,.0f} {s['win_days']:>3d}/{len(days):<4d} {s['A']:>3d}/{s['B']:<3d} {s['total'] / max(1, -s['dd']):>5.1f}")
# engine B detail by side and month (all B candidates, no portfolio)
say("\nEngine B candidates by side and month (each signal on its own, no account limits):")
agg = defaultdict(lambda: [0, 0, 0.0])
for t in cand_b:
    k = (t["side"], "Aug" if t["day"] < U.SPLIT else "Sep")
    agg[k][0] += 1; agg[k][1] += t["pnl"] > 0; agg[k][2] += t["pnl"]
for k in sorted(agg):
    n, w, p = agg[k]
    say(f"  {k[0]:4s} {k[1]}: {n:>3d} trades, {w:>3d} won ({100*w/max(1,n):.0f}%), {p:+,.0f}")
OUT.write_text("\n".join(lines) + "\n")
