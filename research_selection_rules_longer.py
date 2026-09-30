"""
Stock-selection rules compared over a LONGER walk-forward (30 Sep 2026, user:
"we must have chosen bad stocks for August, or maybe we have to change stock
selection based on how NIFTY is performing").

Every Friday (from the first week with 20 sessions of history - about 14
weekly decisions, 29 Jun - 29 Sep) each rule picks 15 stocks from the liquid
F&O universe using ONLY data up to that day. The pick is then scored on the
FOLLOWING week with the strategy's own rules on the stock price ("proxy":
BULLISH resting trigger, -1% stop, breakeven after +0.4%, else close; entries
before 14:00; one position per stock) - with the live 1-hour-green entry
filter, and without it for reference. No option prices, no slot limit: this
ranks LISTS, it is not a rupee backtest. CACHE ONLY.

Rules
  HYBRID            ATH top 40 -> best 20-session proxy -> top 15   (live)
  HYBRID_F          the same, but the 20-session proxy uses the 1-hour filter
  HYBRID lookbacks  10 / 40 sessions;  HYBRID pools 25 / 60 / 80
  FIT               best 20-session proxy from the whole gated universe
  FIT_F / FIT_F10   ... with the filter / 10 sessions
  HITRATE           share of positive proxy days (20 sessions), ATH top 40 pool
  ATH               ATH score only (the old weekly list)
  RESILIENT         best average return on the days NIFTY FELL (last 20 sessions)
  RESILIENT_FIT     the 40 most resilient -> best filtered proxy -> top 15
  LOWCHOP           highest 20-day efficiency ratio (daily closes)
  NIFTY_SWITCH      NIFTY below its 20-day average -> RESILIENT_FIT, else HYBRID_F
  NIFTY_SWITCH2     NIFTY 10-day return < 0 -> RESILIENT_FIT, else HYBRID_F
  RANDOM            300 random lists of 15 (the luck distribution)

Run: uv run python research_selection_rules_longer.py
"""
from __future__ import annotations

import bisect
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta

sys.argv = [sys.argv[0], "aug", "--cache-only"]
import research_super_bollinger_bearish_side as m  # noqa: E402  (sets the long-history data dirs, cache-only guards)
from fno_ath_screener import fetch_fno_universe  # noqa: E402

r = m.r
wf, IST = r.wf, m.IST
TOP = 15
FIRST_WEEK = date(2026, 6, 29)


def hourly(fast):
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


def proxy(sym, filtered: bool):
    """{day: (sum of trade results in % of the stock price, trades)} - r.fit_proxy's rules, optional 1-hour filter."""
    fast = json.loads((r.FIVE / f"{sym}_5min.json").read_text())
    if not fast.get("closes") or len(fast["closes"]) < 300:
        return {}
    sig = r.bt.compute_signals(fast)
    _f, snap = r.fires_with_snapshots(fast, sig)
    ts, o, h, l, c = fast["timestamps"], fast["opens"], fast["highs"], fast["lows"], fast["closes"]
    days = [datetime.fromtimestamp(t, IST).date() for t in ts]
    ends, green = hourly(fast) if filtered else ([], [])
    res, n = defaultdict(float), defaultdict(int)
    i = 1
    while i < len(ts):
        p = snap[i - 1]
        if (p and p[0] == "BULLISH" and days[i - 1] == days[i] and datetime.fromtimestamp(ts[i], IST).time() < dtime(14, 0)
                and h[i] >= p[1]):
            if filtered:
                k = bisect.bisect_right(ends, ts[i]) - 1
                if k < 0 or not green[k]:
                    i += 1
                    continue
            e = max(p[1], o[i])
            peak, out, j = 0.0, None, i
            while j < len(ts) and days[j] == days[i]:
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
            res[days[i]] += out
            n[days[i]] += 1
            i = max(j, i + 1)
            continue
        i += 1
    return {d: (res[d], n[d]) for d in res}


universe = fetch_fno_universe()
dailies = {p.stem: wf.prepare_daily(json.loads(p.read_text())) for p in r.DAILY.glob("*.json")}
all_days = r.trading_days()
syms = [s for s in universe if (r.FIVE / f"{s}_5min.json").exists() and s in dailies]
print(f"universe {len(universe)}, with data {len(syms)}; 5-min history {all_days[0]} .. {all_days[-1]}", flush=True)
PLAIN = {s: proxy(s, False) for s in syms}
FILT = {s: proxy(s, True) for s in syms}
print("proxies computed", flush=True)

