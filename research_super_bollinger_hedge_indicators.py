"""
Hedge-on-drawdown with reversal indicators (30 Sep 2026, user request).
HYBRID Super Bollinger CE trades unchanged; when a CE's open loss reaches
TRIGGER, buy 1 lot of the same stock's ATM PE - optionally only once an
indicator confirms the down-move - and sell the PE on a reversal signal.
Grid (fixed before any result was seen):
  TRIGGER  2000 / 2500 / 3000
  ENTRY    NONE | ST_DOWN (Supertrend 10,3 bearish) | MACD_NEG (12,26,9 hist < 0)
           | ATR_DROP (stock >= 1 x ATR(14) below the CE entry spot)
           - checked on the last COMPLETED 5-min bar; if not confirmed yet,
             the hedge waits while the loss stays >= TRIGGER (until the CE exits)
  EXIT     TRAIL (PE profit >= 1000 then 40% giveback) | ST_FLIP (Supertrend
           turns bullish) | MACD_POS (hist > 0) | TRAIL_OR_ST | ATR_BOUNCE
           (stock >= 1 x ATR above its low since the hedge)
           + always a Rs 2,000 PE stop and the 15:15 exit.
Real 1-min option prices for every PE (expired-options data); slippage on
both PE legs. total = HYBRID baseline + hedge PnL (hedges take no slot).
"""
from __future__ import annotations

import bisect
import csv
import json
from collections import defaultdict
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

import walkforward_selector_eval as wf
import backtest_super_trader_30day as st
from Bollinger.paper_book import modeled_slippage_pct
from SuperTrader.strategy import atr, ema, supertrend

IST = ZoneInfo("Asia/Kolkata")
PE_STOP, TRAIL_ARM, TRAIL_GIVEBACK = 2000, 1000, 0.40
TRIGGERS = [2000, 2500, 3000]
ENTRIES = ["NONE", "ST_DOWN", "MACD_NEG", "ATR_DROP"]
EXITS = ["TRAIL", "ST_FLIP", "MACD_POS", "TRAIL_OR_ST", "ATR_BOUNCE"]
wf.authenticate()
wf.dhan_wrapper.client.Dhan.dhan_http.timeout = 90
pr = st.OptionPricer()
rows = list(csv.DictReader(open("history/bt_super_bollinger_universe_30day/HYBRID_trades.csv")))
base_daily = defaultdict(float)
for r in rows:
    base_daily[r["day"]] += float(r["pnl_modeled"])


def ema_list(vals, n):
    out = [None] * len(vals)
    idx = [i for i, v in enumerate(vals) if v is not None]
    if len(idx) < n:
        return out
    sub = ema([vals[i] for i in idx], n)
    for j, i in enumerate(idx):
        out[i] = sub[j]
    return out


feat5, one = {}, {}


def features(sym):
    if sym not in feat5:
        b = json.load(open(f"history/bt_super_trader_30day/underlying/{sym}_5min.json"))
        c = b["closes"]
        e12, e26 = ema(c, 12), ema(c, 26)
        macd = [a - b_ if a is not None and b_ is not None else None for a, b_ in zip(e12, e26)]
        sig = ema_list(macd, 9)
        hist = [m - s if m is not None and s is not None else None for m, s in zip(macd, sig)]
        feat5[sym] = {"ts": b["timestamps"], "st": supertrend(b["highs"], b["lows"], c, 10, 3.0),
                      "hist": hist, "atr": atr(b["highs"], b["lows"], c, 14)}
        one[sym] = json.load(open(f"history/bt_super_trader_30day/underlying_1m/{sym}_1min.json"))
    return feat5[sym], one[sym]


def bar_at(f, t):
    k = bisect.bisect_right(f["ts"], t - 300) - 1  # last COMPLETED 5-min bar
    return k if k >= 0 else None


events = []  # prepared CE legs
for r in rows:
    sym, d = r["symbol"], date.fromisoformat(r["day"])
    f, m = features(sym)
    t0 = int(datetime.combine(d, datetime.strptime(r["entry_time"], "%H:%M").time(), IST).timestamp())
    t1 = int(datetime.combine(d, datetime.strptime(r["exit_time"], "%H:%M").time(), IST).timestamp())
    sq = int(datetime.combine(d, dtime(15, 15), IST).timestamp())
    spot0 = m["closes"][bisect.bisect_right(m["timestamps"], t0) - 1]
    ce = pr.open_leg(sym, d, "LONG", t0, sq, spot0)
    if ce:
        events.append((r, sym, d, t0, t1, sq, spot0, ce["mins"], float(r["entry"]), int(r["qty"])))


