"""
1-hour filter with a strong-momentum override (1 Oct 2026, user request: "let
the one hour rule get bypassed when there is very strong momentum and very
strong signals for an upside / buy call").

Live rule: a Super Bollinger call is taken only if the stock's last CLOSED
60-min candle (09:15-anchored) is green. This tests "green OR <override>",
where each override is a different definition of "very strong upside
momentum", measured only from data known at the trigger touch (closed 5-min
bars, the forming hour's open, yesterday's bars). Same walk-forward book and
portfolio simulator as research_super_bollinger_chop_filter_resim.py (HYBRID
picks, 3 Aug - 29 Sep, 5 slots, CE only; live hedge on top).

IN-SAMPLE: the 1-hour filter itself was chosen on this window, and each
override only changes a handful of refused trades - read the counts.
CACHE ONLY (no Dhan calls). Research only.
Run: uv run python research_super_bollinger_1h_momentum_override.py
"""
from __future__ import annotations

import bisect
import contextlib
import io
from collections import defaultdict

with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_chop_filters as cf
rs, m = cf.rs, cf.m
u, r = m.u, m.r
SPLIT = "2026-08-31"
picks = m.load_picks("HYBRID")
days = r.trading_days()
as_of = r.weekly_as_of(days)
allowed = {d: picks[a] for d, a in as_of.items() if a in picks}
syms = sorted(set().union(*picks.values()))


def ctx(sym, t):
    """Everything an override may look at, known at time t (trigger touch)."""
    s = cf.series(sym)
    b = s["b"]
    k = bisect.bisect_right(s["ts"], t - 300) - 1          # last CLOSED 5-min bar
    if k < 60:
        return None
    spot = b["closes"][k]
    f = cf.features(sym, t, spot)
    h1 = s["h1"]
    kh = cf.last_closed(h1["end"], t)
    f["h1_green"] = kh >= 1 and h1["c"][kh] > h1["o"][kh]
    # forming hour: the 60-min bucket after the last closed one, if it has started
    if kh + 1 < len(h1["o"]) and h1["ts"][kh + 1] <= t:
        f["form_hour_up_pct"] = (spot / h1["o"][kh + 1] - 1) * 100
    vols = b.get("volumes") or [0] * len(b["closes"])
    prior = vols[max(0, k - 20):k]
    avg = sum(prior) / len(prior) if prior else 0
    f["vol_ratio"] = vols[k] / avg if avg else 0
    f["bar_green"] = b["closes"][k] > b["opens"][k]
    d = cf.datetime.fromtimestamp(t, cf.IST).date()
    di = s["dlist"].index(d) if d in s["days"] else None
    if di:
        prev_close = b["closes"][s["days"][s["dlist"][di - 1]][-1]]
        f["day_up_pct"] = (spot / prev_close - 1) * 100
    return f


OVERRIDES = {
    "stock up >= 1.0% on the day and above day open": lambda f: f.get("day_up_pct", 0) >= 1.0 and f.get("above_day_open"),
    "stock up >= 1.5% on the day and above day open": lambda f: f.get("day_up_pct", 0) >= 1.5 and f.get("above_day_open"),
    "volume surge: last 5m bar green, vol >= 2x 20-bar avg": lambda f: f.get("bar_green") and f.get("vol_ratio", 0) >= 2,
    "trend: ADX(5m) >= 25 and 15m Supertrend up": lambda f: (f.get("adx5") or 0) >= 25 and f.get("st15_up"),
    "efficiency ratio(10) >= 0.6 (clean one-way move)": lambda f: (f.get("er10") or 0) >= 0.6,
    "forming hour up >= 0.5%": lambda f: f.get("form_hour_up_pct", 0) >= 0.5,
    "day breakout: above yesterday's high and 30-min high": lambda f: f.get("above_prev_high") and f.get("above_or30_high"),
    "strict: up >= 1% AND vol surge AND 15m ST up": lambda f: (f.get("day_up_pct", 0) >= 1.0 and f.get("bar_green")
                                                               and f.get("vol_ratio", 0) >= 2 and f.get("st15_up")),
    "strict: ADX >= 25 AND ER10 >= 0.5 AND above day open": lambda f: ((f.get("adx5") or 0) >= 25
                                                                       and (f.get("er10") or 0) >= 0.5
                                                                       and f.get("above_day_open")),
}


def make_gate(override, log):
    def gate(sym, t, _side):
        f = ctx(sym, t)
        if f is None:
            return False
        if f["h1_green"]:
            return True
        if override is not None and override(f):
            log.add((sym, t))
            return True
        return False
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
    return f"{a + b:>+9,.0f} (Aug {a:>+8,.0f} Sep {b:>+8,.0f}) dd {dd:>8,.0f} worst {min(daily.values()):>+8,.0f}"


print("rule                                                   trades refused | calls only | calls + live hedge"
      " | overridden trades: n, won, PnL")
runs = [("no filter", None, False), ("live: last 60-min candle green", None, True)] + \
    [(f"green OR {k}", v, True) for k, v in OVERRIDES.items()]
for name, ov, use_gate in runs:
    log: set = set()
    gate = make_gate(ov, log) if use_gate else None
    with contextlib.redirect_stdout(io.StringIO()):
        trades, st = u.simulate(syms, r.CAP, m.pr, f"HYBRID_{name}", "long", allowed, symbol_gate=gate)
        evs = [rs.prep(t) for t in trades]
        hedge = [rs.put_side(ev, False, None)[0] for ev in evs]
    keys = {(s_, cf.datetime.fromtimestamp(tt, cf.IST).date().isoformat(),
             cf.datetime.fromtimestamp(tt, cf.IST).strftime("%H:%M")) for s_, tt in log}
    ovr = [t for t in trades if (t["symbol"], t["day"], t["entry_time"]) in keys]
    o_pnl = sum(float(t["pnl_modeled"]) for t in ovr)
    o_won = sum(1 for t in ovr if float(t["pnl_modeled"]) > 0)
    print(f"{name[:54]:54s} {len(trades):4d} {st.get('skipped_symbol_gate', 0):4d}"
          f" | {stats(trades, [0.0] * len(trades))} | {stats(trades, hedge)}"
          f" | {len(log)} let through, {len(ovr)} traded, {o_won} won, {o_pnl:+,.0f}", flush=True)
print(f"\nDhan calls made: {m.pr.calls} (cache-only: refused lookups, not network calls)")
