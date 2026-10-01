"""
1-hour filter and the strong-momentum bypass on DIFFERENT stock lists (1 Oct
2026, user: "try with the shadow list stocks that we have created and curated.
Certain times it might be that those stocks aren't performing well. Just try
another back test with a different stock list").

research_super_bollinger_1h_momentum_override.py tested the bypass on the live
HYBRID list only. This repeats it on other weekly walk-forward lists, each
picked every Friday from data up to that day (3 Aug - 29 Sep, 5 slots, calls
only, live hedge on top):
  SHADOW (HYBRID_F40)  the deployed shadow list's rule: ATH top 40 -> best
                       40-session strategy fit WITH the 1-hour filter -> top 15
                       (computed here, same code path as research_selection_
                       rules_longer.py's "HYBRID_F lookback 40")
  HYBRID (live), HYBRID_TOP20, TREND_ATH_GATE (old ATH list), STRATEGY_FIT,
  MOMENTUM, RANDOM_1..3 - from history/bt_walkforward_long/selection_walkforward.json
Rules per list: no filter / live 1-hour green / green OR each bypass.

IN-SAMPLE for the filter; CACHE ONLY (no Dhan calls) - a trade whose option
prices are not cached is skipped and counted ("no option data"). Research only.
Run: uv run python research_super_bollinger_1h_override_lists.py
"""
from __future__ import annotations

import bisect
import contextlib
import io
import json
from collections import defaultdict
from datetime import date, datetime, time as dtime

with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_1h_momentum_override as mo
from fno_ath_screener import fetch_fno_universe

cf, rs, m, u, r = mo.cf, mo.rs, mo.m, mo.u, mo.r
IST = m.IST
wf = r.wf
days = r.trading_days()
as_of = r.weekly_as_of(days)
decision_dates = sorted(set(as_of.values()))


