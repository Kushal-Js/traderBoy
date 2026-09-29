"""
User request (29 Sep 2026): can the Bollinger Hold-Long CE side make more /
lose less with a different holding pattern - profit booking with re-entry
while momentum continues (~Rs 2k per leg), trailing, indicator exits, etc.

Research only (paper strategy, nothing live changes). Same entries, data and
contract resolution as backtest_bollinger_hold_long_30day.py (long-only,
MAX_LOSS 4500 baseline - variant "A" must reproduce that script's number);
only the exit / re-entry policy differs per variant:

  target_rs     book the leg when its profit reaches this (resting limit:
                filled at the target level when a 1-min option HIGH reaches
                it, or at the open if it gapped past).
  reenter       after a target booking, re-buy the SAME contract when the
                underlying makes a new high above the booking minute's high
                while the last closed 5-min bar still passes the entry trend
                filter (close > SMA20 and VI+ > VI-). Each leg has its own
                max-loss and target. No re-entry at/after REENTRY_CUTOFF.
  lock_after_rs / lock_keep
                once peak profit >= lock_after_rs, exit if profit falls to
                lock_keep * peak (0.0 = breakeven stop, 0.5 = keep half);
                lock2_* is an optional second, tighter tier.
  trend_exit    exit at the first 5-min close that fails the trend filter.
  entry_cutoff  no fresh signal entries at/after this time (NSE).

Intra-minute ordering is conservative: a minute whose low hits the stop and
whose high hits the target counts as the stop.

Anti-overfitting: 21 trading days only - every variant is also reported per
half (31 Aug-11 Sep vs 15-29 Sep); only trust a change that helps in both.

Run: HANDOFF_DHAN_ACCESS_TOKEN=... uv run python backtest_bollinger_hold_long_exit_variants.py
     ... backtest_bollinger_hold_long_exit_variants.py --max-concurrent 5   (portfolio slot limit, NSE only)
(uses the cache written by backtest_bollinger_hold_long_30day.py)
"""
from __future__ import annotations

import bisect
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time as dtime

import backtest_bollinger_hold_long_30day as hl
from backtest_bollinger_hold_long_30day import (DELTA, IST, MCX_FRIDAY_SQUARE_OFF, MCX_MULTIPLIER, OUT, SQUARE_OFF,
                                                WINDOW_FROM, WINDOW_TO, Bars, bcfg, bt, fires_with_snapshots,
                                                modeled_slippage_pct, option_series_listed, option_series_rolling,
                                                resolve_contract, summarize, wf)

MAX_LOSS = 4500.0
REENTRY_CUTOFF = dtime(15, 0)
HALF_SPLIT = "2026-09-15"


@dataclass(frozen=True)
class Policy:
    name: str
    target_rs: float | None = None
    reenter: bool = False
    lock_after_rs: float | None = None
    lock_keep: float = 0.0
    lock2_after_rs: float | None = None   # optional second tier (tighter keep once further in profit)
    lock2_keep: float = 0.0
    trend_exit: bool = False
    entry_cutoff: dtime | None = None


