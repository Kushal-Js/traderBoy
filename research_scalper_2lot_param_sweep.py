"""
Scalper BANKNIFTY, 2 lots, variant B (sell one lot at the profit exit, ride one) - limits re-tuned for the
2-lot split plus a few entry/exit ideas (1 Oct 2026, user: "re-backtest with new limits for two lots ... max
profit limit and the target and the trailing stop loss limit should also be adjusted. Just try a couple of
combos ... Also add any of your own interpretation ... which you feel could yield better results").

Same data, signals, prices and cost estimate as research_scalper_banknifty_2lot_variants.py (1 Sep - 1 Oct,
qty 60, option 1-min OHLC exits, est. real = Dhan charges + 1 point slippage per fill). All limits in POINTS
of the option (x 60 = rupees on 2 lots, x 30 per lot):
  ml       max loss on the 2 lots (both lots out), daily stop = 2 x ml
  arm      peak profit (points) above which profit protection is armed
  gb       profit-protection giveback, % off the best price -> sells lot 1 (and both lots in variant A)
  tgt      target % -> sells lot 1 (and both in A); None = no target
  runner   how lot 2 exits after the split: st = Supertrend reversal only (the user's B); be = + a stop at the
           entry price; trailN = + N% off its own best price
Own ideas (each tried on its own and together):
  reentry  fresh = live since 6a66544 (side freed by a fresh 1-min Supertrend formation); regime = the old
           Swing rule (side freed only when the EMA200 regime flips) - 85 of 116 trades ended on a Supertrend
           reversal in the first test, most of them whipsaws
  confirm  1 = enter only if the NEXT 1-min candle also closes on the signal side (entry one minute later)
  stx      tick = live Supertrend exit (index crossing the last closed candle's line); close = only on a 1-min
           candle CLOSE beyond the line
  start    no entries before this time (the first minutes after the open are the noisiest)
Everything here is chosen on these same 22 days - in-sample; read the halves and the trade counts.

    .venv/bin/python research_scalper_2lot_param_sweep.py      (cache only; signals it cannot price are listed)
"""
from __future__ import annotations

import bisect
import itertools
from datetime import date, datetime, time as dtime
from pathlib import Path

import research_scalper_banknifty_2lot_variants as V
import research_swing_index_1min_vs_5min as S

IST, QTY, LOT = V.IST, V.QTY, V.LOT
OUTFILE = Path("research_results/2026-10-01_scalper_2lot_param_sweep.txt")
MISSING: set[str] = set()


