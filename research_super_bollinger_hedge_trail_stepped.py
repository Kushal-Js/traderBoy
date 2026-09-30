"""
Stepped hedge trail + a broker stop that is ratcheted up with it, and the call
profit-trail sweep - on the book that is live now (30 Sep 2026 evening, user:
"backtest and show results first"; also the owed "trail sweep on August").

Book: HYBRID walk-forward picks, 3 Aug - 29 Sep, 1-hour-green entry filter,
4 slots, CE only; hedge = 1 PUT lot at call loss 1,800 (+1 ATR), 1,500 stop,
no time cutoff. Real option prices, modeled slippage. CACHE ONLY.

HEDGE PUT - how the profit is protected once it is up:
  tiers            [(peak profit Rs, share given back)] - the tightest tier
                   reached applies. Live = 30% from 1,000.
  bot exit         the bot sells when a 1-minute CLOSE is at/below the trail
                   level (how every earlier result was computed).
  ratcheted stop   the broker stop-loss order is moved up to the trail level
                   each time the peak rises, so the exit happens the moment the
                   price TOUCHES the level: level from the peak before that
                   minute, filled at the level, or at the minute's open if it
                   opened below (gap). Conservative.

CALL - a profit trail on top of today's rules (max loss 4,500, breakeven
after +1,500, 15:15): once the peak reaches a tier, a resting stop keeps
(1 - giveback) of the peak.

Run: uv run python research_super_bollinger_hedge_trail_stepped.py
"""
from __future__ import annotations

import contextlib
import io
from collections import defaultdict

with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_chop_filter_resim as rsim
rs, m, u, r = rsim.rs, rsim.m, rsim.u, rsim.r
SPLIT, STOP, SLOTS = "2026-08-31", rs.HEDGE_STOP, 4

with contextlib.redirect_stdout(io.StringIO()):
    trades, _st = u.simulate(rsim.syms, SLOTS, m.pr, "TRAIL", "long", rsim.allowed, symbol_gate=rsim.candle_gate(60))
    EVS = [rs.prep(t) for t in trades]
HEDGED = [ev for ev in EVS if ev["hm"] is not None]


def tier(tiers, peak):
    gb = None
    for p_rs, g in tiers:
        if peak >= p_rs:
            gb = g
    return gb


def hedge_run(ev, tiers, touch):
    """-> (pnl, peak profit seen, exit kind)"""
    hm, i0, q0, qty, sq = ev["hm"], ev["i0"], ev["q0"], ev["qty"], ev["sq"]
    peak = 0.0
    for j in range(i0 + 1, len(hm.ts)):
        if hm.ts[j] >= sq:
            return m.mod(q0, hm.o[j], qty), peak, "15:15"
        if (q0 - hm.l[j]) * qty >= STOP:
            return m.mod(q0, min(q0 - STOP / qty, hm.o[j]), qty), peak, "stop"
        if touch:
            gb = tier(tiers, peak)
            if gb is not None:
                lvl = q0 + peak * (1 - gb) / qty
                if hm.l[j] <= lvl:
                    return m.mod(q0, min(lvl, hm.o[j]), qty), peak, "trail"
        peak = max(peak, (hm.h[j] - q0) * qty)
        if not touch:
            gb = tier(tiers, peak)
            if gb is not None and (hm.c[j] - q0) * qty <= peak * (1 - gb):
                return m.mod(q0, hm.c[j], qty), peak, "trail"
    return m.mod(q0, hm.c[-1], qty), peak, "15:15"


def ce_run(ev, tiers):
    tm, p0, qty, t0, t1 = ev["tm"], ev["p0"], ev["qty"], ev["t0"], ev["t1"]
    base = float(ev["tr"]["pnl_modeled"])
    if tm is None:
        return base
    peak = 0.0
    for j, t in enumerate(tm.ts):
        if t <= t0:
            continue
        if t >= t1:
            break
        gb = tier(tiers, peak)
        if gb is not None:
            lvl = p0 + peak * (1 - gb) / qty
            if tm.l[j] <= lvl:
                return m.mod(p0, min(lvl, tm.o[j]), qty)
        peak = max(peak, (tm.h[j] - p0) * qty)
    return base


def book_stats(extra):
    """extra: per trade add-on pnl (aligned with EVS) -> total, Aug, Sep, max dd, worst day of calls + extra."""
    daily = defaultdict(float)
    for ev, e in zip(EVS, extra):
        daily[ev["tr"]["day"]] += float(ev["tr"]["pnl_modeled"]) + e
    eq = pk = dd = 0.0
    for d in sorted(daily):
        eq += daily[d]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    a = sum(v for d, v in daily.items() if d < SPLIT)
    return eq, a, eq - a, dd, min(daily.values())


