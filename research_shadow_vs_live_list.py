"""
Why the seeded shadow list differs from the live list (1 Oct 2026): for every stock on
either list, its trend (ATH) rank and the fit under both rules - LIVE (20 sessions, no
filter) and SHADOW (40 sessions, 1-hour filter) - and its rank inside the top-40 pool.
Same local data and caveats as research_shadow_list_seed.py (daily bars only to 28 Sep).

Output (committed): research_results/2026-10-01_shadow_vs_live_list.txt

Run: uv run python research_shadow_vs_live_list.py <file with the live list> <shadow record json>
"""
import json, sys, glob
from datetime import datetime, date
from pathlib import Path
sys.path.insert(0, ".")
import pandas as pd
from Options.dhan_client import dhan_wrapper
_df = pd.read_csv(sorted(glob.glob("Dependencies*all_instrument *.csv"))[-1], low_memory=False)
dhan_wrapper.instruments = lambda: _df
import stock_selection as S
import walkforward_selector_eval as wf
from fno_ath_screener import fetch_fno_universe, score_stock
IST = S.IST
DAILY = Path("history/walkforward_20260928/daily"); FIVE = Path("history/bt_walkforward_long/underlying")
as_of = date(2026, 9, 30)
dailies = {p.stem: wf.daily_slice(wf.prepare_daily(json.loads(p.read_text())), as_of) for p in DAILY.glob("*.json")}
universe = [s for s in fetch_fno_universe() if s in dailies]
gated = {s: dailies[s] for s in universe if S.liquidity_gate(dailies[s])[0]}
ath = {}
for s, d in gated.items():
    if len(d["close"]) >= S.MIN_DAILY_BARS_FOR_ATH:
        try:
            r = score_stock(s, d)
        except Exception:
            r = None
        if r: ath[s] = r["total_score"]
pool = sorted(ath, key=lambda s: ath[s], reverse=True)[:40]
rank = {s: i + 1 for i, s in enumerate(sorted(ath, key=lambda s: ath[s], reverse=True))}
sessions = sorted({datetime.fromtimestamp(t, IST).date() for t in json.loads((FIVE / "_NIFTY_5min.json").read_text())["timestamps"]
                   if datetime.fromtimestamp(t, IST).date() <= as_of})
def fit(sym, look, flt):
    p = FIVE / f"{sym}_5min.json"
    if not p.exists(): return None, 0
    per = S.fit_by_day(json.loads(p.read_text()), flt)
    days = set(sessions[-look:])
    return round(sum(v[0] for d, v in per.items() if d in days), 2), sum(v[1] for d, v in per.items() if d in days)
live = [l.strip() for l in open(sys.argv[1]).read().split() if l.strip()]
shadow = json.load(open(sys.argv[2]))["shadow"]["symbols"]
# full rankings inside the pool for both rules
def ranking(look, flt):
    sc = {}
    for s in pool:
        f, n = fit(s, look, flt)
        if f is not None and n >= S.FIT_MIN_TRADES: sc[s] = f
    return {s: i + 1 for i, s in enumerate(sorted(sc, key=lambda s: sc[s], reverse=True))}
r20, r40 = ranking(20, False), ranking(40, True)
print(f"pool: top 40 of {len(ath)} liquid stocks by ATH score (daily bars to {max(d for s in dailies for d in [])} )" if False else f"pool = top 40 of {len(ath)} liquid stocks by ATH score; sessions used up to {sessions[-1]}")
print(f"{'stock':12s} {'ATH rank':>8s} {'in pool':>7s} | {'LIVE rule: 20 sessions, no filter':>34s} | {'SHADOW rule: 40 sessions, 1h filter':>36s} | list")
for s in sorted(set(live) | set(shadow), key=lambda s: (s not in live, s not in shadow, s)):
    f20, n20 = fit(s, 20, False); f40, n40 = fit(s, 40, True)
    tag = "both" if s in live and s in shadow else ("LIVE only" if s in live else "SHADOW only")
    print(f"{s:12s} {rank.get(s, '-'):>8} {'yes' if s in pool else 'NO':>7s} | fit {f20:+7.2f}% {n20:3d} trades, rank {str(r20.get(s, '-')):>3s} | "
          f"fit {f40:+7.2f}% {n40:3d} trades, rank {str(r40.get(s, '-')):>3s} | {tag}")
