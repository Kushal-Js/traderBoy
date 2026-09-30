"""
Anti-chop ENTRY filters for Super Bollinger (30 Sep 2026 evening, user: "is
there a technical or structural filter - current 1 hour above the open/close
of the last couple of candles, structure change patterns - to avoid choppy
movements completely? ... our HYBRID list didn't work for August").

Every filter is a yes/no computed from data available AT the entry (completed
bars only, continuous multi-day series). A filter keeps a trade or drops it;
the kept trades are scored on the two combinations the user picked:
  A = calls + live hedge (1,800 / 30% / stop 1,500) + S1 (re-add a call when
      the stock is back at its trigger; each lot own 4,500; add exits with the
      original)
  B = calls + S2 (two PUT lots, sell one at combined +4,000) + the same S1
Trades: HYBRID walk-forward CE book, 3 Aug - 29 Sep (193 trades). Dropping a
trade does not re-run the 5-slot limit (a freed slot could have taken another
signal) - part 3 re-simulates the best filters through the real simulator.

Filters (sources: Choppiness Index / ADX / Kaufman Efficiency Ratio as regime
gates; higher-timeframe permission rule = trade a 5-min signal only when the
1-hour structure agrees):
  1h structure  spot above the open AND close of each of the last 2 closed 1h
                candles; last 1h close up; 1h higher-high + higher-low; spot
                above the 1h EMA20; 1h Supertrend up; spot above this hour's open
  chop/trend    CHOP(14) on 5m and 15m below a level; ADX(14) on 5m/15m above a
                level; Efficiency Ratio(10/20) above a level; 15m Supertrend up
  day           spot above the day's open / yesterday's close / yesterday's
                high / the first 30-min high; above the 20-day average
  market        NIFTY above its day open / yesterday's close; NIFTY 1h
                structure up; NIFTY CHOP(15m) low; NIFTY Efficiency Ratio
  time          entry before 10:00, 10:00-11:30, 11:30-13:00, after 13:00

CACHE ONLY (no Dhan calls). Research only.
Run: uv run python research_super_bollinger_chop_filters.py
"""
from __future__ import annotations

import bisect
import contextlib
import io
import json
import math
from collections import defaultdict
from datetime import datetime, time as dtime

with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_recovery_scenarios as rs
from SuperTrader.strategy import ema, supertrend

m, IST = rs.m, rs.IST
EVENTS = rs.EVENTS
BASE = [float(ev["tr"]["pnl_modeled"]) for ev in EVENTS]
COMBO = {"A hedge+S1": [b + h + s for b, h, s in zip(BASE, rs.HEDGE, rs.S1[rs.own])],
         "B S2+S1": [b + h + s for b, h, s in zip(BASE, rs.S2[4000], rs.S1[rs.own])],
         "plain": BASE}
SPLIT = "2026-08-31"
LONG = m.LONG_DIR


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def resample(b, minutes):
    """Continuous 5-min bars -> `minutes` bars anchored at 09:15 each day (completed ones only matter to callers)."""
    out = {"ts": [], "o": [], "h": [], "l": [], "c": [], "end": []}
    key = None
    for t, o, h, l, c in zip(b["timestamps"], b["opens"], b["highs"], b["lows"], b["closes"]):
        dt = datetime.fromtimestamp(t, IST)
        mins = (dt.hour * 60 + dt.minute) - (9 * 60 + 15)
        k = (dt.date(), mins // minutes)
        if k != key:
            key = k
            out["ts"].append(t); out["o"].append(o); out["h"].append(h); out["l"].append(l); out["c"].append(c)
            out["end"].append(t + 300)
        else:
            out["h"][-1] = max(out["h"][-1], h); out["l"][-1] = min(out["l"][-1], l); out["c"][-1] = c
            out["end"][-1] = t + 300
    return out


def true_ranges(h, l, c):
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def chop(h, l, c, n=14):
    tr, out = true_ranges(h, l, c), [None] * len(c)
    for i in range(n - 1, len(c)):
        rng = max(h[i - n + 1:i + 1]) - min(l[i - n + 1:i + 1])
        out[i] = 100 * math.log10(sum(tr[i - n + 1:i + 1]) / rng) / math.log10(n) if rng > 0 else 100.0
    return out


def adx(h, l, c, n=14):
    tr, size = true_ranges(h, l, c), len(c)
    pdm = [0.0] + [max(h[i] - h[i - 1], 0.0) if h[i] - h[i - 1] > l[i - 1] - l[i] else 0.0 for i in range(1, size)]
    ndm = [0.0] + [max(l[i - 1] - l[i], 0.0) if l[i - 1] - l[i] > h[i] - h[i - 1] else 0.0 for i in range(1, size)]
    out = [None] * size
    if size < 2 * n + 1:
        return out
    atr_, p_, n_ = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(ndm[1:n + 1])
    dxs = []
    for i in range(n, size):
        if i > n:
            atr_, p_, n_ = atr_ - atr_ / n + tr[i], p_ - p_ / n + pdm[i], n_ - n_ / n + ndm[i]
        pdi, ndi = (100 * p_ / atr_ if atr_ else 0.0), (100 * n_ / atr_ if atr_ else 0.0)
        dxs.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0)
        if len(dxs) == n:
            out[i] = sum(dxs) / n
        elif len(dxs) > n:
            out[i] = (out[i - 1] * (n - 1) + dxs[-1]) / n
    return out


