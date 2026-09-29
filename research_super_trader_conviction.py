"""
Super Trader conviction research (30 Sep 2026, user request: "identify real
breakouts with maximum conviction ... fewer trades, large profit").

Underlying-only and fast (no option prices, no Dhan calls - uses the cache
written by backtest_super_trader_30day.py). Every raw Super Trader signal
(SuperTrader/strategy.py entry_signal, 31 Aug - 29 Sep) is graded on what
the STOCK did afterwards, then each conviction filter is measured on the
signals it keeps:
  ret_close  directional % move from entry to the 15:15 close (entry = next
             bar's open, or the open after the confirmation bar for "confirmed");
  mfe / mae  best / worst directional excursion before 15:15;
  failed     a 5-min close back past the breakout bar's far end before 15:15.

Anti-overfitting protocol: filters are CHOSEN on the first half
(31 Aug - 11 Sep, "design") and only then read on the second half
(15 - 29 Sep, "holdout"). A filter that helps only one half is noise.
Winners go on to the real option-priced backtest.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from SuperTrader.strategy import IST, Params, compute_features, ema, entry_signal

CACHE = Path("history/bt_super_trader_30day/underlying")
WINDOW_FROM, WINDOW_TO = date(2026, 8, 31), date(2026, 9, 29)
SPLIT = date(2026, 9, 15)


def load(name: str) -> dict:
    return json.loads((CACHE / f"{name}_5min.json").read_text())


def resample(f, minutes: int) -> dict:
    """Completed higher-timeframe closes keyed by bucket start, plus the EMA20
    of those closes (continuous, no day reset)."""
    buckets, order = {}, []
    for i, t in enumerate(f.ts):
        dt = datetime.fromtimestamp(t, IST)
        mins = (dt.hour * 60 + dt.minute - 555) // minutes * minutes + 555  # 555 = 09:15
        key = (f.day[i], mins)
        if key not in buckets:
            order.append(key)
        buckets[key] = (i, f.c[i])  # last 5-min bar in the bucket
    closes = [buckets[k][1] for k in order]
    e = ema(closes, 20)
    last_bar = {buckets[k][0]: (closes[j], e[j]) for j, k in enumerate(order)}
    return last_bar


def htf_state(last_bar: dict, i: int):
    """(close, ema) of the most recent COMPLETED higher-timeframe bar at or
    before 5-min bar i."""
    best = None
    for j in range(i, max(-1, i - 40), -1):
        if j in last_bar:
            best = last_bar[j]
            break
    return best


def main():
    p = Params()
    nifty = compute_features(load("_NIFTY"), p)
    n_idx = {t: k for k, t in enumerate(nifty.ts)}
    n_day_open = {}
    for k in range(len(nifty.ts)):
        n_day_open.setdefault(nifty.day[k], nifty.o[k])
    n_ema20 = ema(nifty.c, 20)

    rows = []
    for path in sorted(CACHE.glob("*_5min.json")):
        sym = path.name.replace("_5min.json", "")
        if sym.startswith("_"):
            continue
        bars = json.loads(path.read_text())
        if not bars.get("closes") or len(bars["closes"]) < 300:
            continue
        f = compute_features(bars, p)
        h15, h60 = resample(f, 15), resample(f, 60)
        day_open, prev_hi, prev_lo, or_hi, or_lo = {}, {}, {}, {}, {}
        days = []
        for i in range(len(f.ts)):
            d = f.day[i]
            if d not in day_open:
                day_open[d] = f.o[i]
                days.append(d)
            if datetime.fromtimestamp(f.ts[i], IST).time() < datetime.strptime("09:45", "%H:%M").time():
                or_hi[d] = max(or_hi.get(d, f.h[i]), f.h[i])
                or_lo[d] = min(or_lo.get(d, f.l[i]), f.l[i])
        day_hi = defaultdict(float)
        day_lo = {}
        for i in range(len(f.ts)):
            d = f.day[i]
            day_hi[d] = max(day_hi[d], f.h[i])
            day_lo[d] = min(day_lo.get(d, f.l[i]), f.l[i])
        for k in range(1, len(days)):
            prev_hi[days[k]], prev_lo[days[k]] = day_hi[days[k - 1]], day_lo[days[k - 1]]

        for i in range(len(f.ts) - 2):
            d = f.day[i]
            if not (WINDOW_FROM <= d <= WINDOW_TO):
                continue
            sig = entry_signal(f, i, p)
            if not sig:
                continue
            long = sig["side"] == "LONG"
            sgn = 1 if long else -1
            # outcome from entry = next bar open, to the day's last bar close
            last = i
            while last + 1 < len(f.ts) and f.day[last + 1] == d:
                last += 1
            if last <= i:
                continue

            def outcome(entry_idx):
                e = f.o[entry_idx]
                ret = sgn * (f.c[last] - e) / e * 100
                seg = range(entry_idx, last + 1)
                mfe = max(sgn * ((f.h[j] if long else f.l[j]) - e) / e * 100 for j in seg)
                mae = min(sgn * ((f.l[j] if long else f.h[j]) - e) / e * 100 for j in seg)
                failed = any((f.c[j] < sig["stop"]) if long else (f.c[j] > sig["stop"]) for j in seg)
                return ret, mfe, mae, failed

            ret, mfe, mae, failed = outcome(i + 1)
            # confirmation: next bar closes beyond the breakout bar's close, enter the bar after
            conf = (f.c[i + 1] > f.c[i]) if long else (f.c[i + 1] < f.c[i])
            cret = None
            if conf and i + 2 <= last and f.day[i + 2] == d:
                cret = outcome(i + 2)
            k = n_idx.get(f.ts[i])
            n_ok = None
            if k is not None and n_ema20[k] is not None:
                up = nifty.c[k] > n_day_open[nifty.day[k]] and nifty.c[k] > n_ema20[k]
                dn = nifty.c[k] < n_day_open[nifty.day[k]] and nifty.c[k] < n_ema20[k]
                n_ok = up if long else dn
            rs = None
            if k is not None:
                stock_chg = (f.c[i] - day_open[d]) / day_open[d]
                nifty_chg = (nifty.c[k] - n_day_open[nifty.day[k]]) / n_day_open[nifty.day[k]]
                rs = sgn * (stock_chg - nifty_chg) * 100
            s15, s60 = htf_state(h15, i - 1), htf_state(h60, i - 1)
            htf = None
            if s15 and s60 and s15[1] and s60[1]:
                htf = ((s15[0] > s15[1]) and (s60[0] > s60[1])) if long else ((s15[0] < s15[1]) and (s60[0] < s60[1]))
            lvl_prev = (prev_hi.get(d) is not None and ((f.c[i] > prev_hi[d]) if long else (f.c[i] < prev_lo[d])))
            lvl_or = (d in or_hi) and ((f.c[i] > or_hi[d]) if long else (f.c[i] < or_lo[d]))
            prev_rvol = f.rvol[i - 1] if i > 0 else None
            rows.append({
                "sym": sym, "day": d, "time": datetime.fromtimestamp(f.ts[i] + 300, IST).strftime("%H:%M"),
                "side": sig["side"], "score": sig["score"], "rvol": sig["rvol"], "range_atr": sig["range_atr"],
                "ret": ret, "mfe": mfe, "mae": mae, "failed": failed,
                "confirmed": conf, "cret": cret[0] if cret else None, "cfailed": cret[3] if cret else None,
                "nifty_ok": n_ok, "rs": rs, "htf": htf, "lvl_prev": lvl_prev, "lvl_or": lvl_or,
                "prev_rvol": prev_rvol,
            })

    print(f"graded {len(rows)} signals")

    filters = {
        "ALL (baseline)": lambda r: True,
        "nifty aligned": lambda r: r["nifty_ok"] is True,
        "RS vs nifty > 0.5%": lambda r: r["rs"] is not None and r["rs"] > 0.5,
        "HTF 15m+60m aligned": lambda r: r["htf"] is True,
        "breaks prev-day level": lambda r: r["lvl_prev"],
        "breaks opening range": lambda r: r["lvl_or"],
        "RVOL >= 3": lambda r: r["rvol"] >= 3,
        "prev bar RVOL >= 1.5": lambda r: r["prev_rvol"] is not None and r["prev_rvol"] >= 1.5,
        "range >= 2 ATR": lambda r: r["range_atr"] >= 2,
        "before 11:30": lambda r: r["time"] <= "11:30",
        "LONG only": lambda r: r["side"] == "LONG",
        "SHORT only": lambda r: r["side"] == "SHORT",
        "nifty + HTF": lambda r: r["nifty_ok"] is True and r["htf"] is True,
        "nifty + HTF + RS": lambda r: r["nifty_ok"] is True and r["htf"] is True and r["rs"] is not None and r["rs"] > 0.5,
        "nifty + HTF + prev-day lvl": lambda r: r["nifty_ok"] is True and r["htf"] is True and r["lvl_prev"],
        "nifty + HTF + RS + RVOL3": lambda r: (r["nifty_ok"] is True and r["htf"] is True and r["rs"] is not None
                                               and r["rs"] > 0.5 and r["rvol"] >= 3),
    }

    def stats(rs_, key="ret", fkey="failed"):
        vals = [r[key] for r in rs_ if r[key] is not None]
        if not vals:
            return "n=0"
        fails = [r[fkey] for r in rs_ if r[fkey] is not None]
        return (f"n={len(vals):4d} mean={statistics.mean(vals):+6.2f}% med={statistics.median(vals):+6.2f}% "
                f"win={sum(v > 0 for v in vals) / len(vals) * 100:5.1f}% big(>=1%)={sum(v >= 1 for v in vals) / len(vals) * 100:5.1f}% "
                f"fail={sum(fails) / len(fails) * 100 if fails else 0:5.1f}%")

    for label, fn in filters.items():
        kept = [r for r in rows if fn(r)]
        h1 = [r for r in kept if r["day"] < SPLIT]
        h2 = [r for r in kept if r["day"] >= SPLIT]
        print(f"\n{label}")
        print(f"   design  H1  {stats(h1)}")
        print(f"   holdout H2  {stats(h2)}")
        c1 = [r for r in h1 if r["confirmed"]]
        c2 = [r for r in h2 if r["confirmed"]]
        print(f"   +confirm H1 {stats(c1, 'cret', 'cfailed')}")
        print(f"   +confirm H2 {stats(c2, 'cret', 'cfailed')}")
    Path("history/bt_super_trader_30day/conviction_rows.json").write_text(json.dumps(rows, default=str))


if __name__ == "__main__":
    main()
