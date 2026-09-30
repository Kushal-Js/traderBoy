"""
Loss-control tests on the HYBRID Super Bollinger backtest (30 Sep 2026, user
request: make losing days smaller). Same weekly HYBRID picks, same rules;
one switch at a time (backtest_super_bollinger_universe_30day.simulate):
  DLL_6000 / DLL_8000 - daily loss limit: no new entries once the day's
                        realized loss reaches Rs 6,000 / 8,000;
  NIFTY_GATE          - no new CE entry while NIFTY's last completed 5-min
                        close is below both its day open and its 20-bar EMA;
  COOLOFF             - after a MAX_LOSS_HIT, that stock is blocked until the
                        next weekly re-pick;
  plus combinations. Judged by total, both halves and weeks positive.
"""
from __future__ import annotations

import bisect
import io
import contextlib
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import research_stock_selection_walkforward as r
import backtest_super_bollinger_universe_30day as u
import backtest_super_trader_30day as st
from SuperTrader.strategy import IST, ema

r.wf.authenticate()
u.wf.authenticate = lambda: None
st.dhan_wrapper.client.Dhan.dhan_http.timeout = 90
picks = {date.fromisoformat(k): set(v) for k, v in
         json.load(open("history/bt_super_bollinger_universe_30day/selection_walkforward.json"))["HYBRID"]["picks"].items()}
days = r.trading_days()
as_of = r.weekly_as_of(days)
allowed = {d: picks[a] for d, a in as_of.items()}
syms = sorted(set().union(*picks.values()))

n = json.loads(Path("history/bt_super_trader_30day/underlying/_NIFTY_5min.json").read_text())
nts, nc, no = n["timestamps"], n["closes"], n["opens"]
ne = ema(nc, 20)
nday_open = {}
for k, t in enumerate(nts):
    nday_open.setdefault(datetime.fromtimestamp(t, IST).date(), no[k])


def nifty_gate(t, side):
    if side != "LONG":
        return True
    k = bisect.bisect_right(nts, t - 300) - 1  # last COMPLETED 5-min bar
    if k < 0 or ne[k] is None:
        return True
    d = datetime.fromtimestamp(nts[k], IST).date()
    weak = nc[k] < nday_open[d] and nc[k] < ne[k]
    return not weak


variants = {
    "BASELINE (HYBRID)": {},
    "DLL_6000": {"daily_loss_limit": 6000},
    "DLL_8000": {"daily_loss_limit": 8000},
    "NIFTY_GATE": {"entry_gate": nifty_gate},
    "COOLOFF": {"cooloff_week_of": as_of},
    "DLL_8000 + COOLOFF": {"daily_loss_limit": 8000, "cooloff_week_of": as_of},
    "DLL_8000 + NIFTY_GATE + COOLOFF": {"daily_loss_limit": 8000, "entry_gate": nifty_gate, "cooloff_week_of": as_of},
}
pricer = st.OptionPricer()
out = {}
for name, kw in variants.items():
    u._cand_cache.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        tr, stats = u.simulate(syms, 5, pricer, name, "long", allowed, **kw)
    daily, weekly = defaultdict(float), defaultdict(float)
    for t in tr:
        daily[t["day"]] += t["pnl_modeled"]
        weekly[as_of[date.fromisoformat(t["day"])]] += t["pnl_modeled"]
    s = u.summarize(tr)
    worst = sorted(daily.values())[:3]
    out[name] = {"summary": s, "stats": {k: v for k, v in stats.items() if k.startswith("skipped") or k == "entered"},
                 "weekly": {k.isoformat(): round(v) for k, v in sorted(weekly.items())},
                 "daily": {k: round(v) for k, v in sorted(daily.items())}}
    print(f"{name:34s} net {s['net']:>8,} H1 {s['H1']:>8,} H2 {s['H2']:>8,} dd {s['max_dd']:>8,} "
          f"green {s['green_days']}/{s['days']} weeks+ {sum(v > 0 for v in weekly.values())}/5 "
          f"worst days {[round(x) for x in worst]} | {out[name]['stats']} | option calls {pricer.calls}", flush=True)
Path("history/bt_super_bollinger_universe_30day/loss_controls.json").write_text(json.dumps(out, indent=2))
