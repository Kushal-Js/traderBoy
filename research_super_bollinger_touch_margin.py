"""
How much of the live setup's backtest depends on marginal trigger touches
(1 Oct 2026, backtest-realism fix). The live bars miss some spikes that REST
bars show (research_ws_vs_rest_candle_gap.py: of 23 REST-bar entries in
6 sessions, 18 were also live entries, 5 backtest-only, 2 live-only). Here the
live setup is re-run requiring the trigger to be exceeded by a margin, and
with a random ~20% of entries removed, to see how fragile the result is.

Live setup (1 Oct): HYBRID walk-forward picks, 1-hour-green filter, 2 slots,
1 lot CE; hedge 1,800 / 30% trail; S1 + S2 as paper add-ons (shown apart).
3 Aug - 29 Sep, real option prices, modeled slippage. CACHE ONLY.

Run: uv run python research_super_bollinger_touch_margin.py
"""
from __future__ import annotations

import contextlib
import io
import random
from collections import defaultdict

with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_chop_filter_resim as rsim
rs, m, u, r = rsim.rs, rsim.m, rsim.u, rsim.r
SLOTS, SPLIT = 2, "2026-08-31"


def run(margin: float):
    u.TOUCH_MARGIN_PCT = margin
    u._cand_cache.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        trades, st = u.simulate(rsim.syms, SLOTS, m.pr, f"MARGIN{margin}", "long", rsim.allowed, symbol_gate=rsim.candle_gate(60))
        rows = []
        for t in trades:
            ev = rs.prep(t)
            rows.append((t["day"], float(t["pnl_modeled"]), rs.put_side(ev, False, None)[0],
                         rs.put_side(ev, True, 4000)[0] + rs.call_side(ev, confirm=True, lot2_exit="with", arm=0,
                                                                       pair_cap=False, reentry=False)[0]))
    return rows, st


def stats(rows, pick):
    daily = defaultdict(float)
    for x in rows:
        daily[x[0]] += pick(x)
    eq = pk = dd = 0.0
    for d in sorted(daily):
        eq += daily[d]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    a = sum(v for d, v in daily.items() if d < SPLIT)
    return f"{eq:>+9,.0f} (Aug {a:>+8,.0f} Sep {eq - a:>+8,.0f}) dd {dd:>8,.0f}"


print(f"{'trigger must be exceeded by':30s} {'trades':>6s} {'no data':>7s} | {'calls + hedge (real today)':^46s} | {'+ S1 + S2 (paper today)':^46s}")
base = None
for margin in (0.0, 0.02, 0.05, 0.10, 0.20):
    rows, st = run(margin)
    base = base or rows
    print(f"{f'{margin:.2f}%':30s} {len(rows):>6d} {st.get('skipped_no_option_data', 0) + st.get('skipped_no_option_print', 0):>7d} | "
          f"{stats(rows, lambda x: x[1] + x[2]):>46s} | {stats(rows, lambda x: x[1] + x[3]):>46s}")
u.TOUCH_MARGIN_PCT = 0.0
rng = random.Random(7)
res = []
for _ in range(200):
    kept = [x for x in base if rng.random() >= 0.2]
    res.append(sum(x[1] + x[2] for x in kept))
res.sort()
print(f"\nrandomly dropping 20% of the {len(base)} trades (200 draws), calls + hedge: median {res[100]:+,.0f}, "
      f"10th pct {res[20]:+,.0f}, 90th pct {res[180]:+,.0f} (all trades: {sum(x[1] + x[2] for x in base):+,.0f})")
print(f"Dhan calls made: {m.pr.calls} (cache-only: refused lookups)")
