"""
Hedge-on-drawdown test on the HYBRID Super Bollinger backtest (30 Sep 2026,
user idea): when a CE trade's open loss reaches Rs 2,000, buy 1 lot of the
same stock's ATM PE (keep the CE, its rules unchanged) and exit the PE by:
  H1 - CE recovers Rs 1,000 off its low since the hedge, PE stop Rs 2,000, 15:15;
  H2 - PE trailing: after PE profit >= Rs 1,000, exit on a 40% giveback of
       its best profit; PE stop Rs 2,000; 15:15;
  H3 - hold the PE until the CE trade ends (or 15:15).
The CE trades don't depend on the PE (hedges don't take a capacity slot;
funds not modeled), so total = baseline + hedge PnL. Real option prices
(expired-options data) for every PE; slippage on both PE legs.
"""
from __future__ import annotations

import bisect
import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

import walkforward_selector_eval as wf
import backtest_super_trader_30day as st
from Bollinger.paper_book import modeled_slippage_pct

IST = ZoneInfo("Asia/Kolkata")
TRIGGER, PE_STOP, H1_RECOVER, H2_ARM, H2_GIVEBACK = 2000, 2000, 1000, 1000, 0.40
wf.authenticate()
wf.dhan_wrapper.client.Dhan.dhan_http.timeout = 90
pr = st.OptionPricer()
rows = list(csv.DictReader(open("history/bt_super_bollinger_universe_30day/HYBRID_trades.csv")))
one, results = {}, []
for r in rows:
    sym, d = r["symbol"], date.fromisoformat(r["day"])
    if sym not in one:
        one[sym] = json.load(open(f"history/bt_super_trader_30day/underlying_1m/{sym}_1min.json"))
    m = one[sym]
    t0 = int(datetime.combine(d, datetime.strptime(r["entry_time"], "%H:%M").time(), IST).timestamp())
    t1 = int(datetime.combine(d, datetime.strptime(r["exit_time"], "%H:%M").time(), IST).timestamp())
    sq = int(datetime.combine(d, dtime(15, 15), IST).timestamp())
    ce = pr.open_leg(sym, d, "LONG", t0, sq, m["closes"][bisect.bisect_right(m["timestamps"], t0) - 1])
    if not ce:
        continue
    cm, p0, qty = ce["mins"], float(r["entry"]), int(r["qty"])
    th = next((t for i, t in enumerate(cm.ts) if t0 < t <= t1 and (p0 - cm.l[i]) * qty >= TRIGGER), None)
    if th is None:
        continue
    spot = m["closes"][bisect.bisect_right(m["timestamps"], th) - 1]
    pe = pr.open_leg(sym, d, "SHORT", th, sq, spot)
    if not pe:
        results.append((r, None))
        continue
    pm = pe["mins"]
    i0 = bisect.bisect_right(pm.ts, th) - 1
    if i0 < 0 or th - pm.ts[i0] > 300:
        results.append((r, None))
        continue
    q0 = pm.c[i0]

    def pnl(px):
        e = q0 * (1 + modeled_slippage_pct(q0)); x = px * (1 - modeled_slippage_pct(px))
        return (x - e) * qty

    def run(rule):
        ce_low = p0 - TRIGGER / qty
        peak = 0.0
        for k in range(i0 + 1, len(pm.ts)):
            t = pm.ts[k]
            if t >= sq:
                return pnl(pm.o[k]), "15:15"
            if (q0 - pm.l[k]) * qty >= PE_STOP and rule != "H3":
                return pnl(q0 - PE_STOP / qty), "PE_STOP"
            ci = bisect.bisect_right(cm.ts, t) - 1
            if ci >= 0:
                ce_low = min(ce_low, cm.l[ci])
                if rule == "H1" and (cm.c[ci] - ce_low) * qty >= H1_RECOVER:
                    return pnl(pm.c[k]), "CE_RECOVERED"
            if rule == "H2":
                prof = (pm.h[k] - q0) * qty
                peak = max(peak, prof)
                if peak >= H2_ARM and (pm.c[k] - q0) * qty <= peak * (1 - H2_GIVEBACK):
                    return pnl(pm.c[k]), "PE_TRAIL"
            if rule == "H3" and t >= t1:
                return pnl(pm.c[k]), "CE_EXITED"
        return pnl(pm.c[-1]), "END"

    results.append((r, {h: run(h) for h in ("H1", "H2", "H3")}))

base = sum(float(r["pnl_modeled"]) for r in rows)
done = [(r, v) for r, v in results if v]
print(f"CE trades that reached -Rs {TRIGGER}: {len(results)} (PE priced for {len(done)}); option calls made: {pr.calls}")
for h in ("H1", "H2", "H3"):
    hp = [v[h][0] for _, v in done]
    daily = defaultdict(float)
    for r in rows:
        daily[r["day"]] += float(r["pnl_modeled"])
    for r, v in done:
        daily[r["day"]] += v[h][0]
    h1 = sum(v for k, v in daily.items() if k < "2026-09-15"); h2 = sum(v for k, v in daily.items() if k >= "2026-09-15")
    eq = pk = dd = 0.0
    for k in sorted(daily):
        eq += daily[k]; pk = max(pk, eq); dd = min(dd, eq - pk)
    reasons = defaultdict(int)
    for _, v in done:
        reasons[v[h][1]] += 1
    print(f"{h}: hedge PnL {sum(hp):+,.0f} (wins {sum(x > 0 for x in hp)}/{len(hp)}, avg {statistics.mean(hp):+,.0f}) -> "
          f"TOTAL {base + sum(hp):+,.0f} vs baseline {base:+,.0f} | H1 {h1:+,.0f} H2 {h2:+,.0f} dd {dd:,.0f} "
          f"green {sum(v > 0 for v in daily.values())}/{len(daily)} worst {[round(x) for x in sorted(daily.values())[:3]]} | exits {dict(reasons)}")
json.dump([{"day": r["day"], "symbol": r["symbol"], "ce_pnl": float(r["pnl_modeled"]),
            **({h: round(v[h][0]) for h in v} if v else {})} for r, v in results],
          open("history/bt_super_bollinger_universe_30day/hedge_on_drawdown.json", "w"), indent=2)