POLICIES = [
    Policy("A  baseline: hold to 15:15"),
    Policy("B1 book at +2k, no re-entry", target_rs=2000),
    Policy("B2 book at +2k, re-enter on new high", target_rs=2000, reenter=True),
    Policy("B2 book at +1.5k, re-enter", target_rs=1500, reenter=True),
    Policy("B2 book at +3k, re-enter", target_rs=3000, reenter=True),
    Policy("C  after +2k, trail keeping 50%", lock_after_rs=2000, lock_keep=0.5),
    Policy("D  after +2k, breakeven stop", lock_after_rs=2000, lock_keep=0.0),
    Policy("E  exit when 5-min trend filter fails", trend_exit=True),
    Policy("F  no entries after 14:00", entry_cutoff=dtime(14, 0)),
    # Robustness round: neighbours of D and F, then the combination.
    Policy("D' breakeven after +1.5k", lock_after_rs=1500, lock_keep=0.0),
    Policy("D' breakeven after +3k", lock_after_rs=3000, lock_keep=0.0),
    Policy("F' no entries after 13:30", entry_cutoff=dtime(13, 30)),
    Policy("F' no entries after 14:30", entry_cutoff=dtime(14, 30)),
    Policy("G  D + F (BE after +2k, no entry after 14:00)", lock_after_rs=2000, lock_keep=0.0,
           entry_cutoff=dtime(14, 0)),
    Policy("G' BE after +1.5k, no entry after 14:00", lock_after_rs=1500, lock_keep=0.0, entry_cutoff=dtime(14, 0)),
    Policy("G' BE after +3k, no entry after 14:00", lock_after_rs=3000, lock_keep=0.0, entry_cutoff=dtime(14, 0)),
    Policy("H  G + keep 50% after +4k", lock_after_rs=2000, lock_keep=0.0, lock2_after_rs=4000, lock2_keep=0.5,
           entry_cutoff=dtime(14, 0)),
]


def simulate(sym, fast, master, listed, schedule, pol: Policy, skips) -> list[dict]:
    """One symbol on its own, every entry allowed (no capacity limit)."""
    trades: list[dict] = []
    gen = simulate_gen(sym, fast, master, listed, schedule, pol, skips, trades, {"open": False})
    try:
        next(gen)
        while True:
            gen.send(True)
    except StopIteration:
        pass
    return trades


