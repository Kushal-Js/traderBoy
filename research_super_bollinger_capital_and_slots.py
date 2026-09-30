"""
Capital and slots for the live Super Bollinger setup (30 Sep 2026 evening).

User: "How much extra capital would be needed [to run scenario 1 and 2 for
real]? We can make max concurrent trade = 4, that should solve this issue" and
"backtest fewer slots when NIFTY is above its 20-day average".

Book: HYBRID walk-forward picks, 3 Aug - 29 Sep, 1-hour-green entry filter,
CE only, real option prices, modeled slippage (same as research_super_
bollinger_final_setup_trades.py). Add-ons per call trade:
  hedge   1 PUT lot at call loss 1,800 (+1 ATR), 30% trail, 1,500 stop   (REAL today)
  S2      + a 2nd PUT lot on Supertrend-bearish, sell one at combined +4,000 (paper today)
  S1      + one more call when the stock is back at its trigger          (paper today)

A. MONEY NEEDED - the most premium tied up at one moment (all open legs at
   their purchase price), for 5 / 4 / 3 / 2 slots, with and without S1 + S2.
B. WITH A FIXED ACCOUNT - the same trades walked in time order with a cash
   budget: a leg that cannot be paid for is not taken (a skipped call takes
   its add-ons with it; a skipped hedge means no 2nd PUT lot). Cash comes back
   when a leg is sold; profits and losses are not added to the budget.
   Approximation: a call skipped for cash does not hand its slot to a later
   signal.
C. FEWER SLOTS WHEN NIFTY IS STRONG - slot limit lowered on days when NIFTY's
   previous close is above its 20-day average (daily rule), or for the whole
   week when it was above at the weekly pick (weekly rule).

CACHE ONLY. Run: uv run python research_super_bollinger_capital_and_slots.py
"""
from __future__ import annotations

import contextlib
import io
from collections import defaultdict
from datetime import date, datetime

with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_chop_filter_resim as rsim
rs, m, u, r, cf = rsim.rs, rsim.m, rsim.u, rsim.r, rsim.cf
IST = m.IST
GATE = rsim.candle_gate(60)
SPLIT = "2026-08-31"


def book(cap, cap_for_day=None):
    with contextlib.redirect_stdout(io.StringIO()):
        trades, st = u.simulate(rsim.syms, cap, m.pr, f"CAP{cap}", "long", rsim.allowed, symbol_gate=GATE, cap_for_day=cap_for_day)
        rows = []
        for t in trades:
            ev = rs.prep(t)
            h, hi = rs.put_side(ev, False, None)
            s2, s2i = rs.put_side(ev, True, 4000)
            s1, s1i = rs.call_side(ev, confirm=True, lot2_exit="with", arm=0, pair_cap=False, reentry=False)
            rows.append({"t": t, "ev": ev, "call": float(t["pnl_modeled"]), "hedge": h, "hi": hi, "s2": s2, "s2i": s2i, "s1": s1, "s1i": s1i})
    return rows, st


def legs(x, mode):
    """[(open time, close time, premium paid, kind)] of one call trade. mode: calls | hedge | s1s2"""
    ev = x["ev"]
    out = [(ev["t0"], ev["t1"], ev["p0"] * ev["qty"], "call")]
    if mode == "hedge" and x["hi"]:
        out.append((x["hi"]["a_t"], x["hi"]["a_exit_t"], x["hi"]["a_q"] * ev["qty"], "A"))
    if mode == "s1s2":
        i2 = x["s2i"]
        if i2:
            out.append((i2["a_t"], i2["a_exit_t"], i2["a_q"] * ev["qty"], "A"))
            if i2.get("added"):
                out.append((i2["b_t"], i2["b_exit_t"], i2["b_q"] * ev["qty"], "B"))
        if x["s1i"].get("readd"):
            out.append((x["s1i"]["c_t"], x["s1i"]["c_exit_t"], x["s1i"]["c_q"] * ev["qty"], "C"))
    return out


def pnl_of(x, mode):
    return x["call"] + (x["hedge"] if mode == "hedge" else 0.0) + ((x["s2"] + x["s1"]) if mode == "s1s2" else 0.0)


def stats(day_pnl):
    eq = pk = dd = 0.0
    for d in sorted(day_pnl):
        eq += day_pnl[d]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    a = sum(v for d, v in day_pnl.items() if d < SPLIT)
    return eq, a, eq - a, dd, (min(day_pnl.values()) if day_pnl else 0.0)


