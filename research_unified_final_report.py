"""Trade-wise / day-wise report of the FINAL unified strategy (1 Oct 2026): engine A (Super Bollinger pullback calls,
2 slots, hedge + S1 + S2) + engine B (SwingMomentum momentum PUTs, 2 slots, intraday) + market chop gate (NIFTY 2 h
efficiency ratio < 0.10 -> no new entries), premium >= Rs 10, Rs 1.2L account. Chosen for robustness and lowest
worst day among the top configs (research_unified_super_strategy_final.py / research_unified_self_optimizer.py), not
for the highest total. Writes research_results/2026-10-01_unified_final_{trades,days}.csv and the report JSON."""
import csv, json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import research_unified_super_strategy as U

IST = U.IST
days = sorted(U.rsim.allowed)
by_sym_days = defaultdict(set)
for d, syms in U.rsim.allowed.items():
    for s in syms:
        by_sym_days[s].add(d)
cand_a = U.engine_a()
cand_b = [t for sym in sorted(by_sym_days) for t in U.engine_b(sym, by_sym_days[sym], gate=True)]
cand_b_raw = [t for sym in sorted(by_sym_days) for t in U.engine_b(sym, by_sym_days[sym], gate=False)]
ner = U.nifty_er()
cands = sorted(cand_a + cand_b, key=lambda x: (x["t0"], x["engine"]))
FINAL = {"budget": U.BUDGET, "min_premium": U.MIN_PREMIUM, "max_leg": 1e9, "daily_stop": None, "addons": "s1s2",
         "engines": "AB", "slots": 4, "slots_a": 2, "slots_b": 2, "b_sides": ("PUT",), "market_gate": True, "gate_th": 0.10}
fin = U.portfolio(cands, FINAL, ner)
base = {"budget": U.BUDGET, "daily_stop": None, "market_gate": False}
baselines = {
    "Super Bollinger as live (2 slots, hedge)": U.portfolio(cands, {**base, "engines": "A", "slots": 2, "addons": "hedge", "min_premium": 5.0, "max_leg": 1e9}, ner),
    "SwingMomentum (intraday, 5 slots)": U.portfolio(cands, {**base, "engines": "B", "slots": 5, "addons": "none", "min_premium": U.MIN_PREMIUM, "max_leg": 45000}, ner),
    "Swing as live, intraday (no momentum gate)": U.portfolio(sorted(cand_a + cand_b_raw, key=lambda x: (x["t0"], x["engine"])),
        {**base, "engines": "B", "slots": 5, "addons": "none", "min_premium": 5.0, "max_leg": 1e9}, ner),
}
hhmm = lambda ts: datetime.fromtimestamp(ts, IST).strftime("%H:%M")
trades = sorted(fin["trades"], key=lambda t: t["t0"])
with open("research_results/2026-10-01_unified_final_trades.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["date", "engine", "stock", "side", "contract", "entry_time", "exit_time", "entry", "exit", "qty",
                "exit_reason", "legs", "main_pnl", "put_side_pnl", "s1_pnl", "total_pnl"])
    for t in trades:
        w.writerow([t["day"].isoformat(), "A pullback" if t["engine"] == "A" else "B momentum", t["sym"], t["side"],
                    t.get("contract"), hhmm(t["t0"]), hhmm(t["t1"]), round(t["p0"], 2), round(t.get("exit") or 0, 2),
                    t["qty"], t["reason"], t["legs"], round(t["pnl"]), round(t["put_side_pnl"]), round(t["s1_pnl"]),
                    round(t["total"])])
cum = 0.0
day_rows = []
for d in days:
    ts = [t for t in trades if t["day"] == d]
    p = sum(t["total"] for t in ts)
    cum += p
    day_rows.append({"date": d.isoformat(), "trades": len(ts), "won": sum(1 for t in ts if t["total"] > 0),
                     "a": sum(t["total"] for t in ts if t["engine"] == "A"), "b": sum(t["total"] for t in ts if t["engine"] == "B"),
                     "pnl": p, "cum": cum, **{k: v["day"].get(d, 0.0) for k, v in baselines.items()}})
with open("research_results/2026-10-01_unified_final_days.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(day_rows[0]))
    w.writeheader(); w.writerows(day_rows)
# capital actually tied up (peak open premium) for the final
ev = []
for t in trades:
    ev.append((t["t0"], t["p0"] * t["qty"])); ev.append((t["t1"], -t["p0"] * t["qty"]))
ev.sort(); cur = peak = 0
for _t, c in ev:
    cur += c; peak = max(peak, cur)
st = U.stats(fin, days)
reasons = defaultdict(lambda: [0, 0.0])
per_stock = defaultdict(lambda: [0, 0, 0.0])
for t in trades:
    reasons[(t["engine"], t["reason"])][0] += 1; reasons[(t["engine"], t["reason"])][1] += t["total"]
    per_stock[t["sym"]][0] += 1; per_stock[t["sym"]][1] += t["total"] > 0; per_stock[t["sym"]][2] += t["total"]
report = {
    "final_cfg": {k: (list(v) if isinstance(v, tuple) else v) for k, v in FINAL.items()},
    "stats": st, "peak_main_premium": peak, "skips": dict(fin["n"]),
    "baseline_stats": {k: U.stats(v, days) for k, v in baselines.items()},
    "trades": [{"date": t["day"].isoformat(), "engine": t["engine"], "stock": t["sym"], "side": t["side"],
                "contract": t.get("contract"), "entry_time": hhmm(t["t0"]), "exit_time": hhmm(t["t1"]),
                "entry": round(t["p0"], 2), "exit": round(t.get("exit") or 0, 2), "qty": t["qty"], "reason": t["reason"],
                "legs": t["legs"], "main": round(t["pnl"]), "put_side": round(t["put_side_pnl"]), "s1": round(t["s1_pnl"]),
                "total": round(t["total"])} for t in trades],
    "days": day_rows,
    "reasons": [{"engine": e, "reason": r, "n": n, "pnl": p} for (e, r), (n, p) in sorted(reasons.items())],
    "per_stock": sorted([{"stock": s, "n": n, "won": w_, "pnl": p} for s, (n, w_, p) in per_stock.items()], key=lambda x: -x["pnl"]),
}
Path("history/bt_unified_super_strategy").mkdir(parents=True, exist_ok=True)
Path("history/bt_unified_super_strategy/final_report.json").write_text(json.dumps(report, default=str))
print(json.dumps({"stats": st, "peak_main_premium": round(peak), "skips": dict(fin["n"]),
                  "baselines": {k: {kk: round(vv) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in report["baseline_stats"].items()}}, indent=1, default=str))
print("reasons:", [(r["engine"], r["reason"], r["n"], round(r["pnl"])) for r in report["reasons"]])
print("top stocks:", [(x["stock"], x["n"], round(x["pnl"])) for x in report["per_stock"][:6]], "| bottom:", [(x["stock"], x["n"], round(x["pnl"])) for x in report["per_stock"][-5:]])
