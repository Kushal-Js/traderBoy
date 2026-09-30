"""
Super Bollinger on the whole F&O stock universe (30 Sep 2026, user request:
"run Super Bollinger on whole F&O universe and show me results").

Exactly the deployed Super Bollinger rules (SuperBollinger/settings.py
defaults, backtest policy "G'"), only the universe changes (210 F&O stocks,
every NIFTY 50 stock included, instead of the 15-stock watchlist):
  - signal: the deployed Bollinger/Vortex pending-order state machine on the
    continuous 5-min series (bollinger_research.fires_with_snapshots), BULLISH
    only; entry the minute a 1-min HIGH touches the pending trigger armed at
    the previous 5-min close (same day), fill_u = max(trigger, minute open);
    sides="both" (user follow-up) also takes BEARISH triggers - a 1-min LOW
    touching the trigger, fill_u = min(trigger, minute open), ATM PE - as a
    separately reported variant;
  - ATM CE, nearest monthly expiry rolled within 2 trading days, min premium
    Rs 5, lot = current lot size;
  - exits on every option 1-min bar: MAX_LOSS_HIT (Rs 4500), BREAKEVEN_STOP
    once the trade has been Rs 1500 in profit, DAILY_SQUARE_OFF at 15:15;
    no entries from 14:00;
  - portfolio: at most `cap` open trades across all stocks. A touch that
    finds every slot full does NOT use the signal up (the live tick path
    doesn't either), so a later touch in the same bar can still enter.
    Same-minute touches are filled in alphabetical order (no ranking exists).
Real option prices: backtest_super_trader_30day.OptionPricer (Dhan expired-
options data rebuilt for the fixed entry strike up to 28 Sep, the listed
27 OCT contracts on 29 Sep). Entry premium = entry minute's option close
adjusted by 0.5 x (fill - underlying minute close), as in the validated
backtest_bollinger_hold_long_30day.py. Slippage on both legs (live paper
model); brokerage/taxes and the funds check not modeled.

Run: HANDOFF_DHAN_ACCESS_TOKEN=... uv run python backtest_super_bollinger_universe_30day.py [watchlist|universe] [cap] [long|both]
"""
from __future__ import annotations

import bisect
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime
from pathlib import Path

import walkforward_selector_eval as wf
from walkforward_selector_eval import IST, bt, dhan_wrapper
from bollinger_research import fires_with_snapshots
from Bollinger.paper_book import modeled_slippage_pct
from fno_ath_screener import fetch_fno_universe
import backtest_super_trader_30day as st

ROOT = Path("history/bt_super_trader_30day")
OUT = Path("history/bt_super_bollinger_universe_30day")
WINDOW_FROM, WINDOW_TO = date(2026, 8, 31), date(2026, 9, 29)
MAX_LOSS, BE_AFTER, MIN_PREMIUM, DELTA = 4500.0, 1500.0, 5.0, 0.5
ENTRY_CUTOFF, SQUARE_OFF = dtime(14, 0), dtime(15, 15)
WATCHLIST = ["ZYDUSLIFE", "SONACOMS", "DIVISLAB", "AUROPHARMA", "MOTHERSON", "APOLLOHOSP", "APLAPOLLO", "MCX",
             "BOSCHLTD", "LAURUSLABS", "OBEROIRLTY", "RBLBANK", "RADICO", "PHOENIXLTD", "MOTILALOFS"]
HALF_SPLIT = "2026-09-15"


def candidates_for(sym: str, sides: str) -> list[tuple]:
    fast = json.loads((ROOT / "underlying" / f"{sym}_5min.json").read_text())
    p1 = ROOT / "underlying_1m" / f"{sym}_1min.json"
    if not fast.get("closes") or len(fast["closes"]) < 300 or not p1.exists():
        return []
    m = json.loads(p1.read_text())
    signals = bt.compute_signals(fast)
    _fires, snap = fires_with_snapshots(fast, signals)
    fts = fast["timestamps"]
    out = []
    for k, t in enumerate(m["timestamps"]):
        dt = datetime.fromtimestamp(t, IST)
        if not (WINDOW_FROM <= dt.date() <= WINDOW_TO) or dt.time() >= ENTRY_CUTOFF:
            continue
        bi = bisect.bisect_right(fts, t) - 1
        if bi < 1 or datetime.fromtimestamp(fts[bi - 1], IST).date() != dt.date():
            continue
        p = snap[bi - 1]
        if p is None:
            continue
        if p[0] == "BULLISH" and m["highs"][k] >= p[1]:
            out.append((int(t), sym, "LONG", max(p[1], m["opens"][k]), m["closes"][k], fts[bi - 1]))
        elif sides == "both" and p[0] == "BEARISH" and m["lows"][k] <= p[1]:
            out.append((int(t), sym, "SHORT", min(p[1], m["opens"][k]), m["closes"][k], fts[bi - 1]))
    return out