def premium_peaks(rows, mode):
    """(highest premium tied up at one moment, {day: that day's highest})."""
    ev = []
    for x in rows:
        for a, b, cost, _k in legs(x, mode):
            ev.append((a, 1, cost))
            ev.append((max(b, a + 1), 0, -cost))
    ev.sort()
    cur, by_day = 0.0, defaultdict(float)
    for t, _o, c in ev:
        cur += c
        d = datetime.fromtimestamp(t, IST).date().isoformat()
        by_day[d] = max(by_day[d], cur)
    return (max(by_day.values()) if by_day else 0.0), by_day


def walk(rows, budget, mode):
    """Cash-limited replay -> (day pnl, counters)."""
    opens = []
    for i, x in enumerate(rows):
        ev = x["ev"]
        opens.append((ev["t0"], 0, "call", i))
        if mode != "calls" and x["hi"]:
            opens.append((x["hi"]["a_t"], 1, "A", i))
        if mode == "s1s2":
            if x["s2i"].get("added"):
                opens.append((x["s2i"]["b_t"], 2, "B", i))
            if x["s1i"].get("readd"):
                opens.append((x["s1i"]["c_t"], 3, "C", i))
    opens.sort()
    cash, held, took = budget, {}, defaultdict(set)       # held: (i, kind) -> [exit time, cost]
    n = defaultdict(int)
    for t, _o, kind, i in opens:
        for key in [k for k, v in held.items() if v[0] <= t]:
            cash += held.pop(key)[1]
        x = rows[i]
        ev = x["ev"]
        qty = ev["qty"]
        if kind != "call" and "call" not in took[i]:
            continue
        if kind == "B" and "A" not in took[i]:
            continue
        cost, exit_t = {"call": (ev["p0"] * qty, ev["t1"]),
                        "A": (x["hi"]["a_q"] * qty, x["hi"]["a_exit_t"]) if x["hi"] else (0, 0),
                        "B": (x["s2i"].get("b_q", 0) * qty, x["s2i"].get("b_exit_t", 0)),
                        "C": (x["s1i"].get("c_q", 0) * qty, x["s1i"].get("c_exit_t", 0))}[kind]
        if cost > cash:
            n[f"{kind}_skipped"] += 1
            continue
        cash -= cost
        took[i].add(kind)
        n[f"{kind}_taken"] += 1
        held[(i, kind)] = [max(exit_t, t + 1), cost]
        if kind == "B":                                     # with a 2nd lot, lot A follows the S2 exit
            held[(i, "A")][0] = max(x["s2i"]["a_exit_t"], t + 1)
    day = defaultdict(float)
    for i, x in enumerate(rows):
        if "call" not in took[i]:
            continue
        p = x["call"]
        if "A" in took[i]:
            p += x["s2"] if "B" in took[i] else x["hedge"]
        if "C" in took[i]:
            p += x["s1"]
        day[x["t"]["day"]] += p
    return day, n


print("Loading the four books (5 / 4 / 3 / 2 slots, 1-hour filter)...", flush=True)
BOOKS = {cap: book(cap) for cap in (5, 4, 3, 2)}
MODES = (("calls", "calls only"), ("hedge", "calls + hedge (real today)"), ("s1s2", "calls + hedge + S1 + S2"))

print("\n== A. MONEY NEEDED (no cash limit) ==")
print(f"{'slots':5s} {'setup':28s} {'trades':>6s} {'total':>9s} {'Aug':>8s} {'Sep':>8s} {'max dd':>8s} {'worst day':>9s} | {'peak premium':>12s} {'typical day peak':>16s} {'days > 61k':>10s} {'days > 110k':>11s}")
for cap, (rows, st) in BOOKS.items():
    for mode, name in MODES:
        day = defaultdict(float)
        for x in rows:
            day[x["t"]["day"]] += pnl_of(x, mode)
        tot, a, b, dd, worst = stats(day)
        peak, by_day = premium_peaks(rows, mode)
        v = sorted(by_day.values())
        print(f"{cap:<5d} {name:28s} {len(rows):>6d} {tot:>+9,.0f} {a:>+8,.0f} {b:>+8,.0f} {dd:>8,.0f} {worst:>+9,.0f} | {peak:>12,.0f} "
              f"{v[len(v) // 2]:>16,.0f} {sum(x > 61_000 for x in v):>7d}/{len(v):<2d} {sum(x > 110_000 for x in v):>8d}/{len(v):<2d}")
