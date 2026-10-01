"""
1-hour entry filter: 09:15-anchored candle vs a ROLLING last hour (1 Oct 2026,
user: "1 hour candle would be last trading day 14:45 up to 09:30 for today").

  session  the last CLOSED 60-min candle anchored at 09:15 each day (live rule
           since 30 Sep; before 10:15 it is the previous session's 15:15-15:30
           stub)
  rolling  the last 12 CLOSED 5-min bars in trading time, back across the
           overnight gap (at 09:30: previous session 14:45 -> today 09:30)

  overnight 15:00->09:45  (user, 1 Oct 2026, 2nd version) hourly candles on a grid
           anchored at 09:45 whose overnight candle runs previous session
           15:00 -> today 09:45 (30 + 30 min); in-day candles 09:45-10:45 ...
           13:45-14:45 and a 14:45-15:00 stub. Last CLOSED candle, green.
  overnight 14:45->09:30  the first example read the same way (grid at 09:30,
           overnight 14:45 -> 09:30, stub 14:30-14:45).

Same harness as research_super_bollinger_chop_filter_resim.py: HYBRID
walk-forward picks 3 Aug - 29 Sep, CE only, through the portfolio simulator
(the slot limit is respected, a refused trade frees a slot), then the live
hedge and S1 on top. Green = close > open. CACHE ONLY. In-sample.

Run: uv run python research_super_bollinger_1h_rolling_window.py
"""
from __future__ import annotations

import bisect
import contextlib
import io
from collections import defaultdict
from datetime import datetime

with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_chop_filters as cf
rs, m = cf.rs, cf.m
u, r = m.u, m.r
IST = cf.IST
SPLIT = "2026-08-31"
picks = m.load_picks("HYBRID")
days = r.trading_days()
as_of = r.weekly_as_of(days)
allowed = {d: picks[a] for d, a in as_of.items() if a in picks}
syms = sorted(set().union(*picks.values()))
decisions: dict = defaultdict(list)     # gate name -> [(sym, t, green, before_1015)]


def session_gate(sym, t, _side):
    h = cf.series(sym)["h1"]
    k = cf.last_closed(h["end"], t)
    if k < 1:
        return False
    g = h["c"][k] > h["o"][k]
    decisions["session"].append((sym, t, g))
    return g


_ends: dict = {}


def rolling_gate(sym, t, _side, bars=12):
    b = cf.series(sym)["b"]
    if sym not in _ends:
        _ends[sym] = [x + 300 for x in b["timestamps"]]
    k = bisect.bisect_right(_ends[sym], t) - 1
    if k < bars - 1:
        return False
    g = b["closes"][k] > b["opens"][k - bars + 1]
    decisions["rolling"].append((sym, t, g))
    return g


_grid: dict = {}


def grid_candles(sym, anchor_min, overnight_start_min):
    """Hourly candles on a fixed daily grid whose overnight candle spans the session break:
    [overnight_start (prev session) -> anchor (today)], then anchor + 60 ... up to overnight_start."""
    key = (sym, anchor_min, overnight_start_min)
    if key in _grid:
        return _grid[key]
    b = cf.series(sym)["b"]
    bounds = list(range(anchor_min, overnight_start_min, 60)) + [overnight_start_min]
    day_list = sorted({datetime.fromtimestamp(t, IST).date() for t in b["timestamps"]})
    day_idx = {d: i for i, d in enumerate(day_list)}
    out = {"o": [], "c": [], "end": [], "start": []}
    cur = None
    for t, o, c in zip(b["timestamps"], b["opens"], b["closes"]):
        dt = datetime.fromtimestamp(t, IST)
        mm = dt.hour * 60 + dt.minute
        di = day_idx[dt.date()]
        if mm < anchor_min:
            k = (di, -1)                       # overnight candle that ends at today's anchor
        elif mm >= overnight_start_min:
            k = (di + 1, -1)                   # overnight candle that ends at the next session's anchor
        else:
            k = (di, bisect.bisect_right(bounds, mm) - 1)
        if k != cur:
            cur = k
            out["o"].append(o); out["c"].append(c); out["end"].append(t + 300); out["start"].append(t)
        else:
            out["c"][-1] = c
            out["end"][-1] = t + 300
    _grid[key] = out
    return out


