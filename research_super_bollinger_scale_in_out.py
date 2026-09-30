"""
Scale-in / scale-out test on the HYBRID Super Bollinger backtest (30 Sep 2026,
user idea): add 1 more lot on whichever side the trade is going - the CE when
it is winning, the supervisor's PE hedge once the down-move is confirmed
(e.g. Supertrend bearish) - then either book 1 lot quickly and keep the other
on the rules built so far, sell both once the pair's loss is recovered, or
exit both on a profit-protection trail.

HYBRID CE trades (weekly walk-forward picks, G' rules, max 5 open) are the
baseline, unchanged. Every added lot uses the SAME contract as the leg it adds
to (no new fetches). Hedge = the live supervisor rule: CE open loss >= 2,000
and stock >= 1 x ATR(14,5m) below the CE entry spot -> 1 lot ATM PE; exit on a
40% giveback after >= 1,000 profit, Rs 1,500 stop, 15:15.

Intra-minute ordering is conservative: a minute that touches both a stop and a
target counts as the stop; an add fills at its trigger (or the bar open if it
gapped past) and is only managed from the next minute.

CACHE ONLY: any Dhan call raises (market-hours safe; nothing is fetched or
written to the option caches). Research only - nothing live changes.

Run: uv run python research_super_bollinger_scale_in_out.py
"""
from __future__ import annotations

import bisect
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import backtest_super_trader_30day as st
from Bollinger.paper_book import modeled_slippage_pct
from SuperTrader.strategy import atr, supertrend

IST = ZoneInfo("Asia/Kolkata")
OUT = Path("history/bt_super_bollinger_universe_30day")
HEDGE_TRIGGER, HEDGE_STOP, TRAIL_ARM, TRAIL_GIVEBACK = 2000, 1500, 1000, 0.40
HEDGE_CUTOFF, ADD_CUTOFF_CE, ADD_CUTOFF_PE = dtime(15, 0), dtime(14, 0), dtime(14, 30)
HALF_SPLIT = "2026-09-15"


class CacheMiss(BaseException):
    """BaseException so OptionPricer's `except Exception` can't swallow it
    and write an empty cache file."""


def _no_fetch(*_a, **_k):
    raise CacheMiss("cache miss - refusing to call Dhan")


st._retry = _no_fetch
st._save = lambda *_a, **_k: None
_scrip = sorted(Path(".").glob("Dependencies\\all_instrument *.csv"))[-1]
_df = pd.read_csv(_scrip, low_memory=False)
st.dhan_wrapper.instruments = lambda: _df

pr = st.OptionPricer()
rows = list(csv.DictReader(open(OUT / "HYBRID_trades.csv")))
feat5, one = {}, {}


def features(sym):
    if sym not in feat5:
        b = json.load(open(f"history/bt_super_trader_30day/underlying/{sym}_5min.json"))
        feat5[sym] = {"ts": b["timestamps"], "st": supertrend(b["highs"], b["lows"], b["closes"], 10, 3.0),
                      "atr": atr(b["highs"], b["lows"], b["closes"], 14)}
        one[sym] = json.load(open(f"history/bt_super_trader_30day/underlying_1m/{sym}_1min.json"))
    return feat5[sym], one[sym]


def bar_at(f, t):
    k = bisect.bisect_right(f["ts"], t - 300) - 1  # last COMPLETED 5-min bar
    return k if k >= 0 else None


def mod_pnl(p_in, p_out, qty):
    return (p_out * (1 - modeled_slippage_pct(p_out)) - p_in * (1 + modeled_slippage_pct(p_in))) * qty


def hhmm(t):
    return datetime.fromtimestamp(t, IST).time()


