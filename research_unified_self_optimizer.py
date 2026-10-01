"""
Unified strategy with a SELF-OPTIMIZING layer (1 Oct 2026, user: "This strategy should adapt itself also based on
market or momentum in the stocks to carve out maximum profit, we need an intelligent self optimizing algorithm. Show
me trade wise PnL reports also").

What adapts, and how (no hindsight anywhere):
  1. Stocks       - the weekly HYBRID pick (liquidity -> ATH -> the strategy's own recent fit -> top 15), walk-forward.
  2. Market       - intraday: the market chop gate (NIFTY's 2 h efficiency ratio) pauses new entries when the index
                    is going nowhere; optional market-follow switches (B trades only in NIFTY's 2 h direction; A
                    buys calls only while NIFTY is not falling).
  3. Stock state  - per signal: engine B needs the stock in MOMENTUM (Swing/regime), engine A the last 1-hour candle
                    green (Super Bollinger's live filter).
  4. Configuration- SELF-OPTIMIZER: a menu of portfolio configurations (engine slot split, B sides, gate on/off and
                    threshold, market-follow switches). Before each week (or each day), every menu config is scored
                    on the PAST trading days only (trailing window), and the best one trades the next period. All
                    positions are intraday, so a day's P&L under a config does not depend on earlier days - switching
                    at a day boundary is exact.
Reported: every fixed config, the self-optimizer under several lookbacks / scores / rebalance frequencies, the chosen
final setup's trade-wise and day-wise P&L (research_results/2026-10-01_unified_final_trades.csv / _days.csv).

Same data, engines and caveats as research_unified_super_strategy.py (run that first; this is cache only).
The menu itself was built from research on these same two months, so even the walk-forward optimizer is not fully
out-of-sample - read the numbers as the best available evidence, not a forecast.
"""
from __future__ import annotations

import csv
import itertools
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import research_unified_super_strategy as U

IST = U.IST
OUT = Path("research_results/2026-10-01_unified_self_optimizer.txt")
TRADES_CSV = Path("research_results/2026-10-01_unified_final_trades.csv")
DAYS_CSV = Path("research_results/2026-10-01_unified_final_days.csv")
REPORT_JSON = Path("history/bt_unified_super_strategy/final_report.json")


