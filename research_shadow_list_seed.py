"""
Seed the first shadow watchlist from the LOCAL research cache (1 Oct 2026, 00:3x IST).

The Friday 00:00 job builds the shadow list (stock_selection.run_shadow: 40-session
fit with the 1-hour filter) from fresh Dhan data on the droplet; the first run is
Fri 2 Oct. So the shadow paper runner (SuperBollinger/shadow_list.py) had a list
from 1 Oct, this computed one with the SAME function (stock_selection.hybrid_select,
lookback=40, hour_filter=True) as of 30 Sep, from data already on disk: daily bars
to 28 Sep (history/walkforward_20260928/daily), 5-min bars to 30 Sep
(history/bt_walkforward_long/underlying), the local instrument file - no Dhan login
(the live bot's session cannot be used from outside it). An approximation: the
Friday job replaces it.

Output (committed): research_results/2026-10-01_shadow_list_seed.json - identical to
the record the bot loaded (data/super_bollinger_shadow_watchlist.json on the droplet).

Run: uv run python research_shadow_list_seed.py <file with the live list> <output json>
"""
import json, sys
from datetime import datetime, date
from pathlib import Path
sys.path.insert(0, ".")
import stock_selection as S
import walkforward_selector_eval as wf
import glob
import pandas as pd
from Options.dhan_client import dhan_wrapper
_df = pd.read_csv(sorted(glob.glob("Dependencies*all_instrument *.csv"))[-1], low_memory=False)
dhan_wrapper.instruments = lambda: _df          # local instrument file - no Dhan session
from fno_ath_screener import fetch_fno_universe
IST = S.IST
DAILY = Path("history/walkforward_20260928/daily"); FIVE = Path("history/bt_walkforward_long/underlying")
as_of = date(2026, 9, 30)
dailies = {}
for p in DAILY.glob("*.json"):
    d = wf.prepare_daily(json.loads(p.read_text()))
    dailies[p.stem] = wf.daily_slice(d, as_of)
fasts = {}
def get_fast(s):
    if s not in fasts:
        p = FIVE / f"{s}_5min.json"
        fasts[s] = json.loads(p.read_text()) if p.exists() else None
    return fasts[s]
universe = [s for s in fetch_fno_universe() if s in dailies]
dl = {s: dailies[s] for s in universe}
sessions = sorted({datetime.fromtimestamp(t, IST).date() for t in json.loads((FIVE / "_NIFTY_5min.json").read_text())["timestamps"]
                   if datetime.fromtimestamp(t, IST).date() <= as_of})
picks = S.hybrid_select(dl, get_fast, sessions, lookback=S.SHADOW_LOOKBACK_SESSIONS, hour_filter=True)
live = [l.strip() for l in open(sys.argv[1]).read().split() if l.strip()]
shadow = [p["symbol"] for p in picks]
rec = {"as_of": as_of.isoformat(), "picked_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
       "fit_sessions_used": min(S.SHADOW_LOOKBACK_SESSIONS, len(sessions)),
       "seeded_from": "local research cache on 1 Oct 01:xx IST (5-min bars to 30 Sep, daily bars to 28 Sep) - the Friday 00:00 job replaces it",
       "live": {"rule": S.LIVE_RULE, "symbols": live},
       "shadow": {"rule": f"{S.SHADOW_RULE} ({S.SHADOW_LOOKBACK_SESSIONS} sessions, fit with the 1-hour filter)", "symbols": shadow, "picks": picks},
       "only_live": sorted(set(live) - set(shadow)), "only_shadow": sorted(set(shadow) - set(live))}
json.dump(rec, open(sys.argv[2], "w"), indent=1, default=str)
print("shadow:", shadow); print("only live:", rec["only_live"]); print("only shadow:", rec["only_shadow"]); print("sessions:", len(sessions), sessions[-1])
