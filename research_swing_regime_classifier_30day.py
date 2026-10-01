"""30-day backtest of Swing with the LIVE regime classifier (Swing/regime.classify, 04aba33) as the entry gate - day-wise
and per-stock P&L vs Swing as live (1 Oct 2026, user: "Show me it's backtest first for last 30 days with PnL"). Engine, data,
caveats: research_swing_hedge_variants_30day.py / research_swing_chop_regime_30day.py.
Run: HANDOFF_DHAN_ACCESS_TOKEN=<token> .venv/bin/python research_swing_regime_classifier_30day.py"""
import os, sys
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, ".")
import research_swing_chop_regime_30day as C
from Swing import regime
H, R = C.H, C.R
IST = R.IST
data, series = {}, {}
for sym in H.SYMS:
    spot = H.load_spot(sym); fast, slow = H.resample(spot, 5), H.resample(spot, 15); und = R.Series(spot)
    data[sym] = {"spot": spot, "idx": {t: i for i, t in enumerate(spot["ts"])}, "fast": fast, "sig": H.S.signals(fast, slow, 5),
                 "close_at": {t + 300: k for k, t in enumerate(fast["ts"])}, "und": und, "b5": R.five_min(und)}
    series[sym] = ({"timestamp": fast["ts"], "open": fast["o"], "high": fast["h"], "low": fast["l"], "close": fast["c"]},
                   {"timestamp": slow["ts"], "open": slow["o"], "high": slow["h"], "low": slow["l"], "close": slow["c"]})
states = defaultdict(lambda: defaultdict(int))
def gate(sym, side, k, now):
    r = regime.classify(series[sym][0], series[sym][1], datetime.fromtimestamp(now, IST))
    states["signals"][r.state] += 1
    return r.allows_entry
H.ENTRY_GATE = None
base = H.simulate(data)
H.ENTRY_GATE = gate
filt = H.simulate(data)
H.ENTRY_GATE = None
sb, sf = C.stats(base), C.stats(filt)
def day_stats(trades):
    d = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for t in trades:
        k = datetime.fromtimestamp(t["t1"] or t["t0"], IST).date()
        d[k][0] += 1; d[k][1] += t["pnl"] > 0; d[k][2] += t["pnl"]; d[k][3] += t["pnl_raw"]
    return d
db, df = day_stats(base), day_stats(filt)
print("| Day | Swing trades (won) | Swing P&L | With classifier trades (won) | With classifier P&L | Difference |")
print("|---|---|---|---|---|---|")
cb = cf = 0
for day in sorted(set(db) | set(df)):
    b, f = db.get(day, [0, 0, 0.0, 0.0]), df.get(day, [0, 0, 0.0, 0.0])
    cb += b[2]; cf += f[2]
    print(f"| {day:%d %b} | {b[0]} ({b[1]}) | {b[2]:+,.0f} | {f[0]} ({f[1]}) | {f[2]:+,.0f} | {f[2]-b[2]:+,.0f} |")
print()
for name, s in (("Swing as live", sb), ("With classifier (MOMENTUM only)", sf)):
    print(f"{name}: trades {s['n']}, won {s['won']} ({100*s['won']/s['n']:.0f}%), modelled {s['mod']:+,.0f}, raw {s['raw']:+,.0f}, "
          f"1-15 Sep {s['h1']:+,.0f}, 16 Sep-1 Oct {s['h2']:+,.0f}, max dd {s['dd']:+,.0f}, winning days {s['win_days']}/{s['days']}")
print("signal states at the classifier:", dict(states["signals"]))
def by_sym(trades):
    d = defaultdict(lambda: [0, 0.0])
    for t in trades: d[t["sym"]][0] += 1; d[t["sym"]][1] += t["pnl"]
    return d
bs, fs = by_sym(base), by_sym(filt)
print("\n| Stock | Swing trades | Swing P&L | Classifier trades | Classifier P&L |")
print("|---|---|---|---|---|")
for s in sorted(set(bs) | set(fs), key=lambda x: fs.get(x, [0, 0])[1] - bs.get(x, [0, 0])[1], reverse=True):
    print(f"| {s} | {bs.get(s,[0,0])[0]} | {bs.get(s,[0,0])[1]:+,.0f} | {fs.get(s,[0,0])[0]} | {fs.get(s,[0,0])[1]:+,.0f} |")
ex = defaultdict(int)
for t in filt: ex[t["reason"]] += 1
print("\nexits with classifier:", dict(ex), "| Dhan calls:", H.CALLS["n"], "| unpriced:", len(H.MISSING))