def simulate(spot: dict, sig: dict, above: list, p: dict) -> list[dict]:
    close_at = {t + 60: k for k, t in enumerate(spot["ts"])}
    ml_rs = p["ml"] * QTY
    daily = 2 * ml_rs
    trades, pos, consumed, consumed_k, last_k, pending = [], None, None, None, None, None
    cooldown_until = 0
    realised: dict[date, float] = {}

    def close_leg(qty, t_out, px, reason):
        pos["legs"].append({"qty": qty, "t_out": t_out, "exit": px, "reason": reason,
                            "pnl": (px - pos["p0"]) * qty, "pnl_modeled": V.R.mod(pos["p0"], px, qty)})
        realised[pos["day"]] = realised.get(pos["day"], 0.0) + (px - pos["p0"]) * qty
        pos["open"] -= qty

    for i, t in enumerate(spot["ts"]):
        now = t + 60
        day = datetime.fromtimestamp(t, IST).date()
        if not (V.FIRST_DAY <= day <= V.LAST_DAY):
            continue
        hhmm = datetime.fromtimestamp(now, IST).time()
        k_closed = close_at.get(now)
        if k_closed is not None:
            last_k = k_closed
            reg = sig["regime"][last_k]
            if consumed is not None and reg is not None and (1 if reg else -1) != consumed:
                consumed = None
            elif consumed is not None and p["reentry"] == "fresh" and last_k > consumed_k and above[last_k] is not None:
                st_side = 1 if above[last_k] else -1
                fresh_cross = above[last_k - 1] is not None and (
                    (consumed == 1 and not above[last_k - 1] and above[last_k])
                    or (consumed == -1 and above[last_k - 1] and not above[last_k]))
                if st_side != consumed or fresh_cross:
                    consumed = None
        if pos is not None and pos["day"] != day:
            close_leg(pos["open"], pos["last_t"], pos["last_px"], "DAY_END_NO_DATA")
        if pos is not None and pos["open"]:
            j = V.bar_at(pos["opt"], t)
            c = pos["last_px"]
            if j >= 0:
                o, h, l, c = (pos["opt"][x][j] for x in ("o", "h", "l", "c"))
                pos["last_t"], pos["last_px"] = now, c
                p0 = pos["p0"]
                if pos["open"] == QTY:
                    best0 = pos["best"]
                    reason = px = None
                    if l <= p0 - p["ml"]:
                        reason, px = "MAX_LOSS_HIT", min(o, p0 - p["ml"])
                    elif best0 - p0 > p["arm"] and l <= best0 * (1 - p["gb"]):
                        reason, px = "PROFIT_PROTECTION_HIT", min(o, best0 * (1 - p["gb"]))
                    elif l <= p0 * (1 - V.HARD_STOP_PCT):
                        reason, px = "STOP_LOSS_HIT", min(o, p0 * (1 - V.HARD_STOP_PCT))
                    else:
                        pos["best"] = max(best0, h)
                        if p["tgt"] is not None and h >= p0 * (1 + p["tgt"]):
                            reason, px = "TARGET_HIT", max(o, p0 * (1 + p["tgt"]))
                    if reason in ("PROFIT_PROTECTION_HIT", "TARGET_HIT") and p["mode"] == "split":
                        close_leg(LOT, now, px, reason)
                        pos["rbest"] = max(pos["best"], px)
                    elif reason:
                        close_leg(pos["open"], now, px, reason)
                elif pos["open"]:                                   # the riding lot
                    rb = pos["rbest"]
                    if p["runner"] == "be" and l <= p0:
                        close_leg(pos["open"], now, min(o, p0), "RUNNER_BREAKEVEN")
                    elif p["runner"].startswith("trail") and l <= rb * (1 - int(p["runner"][5:]) / 100):
                        close_leg(pos["open"], now, min(o, rb * (1 - int(p["runner"][5:]) / 100)), "RUNNER_TRAIL")
                    else:
                        pos["rbest"] = max(rb, h)
            if pos["open"] and last_k is not None and last_k > pos["k_in"] and sig["st"][last_k] is not None:
                line = sig["st"][last_k]
                if p["stx"] == "tick":
                    hit = (pos["side"] == 1 and spot["l"][i] < line) or (pos["side"] == -1 and spot["h"][i] > line)
                else:
                    hit = k_closed is not None and above[k_closed] is not None and above[k_closed] != (pos["side"] == 1)
                if hit:
                    close_leg(pos["open"], now, c, "SUPERTREND_REVERSAL" if pos["open"] == QTY else "RUNNER_SUPERTREND")
                    if pos["legs"][0]["pnl"] < 0 and p.get("cool"):
                        cooldown_until = now + 60 * p["cool"]
            if pos["open"] and hhmm >= V.SQUARE_OFF:
                close_leg(pos["open"], now, c, "DAILY_SQUARE_OFF" if pos["open"] == QTY else "RUNNER_SQUARE_OFF")
        if pos is not None and not pos["open"]:
            trades.append(pos)
            pos = None
        if k_closed is None or pos is not None or hhmm >= V.SQUARE_OFF or realised.get(day, 0.0) <= -daily \
                or now < cooldown_until or hhmm < p["start"]:
            if k_closed is not None and pos is None:
                pending = None
            continue
        k = k_closed
        side = 0
        if pending is not None:
            ps, pk = pending
            pending = None
            if k == pk + 1 and above[k] is not None and above[k] == (ps == 1):
                side = ps
        sig_side = 1 if sig["bull"][k] else -1 if sig["bear"][k] else 0
        if not side and sig_side and (consumed is None or consumed != sig_side):
            if p["confirm"]:
                pending = (sig_side, k)
            else:
                side = sig_side
        if side and (consumed is None or consumed != side):
            got = V.option_for(day, side, t, spot["c"][i])
            j = V.bar_at(got[1], t) if got else -1
            if j < 0 and got:
                jj = bisect.bisect_right(got[1]["ts"], t) - 1
                j = jj if jj >= 0 and t - got[1]["ts"][jj] <= 120 else -1
            if j < 0:
                MISSING.add(f"{datetime.fromtimestamp(now, IST):%d %b %H:%M} {'CE' if side == 1 else 'PE'}")
            else:
                pos = {"contract": got[0], "side": side, "day": day, "t_in": now, "k_in": k, "p0": got[1]["c"][j],
                       "best": got[1]["c"][j], "rbest": None, "opt": got[1], "open": QTY, "legs": [],
                       "last_t": now, "last_px": got[1]["c"][j]}
                consumed, consumed_k = side, k
    if pos is not None:
        close_leg(pos["open"], pos["last_t"], pos["last_px"], "OPEN_AT_DATA_END")
        trades.append(pos)
    for tr in trades:
        tr["pnl"] = sum(lg["pnl"] for lg in tr["legs"])
        tr["pnl_modeled"] = sum(lg["pnl_modeled"] for lg in tr["legs"])
        tr["real"] = tr["pnl"] - V.real_cost(tr)
    return trades