nifty = json.loads((r.FIVE / "_NIFTY_5min.json").read_text())
nclose = {}
for t, c in zip(nifty["timestamps"], nifty["closes"]):
    nclose[datetime.fromtimestamp(t, IST).date()] = c
ndays = sorted(nclose)

weeks = defaultdict(list)
for d in all_days:
    weeks[d - timedelta(days=d.weekday())].append(d)
DECISIONS = []
for mon in sorted(weeks):
    prior = [x for x in all_days if x < mon]
    if mon >= FIRST_WEEK and len(prior) >= 22:
        DECISIONS.append((prior[-1], mon, weeks[mon]))
print(f"{len(DECISIONS)} weekly decisions: {DECISIONS[0][1]} .. {DECISIONS[-1][1]}", flush=True)


def fit(pool, as_of, table, lookback=20, min_trades=3, hit=False):
    look = [d for d in all_days if d <= as_of][-lookback:]
    sc = {}
    for s in pool:
        per = table.get(s, {})
        vals = [per[d] for d in look if d in per]
        if sum(v[1] for v in vals) >= min_trades:
            sc[s] = (sum(1 for v in vals if v[0] > 0) / len(vals)) if hit else sum(v[0] for v in vals)
    return sc


def top(sc, n=TOP, fill=()):
    ranked = sorted(sc, key=lambda s: sc[s], reverse=True)[:n]
    ranked += [s for s in fill if s not in ranked][:n - len(ranked)]
    return ranked


def daily_returns(sym, as_of, n=20):
    d = dailies[sym]
    idx = [i for i, x in enumerate(d["dates"]) if x <= as_of][-(n + 1):]
    return {d["dates"][i]: d["close"][i] / d["close"][j] - 1 for j, i in zip(idx, idx[1:])}


def resilience(gated, as_of):
    nd = [x for x in ndays if x <= as_of][-21:]
    down = {b for a, b in zip(nd, nd[1:]) if nclose[b] < nclose[a]}
    out = {}
    for s in gated:
        rets = daily_returns(s, as_of)
        vals = [v for day, v in rets.items() if day in down]
        if len(vals) >= 3:
            out[s] = sum(vals) / len(vals)
    return out


def eff_ratio_daily(gated, as_of, n=20):
    out = {}
    for s in gated:
        d = dailies[s]
        c = [d["close"][i] for i, x in enumerate(d["dates"]) if x <= as_of][-(n + 1):]
        if len(c) == n + 1:
            path = sum(abs(b - a) for a, b in zip(c, c[1:]))
            out[s] = (c[-1] - c[0]) / path if path else 0.0
    return out


def nifty_state(as_of):
    nd = [x for x in ndays if x <= as_of]
    c = [nclose[x] for x in nd]
    return {"below_sma20": len(c) >= 20 and c[-1] < sum(c[-20:]) / 20, "ret10": (c[-1] / c[-11] - 1) if len(c) >= 11 else 0.0,
            "ret20": (c[-1] / c[-21] - 1) if len(c) >= 21 else 0.0}


def picks_for(as_of):
    gated = [s for s in syms if wf.gate_check(dailies[s], as_of)[0]]
    ath = wf.ath_scores({s: dailies[s] for s in gated}, as_of)
    pools = {n: sorted(ath, key=lambda s: ath[s], reverse=True)[:n] for n in (25, 40, 60, 80)}
    res = resilience(gated, as_of)
    res_pool = sorted(res, key=lambda s: res[s], reverse=True)[:40]
    ns = nifty_state(as_of)
    P = {
        "HYBRID (live)": top(fit(pools[40], as_of, PLAIN), fill=pools[40]),
        "HYBRID_F (fit with 1h filter)": top(fit(pools[40], as_of, FILT), fill=pools[40]),
        "HYBRID_F lookback 10": top(fit(pools[40], as_of, FILT, 10, 2), fill=pools[40]),
        "HYBRID_F lookback 40": top(fit(pools[40], as_of, FILT, 40), fill=pools[40]),
        "HYBRID_F pool 25": top(fit(pools[25], as_of, FILT), fill=pools[25]),
        "HYBRID_F pool 60": top(fit(pools[60], as_of, FILT), fill=pools[60]),
        "HYBRID_F pool 80": top(fit(pools[80], as_of, FILT), fill=pools[80]),
        "FIT (whole universe)": top(fit(gated, as_of, PLAIN)),
        "FIT_F (whole universe, filter)": top(fit(gated, as_of, FILT)),
        "FIT_F lookback 10": top(fit(gated, as_of, FILT, 10, 2)),
        "HITRATE (ATH 40 pool)": top(fit(pools[40], as_of, FILT, hit=True), fill=pools[40]),
        "ATH only (old list)": pools[40][:TOP],
        "RESILIENT (up on NIFTY down days)": top(res),
        "RESILIENT_FIT": top(fit(res_pool, as_of, FILT), fill=res_pool),
        "LOWCHOP (daily efficiency ratio)": top(eff_ratio_daily(gated, as_of)),
    }
    P["NIFTY_SWITCH (below 20d avg -> RESILIENT_FIT)"] = P["RESILIENT_FIT"] if ns["below_sma20"] else P["HYBRID_F (fit with 1h filter)"]
    P["NIFTY_SWITCH2 (10d return < 0 -> RESILIENT_FIT)"] = P["RESILIENT_FIT"] if ns["ret10"] < 0 else P["HYBRID_F (fit with 1h filter)"]
    return P, gated, ns, fit(pools[40], as_of, FILT)


