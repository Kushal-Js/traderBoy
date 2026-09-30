"""
How much of a 5-minute bar's range does the bot's live (websocket-built) candle
miss compared with Dhan's REST candle? (30 Sep 2026; found when RBLBANK's
409.2 trigger was touched on REST at 09:38 but the live bar's high was 408.45.)

The backtests read REST bars; the live bot sees only the ticks its websocket
delivers. If the live bars miss spikes, the backtests count entries the bot
cannot take (or takes later, at a worse price).

Inputs: the bot's own candle logs (history/<date>_swing_candles_<SYM>_<id>.log,
copied from the droplet to the folder given as argv[1]) and the cached REST
5-minute bars (history/bt_walkforward_long/underlying). No Dhan calls.

Run: uv run python research_ws_vs_rest_candle_gap.py <folder with the candle logs>
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, time as dtime
from pathlib import Path

import stock_selection as S

IST = S.IST
WS_DIR = Path(sys.argv[1])
REST = Path("history/bt_walkforward_long/underlying")

ws = defaultdict(dict)       # sym -> {timestamp: (o, h, l, c)}
for f in sorted(WS_DIR.glob("*_swing_candles_*.log")):
    sym = re.match(r"\d{4}-\d{2}-\d{2}_swing_candles_(.+)_\d+\.log", f.name).group(1)
    for line in f.read_text().splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        dt = datetime.fromisoformat(c["candle_start"])
        if dtime(9, 15) <= dt.time() < dtime(15, 30) and c.get("volume", 0) > 0:
            ws[sym][int(dt.timestamp())] = (c["open"], c["high"], c["low"], c["close"])

rows, per_sym, per_day = [], defaultdict(list), defaultdict(list)
missing_ws = defaultdict(int)
touch = defaultdict(int)
examples = []
for sym in sorted(ws):
    p = REST / f"{sym}_5min.json"
    if not p.exists():
        continue
    fast = json.loads(p.read_text())
    ts, H, L = fast["timestamps"], fast["highs"], fast["lows"]
    snap = S.pending_snapshots(fast)
    ws_days = {datetime.fromtimestamp(t, IST).date() for t in ws[sym]}
    for i, t in enumerate(ts):
        dt = datetime.fromtimestamp(t, IST)
        if dt.date() not in ws_days:
            continue
        w = ws[sym].get(t)
        if w is None:
            missing_ws[dt.date()] += 1
        else:
            up = (H[i] - w[1]) / H[i] * 100          # + = the live bar's high is BELOW the REST high
            dn = (w[2] - L[i]) / L[i] * 100          # + = the live bar's low is ABOVE the REST low
            rows.append((up, dn))
            per_sym[sym].append(up)
            per_day[dt.date()].append(up)
        # would a BULLISH resting trigger touched on the REST bar have been seen live?
        pnd = snap[i - 1] if i else None
        if pnd and pnd[0] == "BULLISH" and datetime.fromtimestamp(ts[i - 1], IST).date() == dt.date() \
                and dt.time() < dtime(14, 0) and H[i] >= pnd[1]:
            touch["rest_touches"] += 1
            if w is None:
                touch["no_live_bar"] += 1
            elif w[1] >= pnd[1]:
                touch["seen_same_bar"] += 1
            else:
                later = None
                for j in range(i + 1, min(i + 4, len(ts))):
                    w2 = ws[sym].get(ts[j])
                    if datetime.fromtimestamp(ts[j], IST).date() == dt.date() and w2 and w2[1] >= pnd[1]:
                        later = j - i
                        break
                touch["seen_1_to_3_bars_later" if later else "not_seen_within_15_min"] += 1
                if len(examples) < 12:
                    examples.append(f"{sym} {dt:%d %b %H:%M} trigger {pnd[1]:.2f}: REST high {H[i]:.2f}, live high {w[1]:.2f} "
                                    f"({'seen ' + str(later) + ' bar(s) later' if later else 'not seen in the next 15 min'})")


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))]


ups = [r[0] for r in rows]
dns = [r[1] for r in rows]
print(f"symbols {len(per_sym)}, sessions {len(per_day)} ({min(per_day)} .. {max(per_day)}), bars compared {len(rows)}; "
      f"REST bars with no live bar at all: {sum(missing_ws.values())} ({dict((str(k), v) for k, v in sorted(missing_ws.items()))})")
for name, v in (("HIGH: REST high above the live bar's high", ups), ("LOW: REST low below the live bar's low", dns)):
    print(f"{name}: in {100 * sum(x > 0.0001 for x in v) / len(v):.0f}% of bars | median shortfall {statistics.median(v):.3f}% | "
          f"90th pct {pct(v, .9):.3f}% | 99th pct {pct(v, .99):.3f}% | worst {max(v):.2f}% | "
          f"bars short by > 0.05%: {100 * sum(x > .05 for x in v) / len(v):.1f}%, > 0.10%: {100 * sum(x > .10 for x in v) / len(v):.1f}%, "
          f"> 0.20%: {100 * sum(x > .20 for x in v) / len(v):.1f}%")
print(f"live bar's high ABOVE the REST high (should not happen): {100 * sum(x < -0.0001 for x in ups) / len(ups):.1f}% of bars")
print("\nby session (share of bars whose live high is short by > 0.05%):")
for d in sorted(per_day):
    v = per_day[d]
    print(f"  {d}: {len(v):5d} bars, {100 * sum(x > .05 for x in v) / len(v):5.1f}%")
print("\nBULLISH resting triggers touched on the REST bars (before 14:00) - would the live bar have shown the touch?")
n = touch["rest_touches"]
for k in ("seen_same_bar", "seen_1_to_3_bars_later", "not_seen_within_15_min", "no_live_bar"):
    print(f"  {k.replace('_', ' '):28s} {touch[k]:4d}  ({100 * touch[k] / n if n else 0:.0f}%)")
print(f"  total touches {n}")
for e in examples:
    print("   e.g.", e)