def summary(trades: list[dict]) -> dict:
    by_day: dict[date, float] = {}
    for t in trades:
        by_day[t["day"]] = by_day.get(t["day"], 0.0) + t["real"]
    eq = peak = dd = 0.0
    for d in sorted(by_day):
        eq += by_day[d]; peak = max(peak, eq); dd = min(dd, eq - peak)
    h1 = sum(t["real"] for t in trades if t["day"] <= date(2026, 9, 15))
    real = sum(t["real"] for t in trades)
    return {"n": len(trades), "raw": sum(t["pnl"] for t in trades), "real": real, "dd": dd, "h1": h1, "h2": real - h1,
            "mod": sum(t["pnl_modeled"] for t in trades), "wins": sum(1 for t in trades if t["real"] > 0),
            "splits": sum(1 for t in trades if len(t["legs"]) > 1),
            "pos_days": sum(1 for v in by_day.values() if v > 0), "days": len(by_day)}


def label(p: dict) -> str:
    tgt = "none" if p["tgt"] is None else f"{int(p['tgt'] * 100)}%"
    s = (f"{'A' if p['mode'] == 'all' else 'B'} ml {p['ml']}pt  arm {p['arm']}pt  gb {p['gb'] * 100:.0f}%  tgt {tgt:>4s}  "
         f"run {p['runner'] if p['mode'] == 'split' else '-':7s} | {p['reentry']:6s} conf {p['confirm']} stx {p['stx']:5s} "
         f"start {p['start']:%H:%M}")
    return s + (f" cool {p['cool']}m" if p.get("cool") else "")