def hedge(ev, trig, entry, exit_):
    r, sym, d, t0, t1, sq, spot0, cm, p0, qty = ev
    f, m = features(sym)
    th = None
    for i, t in enumerate(cm.ts):
        if t <= t0 or t > t1:
            continue
        if (p0 - cm.l[i]) * qty < trig:
            continue
        k = bar_at(f, t)
        if k is None:
            continue
        ok = (entry == "NONE" or (entry == "ST_DOWN" and f["st"][k] == -1)
              or (entry == "MACD_NEG" and f["hist"][k] is not None and f["hist"][k] < 0)
              or (entry == "ATR_DROP" and f["atr"][k] is not None
                  and spot0 - m["closes"][bisect.bisect_right(m["timestamps"], t) - 1] >= f["atr"][k]))
        if ok:
            th = t
            break
    if th is None:
        return None
    spot = m["closes"][bisect.bisect_right(m["timestamps"], th) - 1]
    pe = pr.open_leg(sym, d, "SHORT", th, sq, spot)
    if not pe:
        return None
    pm = pe["mins"]
    i0 = bisect.bisect_right(pm.ts, th) - 1
    if i0 < 0 or th - pm.ts[i0] > 300:
        return None
    q0 = pm.c[i0]
    kh = bar_at(f, th)
    atr_h = f["atr"][kh] if kh is not None else None

    def pnl(px):
        return (px * (1 - modeled_slippage_pct(px)) - q0 * (1 + modeled_slippage_pct(q0))) * qty

    peak, low_u = 0.0, spot
    for j in range(i0 + 1, len(pm.ts)):
        t = pm.ts[j]
        if t >= sq:
            return pnl(pm.o[j])
        if (q0 - pm.l[j]) * qty >= PE_STOP:
            return pnl(q0 - PE_STOP / qty)
        u = bisect.bisect_right(m["timestamps"], t) - 1
        if u >= 0:
            low_u = min(low_u, m["lows"][u])
        k = bar_at(f, t)
        st_up = k is not None and f["st"][k] == 1
        prof = (pm.h[j] - q0) * qty
        peak = max(peak, prof)
        trail_hit = peak >= TRAIL_ARM and (pm.c[j] - q0) * qty <= peak * (1 - TRAIL_GIVEBACK)
        if exit_ == "TRAIL" and trail_hit:
            return pnl(pm.c[j])
        if exit_ == "ST_FLIP" and st_up:
            return pnl(pm.c[j])
        if exit_ == "MACD_POS" and k is not None and f["hist"][k] is not None and f["hist"][k] > 0:
            return pnl(pm.c[j])
        if exit_ == "TRAIL_OR_ST" and (trail_hit or st_up):
            return pnl(pm.c[j])
        if exit_ == "ATR_BOUNCE" and atr_h and u >= 0 and m["closes"][u] - low_u >= atr_h:
            return pnl(pm.c[j])
    return pnl(pm.c[-1])


base = sum(base_daily.values())
print(f"baseline {base:+,.0f} | CE legs priced {len(events)}/{len(rows)}", flush=True)
res = []
for trig in TRIGGERS:
    for entry in ENTRIES:
        for exit_ in EXITS:
            daily = dict(base_daily)
            hp, n = {"H1": 0.0, "H2": 0.0}, 0
            for ev in events:
                v = hedge(ev, trig, entry, exit_)
                if v is None:
                    continue
                n += 1
                daily[ev[0]["day"]] += v
                hp["H1" if ev[0]["day"] < "2026-09-15" else "H2"] += v
            tot = sum(daily.values())
            res.append((trig, entry, exit_, n, hp["H1"] + hp["H2"], hp["H1"], hp["H2"], tot, min(daily.values())))
            print(f"T{trig} {entry:9s} {exit_:11s} hedges {n:3d}  hedge {hp['H1'] + hp['H2']:+8,.0f} "
                  f"(1st half {hp['H1']:+7,.0f}, 2nd half {hp['H2']:+7,.0f})  total {tot:+8,.0f}  worst day {min(daily.values()):+8,.0f}  calls {pr.calls}", flush=True)
json.dump([dict(zip(["trigger", "entry", "exit", "hedges", "hedge_pnl", "hedge_h1", "hedge_h2", "total", "worst_day"], x))
           for x in res], open("history/bt_super_bollinger_universe_30day/hedge_indicator_grid.json", "w"), indent=2)
both = [x for x in res if x[5] > 0 and x[6] > 0]
print(f"\ncombinations whose hedge made money in BOTH halves: {len(both)} of {len(res)}")
for x in sorted(both, key=lambda x: -x[4])[:10]:
    print(f"  T{x[0]} {x[1]} {x[2]}: hedge {x[4]:+,.0f} (1st {x[5]:+,.0f} / 2nd {x[6]:+,.0f}) total {x[7]:+,.0f}")