def main() -> None:
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    days = sorted(U.rsim.allowed)
    by_sym_days = defaultdict(set)
    for d, syms in U.rsim.allowed.items():
        for s in syms:
            by_sym_days[s].add(d)
    cand_a = U.engine_a()
    cand_b = [t for sym in sorted(by_sym_days) for t in U.engine_b(sym, by_sym_days[sym], gate=True)]
    ner = U.nifty_er()
    cands = sorted(cand_a + cand_b, key=lambda x: (x["t0"], x["engine"]))
    base = {"budget": U.BUDGET, "min_premium": U.MIN_PREMIUM, "daily_stop": None, "addons": "s1s2"}

    # ---- the menu ----
    menu = []
    for (sa, sb, sides), gate, leg, follow in itertools.product(
            ((2, 0, ("PUT",)), (2, 2, ("PUT",)), (2, 2, ("CALL", "PUT")), (3, 2, ("CALL", "PUT"))),
            (None, 0.10, 0.15), (45_000, 1e9), ("off", "B", "AB")):
        if sb == 0 and follow == "B":
            continue
        name = (f"A{sa}+B{sb}{'' if sb == 0 else ('(puts)' if sides == ('PUT',) else '(both)')}"
                f"{'' if gate is None else f' gate {gate:.2f}'}{'' if leg > 1e8 else ' leg<=45k'}"
                f"{'' if follow == 'off' else (' B-follows-NIFTY' if follow == 'B' else ' A+B-follow-NIFTY')}")
        cfg = {**base, "engines": "AB" if sb else "A", "slots": sa + sb, "slots_a": sa, "slots_b": sb, "b_sides": sides,
               "market_gate": gate is not None, "gate_th": gate or 0.15, "max_leg": leg,
               "b_follow": follow in ("B", "AB"), "a_follow": follow == "AB"}
        r = U.portfolio(cands, cfg, ner)
        menu.append({"name": name, "cfg": cfg, "r": r, "s": U.stats(r, days)})
    say("UNIFIED STRATEGY + SELF-OPTIMIZER - Rs 1.2L, 3 Aug - 29 Sep 2026 (41 days), walk-forward picks, modelled P&L")
    say(f"Menu: {len(menu)} configurations (all: hedge + S1 + S2 on engine A, premium >= Rs {U.MIN_PREMIUM:g})")
    say(f"\n{'fixed configuration':58s} {'total':>9s} {'Aug':>8s} {'Sep':>8s} {'max dd':>8s} {'worst':>8s} {'win d':>6s} "
        f"{'A/B':>7s} {'P/DD':>5s}")
    for x in sorted(menu, key=lambda x: -x["s"]["total"] / max(1, -x["s"]["dd"])):
        s = x["s"]
        say(f"{x['name']:58s} {s['total']:>+9,.0f} {s['aug']:>+8,.0f} {s['sep']:>+8,.0f} {s['dd']:>+8,.0f} {s['worst']:>+8,.0f} "
            f"{s['win_days']:>3d}/41 {s['A']:>3d}/{s['B']:<3d} {s['total'] / max(1, -s['dd']):>5.1f}")

    # ---- the self-optimizer, walk-forward ----
    def score(vals: list, how: str) -> float:
        if not vals:
            return 0.0
        tot = sum(vals)
        if how == "pnl":
            return tot
        dd = U._dd(vals)
        return tot / max(1000.0, -dd)

    week_of = {d: d.isocalendar()[:2] for d in days}
    default = next(x for x in menu if x["name"] == "A2+B0")      # before any history: plain pullback engine
    results = []
    for lookback, how, freq in itertools.product((10, 15, 20, 10_000), ("pnl", "pnl/dd"), ("week", "day")):
        realized, chosen, prev_key = {}, [], None
        for i, d in enumerate(days):
            key = week_of[d] if freq == "week" else d
            if key != prev_key:
                past = days[max(0, i - lookback):i]
                if len(past) < 5:
                    pick = default
                else:
                    pick = max(menu, key=lambda x: score([x["r"]["day"].get(p, 0.0) for p in past], how))
                prev_key = key
            realized[d] = pick["r"]["day"].get(d, 0.0)
            chosen.append((d, pick["name"]))
        r = {"day": realized, "trades": []}
        st = U.stats(r, days)
        results.append({"name": f"self-optimizer lookback {lookback if lookback < 10_000 else 'all'} days, score {how}, "
                                f"re-pick every {freq}", "s": st, "chosen": chosen, "lookback": lookback, "how": how,
                        "freq": freq})
    say(f"\n{'SELF-OPTIMIZER (picks from the menu on PAST days only)':72s} {'total':>9s} {'Aug':>8s} {'Sep':>8s} "
        f"{'max dd':>8s} {'worst':>8s} {'win d':>6s} {'P/DD':>5s}")
    for x in sorted(results, key=lambda x: -x["s"]["total"] / max(1, -x["s"]["dd"])):
        s = x["s"]
        say(f"{x['name']:72s} {s['total']:>+9,.0f} {s['aug']:>+8,.0f} {s['sep']:>+8,.0f} {s['dd']:>+8,.0f} "
            f"{s['worst']:>+8,.0f} {s['win_days']:>3d}/41 {s['total'] / max(1, -s['dd']):>5.1f}")
    fixed_sorted = sorted(menu, key=lambda x: x["s"]["total"])
    say(f"\nFixed menu configs: median total {fixed_sorted[len(menu) // 2]['s']['total']:+,.0f}, worst "
        f"{fixed_sorted[0]['s']['total']:+,.0f} ({fixed_sorted[0]['name']}), best {fixed_sorted[-1]['s']['total']:+,.0f} "
        f"({fixed_sorted[-1]['name']}) - the best fixed config is only known in hindsight.")
    so_sorted = sorted(results, key=lambda x: x["s"]["total"])
    say(f"Self-optimizer variants: median total {so_sorted[len(results) // 2]['s']['total']:+,.0f}, worst "
        f"{so_sorted[0]['s']['total']:+,.0f}, best {so_sorted[-1]['s']['total']:+,.0f}")

    # ---- final choice: the a-priori optimizer setting (weekly, 15 days, pnl/dd) - not the best-looking one ----
    final = next(x for x in results if x["lookback"] == 15 and x["how"] == "pnl/dd" and x["freq"] == "week")
    say(f"\nFINAL (chosen a priori, not by its result): {final['name']}")
    say(f"  total {final['s']['total']:+,.0f} | Aug {final['s']['aug']:+,.0f} | Sep {final['s']['sep']:+,.0f} | "
        f"max dd {final['s']['dd']:+,.0f} | worst day {final['s']['worst']:+,.0f} | winning days {final['s']['win_days']}/41")
    weeks = []
    for d, name in final["chosen"]:
        if not weeks or weeks[-1][1] != name:
            weeks.append([d, name])
    say("  configuration in use: " + "; ".join(f"from {d:%d %b}: {n}" for d, n in weeks))
    by_name = {x["name"]: x for x in menu}
    final_trades = []
    for d, name in final["chosen"]:
        for t in by_name[name]["r"]["trades"]:
            if t["day"] == d:
                final_trades.append({**t, "config": name})
    final_trades.sort(key=lambda t: t["t0"])
    hhmm = lambda ts: datetime.fromtimestamp(ts, IST).strftime("%H:%M")
    with TRADES_CSV.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "engine", "stock", "side", "contract", "entry_time", "exit_time", "entry", "exit", "qty",
                    "exit_reason", "legs", "main_pnl", "put_side_pnl", "s1_pnl", "total_pnl", "config"])
        for t in final_trades:
            w.writerow([t["day"].isoformat(), "A pullback" if t["engine"] == "A" else "B momentum", t["sym"], t["side"],
                        t.get("contract"), hhmm(t["t0"]), hhmm(t["t1"]), round(t["p0"], 2),
                        round(t.get("exit") or 0, 2), t["qty"], t["reason"], t["legs"], round(t["pnl"]),
                        round(t["put_side_pnl"]), round(t["s1_pnl"]), round(t["total"]), t["config"]])
    with DAYS_CSV.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "trades", "won", "pnl", "cumulative", "config"])
        cum = 0.0
        chosen = dict(final["chosen"])
        for d in days:
            ts = [t for t in final_trades if t["day"] == d]
            p = sum(t["total"] for t in ts)
            cum += p
            w.writerow([d.isoformat(), len(ts), sum(1 for t in ts if t["total"] > 0), round(p), round(cum), chosen[d]])
    say(f"  trades {len(final_trades)} (A {sum(1 for t in final_trades if t['engine'] == 'A')}, "
        f"B {sum(1 for t in final_trades if t['engine'] == 'B')}), won {sum(1 for t in final_trades if t['total'] > 0)}; "
        f"trade-wise CSV {TRADES_CSV}, day-wise {DAYS_CSV}")
    # baselines for the report
    sb_live = U.portfolio(cands, {**base, "engines": "A", "slots": 2, "addons": "hedge", "min_premium": 5.0,
                                  "max_leg": 1e9, "market_gate": False}, ner)
    b_alone = U.portfolio(cands, {**base, "engines": "B", "slots": 5, "addons": "none", "max_leg": 45_000,
                                  "market_gate": False}, ner)
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps({
        "final": {"name": final["name"], "stats": final["s"], "chosen": [(d.isoformat(), n) for d, n in final["chosen"]]},
        "trades": [{**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in t.items()},
                    "entry_time": hhmm(t["t0"]), "exit_time": hhmm(t["t1"])} for t in final_trades],
        "days": [d.isoformat() for d in days],
        "baselines": {"Super Bollinger as live (2 slots, hedge)": {d.isoformat(): sb_live["day"].get(d, 0.0) for d in days},
                      "SwingMomentum (intraday, 5 slots)": {d.isoformat(): b_alone["day"].get(d, 0.0) for d in days},
                      "Unified final": {d.isoformat(): final["s"] and sum(t["total"] for t in final_trades if t["day"] == d)
                                        for d in days}},
        "baseline_stats": {"Super Bollinger as live (2 slots, hedge)": U.stats(sb_live, days),
                           "SwingMomentum (intraday, 5 slots)": U.stats(b_alone, days)},
        "menu": [{"name": x["name"], "stats": x["s"]} for x in menu],
        "optimizers": [{"name": x["name"], "stats": x["s"]} for x in results]}, default=str))
    OUT.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