def main() -> None:
    lines: list[str] = []
    say = lambda s="": (print(s), lines.append(s))
    spot = V.load_spot()
    sig = S.signals(spot, S.resample(spot, 15), 1)
    above = [None if sig["st"][k] is None else spot["c"][k] > sig["st"][k] for k in range(len(spot["ts"]))]
    base = {"mode": "split", "ml": 50, "arm": 100, "gb": 0.02, "tgt": 0.35, "runner": "st", "reentry": "fresh",
            "confirm": 0, "stx": "tick", "start": dtime(9, 15), "cool": 0}
    runs: list[tuple[dict, dict]] = []

    def run(**kw):
        p = {**base, **kw}
        s = summary(simulate(spot, sig, above, p))
        runs.append((p, s))
        return s

    run(mode="all")                                            # reference rows: the first test's A and B
    run()
    # 1. limits for the 2-lot split (live entries, live re-entry rule)
    for ml, arm, gb, tgt, runner in itertools.product((40, 50, 75), (60, 100, 150), (0.02, 0.04),
                                                      (0.25, 0.35, None), ("st", "be", "trail10", "trail20")):
        run(ml=ml, arm=arm, gb=gb, tgt=tgt, runner=runner)
    grid1 = runs[2:]
    # 2. own ideas on the live limits and on the best limits of step 1
    best1 = max(grid1, key=lambda r: r[1]["real"])[0]
    ideas = [{"reentry": "regime"}, {"confirm": 1}, {"stx": "close"}, {"start": dtime(9, 30)}, {"cool": 5},
             {"reentry": "regime", "confirm": 1}, {"reentry": "regime", "stx": "close"}, {"confirm": 1, "stx": "close"},
             {"reentry": "regime", "confirm": 1, "stx": "close"}, {"reentry": "regime", "start": dtime(9, 30)},
             {"reentry": "regime", "confirm": 1, "stx": "close", "start": dtime(9, 30)}]
    lim_keys = ("ml", "arm", "gb", "tgt", "runner")
    for src in (base, best1):
        for idea in ideas:
            run(**{k: src[k] for k in lim_keys}, **idea)
            run(mode="all", **{k: src[k] for k in lim_keys}, **idea)
    head = (f"{'combination':118s} {'trades':>6s} {'wins':>4s} {'split':>5s} | {'raw':>8s} {'est. real':>9s} "
            f"{'1-15 Sep':>9s} {'16 Sep-1 Oct':>12s} {'max DD':>8s} {'+days':>6s} | {'paper':>8s}")
    row = lambda p, s: (f"{label(p):118s} {s['n']:>6d} {s['wins']:>4d} {s['splits']:>5d} | {s['raw']:>+8,.0f} "
                        f"{s['real']:>+9,.0f} {s['h1']:>+9,.0f} {s['h2']:>+12,.0f} {s['dd']:>+8,.0f} "
                        f"{s['pos_days']:>3d}/{s['days']:<2d} | {s['mod']:>+8,.0f}")
    say(f"SCALPER BANKNIFTY 2 lots (qty {QTY}) - parameter sweep, {V.FIRST_DAY:%d %b} .. {V.LAST_DAY:%d %b %Y}; "
        f"{len(runs)} runs; est. real = raw - Dhan charges - 1 pt slippage per fill; limits in option POINTS "
        f"(x60 = Rs on 2 lots)")
    say(f"signals that could not be priced in any run: {len(MISSING)}")
    say()
    say("REFERENCE (first test: live limits doubled, live re-entry rule)")
    say(head)
    say(row(*runs[0])); say(row(*runs[1]))
    say()
    say("1. LIMITS for the split (B), live entries - top 15 by est. real, then the worst 3")
    say(head)
    ranked = sorted(grid1, key=lambda r: -r[1]["real"])
    for p, s in ranked[:15]:
        say(row(p, s))
    say("  ...")
    for p, s in ranked[-3:]:
        say(row(p, s))
    say()
    say("   average est. real over the grid by one setting (all other settings mixed):")
    for key, vals in (("ml", (40, 50, 75)), ("arm", (60, 100, 150)), ("gb", (0.02, 0.04)), ("tgt", (0.25, 0.35, None)),
                      ("runner", ("st", "be", "trail10", "trail20"))):
        parts = []
        for v in vals:
            xs = [s["real"] for p, s in grid1 if p[key] == v]
            parts.append(f"{v}: {sum(xs) / len(xs):+,.0f}")
        say(f"   {key:7s} " + "   ".join(parts))
    say()
    say("2. OWN IDEAS (entries/exits) - on the live limits and on the best limits of step 1, B and A")
    say(head)
    for p, s in sorted(runs[2 + len(grid1):], key=lambda r: -r[1]["real"]):
        say(row(p, s))
    say()
    say("3. EDGE CHECK - is the best of step 1 a peak or a trend? (ml 75, no target, runner st where B)")
    say(head)
    for mode in ("all", "split"):
        for re_, stx in (("fresh", "tick"), ("fresh", "close"), ("regime", "close")):
            for arm in (100, 150, 200, 250):
                for gb in (0.04, 0.06):
                    p = {**base, "mode": mode, "ml": 75, "tgt": None, "reentry": re_, "stx": stx, "arm": arm, "gb": gb}
                    say(row(p, summary(simulate(spot, sig, above, p))))
    if MISSING:
        say()
        say("Signals without option data (skipped where they occurred): " + ", ".join(sorted(MISSING)))
    OUTFILE.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