# ---- shadow-list rule (copied from research_selection_rules_longer.py, cache only) ----
def _hourly(fast):
    ends, green, key = [], [], None
    o0 = c0 = None
    for t, o, c in zip(fast["timestamps"], fast["opens"], fast["closes"]):
        dt = datetime.fromtimestamp(t, IST)
        k = (dt.date(), (dt.hour * 60 + dt.minute - 555) // 60)
        if k != key:
            if key is not None:
                green.append(c0 > o0)
            key, o0 = k, o
            ends.append(t + 300)
        else:
            ends[-1] = t + 300
        c0 = c
    if key is not None:
        green.append(c0 > o0)
    return ends, green


def _proxy_filtered(sym):
    path = r.FIVE / f"{sym}_5min.json"
    if not path.exists():
        return {}
    fast = json.loads(path.read_text())
    if not fast.get("closes") or len(fast["closes"]) < 300:
        return {}
    sig = r.bt.compute_signals(fast)
    _f, snap = r.fires_with_snapshots(fast, sig)
    ts, o, h, l, c = fast["timestamps"], fast["opens"], fast["highs"], fast["lows"], fast["closes"]
    dd = [datetime.fromtimestamp(t, IST).date() for t in ts]
    ends, green = _hourly(fast)
    res, n = defaultdict(float), defaultdict(int)
    i = 1
    while i < len(ts):
        p = snap[i - 1]
        if (p and p[0] == "BULLISH" and dd[i - 1] == dd[i] and datetime.fromtimestamp(ts[i], IST).time() < dtime(14, 0)
                and h[i] >= p[1]):
            k = bisect.bisect_right(ends, ts[i]) - 1
            if k < 0 or not green[k]:
                i += 1
                continue
            e = max(p[1], o[i])
            peak, out, j = 0.0, None, i
            while j < len(ts) and dd[j] == dd[i]:
                lo = (l[j] - e) / e * 100
                if lo <= -1.0:
                    out = -1.0
                    break
                if peak >= 0.4 and lo <= 0:
                    out = 0.0
                    break
                peak = max(peak, (h[j] - e) / e * 100)
                j += 1
            if out is None:
                out = (c[j - 1] - e) / e * 100
            res[dd[i]] += out
            n[dd[i]] += 1
            i = max(j, i + 1)
            continue
        i += 1
    return {d: (res[d], n[d]) for d in res}


def shadow_picks():
    universe = fetch_fno_universe()
    dailies = {p.stem: wf.prepare_daily(json.loads(p.read_text())) for p in r.DAILY.glob("*.json")}
    all_days = sorted({datetime.fromtimestamp(t, IST).date()
                       for t in json.loads((r.FIVE / "_NIFTY_5min.json").read_text())["timestamps"]})
    syms = [s for s in universe if s in dailies and (r.FIVE / f"{s}_5min.json").exists()]
    table = {}
    out = {}
    for dd in decision_dates:
        gated = [s for s in syms if wf.gate_check(dailies[s], dd)[0]]
        ath = wf.ath_scores({s: dailies[s] for s in gated}, dd)
        pool = sorted(ath, key=lambda s: ath[s], reverse=True)[:40]
        look = [d for d in all_days if d <= dd][-40:]
        sc = {}
        for s in pool:
            if s not in table:
                table[s] = _proxy_filtered(s)
            vals = [table[s][d] for d in look if d in table[s]]
            if sum(v[1] for v in vals) >= 3:
                sc[s] = sum(v[0] for v in vals)
        ranked = sorted(sc, key=lambda s: sc[s], reverse=True)[:15]
        ranked += [s for s in pool if s not in ranked][:15 - len(ranked)]
        out[dd] = set(ranked)
    return out


LISTS = {"SHADOW (HYBRID_F40)": None, "HYBRID (live)": "HYBRID", "HYBRID_TOP20": "HYBRID_TOP20",
         "TREND_ATH_GATE (old ATH list)": "TREND_ATH_GATE", "STRATEGY_FIT": "STRATEGY_FIT", "MOMENTUM": "MOMENTUM",
         "RANDOM_1": "RANDOM_1", "RANDOM_2": "RANDOM_2", "RANDOM_3": "RANDOM_3"}
RULES = [("no filter", None, False), ("1h green (live)", None, True),
         ("OR day up >= 1.0%", mo.OVERRIDES["stock up >= 1.0% on the day and above day open"], True),
         ("OR day up >= 1.5%", mo.OVERRIDES["stock up >= 1.5% on the day and above day open"], True),
         ("OR volume surge", mo.OVERRIDES["volume surge: last 5m bar green, vol >= 2x 20-bar avg"], True),
         ("OR ADX>=25 + 15m ST up", mo.OVERRIDES["trend: ADX(5m) >= 25 and 15m Supertrend up"], True),
         ("OR forming hour up >= 0.5%", mo.OVERRIDES["forming hour up >= 0.5%"], True)]

picks_all = {}
with contextlib.redirect_stdout(io.StringIO()):
    picks_all["SHADOW (HYBRID_F40)"] = shadow_picks()
data = json.loads((m.LONG_DIR / "selection_walkforward.json").read_text())
for name, key in LISTS.items():
    if key:
        picks_all[name] = {date.fromisoformat(k): set(v) for k, v in data[key]["picks"].items()}

sh, hy = picks_all["SHADOW (HYBRID_F40)"], picks_all["HYBRID (live)"]
print("SHADOW vs HYBRID overlap per decision date:",
      ", ".join(f"{d.isoformat()[5:]} {len(sh[d] & hy.get(d, set()))}/15" for d in decision_dates if d in sh), "\n")

summary = []
for lname, picks in picks_all.items():
    allowed = {d: picks[a] for d, a in as_of.items() if a in picks}
    syms = sorted(set().union(*picks.values()))
    print(f"== {lname} ({len(syms)} distinct stocks over the window)")
    for rname, ov, use_gate in RULES:
        log: set = set()
        gate = mo.make_gate(ov, log) if use_gate else None
        with contextlib.redirect_stdout(io.StringIO()):
            trades, st = u.simulate(syms, r.CAP, m.pr, f"{lname}_{rname}", "long", allowed, symbol_gate=gate)
            evs = [rs.prep(t) for t in trades]
            hedge = [rs.put_side(ev, False, None)[0] for ev in evs]
        keys = {(s_, datetime.fromtimestamp(tt, IST).date().isoformat(), datetime.fromtimestamp(tt, IST).strftime("%H:%M"))
                for s_, tt in log}
        ovr = [t for t in trades if (t["symbol"], t["day"], t["entry_time"]) in keys]
        o_pnl = sum(float(t["pnl_modeled"]) for t in ovr)
        o_won = sum(1 for t in ovr if float(t["pnl_modeled"]) > 0)
        print(f"  {rname:28s} {len(trades):4d} tr, refused {st.get('skipped_symbol_gate', 0):3d}, no-opt {st.get('skipped_no_option_data', 0):3d}"
              f" | calls {mo.stats(trades, [0.0] * len(trades))} | +hedge {mo.stats(trades, hedge)}"
              + (f" | bypass: {len(ovr)} traded, {o_won} won, {o_pnl:+,.0f}" if ov else ""), flush=True)
    print(flush=True)
print(f"Dhan calls made: {m.pr.calls} (cache-only: refused lookups, not network calls)")
