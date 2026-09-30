"""
Stop-and-re-enter test on the HYBRID Super Bollinger backtest (30 Sep 2026,
user idea): exit a losing trade at a Rs cap, watch the same option, and buy
it again if it trades back up to the original entry price. Same weekly
HYBRID picks and rules otherwise. Judged by total, both halves, weeks
positive, worst day and drawdown.
"""
from __future__ import annotations

import io
import contextlib
import json
from collections import defaultdict
from datetime import date, time as dtime
from pathlib import Path

import research_stock_selection_walkforward as r
import backtest_super_bollinger_universe_30day as u
import backtest_super_trader_30day as st

r.wf.authenticate()
u.wf.authenticate = lambda: None
st.dhan_wrapper.client.Dhan.dhan_http.timeout = 90
picks = {date.fromisoformat(k): set(v) for k, v in
         json.load(open("history/bt_super_bollinger_universe_30day/selection_walkforward.json"))["HYBRID"]["picks"].items()}
days = r.trading_days()
as_of = r.weekly_as_of(days)
allowed = {d: picks[a] for d, a in as_of.items()}
syms = sorted(set().union(*picks.values()))

variants = {
    "BASELINE (HYBRID, Rs 4,500 max loss)": {},
    "STOP 2000, no re-entry": {"soft_stop_rs": 2000, "max_reentries": 0},
    "STOP 2000, 1 re-entry until 14:00": {"soft_stop_rs": 2000, "max_reentries": 1, "reentry_cutoff": dtime(14, 0)},
    "STOP 2000, 1 re-entry until 15:00": {"soft_stop_rs": 2000, "max_reentries": 1, "reentry_cutoff": dtime(15, 0)},
    "STOP 2000, 2 re-entries until 15:00": {"soft_stop_rs": 2000, "max_reentries": 2, "reentry_cutoff": dtime(15, 0)},
    "STOP 1500, 1 re-entry until 15:00": {"soft_stop_rs": 1500, "max_reentries": 1, "reentry_cutoff": dtime(15, 0)},
    "STOP 2500, 1 re-entry until 15:00": {"soft_stop_rs": 2500, "max_reentries": 1, "reentry_cutoff": dtime(15, 0)},
}
pricer = st.OptionPricer()
out = {}
for name, kw in variants.items():
    with contextlib.redirect_stdout(io.StringIO()):
        tr, stats = u.simulate(syms, 5, pricer, name, "long", allowed, **kw)
    daily, weekly, reasons = defaultdict(float), defaultdict(float), defaultdict(lambda: [0, 0.0])
    for t in tr:
        daily[t["day"]] += t["pnl_modeled"]
        weekly[as_of[date.fromisoformat(t["day"])]] += t["pnl_modeled"]
        reasons[t["reason"]][0] += 1
        reasons[t["reason"]][1] += t["pnl_modeled"]
    s = u.summarize(tr)
    out[name] = {"summary": s, "stats": stats, "daily": {k: round(v) for k, v in sorted(daily.items())},
                 "weekly": {k.isoformat(): round(v) for k, v in sorted(weekly.items())},
                 "by_reason": {k: [n, round(v)] for k, (n, v) in reasons.items()}}
    worst = sorted(daily.values())[:3]
    print(f"{name:40s} net {s['net']:>8,} H1 {s['H1']:>8,} H2 {s['H2']:>8,} dd {s['max_dd']:>8,} "
          f"green {s['green_days']}/{s['days']} weeks+ {sum(v > 0 for v in weekly.values())}/5 "
          f"worst {[round(x) for x in worst]} trades {s['n']} reentered {stats.get('reentered', 0)} "
          f"| {out[name]['by_reason']} | calls {pricer.calls}", flush=True)
Path("history/bt_super_bollinger_universe_30day/stop_reenter.json").write_text(json.dumps(out, indent=2))