def forward(pick, week_days, table):
    tot = n = 0
    for s in pick:
        for d in week_days:
            v = table.get(s, {}).get(d)
            if v:
                tot += v[0]
                n += v[1]
    return tot, n


def spearman(xs, ys):
    if len(xs) < 5:
        return None
    rx = {v: i for i, v in enumerate(sorted(range(len(xs)), key=lambda i: xs[i]))}
    ry = {v: i for i, v in enumerate(sorted(range(len(ys)), key=lambda i: ys[i]))}
    n = len(xs)
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1 - 6 * d2 / (n * (n * n - 1))


rows = defaultdict(list)          # rule -> [(monday, filtered fwd, plain fwd, trades)]
rand = defaultdict(list)          # draw -> weekly filtered fwd
rng = random.Random(11)
nifty_rows, corr = [], []
for as_of, mon, wdays in DECISIONS:
    P, gated, ns, fit40 = picks_for(as_of)
    for name, pick in P.items():
        f, n = forward(pick, wdays, FILT)
        p, _n = forward(pick, wdays, PLAIN)
        rows[name].append((mon, f, p, n))
    for k in range(300):
        pick = rng.sample(gated, min(TOP, len(gated)))
        rand[k].append(forward(pick, wdays, FILT)[0])
    nw = [nclose[d] for d in wdays if d in nclose]
    prev = nclose[as_of]
    nifty_rows.append((mon, 100 * (nw[-1] / prev - 1) if nw else 0.0, ns))
    xs = list(fit40)
    corr.append((mon, spearman([fit40[s] for s in xs], [forward([s], wdays, FILT)[0] for s in xs]), len(xs)))


def month(mon):
    return "Jul" if mon < date(2026, 8, 3) else ("Aug" if mon < date(2026, 8, 31) else "Sep")


rand_tot = sorted(sum(v) for v in rand.values())
print(f"\n== RULES, scored on the FOLLOWING week (sum of % moves captured by 15 stocks, with the 1-hour filter) - {len(DECISIONS)} weeks ==")
print(f"{'rule':50s} {'total':>8s} {'Jul':>7s} {'Aug':>7s} {'Sep':>7s} {'weeks+':>7s} {'worst wk':>8s} {'beats random':>12s} {'no-filter total':>15s}")
out = {}
for name, v in sorted(rows.items(), key=lambda kv: -sum(x[1] for x in kv[1])):
    tot = sum(x[1] for x in v)
    by = defaultdict(float)
    for mon, f, _p, _n in v:
        by[month(mon)] += f
    pct = 100 * bisect.bisect_left(rand_tot, tot) / len(rand_tot)
    out[name] = {"total": round(tot, 2), **{k: round(x, 2) for k, x in by.items()}, "weeks_pos": sum(1 for x in v if x[1] > 0),
                 "pct_vs_random": round(pct), "plain_total": round(sum(x[2] for x in v), 2)}
    print(f"{name:50s} {tot:>+8.1f} {by['Jul']:>+7.1f} {by['Aug']:>+7.1f} {by['Sep']:>+7.1f} {sum(1 for x in v if x[1] > 0):>4d}/{len(v):<2d} "
          f"{min(x[1] for x in v):>+8.1f} {pct:>10.0f}%  {sum(x[2] for x in v):>+14.1f}")
print(f"{'RANDOM lists (300): median / 10th-90th pct':50s} {statistics.median(rand_tot):>+8.1f}   range {rand_tot[30]:+.1f} .. {rand_tot[270]:+.1f}")