# --------------------------------------------------------------------------- #
# Prepare CE legs (+ the live hedge on each) once
# --------------------------------------------------------------------------- #
events, unpriced = [], 0
for r in rows:
    sym, d = r["symbol"], date.fromisoformat(r["day"])
    f, m = features(sym)
    t0 = int(datetime.combine(d, datetime.strptime(r["entry_time"], "%H:%M").time(), IST).timestamp())
    t1 = int(datetime.combine(d, datetime.strptime(r["exit_time"], "%H:%M").time(), IST).timestamp())
    sq = int(datetime.combine(d, dtime(15, 15), IST).timestamp())
    spot0 = m["closes"][bisect.bisect_right(m["timestamps"], t0) - 1]
    try:
        ce = pr.open_leg(sym, d, "LONG", t0, sq, spot0)
    except (CacheMiss, Exception):  # noqa: BLE001
        ce = None
    if not ce:
        unpriced += 1
        events.append({"r": r, "cm": None})
        continue
    ev = {"r": r, "sym": sym, "d": d, "t0": t0, "t1": t1, "sq": sq, "spot0": spot0, "cm": ce["mins"],
          "p0": float(r["entry"]), "qty": int(r["qty"]), "hedge": None}
    # live hedge trigger
    cm, p0, qty = ev["cm"], ev["p0"], ev["qty"]
    th = None
    for i, t in enumerate(cm.ts):
        if t <= t0 or t > t1 or hhmm(t) >= HEDGE_CUTOFF or (p0 - cm.l[i]) * qty < HEDGE_TRIGGER:
            continue
        k = bar_at(f, t)
        if k is not None and f["atr"][k] is not None and \
                spot0 - m["closes"][bisect.bisect_right(m["timestamps"], t) - 1] >= f["atr"][k]:
            th = t
            break
    if th is not None:
        spot = m["closes"][bisect.bisect_right(m["timestamps"], th) - 1]
        try:
            pe = pr.open_leg(sym, d, "SHORT", th, sq, spot)
        except (CacheMiss, Exception):  # noqa: BLE001
            pe = None
        if pe:
            pm = pe["mins"]
            i0 = bisect.bisect_right(pm.ts, th) - 1
            if i0 >= 0 and th - pm.ts[i0] <= 300:
                ev["hedge"] = {"th": th, "pm": pm, "i0": i0, "q0": pm.c[i0]}
    events.append(ev)


# --------------------------------------------------------------------------- #
# CE-side variants (original lot = the CSV G' trade, untouched)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CeAdd:
    name: str
    add_at_rs: float | None          # add when the CE is this much in profit; None = 2nd lot at entry
    need_st_up: bool = False         # also require Supertrend(10,3) bullish on the last closed 5-min bar
    own_stop_rs: float | None = None  # added lot's own stop (Rs below its fill); None = exits with the original
    book_rs: float | None = None      # book the added lot at this profit over its fill
    st_exit: bool = False             # sell the added lot when Supertrend(10,3) 5-min turns bearish


CE_VARIANTS = [
    CeAdd("CE-U  2 lots at entry, book 2nd at +2k", None, book_rs=2000),
    CeAdd("CE-U' 2 lots at entry, book 2nd at +1.5k", None, book_rs=1500),
    CeAdd("CE-P1 add at +1.5k, exit with original", 1500),
    CeAdd("CE-P1s add at +1.5k, own stop -750", 1500, own_stop_rs=750),
    CeAdd("CE-P1st add at +1.5k & ST up, own stop -750", 1500, need_st_up=True, own_stop_rs=750),
    CeAdd("CE-P2 add at +1.5k, book add +1.5k, stop -750", 1500, own_stop_rs=750, book_rs=1500),
    CeAdd("CE-P2b add at +1.5k, book add +1k, stop -750", 1500, own_stop_rs=750, book_rs=1000),
    CeAdd("CE-P3 add at +3k, own stop -1k", 3000, own_stop_rs=1000),
    CeAdd("CE-P3b add at +3k, book add +1.5k, stop -1k", 3000, own_stop_rs=1000, book_rs=1500),
    # User follow-up (30 Sep ~11:35 IST): sell the added lot on a trend reversal / a loss cap
    CeAdd("CE-P1x add at +1.5k, sell add on ST bearish", 1500, st_exit=True),
    CeAdd("CE-P1c add at +1.5k, sell add at -1k", 1500, own_stop_rs=1000),
    CeAdd("CE-P1xc add +1.5k, sell add ST bearish or -1k", 1500, own_stop_rs=1000, st_exit=True),
    CeAdd("CE-P3x add at +3k, sell add on ST bearish", 3000, st_exit=True),
]