_cand_cache: dict = {}


def simulate(syms: list[str], cap: int, pricer: st.OptionPricer, label: str, sides: str = "long",
             allowed: dict | None = None, daily_loss_limit: float | None = None,
             entry_gate=None, cooloff_week_of: dict | None = None,
             soft_stop_rs: float | None = None, max_reentries: int = 0, reentry_cutoff: dtime = dtime(14, 0)):
    """`allowed` (optional): {date: set of symbols} - only those symbols may
    enter on that day (walk-forward stock selection).
    Loss-reduction switches (30 Sep 2026, all off by default):
      daily_loss_limit - no new entries once the day's realized modeled PnL
                         is at or below -daily_loss_limit;
      entry_gate(t, side) -> bool - extra check before each new entry (e.g.
                         a NIFTY-weakness filter);
      cooloff_week_of  - {date: week key}; after a MAX_LOSS_HIT the stock is
                         blocked for the rest of that week key.
      soft_stop_rs / max_reentries / reentry_cutoff - stop-and-re-enter (user
                         idea, 30 Sep): exit at a Rs soft_stop_rs open loss
                         (SOFT_STOP), keep watching that option, and buy it
                         again when it trades back up to the ORIGINAL entry
                         price, before reentry_cutoff, at most max_reentries
                         times per signal; re-entered legs use the same exits."""
    events = []
    for s in syms:
        if (s, sides) not in _cand_cache:
            _cand_cache[(s, sides)] = candidates_for(s, sides)
        events += _cand_cache[(s, sides)]
    if allowed is not None:
        events = [e for e in events if e[1] in allowed.get(datetime.fromtimestamp(e[0], IST).date(), set())]
    events.sort()
    days = sorted({datetime.fromtimestamp(e[0], IST).date() for e in events})
    by_t = defaultdict(list)
    for e in events:
        by_t[e[0]].append(e)
    for d in days:  # end-of-day sentinel so every position gets squared off
        by_t.setdefault(int(datetime.combine(d, dtime(15, 16), IST).timestamp()), [])
    trades, open_pos, consumed, stats = [], {}, set(), defaultdict(int)
    day_pnl = defaultdict(float)
    blocked = set()  # (symbol, week key)
    watches = {}     # symbol -> stopped-out leg waiting for the option to get back to its entry price
    peak_open, peak_capital = 0, 0.0

    def close(sym, t, px, reason):
        pos = open_pos.pop(sym)
        e_mod = pos["p0"] * (1 + modeled_slippage_pct(pos["p0"]))
        x_mod = px * (1 - modeled_slippage_pct(px))
        day_pnl[pos["day"]] += (x_mod - e_mod) * pos["qty"]
        if reason == "MAX_LOSS_HIT" and cooloff_week_of is not None:
            blocked.add((sym, cooloff_week_of.get(pos["day"])))
        if reason == "SOFT_STOP" and pos.get("reentries_left", 0) > 0:
            watches[sym] = {**pos, "k": bisect.bisect_right(pos["mins"].ts, t), "trigger": pos["orig_p0"]}
        trades.append({"run": label, "symbol": sym, "side": pos["side"], "contract": pos["name"], "day": pos["day"].isoformat(),
                       "entry_time": datetime.fromtimestamp(pos["t0"], IST).strftime("%H:%M"),
                       "exit_time": datetime.fromtimestamp(t, IST).strftime("%H:%M"),
                       "entry": round(pos["p0"], 2), "exit": round(px, 2), "qty": pos["qty"], "reason": reason,
                       "pnl_raw": round((px - pos["p0"]) * pos["qty"], 2),
                       "pnl_modeled": round((x_mod - e_mod) * pos["qty"], 2)})

    def advance(sym, until):
        pos = open_pos[sym]
        mm = pos["mins"]
        k = pos["k"]
        while k < len(mm.ts) and mm.ts[k] < until:
            t = mm.ts[k]
            if datetime.fromtimestamp(t, IST).time() >= SQUARE_OFF or datetime.fromtimestamp(t, IST).date() != pos["day"]:
                close(sym, t, mm.o[k], "DAILY_SQUARE_OFF")
                return
            level = pos["p0"] - (soft_stop_rs or MAX_LOSS) / pos["qty"]
            if mm.l[k] <= level:
                close(sym, t, min(level, mm.o[k]), "SOFT_STOP" if soft_stop_rs else "MAX_LOSS_HIT")
                return
            if pos["peak"] >= BE_AFTER and mm.l[k] <= pos["p0"]:
                close(sym, t, min(pos["p0"], mm.o[k]), "BREAKEVEN_STOP_HIT")
                return
            pos["peak"] = max(pos["peak"], (mm.h[k] - pos["p0"]) * pos["qty"])
            k += 1
        pos["k"] = k
        if datetime.fromtimestamp(until, IST).time() > SQUARE_OFF:
            idx = bisect.bisect_right(mm.ts, int(datetime.combine(pos["day"], SQUARE_OFF, IST).timestamp())) - 1
            close(sym, until, mm.c[idx] if idx >= 0 else pos["p0"], "DAILY_SQUARE_OFF")

    def advance_watch(sym, until):
        w = watches[sym]
        mm, k = w["mins"], w["k"]
        while k < len(mm.ts) and mm.ts[k] < until:
            tt = mm.ts[k]
            dtt = datetime.fromtimestamp(tt, IST)
            if dtt.date() != w["day"] or dtt.time() >= reentry_cutoff:
                del watches[sym]
                return
            if mm.h[k] >= w["trigger"]:
                if sym in open_pos or len(open_pos) >= cap:
                    stats["reentry_skipped_full"] += 1
                    del watches[sym]
                    return
                p0 = max(w["trigger"], mm.o[k])
                open_pos[sym] = {"side": w["side"], "p0": p0, "orig_p0": w["orig_p0"], "qty": w["qty"], "t0": tt,
                                 "day": w["day"], "mins": mm, "k": k + 1, "peak": 0.0, "name": w["name"],
                                 "reentries_left": w["reentries_left"] - 1}
                stats["reentered"] += 1
                del watches[sym]
                advance(sym, until)  # manage the re-entered leg up to `until`
                return
            k += 1
        w["k"] = k

    last_day = None
    for t in sorted(by_t):
        d = datetime.fromtimestamp(t, IST).date()
        if d != last_day:
            if last_day is not None:
                print(f"  [{label}] {last_day}: {len(trades)} trades so far, {pricer.calls} option calls", flush=True)
            last_day = d
        for sym in list(open_pos):
            advance(sym, t)
        for sym in list(watches):
            advance_watch(sym, t)
        for (_t, sym, side, fill_u, und_close, key) in by_t[t]:
            if (sym, key) in consumed or sym in open_pos or sym in watches:
                continue
            if len(open_pos) >= cap:
                stats["touch_while_full"] += 1
                continue
            if daily_loss_limit is not None and day_pnl[d] <= -daily_loss_limit:
                stats["skipped_daily_loss_limit"] += 1
                continue
            if cooloff_week_of is not None and (sym, cooloff_week_of.get(d)) in blocked:
                stats["skipped_cooloff"] += 1
                continue
            if entry_gate is not None and not entry_gate(t, side):
                stats["skipped_entry_gate"] += 1
                continue
            consumed.add((sym, key))
            qty = pricer.lot(sym)
            if not qty:
                stats["skipped_no_lot"] += 1
                continue
            sq = int(datetime.combine(d, SQUARE_OFF, IST).timestamp())
            leg = pricer.open_leg(sym, d, side, t, sq, fill_u)
            if leg is None:
                stats["skipped_no_option_data"] += 1
                continue
            mm = leg["mins"]
            i = bisect.bisect_right(mm.ts, t) - 1
            if i < 0 or t - mm.ts[i] > 300 or datetime.fromtimestamp(mm.ts[i], IST).date() != d:
                stats["skipped_no_option_print"] += 1
                continue
            p0 = max(mm.c[i] + (1 if side == "LONG" else -1) * DELTA * (fill_u - und_close), 0.05)
            if p0 < MIN_PREMIUM:
                stats["skipped_premium_below_min"] += 1
                continue
            open_pos[sym] = {"side": side, "p0": p0, "orig_p0": p0, "reentries_left": max_reentries, "qty": qty, "t0": t, "day": d, "mins": mm, "k": bisect.bisect_right(mm.ts, t),
                             "peak": 0.0, "name": f"{sym} {leg['expiry']:%d %b} {leg['strike']:g} {leg['ot']}"}
            stats["entered"] += 1
        peak_open = max(peak_open, len(open_pos))
        peak_capital = max(peak_capital, sum(pp["p0"] * pp["qty"] for pp in open_pos.values()))
    stats["peak_open_positions"] = peak_open
    stats["peak_premium_deployed_rs"] = round(peak_capital)
    return trades, dict(stats)