def eff_ratio(c, n):
    out = [None] * len(c)
    for i in range(n, len(c)):
        path = sum(abs(c[j] - c[j - 1]) for j in range(i - n + 1, i + 1))
        out[i] = abs(c[i] - c[i - n]) / path if path else 0.0
    return out


_cache: dict = {}


def series(sym):
    if sym not in _cache:
        name = "_NIFTY" if sym == "NIFTY" else sym
        b = json.loads((LONG / "underlying" / f"{name}_5min.json").read_text())
        h, l, c = b["highs"], b["lows"], b["closes"]
        m15, h1 = resample(b, 15), resample(b, 60)
        days = defaultdict(list)
        for i, t in enumerate(b["timestamps"]):
            days[datetime.fromtimestamp(t, IST).date()].append(i)
        dlist = sorted(days)
        daily_close = [c[days[d][-1]] for d in dlist]
        _cache[sym] = {
            "b": b, "ts": b["timestamps"], "chop5": chop(h, l, c), "adx5": adx(h, l, c), "er10": eff_ratio(c, 10),
            "er20": eff_ratio(c, 20), "m15": m15, "chop15": chop(m15["h"], m15["l"], m15["c"]),
            "adx15": adx(m15["h"], m15["l"], m15["c"]), "st15": supertrend(m15["h"], m15["l"], m15["c"], 10, 3.0),
            "h1": h1, "ema1h": ema(h1["c"], 20), "st1h": supertrend(h1["h"], h1["l"], h1["c"], 10, 3.0),
            "days": days, "dlist": dlist, "daily_close": daily_close,
        }
    return _cache[sym]


def last_closed(ends, t):
    """Index of the last bar whose END is <= t."""
    return bisect.bisect_right(ends, t) - 1


def features(sym, t0, spot):
    s = series(sym)
    b = s["b"]
    k = bisect.bisect_right(s["ts"], t0 - 300) - 1                        # last CLOSED 5-min bar
    d = datetime.fromtimestamp(t0, IST).date()
    f = {}
    if k < 60:
        return f
    f["chop5"], f["adx5"], f["er10"], f["er20"] = s["chop5"][k], s["adx5"][k], s["er10"][k], s["er20"][k]
    k15 = last_closed(s["m15"]["end"], t0)
    f["chop15"], f["adx15"], f["st15_up"] = s["chop15"][k15], s["adx15"][k15], s["st15"][k15] == 1
    h1 = s["h1"]
    kh = last_closed(h1["end"], t0)                                       # last CLOSED 1h candle
    if kh >= 2:
        f["h1_above_last2"] = all(spot > max(h1["o"][j], h1["c"][j]) for j in (kh, kh - 1))
        f["h1_above_last1"] = spot > max(h1["o"][kh], h1["c"][kh])
        f["h1_close_up"] = h1["c"][kh] > h1["c"][kh - 1]
        f["h1_hh_hl"] = h1["h"][kh] > h1["h"][kh - 1] and h1["l"][kh] > h1["l"][kh - 1]
        f["h1_last_green"] = h1["c"][kh] > h1["o"][kh]
        f["h1_above_ema20"] = s["ema1h"][kh] is not None and spot > s["ema1h"][kh]
        f["h1_st_up"] = s["st1h"][kh] == 1
        if kh + 1 < len(h1["o"]) and h1["ts"][kh + 1] <= t0:
            f["h1_above_hour_open"] = spot > h1["o"][kh + 1]              # the forming hour's open
    idx = s["days"].get(d, [])
    di = s["dlist"].index(d) if d in s["days"] else None
    if idx and di:
        day_open = b["opens"][idx[0]]
        prev = s["days"][s["dlist"][di - 1]]
        f["above_day_open"] = spot > day_open
        f["above_prev_close"] = spot > b["closes"][prev[-1]]
        f["above_prev_high"] = spot > max(b["highs"][i] for i in prev)
        first30 = [i for i in idx[:6] if s["ts"][i] + 300 <= t0]
        if len(first30) == 6:
            f["above_or30_high"] = spot > max(b["highs"][i] for i in first30)
        if di >= 20:
            f["above_sma20d"] = spot > sum(s["daily_close"][di - 20:di]) / 20
        f["gap_up"] = day_open > b["closes"][prev[-1]]
    return f


