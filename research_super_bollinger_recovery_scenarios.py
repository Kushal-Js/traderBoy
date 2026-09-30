"""
Two user scenarios on top of Super Bollinger's CE trades (30 Sep 2026 evening).

S2 "ride the fall with two PUT lots"
   lot A = the live hedge (CE loss >= 1,800 + 1 ATR drop), stop 1,500, 30% trail
   lot B = added when Supertrend(10,3) on the last closed 5-min bar is BEARISH
           (own 750 stop)
   when the COMBINED profit of A+B reaches TARGET -> sell lot B (resting limit)
   and ride lot A on the 30% trail with a floor at its purchase price.
   If A exits first (stop/trail) B goes with it. 15:15 square-off.

S1 "the stock recovers to its trigger -> one more CALL"
   after the dip (the hedge trigger fired), when the STOCK is back at/above the
   price the entry signal triggered at, before 14:00, optionally only with
   Supertrend BULLISH: buy one more lot of the same call.
   - the PAIR has ONE max loss of 4,500 (both lots sold together), or - for
     comparison - each lot its own 4,500;
   - the added lot is sold on a 30% giveback trail (armed at +1,000 / +2,000),
     or on a Supertrend flip, or just exits with the original;
   - the original lot keeps its own rules (breakeven after +1,500, 15:15);
   - re-entry: if the original call was already stopped out and the stock comes
     back to the trigger, buy one lot again (managed by the normal call rules).

Trades: the HYBRID walk-forward CE book, 3 Aug - 29 Sep (from
history/bt_walkforward_long/bearish_side_aug.json). Real option prices from the
cache - CACHE ONLY, no Dhan calls. Modeled slippage on every leg; conservative
intra-minute ordering (stop before target). Research only.

Run: uv run python research_super_bollinger_recovery_scenarios.py
"""
from __future__ import annotations

import bisect
import json
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime

sys.argv = [sys.argv[0], "aug", "--cache-only"]
import research_super_bollinger_bearish_side as m  # noqa: E402

IST = m.IST
HEDGE_TRIGGER, HEDGE_STOP, ARM, GIVEBACK, B_STOP = 1800, 1500, 1000, 0.30, 750
MAX_LOSS, BE_AFTER = 4500, 1500
ENTRY_CUTOFF = dtime(14, 0)
SPLIT = "2026-08-31"
trades = json.load(open("history/bt_walkforward_long/bearish_side_aug.json"))["long"]["trades"]


def prep(tr):
    sym, d = tr["symbol"], date.fromisoformat(tr["day"])
    f, m1 = m.feats(sym)
    t0 = int(datetime.combine(d, datetime.strptime(tr["entry_time"], "%H:%M").time(), IST).timestamp())
    t1 = int(datetime.combine(d, datetime.strptime(tr["exit_time"], "%H:%M").time(), IST).timestamp())
    sq = int(datetime.combine(d, dtime(15, 15), IST).timestamp())
    spot0 = m.spot_at(m1, t0)
    tm = m.leg(sym, d, "LONG", t0, sq, spot0)
    ev = {"tr": tr, "sym": sym, "d": d, "f": f, "m1": m1, "t0": t0, "t1": t1, "sq": sq, "spot0": spot0, "tm": tm,
          "p0": float(tr["entry"]), "qty": int(tr["qty"]), "exit_px": float(tr["exit"]), "th": None, "hm": None}
    if tm is None:
        return ev
    p0, qty = ev["p0"], ev["qty"]
    for i, t in enumerate(tm.ts):
        if t <= t0 or t > t1 or (p0 - tm.l[i]) * qty < HEDGE_TRIGGER:
            continue
        k, s = m.bar_at(f, t), m.spot_at(m1, t)
        if k is not None and f["atr"][k] is not None and s is not None and spot0 - s >= f["atr"][k]:
            ev["th"] = t
            break
    if ev["th"] is not None:
        hm = m.leg(sym, d, "SHORT", ev["th"], sq, m.spot_at(m1, ev["th"]))
        if hm is not None:
            i0 = bisect.bisect_right(hm.ts, ev["th"]) - 1
            if i0 >= 0 and ev["th"] - hm.ts[i0] <= 300:
                ev["hm"], ev["i0"], ev["q0"] = hm, i0, hm.c[i0]
    return ev


EVENTS = [prep(t) for t in trades]