rows4 = BOOKS[4][0]
prem = sorted(x["ev"]["p0"] * x["ev"]["qty"] for x in rows4)
print(f"one call lot costs: median {prem[len(prem) // 2]:,.0f}, smallest {prem[0]:,.0f}, largest {prem[-1]:,.0f} (4-slot book, {len(prem)} trades)")

print("\n== B. WITH A FIXED ACCOUNT (cash-limited replay) ==")
print(f"{'account':>8s} {'slots':>5s} {'setup':28s} {'total':>9s} {'Aug':>8s} {'Sep':>8s} {'max dd':>8s} {'worst day':>9s} | calls taken/skipped, hedges taken/skipped, 2nd PUT taken/skipped, extra call taken/skipped")
for budget in (61_000, 110_000, 150_000, 200_000, 250_000):
    for cap in (4, 3, 2):
        rows = BOOKS[cap][0]
        for mode, name in MODES[1:]:
            day, n = walk(rows, budget, mode)
            tot, a, b, dd, worst = stats(day)
            print(f"{budget:>8,d} {cap:>5d} {name:28s} {tot:>+9,.0f} {a:>+8,.0f} {b:>+8,.0f} {dd:>8,.0f} {worst:>+9,.0f} | "
                  f"{n['call_taken']}/{n['call_skipped']}, {n['A_taken']}/{n['A_skipped']}, {n['B_taken']}/{n['B_skipped']}, {n['C_taken']}/{n['C_skipped']}")
    print()

# ---- C. NIFTY regime ----
ns = cf.series("NIFTY")
dl, dc = ns["dlist"], ns["daily_close"]
above_daily = {}
for k, d in enumerate(dl):
    if k >= 20:
        above_daily[d] = dc[k - 1] > sum(dc[k - 20:k]) / 20        # yesterday's close vs the 20 closes up to yesterday
above_weekly = {d: above_daily.get(min((x for x in dl if x > a), default=d), False) for d, a in rsim.as_of.items()}
win = [d for d in dl if date(2026, 8, 3) <= d <= date(2026, 9, 29)]
print("== C. FEWER SLOTS WHEN NIFTY IS ABOVE ITS 20-DAY AVERAGE (normal limit 4) ==")
print(f"days in the window: {len(win)}; NIFTY above its 20-day average on {sum(above_daily.get(d, False) for d in win)} (daily rule), "
      f"{sum(above_weekly.get(d, False) for d in win)} (weekly rule)")
base_rows = BOOKS[4][0]
by_reg = {True: defaultdict(float), False: defaultdict(float)}
cnt = {True: 0, False: 0}
for x in base_rows:
    d = date.fromisoformat(x["t"]["day"])
    reg = above_daily.get(d, False)
    cnt[reg] += 1
    for mode, _n in MODES:
        by_reg[reg][mode] += pnl_of(x, mode)
for reg, name in ((True, "NIFTY above its 20-day average"), (False, "NIFTY below its 20-day average")):
    print(f"  4 slots, trades on days with {name}: {cnt[reg]:3d} trades | calls {by_reg[reg]['calls']:>+9,.0f} | + hedge {by_reg[reg]['hedge']:>+9,.0f} | + hedge + S1 + S2 {by_reg[reg]['s1s2']:>+9,.0f}")
print(f"\n{'rule':8s} {'slots when above':>16s} {'trades':>6s} | {'calls only':>34s} | {'calls + hedge':>34s} | {'calls + hedge + S1 + S2':>34s}")
for rule, amap in (("daily", above_daily), ("weekly", above_weekly)):
    for low in (4, 3, 2, 1, 0):
        rows = base_rows if low == 4 else book(4, cap_for_day=lambda d, a=amap, lo=low: lo if a.get(d, False) else 4)[0]
        cells = []
        for mode, _n in MODES:
            day = defaultdict(float)
            for x in rows:
                day[x["t"]["day"]] += pnl_of(x, mode)
            tot, a, b, dd, worst = stats(day)
            cells.append(f"{tot:>+9,.0f} (Aug {a:>+7,.0f}) dd {dd:>7,.0f}")
        print(f"{rule:8s} {low:>16d} {len(rows):>6d} | {cells[0]:>34s} | {cells[1]:>34s} | {cells[2]:>34s}")
print(f"\nDhan calls made: {m.pr.calls} (cache-only: refused lookups, not network calls)")
