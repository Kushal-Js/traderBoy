"""
Day-by-day and trade-by-trade result of the setup chosen on 30 Sep 2026:
  entry filter   the stock's last closed 1-hour candle is green
  call           Super Bollinger CE (max loss 4,500, breakeven after +1,500, 15:15)
  PUT side (S2)  hedge at CE loss 1,800 (+1 ATR), 2nd PUT lot on Supertrend-bearish, sell one lot at combined
                 +4,000, ride the other on the 30% trail with a purchase-price floor
  call side (S1) one more call when the stock is back at its trigger (Supertrend bullish, before 14:00), own
                 4,500 max loss, exits with the original
HYBRID walk-forward list, 3 Aug - 29 Sep, 5 slots, real option prices, modeled slippage. CACHE ONLY.
Writes research_results/2026-09-30_final_setup_trades.csv and ..._daily.csv.

Run: uv run python research_super_bollinger_final_setup_trades.py
"""
from __future__ import annotations

import contextlib
import csv
import io
from collections import defaultdict

with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_chop_filter_resim as rsim
rs, m, u, r = rsim.rs, rsim.m, rsim.u, rsim.r
with contextlib.redirect_stdout(io.StringIO()):
    trades, st = u.simulate(rsim.syms, r.CAP, m.pr, "FINAL", "long", rsim.allowed, symbol_gate=rsim.candle_gate(60))
    evs = [rs.prep(t) for t in trades]
    s2 = [rs.put_side(ev, True, 4000) for ev in evs]
    s1 = [rs.call_side(ev, confirm=True, lot2_exit="with", arm=0, pair_cap=False, reentry=False) for ev in evs]
rows, daily = [], defaultdict(lambda: defaultdict(float))
for t, ev, (p2, i2), (p1, i1) in zip(trades, evs, s2, s1):
    call = float(t["pnl_modeled"])
    put_note = "" if ev["th"] is None else ("hedge" + (" + 2nd lot" if i2.get("added") else "") + (", one sold at target" if i2.get("booked") else ""))
    rows.append({"day": t["day"], "symbol": t["symbol"], "contract": t["contract"], "entry_time": t["entry_time"],
                 "exit_time": t["exit_time"], "qty": t["qty"], "entry": t["entry"], "exit": t["exit"], "exit_reason": t["reason"],
                 "call_pnl": round(call), "put_side_pnl": round(p2), "put_side": put_note,
                 "extra_call_pnl": round(p1), "extra_call": "re-added" if i1.get("readd") else "",
                 "trade_total": round(call + p2 + p1)})
    d = daily[t["day"]]
    d["trades"] += 1; d["call"] += call; d["put"] += p2; d["extra_call"] += p1; d["total"] += call + p2 + p1
    d["wins"] += call + p2 + p1 > 0
with open("research_results/2026-09-30_final_setup_trades.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)
cum = peak = dd = 0.0
out = []
for day in sorted(daily):
    d = daily[day]
    cum += d["total"]; peak = max(peak, cum); dd = min(dd, cum - peak)
    out.append({"day": day, "trades": int(d["trades"]), "winning_trades": int(d["wins"]), "calls": round(d["call"]),
                "put_side": round(d["put"]), "extra_call": round(d["extra_call"]), "day_total": round(d["total"]),
                "running_total": round(cum)})
with open("research_results/2026-09-30_final_setup_daily.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0]))
    w.writeheader(); w.writerows(out)
print(f"trades {len(trades)} (refused by the 1h filter {st.get('skipped_symbol_gate', 0)}) | hedges {sum(1 for e in evs if e['th'])}"
      f" | 2nd PUT lots {sum(1 for _p, i in s2 if i.get('added'))} (target hit {sum(1 for _p, i in s2 if i.get('booked'))})"
      f" | call re-adds {sum(1 for _p, i in s1 if i.get('readd'))}")
print(f"{'day':10s} {'trades':>6s} {'wins':>4s} {'calls':>9s} {'PUT side':>9s} {'extra call':>10s} {'DAY TOTAL':>10s} {'running':>10s}")
for o in out:
    print(f"{o['day']:10s} {o['trades']:>6d} {o['winning_trades']:>4d} {o['calls']:>+9,} {o['put_side']:>+9,} {o['extra_call']:>+10,} {o['day_total']:>+10,} {o['running_total']:>+10,}")
tot = {k: sum(o[k] for o in out) for k in ("trades", "winning_trades", "calls", "put_side", "extra_call", "day_total")}
aug = sum(o["day_total"] for o in out if o["day"] < "2026-08-31")
print(f"{'TOTAL':10s} {tot['trades']:>6d} {tot['winning_trades']:>4d} {tot['calls']:>+9,} {tot['put_side']:>+9,} {tot['extra_call']:>+10,} {tot['day_total']:>+10,}")
print(f"August {aug:+,} | September {tot['day_total'] - aug:+,} | max drawdown {dd:,.0f} | worst day {min(o['day_total'] for o in out):+,} | "
      f"best day {max(o['day_total'] for o in out):+,} | green days {sum(o['day_total'] > 0 for o in out)}/{len(out)}")