def all_features(ev):
    t0 = ev["t0"]
    f = features(ev["sym"], t0, ev["spot0"])
    n = series("NIFTY")
    kn = bisect.bisect_right(n["ts"], t0 - 300) - 1
    nspot = n["b"]["closes"][kn]
    for k, v in features("NIFTY", t0, nspot).items():
        f["nifty_" + k] = v
    hm = datetime.fromtimestamp(t0, IST).time()
    f["time_before_10"] = hm < dtime(10, 0)
    f["time_10_1130"] = dtime(10, 0) <= hm < dtime(11, 30)
    f["time_1130_13"] = dtime(11, 30) <= hm < dtime(13, 0)
    f["time_after_13"] = hm >= dtime(13, 0)
    return f


FEATS = [all_features(ev) for ev in EVENTS]


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def score(keep, values):
    daily = defaultdict(float)
    for ev, k, v in zip(EVENTS, keep, values):
        if k:
            daily[ev["tr"]["day"]] += v
    eq = pk = dd = 0.0
    for day in sorted(daily):
        eq += daily[day]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    a = sum(v for day, v in daily.items() if day < SPLIT)
    return {"n": sum(keep), "total": round(a + sum(v for day, v in daily.items() if day >= SPLIT)), "aug": round(a),
            "sep": round(sum(v for day, v in daily.items() if day >= SPLIT)), "dd": round(dd),
            "worst": round(min(daily.values())) if daily else 0}


def line(name, keep):
    a, b, p = score(keep, COMBO["A hedge+S1"]), score(keep, COMBO["B S2+S1"]), score(keep, COMBO["plain"])
    print(f"  {name:44s} n={a['n']:3d} | A {a['total']:>+8,} (Aug {a['aug']:>+7,} Sep {a['sep']:>+7,}) dd {a['dd']:>7,} worst {a['worst']:>+7,}"
          f" | B {b['total']:>+8,} dd {b['dd']:>7,} | plain {p['total']:>+8,} dd {p['dd']:>7,}", flush=True)
    return {"name": name, "A": a, "B": b, "plain": p}


def test(name, fn):
    keep = [bool(fn(f)) if f else False for f in FEATS]
    return line(name, keep), keep


ALL = [True] * len(EVENTS)
results = []
print(f"{len(EVENTS)} trades. A = calls + live hedge + S1(own caps, no trail); B = calls + S2(4,000) + S1. Columns: kept trades, total, Aug, Sep, max drawdown, worst day.\n")
print("== NO FILTER ==")
base_row = line("all trades", ALL)