def simulate_gen(sym, fast, master, listed, schedule, pol: Policy, skips, trades: list, holder: dict):
    """Generator form of the simulation so run_with_capacity() can step every
    symbol through the same clock. Yields ("time", t) before each 1-min bar
    (after which that bar's exits are processed) and ("entry", t) whenever it
    wants to open a leg - the value sent back decides whether it may.
    holder["open"] mirrors whether this symbol currently holds a leg."""
    is_mcx = sym in MCX_MULTIPLIER
    signals = bt.compute_signals(fast)
    valid = signals["valid_bullish"]
    _fires, snap = fires_with_snapshots(fast, signals)
    fts = fast["timestamps"]
    fset = set(fts)
    m = Bars(master)
    last_of_day = {}
    for t in m.ts:
        last_of_day[datetime.fromtimestamp(t, IST).date()] = t
    pos, consumed, rearm = None, set(), None
    opt_cache: dict = {}

    def opt_for(contract, day):
        expiry, strike, sid, lot, code = contract
        key = sid or (day, code, strike)
        if key not in opt_cache:
            s = option_series_listed(sym, sid) if sid else option_series_rolling(sym, day, code, strike, "CE")
            opt_cache[key] = Bars(s) if s["closes"] else None
        return opt_cache[key]

    def close(t, px, reason):
        e_mod = pos["p0"] * (1 + modeled_slippage_pct(pos["p0"]))
        x_mod = px * (1 - modeled_slippage_pct(px))
        trades.append({"policy": pol.name, "symbol": sym, "contract": pos["name"], "day": pos["day"].isoformat(),
                       "leg": pos["leg"], "entry_time": datetime.fromtimestamp(pos["t0"], IST).strftime("%H:%M"),
                       "exit_time": datetime.fromtimestamp(t, IST).strftime("%H:%M"),
                       "entry": round(pos["p0"], 2), "exit": round(px, 2), "qty": pos["lot"], "reason": reason,
                       "pnl_raw": round((px - pos["p0"]) * pos["lot"], 2),
                       "pnl_modeled": round((x_mod - e_mod) * pos["lot"], 2)})

    def open_pos(t, ob, p0, lot, d, name, contract, leg):
        return {"opt": ob, "p0": p0, "lot": lot, "t0": t, "day": d, "name": name, "contract": contract,
                "leg": leg, "peak": 0.0}

    for k, t in enumerate(m.ts):
        dt = datetime.fromtimestamp(t, IST)
        d = dt.date()
        if d > WINDOW_TO:
            break
        holder["open"] = pos is not None
        yield ("time", t)
        eod = not is_mcx and (dt.time() >= SQUARE_OFF or t == last_of_day[d])
        bar_i = bisect.bisect_right(fts, t) - 1

        # ---- manage open leg ----
        if pos is not None:
            ob, lot, p0 = pos["opt"], pos["lot"], pos["p0"]
            i = ob.pos.get(t)
            if i is not None and not (not is_mcx and dt.time() >= SQUARE_OFF):
                stop = p0 - MAX_LOSS / lot
                if pol.lock_after_rs is not None and pos["peak"] >= pol.lock_after_rs:
                    stop = max(stop, p0 + pol.lock_keep * pos["peak"] / lot)
                if pol.lock2_after_rs is not None and pos["peak"] >= pol.lock2_after_rs:
                    stop = max(stop, p0 + pol.lock2_keep * pos["peak"] / lot)
                target = p0 + pol.target_rs / lot if pol.target_rs else None
                trend_fail = (pol.trend_exit and t in fset and bar_i >= 1 and fts[bar_i - 1] >= pos["t0"] - 300
                              and not valid[bar_i - 1])
                if ob.l[i] <= stop:
                    locked = stop > p0 - MAX_LOSS / lot
                    close(t, min(stop, ob.o[i]), "PROFIT_LOCK_STOP" if locked else "MAX_LOSS_HIT")
                    pos = None
                elif target is not None and ob.h[i] >= target:
                    close(t, max(target, ob.o[i]), "TARGET_BOOKED")
                    if pol.reenter:
                        rearm = {"ref_u": m.h[k], "contract": pos["contract"], "ob": ob, "lot": lot,
                                 "name": pos["name"], "leg": pos["leg"] + 1, "day": d}
                    pos = None
                elif trend_fail:
                    close(t, ob.o[i], "TREND_EXIT")
                    pos = None
                else:
                    pos["peak"] = max(pos["peak"], (ob.h[i] - p0) * lot)
            if pos is not None and eod:
                sq = int(datetime.combine(d, SQUARE_OFF, IST).timestamp())
                j = ob.pos.get(sq)
                close(sq, ob.o[j] if j is not None else ob.c[ob.at_or_before(sq)], "DAILY_SQUARE_OFF")
                pos = None
            elif pos is not None and is_mcx and dt.weekday() == 4 and dt.time() >= MCX_FRIDAY_SQUARE_OFF:
                j = ob.pos.get(t)
                close(t, ob.o[j] if j is not None else ob.c[ob.at_or_before(t)], "FRIDAY_SQUARE_OFF")
                pos = None

        if d < WINDOW_FROM or pos is not None or eod:
            continue
        if is_mcx and dt.weekday() == 4 and dt.time() >= MCX_FRIDAY_SQUARE_OFF:
            continue

        # ---- momentum re-entry of the same contract ----
        if rearm is not None:
            if rearm["day"] != d or (not is_mcx and dt.time() >= REENTRY_CUTOFF):
                rearm = None
            elif (m.h[k] > rearm["ref_u"] and bar_i >= 1 and valid[bar_i - 1]
                  and datetime.fromtimestamp(fts[bar_i - 1], IST).date() == d):
                ob = rearm["ob"]
                i = ob.pos.get(t)
                holder["open"] = False
                if i is not None and not (yield ("entry", t)):
                    skips["capacity_full"] += 1
                    rearm = None
                elif i is not None:
                    fill_u = max(rearm["ref_u"], m.o[k])
                    p0 = max(ob.c[i] + DELTA * (fill_u - m.c[k]), 0.05)
                    pos = open_pos(t, ob, p0, rearm["lot"], d, rearm["name"], rearm["contract"], rearm["leg"])
                    rearm = None
                    continue

        # ---- fresh signal entry (identical to the Hold-Long backtest) ----
        if pol.entry_cutoff and not is_mcx and dt.time() >= pol.entry_cutoff:
            continue
        if bar_i < 1 or datetime.fromtimestamp(fts[bar_i - 1], IST).date() != d:
            continue
        p = snap[bar_i - 1]
        if p is None or p[0] != "BULLISH" or m.h[k] < p[1] or fts[bar_i - 1] in consumed:
            continue
        consumed.add(fts[bar_i - 1])
        fill_u = max(p[1], m.o[k])
        contract = resolve_contract(sym, d, fill_u, listed, schedule, "CE")
        if isinstance(contract, str):
            skips[contract] += 1
            continue
        ob = opt_for(contract, d)
        i = ob.at_or_before(t) if ob else None
        if i is None or datetime.fromtimestamp(ob.ts[i], IST).date() != d:
            skips["option_price_missing"] += 1
            continue
        p0 = max(ob.c[i] + DELTA * (fill_u - m.c[k]), 0.05)
        if not is_mcx and p0 < bcfg.MIN_ATM_PREMIUM_RS:
            skips["premium_below_5"] += 1
            continue
        holder["open"] = False
        if not (yield ("entry", t)):
            skips["capacity_full"] += 1
            continue
        expiry, strike, _sid, lot, _code = contract
        rearm = None
        pos = open_pos(t, ob, p0, lot, d, f"{sym} {expiry:%d %b} {strike:g} CE", contract, 1)

    if pos is not None:
        j = pos["opt"].at_or_before(m.ts[-1])
        close(pos["opt"].ts[j], pos["opt"].c[j], "OPEN_AT_WINDOW_END")
    holder["open"] = False