def ce_add(ev, v: CeAdd):
    """(pnl_modeled, fill_t, exit_t, fill_px) of the added CE lot, or None."""
    cm, p0, qty, t0, t1 = ev["cm"], ev["p0"], ev["qty"], ev["t0"], ev["t1"]
    exit_px_orig = float(ev["r"]["exit"])
    f, _m = features(ev["sym"])
    if v.add_at_rs is None:
        ia, fill = None, p0
        ta = t0
    else:
        lvl = p0 + v.add_at_rs / qty
        # plain: resting buy-stop at the level; with the Supertrend check: buy at market (minute close)
        # the first minute the CE is >= the level AND the last closed 5-min bar is Supertrend-bullish
        ia = next((i for i, t in enumerate(cm.ts) if t0 < t < t1 and hhmm(t) < ADD_CUTOFF_CE
                   and ((cm.c[i] >= lvl and (k := bar_at(f, t)) is not None and f["st"][k] == 1)
                        if v.need_st_up else cm.h[i] >= lvl)), None)
        if ia is None:
            return None
        fill = cm.c[ia] if v.need_st_up else max(lvl, cm.o[ia])
        ta = cm.ts[ia]
    stop = fill - v.own_stop_rs / qty if v.own_stop_rs else None
    tgt = fill + v.book_rs / qty if v.book_rs else None
    for j, t in enumerate(cm.ts):
        if t <= ta:
            continue
        if t >= t1:
            px = exit_px_orig
            if stop is not None and cm.l[j] <= stop:
                px = min(px, min(stop, cm.o[j]))
            return mod_pnl(fill, px, qty), ta, t1, fill
        if stop is not None and cm.l[j] <= stop:
            return mod_pnl(fill, min(stop, cm.o[j]), qty), ta, t, fill
        if tgt is not None and cm.h[j] >= tgt:
            return mod_pnl(fill, max(tgt, cm.o[j]), qty), ta, t, fill
        if v.st_exit and (k := bar_at(f, t)) is not None and f["ts"][k] + 300 > ta and f["st"][k] == -1:
            return mod_pnl(fill, cm.c[j], qty), ta, t, fill   # bar closed after the add, bearish -> sell at market
    return mod_pnl(fill, exit_px_orig, qty), ta, t1, fill


# --------------------------------------------------------------------------- #
# PE-side: live hedge + optional 2nd PE lot
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PeAdd:
    name: str
    add: str | None          # None | "PX" (hedge +1,000) | "ST" (Supertrend bearish) | "BOTH"
    exit: str = "TOGETHER"   # QUICK | TOGETHER | RECOVER | PAIRTRAIL
    book_rs: float = 1000    # QUICK: book the added lot at this profit over its fill
    own_stop_rs: float = 750


PE_VARIANTS = [PeAdd("PE-0  live hedge only (1 lot)", None)]
for add in ("PX", "ST", "BOTH"):
    PE_VARIANTS += [PeAdd(f"PE-{add:<4} add, book add +1k (sell 1 quick)", add, "QUICK", 1000),
                    PeAdd(f"PE-{add:<4} add, book add +1.5k", add, "QUICK", 1500),
                    PeAdd(f"PE-{add:<4} add, both on hedge trail", add, "TOGETHER"),
                    PeAdd(f"PE-{add:<4} add, sell both once pair loss recovered", add, "RECOVER"),
                    PeAdd(f"PE-{add:<4} add, pair profit-protection trail", add, "PAIRTRAIL")]