# --------------------------------------------------------------------------- #
# S2: PUT side
# --------------------------------------------------------------------------- #
def put_side(ev, add: bool, target: float | None):
    """-> (pnl, info). target None with add=True = both lots ride lot A's rules."""
    if ev["hm"] is None:
        return 0.0, {}
    hm, i0, q0, qty, sq, f = ev["hm"], ev["i0"], ev["q0"], ev["qty"], ev["sq"], ev["f"]
    a_open, b, booked = True, None, False          # b = {"q":..., "open":bool}
    a_stop, peak, pnl = q0 - HEDGE_STOP / qty, 0.0, 0.0
    info = {"added": False, "booked": False, "a_t": hm.ts[i0], "a_q": q0}      # leg times/prices: for capital studies only

    def out(t):
        info["a_exit_t"] = t
        if b:
            info.setdefault("b_exit_t", t)
        return pnl, info

    for j in range(i0 + 1, len(hm.ts)):
        t = hm.ts[j]
        if t >= sq:
            pnl += m.mod(q0, hm.o[j], qty)
            if b and b["open"]:
                pnl += m.mod(b["q"], hm.o[j], qty)
            return out(t)
        if hm.l[j] <= a_stop:                                        # A's stop / floor -> everything out
            pnl += m.mod(q0, min(a_stop, hm.o[j]), qty)
            if b and b["open"]:
                pnl += m.mod(b["q"], hm.c[j], qty)
            return out(t)
        if b and b["open"] and hm.l[j] <= b["q"] - B_STOP / qty:       # B's own stop
            pnl += m.mod(b["q"], min(b["q"] - B_STOP / qty, hm.o[j]), qty)
            b["open"] = False
            info["b_exit_t"] = t
        if target is not None and b and b["open"] and not booked:
            lvl = (target / qty + q0 + b["q"]) / 2
            if hm.h[j] >= lvl:                                         # combined profit reached: book lot B
                pnl += m.mod(b["q"], max(lvl, hm.o[j]), qty)
                b["open"], booked, info["booked"] = False, True, True
                info["b_exit_t"] = t
                a_stop = max(a_stop, q0)                               # kept lot: floor at its purchase price
        peak = max(peak, (hm.h[j] - q0) * qty)
        if peak >= ARM and (hm.c[j] - q0) * qty <= peak * (1 - GIVEBACK):
            pnl += m.mod(q0, hm.c[j], qty)
            if b and b["open"]:
                pnl += m.mod(b["q"], hm.c[j], qty)
            return out(t)
        if add and b is None:
            k = m.bar_at(f, t)
            if k is not None and f["st"][k] == -1:
                b = {"q": hm.c[j], "open": True}
                info["added"] = True
                info["b_t"], info["b_q"] = t, hm.c[j]
    pnl += m.mod(q0, hm.c[-1], qty)
    if b and b["open"]:
        pnl += m.mod(b["q"], hm.c[-1], qty)
    return out(hm.ts[-1])