def run_with_capacity(loaded: dict, pol: Policy, max_concurrent: int, skips) -> list[dict]:
    """All symbols stepped minute by minute on one clock, at most
    `max_concurrent` legs open across the whole watchlist. Within a minute,
    every symbol's exits are processed before any entry is granted (a leg
    closing frees its slot for the same minute); simultaneous entry requests
    are granted in watchlist order. A signal that finds every slot taken is
    skipped, not queued."""
    trades: list[dict] = []
    gens, holders, nxt = {}, {}, {}
    for sym, ((fast, master), listed, schedule) in loaded.items():
        holders[sym] = {"open": False}
        gens[sym] = simulate_gen(sym, fast, master, listed, schedule, pol, skips, trades, holders[sym])
        nxt[sym] = next(gens[sym], None)

    def advance(sym, value):
        try:
            nxt[sym] = gens[sym].send(value)
        except StopIteration:
            nxt[sym] = None

    while any(v is not None for v in nxt.values()):
        t = min(v[1] for v in nxt.values() if v is not None)
        requests = []
        for sym in loaded:
            if nxt[sym] is not None and nxt[sym][0] == "time" and nxt[sym][1] == t:
                advance(sym, True)
                if nxt[sym] is not None and nxt[sym][0] == "entry":
                    requests.append(sym)
        open_now = sum(1 for h in holders.values() if h["open"])
        for sym in requests:
            ok = open_now < max_concurrent
            open_now += ok
            advance(sym, ok)
    return trades