HEDGE_VARIANTS = {
    "LIVE: 30% from 1,000": [(1000, .30)],
    "40% from 1,000 (live until 30 Sep 12:16)": [(1000, .40)],
    "25% from 1,000": [(1000, .25)],
    "20% from 1,000": [(1000, .20)],
    "PROPOSED stepped: 40% from 1k, 25% from 2k": [(1000, .40), (2000, .25)],
    "stepped: 30% from 1k, 20% from 2k": [(1000, .30), (2000, .20)],
    "stepped: 30% from 1k, 25% from 2k, 20% from 3k": [(1000, .30), (2000, .25), (3000, .20)],
    "stepped: 40% from 1k, 30% from 2k, 20% from 3k": [(1000, .40), (2000, .30), (3000, .20)],
    "stepped: 30% from 1k, 15% from 3k": [(1000, .30), (3000, .15)],
    "arm later: 30% from 1,500": [(1500, .30)],
    "arm earlier: 30% from 500": [(500, .30)],
}
print(f"book: {len(EVS)} call trades ({SLOTS} slots, 1-hour filter), hedge fired and priced on {len(HEDGED)}")
print(f"peaks: {sum(1 for ev in HEDGED if hedge_run(ev, [(1000, .30)], False)[1] >= 1000)} hedges were ever up 1,000+, "
      f"{sum(1 for ev in HEDGED if hedge_run(ev, [(10**9, 0)], False)[1] >= 2000)} up 2,000+, "
      f"{sum(1 for ev in HEDGED if hedge_run(ev, [(10**9, 0)], False)[1] >= 3000)} up 3,000+ (before any trail exit)")
print("\n== HEDGE PUT: trail variants x how the exit is executed ==")
print(f"{'trail':48s} | {'BOT EXIT (1-min close)':^52s} | {'RATCHETED BROKER STOP (touch)':^52s}")
print(f"{'':48s} | {'hedge PnL':>9s} {'Aug':>8s} {'Sep':>8s} {'wins':>6s} {'book dd':>8s} {'trail exits':>11s} | {'hedge PnL':>9s} {'Aug':>8s} {'Sep':>8s} {'wins':>6s} {'book dd':>8s} {'trail exits':>11s}")
for name, tiers in HEDGE_VARIANTS.items():
    cells = []
    for touch in (False, True):
        by_ev = {id(ev): hedge_run(ev, tiers, touch) for ev in HEDGED}
        extra = [by_ev[id(ev)][0] if id(ev) in by_ev else 0.0 for ev in EVS]
        res = [(ev["tr"]["day"], *by_ev[id(ev)]) for ev in HEDGED]
        tot = sum(x[1] for x in res)
        a = sum(x[1] for x in res if x[0] < SPLIT)
        cells.append(f"{tot:>+9,.0f} {a:>+8,.0f} {tot - a:>+8,.0f} {sum(x[1] > 0 for x in res):>3d}/{len(res):<2d} {book_stats(extra)[3]:>8,.0f} {sum(x[3] == 'trail' for x in res):>11d}")
    print(f"{name:48s} | {cells[0]} | {cells[1]}")
print("   (book dd = max drawdown of calls + hedge together)")

print("\n== CALL: profit trail on top of today's rules ==")
b = book_stats([0.0] * len(EVS))
print(f"{'TODAY (no trail)':48s} total {b[0]:>+9,.0f}  Aug {b[1]:>+8,.0f}  Sep {b[2]:>+8,.0f}  dd {b[3]:>8,.0f}  worst day {b[4]:>+8,.0f}")
CE_VARIANTS = {}
for arm in (2000, 3000, 4000, 6000):
    for g in (.30, .40, .50):
        CE_VARIANTS[f"after +{arm // 1000}k, give back {int(g * 100)}%"] = [(arm, g)]
CE_VARIANTS["stepped: 50% from 3k, 35% from 6k, 25% from 10k"] = [(3000, .50), (6000, .35), (10000, .25)]
CE_VARIANTS["stepped: 50% from 4k, 30% from 8k"] = [(4000, .50), (8000, .30)]
CE_VARIANTS["only big winners: 30% from 8k"] = [(8000, .30)]
CE_VARIANTS["only big winners: 25% from 10k"] = [(10000, .25)]
for name, tiers in CE_VARIANTS.items():
    extra = [ce_run(ev, tiers) - float(ev["tr"]["pnl_modeled"]) for ev in EVS]
    s = book_stats(extra)
    print(f"{name:48s} total {s[0]:>+9,.0f}  Aug {s[1]:>+8,.0f}  Sep {s[2]:>+8,.0f}  dd {s[3]:>8,.0f}  worst day {s[4]:>+8,.0f}  "
          f"({sum(1 for v in extra if abs(v) > 1)} trades exit earlier, {s[0] - b[0]:>+9,.0f} vs today)")