def grid_gate(anchor_min, overnight_start_min, label):
    def gate(sym, t, _side):
        h = grid_candles(sym, anchor_min, overnight_start_min)
        k = cf.last_closed(h["end"], t)
        if k < 1:
            return False
        g = h["c"][k] > h["o"][k]
        decisions[label].append((sym, t, g))
        return g
    return gate


def stats(trades, extra):
    daily = defaultdict(float)
    for t, e in zip(trades, extra):
        daily[t["day"]] += float(t["pnl_modeled"]) + e
    eq = pk = dd = 0.0
    for d in sorted(daily):
        eq += daily[d]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    a = sum(v for d, v in daily.items() if d < SPLIT)
    b = sum(v for d, v in daily.items() if d >= SPLIT)
    wins = sum(1 for t, e in zip(trades, extra) if float(t["pnl_modeled"]) + e > 0)
    return (f"{a + b:>+9,.0f} (Aug {a:>+8,.0f} Sep {b:>+8,.0f}) dd {dd:>8,.0f} worst day {min(daily.values()):>+8,.0f}"
            f" wins {wins}/{len(trades)}")


def early(trades):
    """Trades entered before 10:15 (where the two rules differ most)."""
    out = [t for t in trades if str(t.get("entry_time", "99:99")) < "10:15"]
    return len(out), sum(float(t["pnl_modeled"]) for t in out)


print("filter                    trades  skipped(filter / no option data) | plain | + hedge + S1 | entries before 10:15")
GATES = (("no filter", None), ("session (live now)", session_gate), ("rolling last 60 min", rolling_gate),
         ("overnight 15:00->09:45", grid_gate(9 * 60 + 45, 15 * 60, "g0945")),
         ("overnight 14:45->09:30", grid_gate(9 * 60 + 30, 14 * 60 + 45, "g0930")))
for name, gate in GATES:
    with contextlib.redirect_stdout(io.StringIO()):
        trades, st = u.simulate(syms, r.CAP, m.pr, f"HYBRID_1h_{name}", "long", allowed, symbol_gate=gate)
        evs = [rs.prep(t) for t in trades]
        hedge = [rs.put_side(ev, False, None)[0] for ev in evs]
        s1 = [rs.call_side(ev, confirm=True, lot2_exit="with", arm=0, pair_cap=False, reentry=False)[0] for ev in evs]
    n_early, pnl_early = early(trades)
    print(f"{name:25s} {len(trades):4d}   {st.get('skipped_symbol_gate', 0):3d} / {st.get('skipped_no_option_data', 0):3d}"
          f"\n      plain        {stats(trades, [0.0] * len(trades))}\n      +hedge +S1   "
          f"{stats(trades, [a + b for a, b in zip(hedge, s1)])}\n      before 10:15 {n_early} trades {pnl_early:+,.0f}",
          flush=True)

# Where each rule disagrees with the live (session) rule on the same signal
sess = {(s, t): g for s, t, g in decisions["session"]}
for other in ("rolling", "g0945", "g0930"):
    oth = {(s, t): g for s, t, g in decisions[other]}
    both = set(sess) & set(oth)
    dis = [k for k in both if sess[k] != oth[k]]
    early_dis = [k for k in dis if datetime.fromtimestamp(k[1], IST).strftime("%H:%M") < "10:15"]
    print(f"\nsession vs {other}: {len(both)} signals judged by both, disagree on {len(dis)} ({len(early_dis)} before "
          f"10:15): session-only green {sum(1 for k in dis if sess[k])}, {other}-only green {sum(1 for k in dis if oth[k])}")
print(f"Dhan calls made: {m.pr.calls} (cache-only: these are refused lookups, not network calls)")