def pe_run(ev, v: PeAdd):
    """[(pnl_modeled, t_in, t_out, px_in)] for the hedge lot(s)."""
    h = ev.get("hedge")
    if not h:
        return []
    pm, i0, q0, th, qty, sq = h["pm"], h["i0"], h["q0"], h["th"], ev["qty"], ev["sq"]
    cm, p0, t1 = ev["cm"], ev["p0"], ev["t1"]
    ce_exit_raw = float(ev["r"]["pnl_raw"])
    f, _m = features(ev["sym"])
    legs = [{"q": q0, "t_in": th, "open": True, "own_stop": q0 - HEDGE_STOP / qty, "tgt": None, "add": False}]
    peak1, pair_peak, out = 0.0, 0.0, []

    def close(leg, px, t):
        leg["open"] = False
        out.append((mod_pnl(leg["q"], px, qty), leg["t_in"], t, leg["q"]))

    for j in range(i0 + 1, len(pm.ts)):
        t = pm.ts[j]
        live = [lg for lg in legs if lg["open"]]
        if not live:
            break
        if t >= sq:
            for lg in live:
                close(lg, pm.o[j], t)
            break
        # own stops / quick-book targets (stop first)
        for lg in live:
            if pm.l[j] <= lg["own_stop"]:
                close(lg, min(lg["own_stop"], pm.o[j]), t)
            elif lg["tgt"] is not None and pm.h[j] >= lg["tgt"]:
                close(lg, max(lg["tgt"], pm.o[j]), t)
        first = legs[0]
        if not first["open"]:   # the kept lot is gone -> the add goes with it
            for lg in [lg for lg in legs if lg["open"]]:
                close(lg, pm.c[j], t)
            break
        live = [lg for lg in legs if lg["open"]]
        peak1 = max(peak1, (pm.h[j] - q0) * qty)
        trail_hit = peak1 >= TRAIL_ARM and (pm.c[j] - q0) * qty <= peak1 * (1 - TRAIL_GIVEBACK)
        added = [lg for lg in legs if lg["add"]]
        if v.exit == "PAIRTRAIL" and added:
            prof = sum((pm.h[j] - lg["q"]) * qty for lg in live)
            pair_peak = max(pair_peak, prof)
            now = sum((pm.c[j] - lg["q"]) * qty for lg in live)
            if pair_peak >= 2 * TRAIL_ARM and now <= pair_peak * (1 - TRAIL_GIVEBACK):
                for lg in live:
                    close(lg, pm.c[j], t)
                break
        elif v.exit == "RECOVER" and added:
            ci = bisect.bisect_right(cm.ts, t) - 1
            ce_now = ce_exit_raw if t >= t1 else ((cm.c[ci] - p0) * qty if ci >= 0 else 0.0)
            pe_now = sum((pm.c[j] - lg["q"]) * qty for lg in live) + \
                sum((x[0]) for x in out)
            if ce_now + pe_now >= 0:
                for lg in live:
                    close(lg, pm.c[j], t)
                break
            if trail_hit:
                for lg in live:
                    close(lg, pm.c[j], t)
                break
        elif trail_hit:
            for lg in [lg for lg in legs if lg["open"]]:   # kept lot rode the hedge trail; the add goes with it
                close(lg, pm.c[j], t)
            break
        # the add (one per hedge)
        if v.add and not added and hhmm(t) < ADD_CUTOFF_PE and first["open"]:
            px_ok = (pm.h[j] - q0) * qty >= TRAIL_ARM
            k = bar_at(f, t)
            st_ok = k is not None and f["st"][k] == -1
            want = {"PX": px_ok, "ST": st_ok, "BOTH": px_ok and st_ok}[v.add]
            if want:
                fill = max(q0 + TRAIL_ARM / qty, pm.o[j]) if v.add == "PX" else pm.c[j]
                legs.append({"q": fill, "t_in": t, "open": True, "own_stop": fill - v.own_stop_rs / qty,
                             "tgt": fill + v.book_rs / qty if v.exit == "QUICK" else None, "add": True})
    for lg in legs:
        if lg["open"]:
            close(lg, pm.c[-1], pm.ts[-1])
    return out


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def peak_capital(legs):
    """Max premium outlay open at once (entry_px * qty summed over overlapping legs)."""
    pts = []
    for t_in, t_out, cost in legs:
        pts += [(t_in, cost), (t_out, -cost)]
    cur = best = 0.0
    for _t, c in sorted(pts, key=lambda x: (x[0], x[1])):
        cur += c
        best = max(best, cur)
    return best


def score(name, extra_by_trade, extra_legs):
    daily = defaultdict(float)
    legs = []
    for ev in events:
        r = ev["r"]
        daily[r["day"]] += float(r["pnl_modeled"])
        if ev.get("cm") is not None:
            legs.append((ev["t0"], ev["t1"], ev["p0"] * ev["qty"]))
    for day, pnl in extra_by_trade:
        daily[day] += pnl
    legs += extra_legs
    eq = pk = dd = 0.0
    for k in sorted(daily):
        eq += daily[k]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    h1 = sum(v for k, v in daily.items() if k < HALF_SPLIT)
    h2 = sum(v for k, v in daily.items() if k >= HALF_SPLIT)
    by_day_cap = defaultdict(list)
    for lg in legs:
        by_day_cap[datetime.fromtimestamp(lg[0], IST).date()].append(lg)
    caps = {d: peak_capital(v) for d, v in by_day_cap.items()}
    extra = sum(p for _d, p in extra_by_trade)
    res = {"name": name, "total": round(sum(daily.values())), "extra": round(extra), "n_extra": len(extra_by_trade),
           "extra_wins": sum(p > 0 for _d, p in extra_by_trade), "H1": round(h1), "H2": round(h2),
           "max_dd": round(dd), "worst_day": round(min(daily.values())),
           "green": f"{sum(v > 0 for v in daily.values())}/{len(daily)}",
           "peak_capital": round(max(caps.values())), "days_cap_over_1_2L": sum(c > 120000 for c in caps.values())}
    print(f"{name:52s} total {res['total']:>+8,} extra {res['extra']:>+8,} ({res['extra_wins']}/{res['n_extra']} won) "
          f"H1 {res['H1']:>+8,} H2 {res['H2']:>+8,} dd {res['max_dd']:>8,} worst {res['worst_day']:>+7,} "
          f"green {res['green']} peakcap {res['peak_capital']:>8,} days>1.2L {res['days_cap_over_1_2L']}", flush=True)
    return res