# --------------------------------------------------------------------------- #
# S1: CALL side
# --------------------------------------------------------------------------- #
def call_side(ev, confirm: bool, lot2_exit: str, arm: float, pair_cap: bool, reentry: bool):
    """-> (pnl change vs the base trade, info). lot2_exit: "with" | "trail" | "st"."""
    tm, f, m1 = ev["tm"], ev["f"], ev["m1"]
    if tm is None or ev["th"] is None:
        return 0.0, {}
    p0, qty, t1, sq, spot0, exit_px = ev["p0"], ev["qty"], ev["t1"], ev["sq"], ev["spot0"], ev["exit_px"]
    base = float(ev["tr"]["pnl_modeled"])
    ia = None
    for i, t in enumerate(tm.ts):
        if t <= ev["th"] or m.hhmm(t) >= ENTRY_CUTOFF:
            continue
        u = bisect.bisect_right(m1["timestamps"], t) - 1
        if u < 0 or m1["highs"][u] < spot0:                            # the stock is back at its trigger price
            continue
        if confirm:
            k = m.bar_at(f, t)
            if k is None or f["st"][k] != 1:
                continue
        ia = i
        break
    if ia is None:
        return 0.0, {}
    ta, p2 = tm.ts[ia], tm.c[ia]
    orig_open = ta < t1
    if not orig_open and not reentry:
        return 0.0, {}
    info = {"readd": orig_open, "reentry": not orig_open, "c_t": ta, "c_q": p2, "c_exit_t": sq}   # leg time/price: capital studies
    peak2, lot1_pnl, be_armed = 0.0, base, False
    for j in range(ia + 1, len(tm.ts)):
        t = tm.ts[j]
        info["c_exit_t"] = t                                           # overwritten until the minute it actually exits
        if t >= sq:
            return (lot1_pnl - base) + m.mod(p2, tm.o[j], qty), info
        if orig_open and t >= t1:                                      # the original lot exits by its own rules
            orig_open = False
            if lot2_exit == "with":
                return (lot1_pnl - base) + m.mod(p2, exit_px, qty), info
        if orig_open and pair_cap:                                     # ONE max loss for both lots
            lvl = (p0 + p2 - MAX_LOSS / qty) / 2
            if tm.l[j] <= lvl:
                px = min(lvl, tm.o[j])
                return (m.mod(p0, px, qty) - base) + m.mod(p2, px, qty), {**info, "pair_stop": True}
        if tm.l[j] <= p2 - MAX_LOSS / qty:                             # the added lot's own max loss
            return (lot1_pnl - base) + m.mod(p2, min(p2 - MAX_LOSS / qty, tm.o[j]), qty), info
        if not orig_open and lot2_exit == "with":                      # re-entry / orphaned add on the normal call rules
            if be_armed and tm.l[j] <= p2:
                return (lot1_pnl - base) + m.mod(p2, min(p2, tm.o[j]), qty), info
            be_armed = be_armed or (tm.h[j] - p2) * qty >= BE_AFTER
        peak2 = max(peak2, (tm.h[j] - p2) * qty)
        if lot2_exit == "trail" and peak2 >= arm and (tm.c[j] - p2) * qty <= peak2 * (1 - GIVEBACK):
            return (lot1_pnl - base) + m.mod(p2, tm.c[j], qty), info
        if lot2_exit == "st":
            k = m.bar_at(f, t)
            if k is not None and f["ts"][k] + 300 > ta and f["st"][k] == -1:
                return (lot1_pnl - base) + m.mod(p2, tm.c[j], qty), info
    return (lot1_pnl - base) + m.mod(p2, tm.c[-1], qty), info


# --------------------------------------------------------------------------- #
def report(name, extra, n=None):
    daily = defaultdict(float)
    for ev, e in zip(EVENTS, extra):
        daily[ev["tr"]["day"]] += float(ev["tr"]["pnl_modeled"]) + e
    eq = pk = dd = 0.0
    for k in sorted(daily):
        eq += daily[k]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    a = sum(e for ev, e in zip(EVENTS, extra) if ev["tr"]["day"] < SPLIT)
    b = sum(e for ev, e in zip(EVENTS, extra) if ev["tr"]["day"] >= SPLIT)
    print(f"  {name:58s} {('n=' + str(n)) if n is not None else '':7s} extra {a + b:>+9,.0f} (Aug {a:>+8,.0f} Sep {b:>+8,.0f})"
          f"  total {sum(daily.values()):>+9,.0f}  dd {dd:>9,.0f}  worst {min(daily.values()):>+8,.0f}", flush=True)
    return {"extra": round(a + b), "aug": round(a), "sep": round(b), "total": round(sum(daily.values())), "dd": round(dd),
            "worst_day": round(min(daily.values())), "n": n}


out = {}
hedged = sum(1 for ev in EVENTS if ev["hm"] is not None)
print(f"CE trades {len(EVENTS)} | hedge fired on {sum(1 for ev in EVENTS if ev['th'])} (priced {hedged}) | Dhan calls {m.pr.calls}")
print("\n== PLAIN ==")
out["plain"] = report("calls only, no hedge", [0.0] * len(EVENTS))
print("\n== S2: PUT side (hedge 1,800 / stop 1,500 / 30% trail) ==")
res = [put_side(ev, False, None) for ev in EVENTS]
out["hedge"] = report("hedge, 1 PUT lot (live rule)", [r[0] for r in res], hedged)
HEDGE = [r[0] for r in res]
res = [put_side(ev, True, None) for ev in EVENTS]
out["2lots_ride"] = report("2 PUT lots, both ride lot A's trail", [r[0] for r in res], sum(1 for r in res if r[1].get("added")))
S2 = {}
for target in (3000, 4000, 4500, 5000, 6000):
    res = [put_side(ev, True, target) for ev in EVENTS]
    S2[target] = [r[0] for r in res]
    out[f"s2_{target}"] = report(f"2 PUT lots, sell one at combined +{target:,}, ride the other",
                                 S2[target], f"{sum(1 for r in res if r[1].get('added'))}/{sum(1 for r in res if r[1].get('booked'))}")