GROUPS = {
    "1-HOUR STRUCTURE (user's idea)": [
        ("spot above open+close of last 2 x 1h candles", lambda f: f.get("h1_above_last2")),
        ("spot above open+close of last 1h candle", lambda f: f.get("h1_above_last1")),
        ("last 1h close above the one before", lambda f: f.get("h1_close_up")),
        ("1h higher high + higher low", lambda f: f.get("h1_hh_hl")),
        ("last 1h candle green", lambda f: f.get("h1_last_green")),
        ("spot above 1h EMA20", lambda f: f.get("h1_above_ema20")),
        ("1h Supertrend up", lambda f: f.get("h1_st_up")),
        ("spot above this hour's open", lambda f: f.get("h1_above_hour_open", True)),
    ],
    "CHOP / TREND STRENGTH": [(f"CHOP(14) 5m < {x}", lambda f, x=x: f.get("chop5") is not None and f["chop5"] < x) for x in (45, 50, 55, 61.8)]
    + [(f"CHOP(14) 15m < {x}", lambda f, x=x: f.get("chop15") is not None and f["chop15"] < x) for x in (45, 50, 55, 61.8)]
    + [(f"ADX(14) 5m >= {x}", lambda f, x=x: f.get("adx5") is not None and f["adx5"] >= x) for x in (15, 20, 25, 30)]
    + [(f"ADX(14) 15m >= {x}", lambda f, x=x: f.get("adx15") is not None and f["adx15"] >= x) for x in (15, 20, 25, 30)]
    + [(f"Efficiency Ratio(10) >= {x}", lambda f, x=x: f.get("er10") is not None and f["er10"] >= x) for x in (0.2, 0.3, 0.4)]
    + [(f"Efficiency Ratio(20) >= {x}", lambda f, x=x: f.get("er20") is not None and f["er20"] >= x) for x in (0.2, 0.3, 0.4)]
    + [("15m Supertrend up", lambda f: f.get("st15_up"))],
    "DAY CONTEXT": [
        ("spot above the day's open", lambda f: f.get("above_day_open")),
        ("spot above yesterday's close", lambda f: f.get("above_prev_close")),
        ("spot above yesterday's high", lambda f: f.get("above_prev_high")),
        ("spot above the first 30-min high", lambda f: f.get("above_or30_high", True)),
        ("spot above the 20-day average", lambda f: f.get("above_sma20d")),
        ("stock gapped up today", lambda f: f.get("gap_up")),
    ],
    "MARKET (NIFTY)": [
        ("NIFTY above its day open", lambda f: f.get("nifty_above_day_open")),
        ("NIFTY above yesterday's close", lambda f: f.get("nifty_above_prev_close")),
        ("NIFTY above open+close of last 2 x 1h candles", lambda f: f.get("nifty_h1_above_last2")),
        ("NIFTY last 1h close up", lambda f: f.get("nifty_h1_close_up")),
        ("NIFTY 1h Supertrend up", lambda f: f.get("nifty_h1_st_up")),
        ("NIFTY above 1h EMA20", lambda f: f.get("nifty_h1_above_ema20")),
        ("NIFTY CHOP(14) 15m < 50", lambda f: f.get("nifty_chop15") is not None and f["nifty_chop15"] < 50),
        ("NIFTY CHOP(14) 15m < 61.8", lambda f: f.get("nifty_chop15") is not None and f["nifty_chop15"] < 61.8),
        ("NIFTY ADX(14) 15m >= 20", lambda f: f.get("nifty_adx15") is not None and f["nifty_adx15"] >= 20),
        ("NIFTY above the 20-day average", lambda f: f.get("nifty_above_sma20d")),
    ],
    "TIME OF ENTRY": [
        ("entry before 10:00", lambda f: f.get("time_before_10")),
        ("entry 10:00-11:30", lambda f: f.get("time_10_1130")),
        ("entry 11:30-13:00", lambda f: f.get("time_1130_13")),
        ("entry after 13:00", lambda f: f.get("time_after_13")),
        ("NOT 11:30-13:00", lambda f: not f.get("time_1130_13")),
        ("NOT after 13:00", lambda f: not f.get("time_after_13")),
    ],
}
KEEPS = {}
for group, items in GROUPS.items():
    print(f"\n== {group} ==")
    for name, fn in items:
        row, keep = test(name, fn)
        results.append(row)
        KEEPS[name] = keep

# --------------------------------------------------------------------------- #
# Which single filters help in BOTH months and cut the drawdown? Then pairs.
# --------------------------------------------------------------------------- #
bA = base_row["A"]


def good(r):
    a = r["A"]
    return a["n"] >= 60 and a["dd"] > bA["dd"] and a["total"] >= 0.85 * bA["total"] and a["aug"] >= bA["aug"] - 3000


winners = sorted([r for r in results if good(r)], key=lambda r: -(r["A"]["total"] / max(1, -r["A"]["dd"])))
print(f"\n== SINGLE FILTERS THAT CUT THE DRAWDOWN, keep >= 85% of combo A's profit and do not hurt August ({len(winners)}) ==")
for r in winners:
    a = r["A"]
    print(f"  {r['name']:44s} n={a['n']:3d}  A {a['total']:>+8,} (Aug {a['aug']:>+7,} Sep {a['sep']:>+7,}) dd {a['dd']:>7,}  profit/drawdown {a['total'] / max(1, -a['dd']):.2f}"
          f"  (no filter: {bA['total'] / max(1, -bA['dd']):.2f})")

print("\n== PAIRS of those filters (both must pass) ==")
pairs = []
names = [r["name"] for r in winners[:10]]
for i in range(len(names)):
    for j in range(i + 1, len(names)):
        keep = [x and y for x, y in zip(KEEPS[names[i]], KEEPS[names[j]])]
        a, b = score(keep, COMBO["A hedge+S1"]), score(keep, COMBO["B S2+S1"])
        if a["n"] >= 50:
            pairs.append((a["total"] / max(1, -a["dd"]), names[i], names[j], a, b))
for ratio, n1, n2, a, b in sorted(pairs, key=lambda x: -x[0])[:12]:
    print(f"  {n1} + {n2}\n      n={a['n']:3d}  A {a['total']:>+8,} (Aug {a['aug']:>+7,} Sep {a['sep']:>+7,}) dd {a['dd']:>7,} worst {a['worst']:>+7,}  "
          f"profit/dd {ratio:.2f} | B {b['total']:>+8,} dd {b['dd']:>7,}")

json.dump({"base": base_row, "results": results}, open("history/bt_walkforward_long/chop_filters.json", "w"), indent=1)
print(f"\nDhan calls made: {m.pr.calls}")