def main():
    wf.authenticate()
    loaded = {}
    for sym in hl.WATCHLIST:
        u = hl.load_underlying(sym)
        if u is None:
            continue
        listed = hl.listed_options(sym)
        loaded[sym] = (u, listed, hl.expiry_schedule(sym, listed))

    all_trades, report = [], {}
    for pol in POLICIES:
        skips = defaultdict(int)
        tr = []
        for sym, ((fast, master), listed, schedule) in loaded.items():
            tr += simulate(sym, fast, master, listed, schedule, pol, skips)
        all_trades += tr
        first = [t for t in tr if t["day"] < HALF_SPLIT]
        second = [t for t in tr if t["day"] >= HALF_SPLIT]
        reasons = defaultdict(lambda: [0, 0.0])
        daily = defaultdict(float)
        for t in tr:
            reasons[t["reason"]][0] += 1
            reasons[t["reason"]][1] += t["pnl_modeled"]
            daily[t["day"]] += t["pnl_modeled"]
        report[pol.name] = {"overall": summarize(tr), "first_half": summarize(first)["net"] if first else 0.0,
                            "second_half": summarize(second)["net"] if second else 0.0,
                            "reentry_legs": sum(1 for t in tr if t["leg"] > 1),
                            "by_reason": {k: [n, round(p)] for k, (n, p) in reasons.items()},
                            "daily": {k: round(v) for k, v in sorted(daily.items())},
                            "skips": dict(skips)}
        o = report[pol.name]["overall"]
        print(f"{pol.name:<42} n={o['n']:>4} win={o['win_pct']:>5}% net={o['net']:>10,.0f} "
              f"raw={o['net_raw']:>10,.0f} dd={o['max_dd']:>9,.0f} green={o['green_days']}/{o['days']} "
              f"H1={report[pol.name]['first_half']:>9,.0f} H2={report[pol.name]['second_half']:>9,.0f}", flush=True)

    with open(OUT / "exit_variants_trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
        w.writeheader()
        w.writerows(all_trades)
    (OUT / "exit_variants_summary.json").write_text(json.dumps(report, indent=2))


def main_capacity(max_concurrent: int) -> None:
    """User request (29 Sep 2026): the best rules (G') and the baseline (A),
    NSE only (COPPER/NATURALGAS excluded), with and without a portfolio-wide
    limit of `max_concurrent` open legs."""
    wf.authenticate()
    loaded = {}
    for sym in hl.WATCHLIST:
        if sym in MCX_MULTIPLIER:
            continue
        u = hl.load_underlying(sym)
        if u is None:
            continue
        listed = hl.listed_options(sym)
        loaded[sym] = (u, listed, hl.expiry_schedule(sym, listed))
    picks = [pol for pol in POLICIES if pol.name.startswith(("A ", "G' BE after +1.5k"))]
    report, all_trades = {}, []
    for pol in picks:
        for cap in (None, max_concurrent):
            skips = defaultdict(int)
            if cap is None:
                tr = []
                for sym, ((fast, master), listed, schedule) in loaded.items():
                    tr += simulate(sym, fast, master, listed, schedule, pol, skips)
            else:
                tr = run_with_capacity(loaded, pol, cap, skips)
            label = f"{pol.name} | max_concurrent={cap or 'none'}"
            for t in tr:
                t["run"] = label
            all_trades += tr
            daily = defaultdict(float)
            for t in tr:
                daily[t["day"]] += t["pnl_modeled"]
            reasons = defaultdict(lambda: [0, 0.0])
            for t in tr:
                reasons[t["reason"]][0] += 1
                reasons[t["reason"]][1] += t["pnl_modeled"]
            report[label] = {"overall": summarize(tr),
                             "first_half": round(sum(v for d, v in daily.items() if d < HALF_SPLIT), 2),
                             "second_half": round(sum(v for d, v in daily.items() if d >= HALF_SPLIT), 2),
                             "by_reason": {k: [n, round(p)] for k, (n, p) in reasons.items()},
                             "daily": {k: round(v) for k, v in sorted(daily.items())}, "skips": dict(skips)}
            print(f"{label}: {report[label]['overall']} H1={report[label]['first_half']:,.0f} "
                  f"H2={report[label]['second_half']:,.0f} skips={dict(skips)}", flush=True)
    with open(OUT / f"capacity{max_concurrent}_trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
        w.writeheader()
        w.writerows(all_trades)
    (OUT / f"capacity{max_concurrent}_summary.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "--max-concurrent":
        main_capacity(int(sys.argv[2]))
    else:
        main()