print("     (n = second lots added / times the combined target was reached)")

print("\n== S1: one more CALL when the stock is back at its trigger (extra shown WITHOUT any hedge) ==")
S1 = {}
for name, kw in (
    ("no confirm, pair cap 4,500, add exits with original", dict(confirm=False, lot2_exit="with", arm=0, pair_cap=True, reentry=False)),
    ("Supertrend bullish, pair cap 4,500, add exits with original", dict(confirm=True, lot2_exit="with", arm=0, pair_cap=True, reentry=False)),
    ("Supertrend bullish, pair cap 4,500, add 30% trail from +1,000", dict(confirm=True, lot2_exit="trail", arm=1000, pair_cap=True, reentry=False)),
    ("Supertrend bullish, pair cap 4,500, add 30% trail from +2,000", dict(confirm=True, lot2_exit="trail", arm=2000, pair_cap=True, reentry=False)),
    ("Supertrend bullish, pair cap 4,500, add sold on Supertrend flip", dict(confirm=True, lot2_exit="st", arm=0, pair_cap=True, reentry=False)),
    ("Supertrend bullish, EACH lot own 4,500, add 30% trail from +1,000", dict(confirm=True, lot2_exit="trail", arm=1000, pair_cap=False, reentry=False)),
    ("Supertrend bullish, EACH lot own 4,500, add exits with original", dict(confirm=True, lot2_exit="with", arm=0, pair_cap=False, reentry=False)),
    ("no confirm, EACH lot own 4,500, add exits with original", dict(confirm=False, lot2_exit="with", arm=0, pair_cap=False, reentry=False)),
    ("... + re-buy 1 lot if the first call was already stopped out", dict(confirm=True, lot2_exit="trail", arm=1000, pair_cap=True, reentry=True)),
    ("no confirm, pair cap, 30% trail from +1,000, + re-buy", dict(confirm=False, lot2_exit="trail", arm=1000, pair_cap=True, reentry=True)),
):
    res = [call_side(ev, **kw) for ev in EVENTS]
    S1[name] = [r[0] for r in res]
    n = f"{sum(1 for r in res if r[1].get('readd'))}+{sum(1 for r in res if r[1].get('reentry'))}"
    out["s1 " + name] = report(name, S1[name], n)
    out["s1 " + name]["pair_stops"] = sum(1 for r in res if r[1].get("pair_stop"))
    wins = sum(1 for r in res if r[1] and r[0] > 0)
    print(f"        pair max-loss exits: {out['s1 ' + name]['pair_stops']}   re-adds/re-buys that added money: {wins} of {sum(1 for r in res if r[1])}")
print("     (n = second lots added while the first call was open + re-buys after it was stopped)")

print("\n== TOGETHER: hedge / S2 on the PUT side + S1 on the CALL side ==")
s1_key = "Supertrend bullish, pair cap 4,500, add 30% trail from +1,000"
for label, put in (("live hedge only", HEDGE), ("S2 sell one at +4,000", S2[4000]), ("S2 sell one at +4,500", S2[4500]), ("S2 sell one at +5,000", S2[5000])):
    out[f"combo {label}"] = report(f"{label}  +  S1 ({s1_key[19:]})", [a + b for a, b in zip(put, S1[s1_key])])
best = "Supertrend bullish, pair cap 4,500, add exits with original"
for label, put in (("live hedge only", HEDGE), ("S2 sell one at +4,000", S2[4000]), ("S2 sell one at +4,500", S2[4500])):
    out[f"combo-best {label}"] = report(f"{label}  +  S1 (add exits with original, no trail)", [a + b for a, b in zip(put, S1[best])])
own = "Supertrend bullish, EACH lot own 4,500, add exits with original"
for label, put in (("no hedge", [0.0] * len(EVENTS)), ("live hedge only", HEDGE), ("S2 sell one at +4,000", S2[4000]), ("S2 sell one at +4,500", S2[4500])):
    out[f"combo-own {label}"] = report(f"{label}  +  S1 (each lot own 4,500, add exits with original)", [a + b for a, b in zip(put, S1[own])])
print(f"\nDhan calls made: {m.pr.calls}")
json.dump(out, open("history/bt_walkforward_long/recovery_scenarios.json", "w"), indent=2)