priced = [ev for ev in events if ev.get("cm") is not None]
print(f"HYBRID CE trades {len(rows)} (priced {len(priced)}, unpriced {unpriced}); live-rule hedges "
      f"{sum(1 for ev in priced if ev['hedge'])}; Dhan calls made: {pr.calls}\n")
report = {"ce": [], "pe": []}
print("== CE side (baseline = HYBRID G', 1 lot) ==")
report["ce"].append(score("CE-BASE G' 1 lot (no add)", [], []))
for v in CE_VARIANTS:
    extra, legs = [], []
    for ev in priced:
        x = ce_add(ev, v)
        if x:
            extra.append((ev["r"]["day"], x[0]))
            legs.append((x[1], x[2], x[3] * ev["qty"]))
    report["ce"].append(score(v.name, extra, legs))

print("\n== PE side (baseline + live hedge rule; adds use the same PE contract) ==")
for v in PE_VARIANTS:
    extra, legs = [], []
    for ev in priced:
        for pnl, t_in, t_out, q in pe_run(ev, v):
            extra.append((ev["r"]["day"], pnl))
            legs.append((t_in, t_out, q * ev["qty"]))
    report["pe"].append(score(v.name, extra, legs))
print(f"\nDhan calls made: {pr.calls}")
(OUT / "scale_in_out.json").write_text(json.dumps(report, indent=2))


# --------------------------------------------------------------------------- #
# Funds-limited replay: the live funds check skips an entry/add that doesn't
# fit in the budget (premium outlay), so a pyramid can't always add.
# --------------------------------------------------------------------------- #
def budget_replay(v: CeAdd | None, budget: float):
    ev_list = []   # (t, kind, key, cost, pnl)
    for n, ev in enumerate(priced):
        cost = ev["p0"] * ev["qty"]
        ev_list += [(ev["t0"], 1, ("B", n), cost, float(ev["r"]["pnl_modeled"])), (ev["t1"], 0, ("B", n), cost, 0.0)]
        x = ce_add(ev, v) if v else None
        if x:
            ev_list += [(x[1], 2, ("A", n), x[3] * ev["qty"], x[0]), (x[2], 0, ("A", n), x[3] * ev["qty"], 0.0)]
    used, taken, daily, skipped = 0.0, set(), defaultdict(float), defaultdict(int)
    for t, kind, key, cost, pnl in sorted(ev_list, key=lambda e: (e[0], e[1])):
        if kind == 0:
            if key in taken:
                used -= cost
            continue
        if kind == 2 and ("B", key[1]) not in taken:
            continue
        if used + cost > budget:
            skipped["entry" if kind == 1 else "add"] += 1
            continue
        taken.add(key)
        used += cost
        daily[datetime.fromtimestamp(t, IST).date().isoformat()] += pnl
    eq = pk = dd = 0.0
    for k in sorted(daily):
        eq += daily[k]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return round(sum(daily.values())), round(dd), round(min(daily.values())), dict(skipped)


print("\n== CE side under a funds limit (premium outlay; entries/adds that don't fit are skipped) ==")
for budget in (120000, 200000, 250000):
    for v in [None] + [x for x in CE_VARIANTS if x.name.startswith(("CE-U ", "CE-P1 ", "CE-P1st", "CE-P3 "))]:
        tot, dd, worst, sk = budget_replay(v, budget)
        print(f"budget {budget:>7,}  {(v.name if v else 'CE-BASE G  1 lot'):52s} total {tot:>+8,} dd {dd:>8,} "
              f"worst {worst:>+8,} skipped {sk}", flush=True)