print("\n== WEEK BY WEEK: HYBRID (live) vs HYBRID_F vs random median, and the market ==")
print(f"{'week':12s} {'HYBRID':>8s} {'HYBRID_F':>9s} {'RESIL_FIT':>10s} {'rand med':>9s} {'NIFTY wk%':>10s} {'NIFTY<20d avg':>14s} {'NIFTY 10d%':>11s} {'fit->next-week corr':>20s}")
for i, (as_of, mon, _w) in enumerate(DECISIONS):
    rmed = statistics.median(v[i] for v in rand.values())
    ns = nifty_rows[i][2]
    c = corr[i][1]
    print(f"{mon.isoformat():12s} {rows['HYBRID (live)'][i][1]:>+8.1f} {rows['HYBRID_F (fit with 1h filter)'][i][1]:>+9.1f} "
          f"{rows['RESILIENT_FIT'][i][1]:>+10.1f} {rmed:>+9.1f} {nifty_rows[i][1]:>+10.2f} {str(ns['below_sma20']):>14s} {100 * ns['ret10']:>+11.2f} "
          f"{(f'{c:+.2f}' if c is not None else 'n/a'):>20s}")
cs = [c for _m, c, _n in corr if c is not None]
print(f"average rank correlation between a stock's 20-session fit and its NEXT week: {sum(cs) / len(cs):+.3f} (0 = the fit says nothing)")

# ---- does the market regime decide whether selection works? ----
below = [i for i in range(len(DECISIONS)) if nifty_rows[i][2]["below_sma20"]]
above = [i for i in range(len(DECISIONS)) if not nifty_rows[i][2]["below_sma20"]]


def pct_vs_random(total, idxs):
    dist = sorted(sum(v[i] for i in idxs) for v in rand.values())
    return 100 * bisect.bisect_left(dist, total) / len(dist), statistics.median(dist)


print(f"\n== BY MARKET REGIME at the time of the pick: NIFTY ABOVE its 20-day average ({len(above)} weeks) vs BELOW ({len(below)} weeks) ==")
print(f"{'rule':50s} {'ABOVE total':>11s} {'beats random':>12s} {'BELOW total':>12s} {'beats random':>12s}")
regime = {}
for name, v in sorted(rows.items(), key=lambda kv: -sum(x[1] for x in kv[1])):
    a = sum(v[i][1] for i in above)
    b = sum(v[i][1] for i in below)
    pa, _ = pct_vs_random(a, above)
    pb, _ = pct_vs_random(b, below)
    regime[name] = {"above": round(a, 2), "above_pct": round(pa), "below": round(b, 2), "below_pct": round(pb)}
    print(f"{name:50s} {a:>+11.1f} {pa:>11.0f}% {b:>+12.1f} {pb:>11.0f}%")
print(f"{'RANDOM median':50s} {pct_vs_random(0, above)[1]:>+11.1f} {'':>12s} {pct_vs_random(0, below)[1]:>+12.1f}")
ca = [corr[i][1] for i in above if corr[i][1] is not None]
cb = [corr[i][1] for i in below if corr[i][1] is not None]
print(f"fit -> next-week rank correlation: NIFTY above its 20-day average {sum(ca) / len(ca):+.3f}  |  below {sum(cb) / len(cb):+.3f}")

print("\n== BY MONTH: where the live HYBRID list sits among 300 random lists ==")
for mname in ("Jul", "Aug", "Sep"):
    idxs = [i for i, d in enumerate(DECISIONS) if month(d[1]) == mname]
    tot = sum(rows["HYBRID (live)"][i][1] for i in idxs)
    pc, med = pct_vs_random(tot, idxs)
    print(f"  {mname}: HYBRID {tot:+.1f}  random median {med:+.1f}  -> better than {pc:.0f}% of random lists ({len(idxs)} weeks)")

print("\n== HOW OFTEN THE LIST CHANGES: stocks in common with the previous week's HYBRID list ==")
prev = None
for as_of, mon, _w in DECISIONS:
    cur = picks_for(as_of)[0]["HYBRID (live)"]
    if prev is not None:
        print(f"  {mon}: {len(set(cur) & set(prev))}/15 kept")
    prev = cur
json.dump({"rules": out, "regime": regime, "random": {"median": statistics.median(rand_tot), "p10": rand_tot[30], "p90": rand_tot[270]},
           "weeks": [d[1].isoformat() for d in DECISIONS]}, open("history/bt_walkforward_long/selection_rules_longer.json", "w"), indent=1)