def summarize(tr):
    if not tr:
        return {"n": 0}
    pnl = [t["pnl_modeled"] for t in tr]
    daily = defaultdict(float)
    for t in tr:
        daily[t["day"]] += t["pnl_modeled"]
    eq = pk = dd = 0.0
    for d in sorted(daily):
        eq += daily[d]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    wins = [x for x in pnl if x > 0]
    return {"n": len(tr), "win_pct": round(len(wins) / len(tr) * 100, 1), "net": round(sum(pnl)),
            "net_raw": round(sum(t["pnl_raw"] for t in tr)), "avg_win": round(statistics.mean(wins)) if wins else 0,
            "avg_loss": round(statistics.mean([x for x in pnl if x <= 0])) if len(wins) < len(pnl) else 0,
            "green_days": sum(v > 0 for v in daily.values()), "days": len(daily), "max_dd": round(dd),
            "H1": round(sum(v for k, v in daily.items() if k < HALF_SPLIT)),
            "H2": round(sum(v for k, v in daily.items() if k >= HALF_SPLIT))}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "universe"
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    sides = sys.argv[3] if len(sys.argv) > 3 else "long"
    wf.authenticate()
    dhan_wrapper.client.Dhan.dhan_http.timeout = 90
    syms = WATCHLIST if which == "watchlist" else fetch_fno_universe()
    pricer = st.OptionPricer()
    label = f"{which}_cap{cap}_{sides}"
    t0 = time.time()
    tr, stats = simulate(syms, cap, pricer, label, sides)
    reasons = defaultdict(lambda: [0, 0.0])
    per_sym = defaultdict(lambda: [0, 0.0])
    per_side = defaultdict(lambda: [0, 0.0])
    daily = defaultdict(lambda: [0, 0.0])
    for t in tr:
        reasons[t["reason"]][0] += 1
        reasons[t["reason"]][1] += t["pnl_modeled"]
        per_side[t["side"]][0] += 1
        per_side[t["side"]][1] += t["pnl_modeled"]
        per_sym[t["symbol"]][0] += 1
        per_sym[t["symbol"]][1] += t["pnl_modeled"]
        daily[t["day"]][0] += 1
        daily[t["day"]][1] += t["pnl_modeled"]
    report = {"summary": summarize(tr), "stats": stats,
              "by_reason": {k: [n, round(v)] for k, (n, v) in reasons.items()},
              "by_side": {k: [n, round(v)] for k, (n, v) in per_side.items()},
              "by_symbol": {k: [n, round(v)] for k, (n, v) in sorted(per_sym.items(), key=lambda x: x[1][1])},
              "daily": {k: [n, round(v)] for k, (n, v) in sorted(daily.items())}}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{label}_summary.json").write_text(json.dumps(report, indent=2))
    if tr:
        with open(OUT / f"{label}_trades.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(tr[0].keys()))
            w.writeheader()
            w.writerows(tr)
    print(f"[{label}] {report['summary']} stats={stats} by_side={report['by_side']} by_reason={report['by_reason']} "
          f"({time.time() - t0:.0f}s, {pricer.calls} option calls)", flush=True)


if __name__ == "__main__":
    main()
