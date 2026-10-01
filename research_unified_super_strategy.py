"""
UNIFIED "super strategy" research (1 Oct 2026, user: "create a single unified strategy which uses the best from all
the strategies (swing momentum and Super Bollinger and other) ... captures momentum and pull back based momentum
also having hedged based setup and it can select stock also based on performances ... handle choppy market days
also gracefully (consider full 1.2 lac amount)").

Window: 3 Aug - 29 Sep 2026 (41 trading days). Universe: Super Bollinger's HYBRID weekly picks (liquidity -> ATH ->
the strategy's own recent fit -> top 15), picked walk-forward (no hindsight) - 47 stocks over the window. Real option
prices (Dhan rolling data for expired contracts, the listed contract from 29 Sep), modelled slippage on every leg.

Two signal engines, both rules fixed BEFORE this test from earlier evidence:
  A  PULLBACK MOMENTUM = Super Bollinger as live: Bollinger/Vortex pullback, resting BULLISH buy-stop at the swing high,
     last 1-hour candle green, ATM CALL, max loss 4,500, breakeven after +1,500, entries until 14:00, out 15:15.
     Add-ons: the live PUT hedge (call down 1,800 and the stock 1 ATR below entry -> 1 ATM PUT, 30% trail from +1,000,
     1,500 stop); optional S1 (re-add a call back at the trigger) + S2 (2nd PUT lot on Supertrend-bearish, sell one at
     combined +4,000) - research_super_bollinger_* (designed on Aug-Sep: in-sample).
  B  TREND MOMENTUM = SwingMomentum, made intraday: Swing v3 entry on closed 5-min candles (CALL or PUT), only when
     Swing/regime.classify says MOMENTUM (2 h efficiency ratio >= 0.25 and today's >= 0.15; thresholds chosen on
     Sep 2026 -> AUGUST IS OUT-OF-SAMPLE for them), NSE volume floor, fresh-formation re-entry, Swing's options exit
     ladder (max loss 4,500 / profit protection over 3,000, 2% giveback / target +35% / -20% / Supertrend reversal),
     entries until 14:30, out 15:15 (no overnight gap risk; cash comes back every day). No hedge (it lost on Swing).
     Option = the same ATM / expiry-roll policy as A (Super Bollinger's pricer).
PORTFOLIO (the new layer - this is what the grid tests): Rs 1,20,000 cash for premium (every leg incl. hedges paid
from it, returned at exit; profits are not added), one position per stock across both engines, a slot limit, A first
when both fire at once, option premium >= Rs 10 (cheap high-lot options lose their edge to slippage - MOTHERSON on
Swing), one leg <= Rs 45,000, optional daily loss stop (no new entries once the day's realised loss reaches it),
optional market chop gate (NIFTY's own 2 h efficiency ratio < 0.15 -> no new entries).

Validation: every config is reported for AUGUST and SEPTEMBER separately; the portfolio config is picked on August
and judged on September (and vice versa). Two months is a small sample - read the numbers as evidence, not a promise.

Run after 15:30 IST (missing option prices are fetched):
    HANDOFF_DHAN_ACCESS_TOKEN=<token> .venv/bin/python research_unified_super_strategy.py
Without a token it runs on the cache only (unpriced signals are skipped and counted).
"""
from __future__ import annotations

import bisect
import contextlib
import heapq
import io
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime
from pathlib import Path

HAVE_TOKEN = bool(os.environ.get("HANDOFF_DHAN_ACCESS_TOKEN"))
_argv = sys.argv[:]
sys.argv = [sys.argv[0], "aug"] + ([] if HAVE_TOKEN else ["--cache-only"])
with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_bearish_side as m          # option pricer (fetching only with a token)
sys.argv = _argv
with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_chop_filter_resim as rsim  # walk-forward picks, Super Bollinger engine + overlays
import research_swing_index_1min_vs_5min as S                 # Swing v3 signal code (replayed vs live: 0 mismatches)
from Swing import regime

rs, u = rsim.rs, rsim.u
IST = m.IST
OUT = Path("research_results/2026-10-01_unified_super_strategy.txt")
CACHE = Path("history/bt_unified_super_strategy")
SPLIT = date(2026, 9, 1)
BUDGET = 120_000
MIN_PREMIUM, MAX_LEG = 10.0, 45_000
B_CUTOFF, SQUARE_OFF = dtime(14, 30), dtime(15, 15)
B_MAX_LOSS, B_TARGET, B_HARD, B_PP, B_GIVEBACK, VOL_MIN = 4500.0, 0.35, 0.20, 3000.0, 0.02, 0.6
UNPRICED = {"A": 0, "B": 0}


def ts_of(d: date, t: dtime) -> int:
    return int(datetime.combine(d, t, IST).timestamp())


# --------------------------------------------------------------------------- #
# Engine A - Super Bollinger as live, every touch (no slot limit: the portfolio decides)
# --------------------------------------------------------------------------- #
def engine_a() -> list[dict]:
    with contextlib.redirect_stdout(io.StringIO()):
        trades, _st = u.simulate(rsim.syms, 99, m.pr, "UNIFIED_A", "long", rsim.allowed,
                                 symbol_gate=rsim.candle_gate(60))
    out = []
    for t in trades:
        with contextlib.redirect_stdout(io.StringIO()):
            ev = rs.prep(t)
            h, hi = rs.put_side(ev, False, None)
            s2, s2i = rs.put_side(ev, True, 4000)
            s1, s1i = rs.call_side(ev, confirm=True, lot2_exit="with", arm=0, pair_cap=False, reentry=False)
        if ev.get("tm") is None:
            UNPRICED["A"] += 1
        out.append({"engine": "A", "sym": t["symbol"], "day": date.fromisoformat(t["day"]), "t0": ev["t0"],
                    "t1": ev["t1"], "p0": ev["p0"], "qty": ev["qty"], "pnl": float(t["pnl_modeled"]),
                    "exit": float(t["exit"]), "contract": t["contract"], "pnl_raw": float(t["pnl_raw"]),
                    "reason": t["reason"], "side": "CALL", "hedge": h, "hi": hi, "s2": s2, "s2i": s2i, "s1": s1,
                    "s1i": s1i})
    return out


# --------------------------------------------------------------------------- #
# Engine B - SwingMomentum, intraday, every signal per stock (no slot limit)
# --------------------------------------------------------------------------- #
def resample(s: dict, minutes: int) -> dict:
    out = {"ts": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    key = None
    for t, o, h, l, c, v in zip(s["ts"], s["o"], s["h"], s["l"], s["c"], s["v"]):
        dt = datetime.fromtimestamp(t, IST)
        mins = dt.hour * 60 + dt.minute - (9 * 60 + 15)
        if mins < 0:
            continue
        k = (dt.date(), mins // minutes)
        if k != key:
            key = k
            out["ts"].append(ts_of(dt.date(), dtime(9, 15)) + (mins // minutes) * minutes * 60)
            for f, x in (("o", o), ("h", h), ("l", l), ("c", c), ("v", v)):
                out[f].append(x)
        else:
            out["h"][-1] = max(out["h"][-1], h)
            out["l"][-1] = min(out["l"][-1], l)
            out["c"][-1] = c
            out["v"][-1] += v
    return out


def engine_b(sym: str, allowed_days: set, gate: bool = True) -> list[dict]:
    j = json.loads(Path(f"history/bt_walkforward_long/underlying_1m/{sym}_1min.json").read_text())
    spot = {"ts": [int(t) for t in j["timestamps"]], "o": j["opens"], "h": j["highs"], "l": j["lows"],
            "c": j["closes"], "v": j.get("volumes") or [0] * len(j["timestamps"])}
    fast, slow = resample(spot, 5), resample(spot, 15)
    if len(fast["ts"]) < 260:
        return []
    sig = S.signals(fast, slow, 5)
    close_t = [t + 300 for t in fast["ts"]]
    b5 = {"timestamp": fast["ts"], "open": fast["o"], "high": fast["h"], "low": fast["l"], "close": fast["c"]}
    b15 = {"timestamp": slow["ts"], "open": slow["o"], "high": slow["h"], "low": slow["l"], "close": slow["c"]}
    sidx = {t: i for i, t in enumerate(spot["ts"])}
    lot = m.pr.lot(sym)
    trades, consumed, consumed_k, busy_until = [], None, None, 0
    for k in range(1, len(fast["ts"])):
        c_t = close_t[k]
        dt = datetime.fromtimestamp(c_t, IST)
        day = dt.date()
        # re-entry state (live 33916b1): regime other side / Supertrend other side after the entry candle / fresh cross
        if consumed is not None:
            reg, line, pline = sig["regime"][k], sig["st"][k], sig["st"][k - 1]
            st_side = None if line is None else (1 if fast["c"][k] > line else -1)
            prev_side = None if pline is None else (1 if fast["c"][k - 1] > pline else -1)
            if (reg is not None and (1 if reg else -1) != consumed) or (k > consumed_k and (
                    (st_side is not None and st_side != consumed) or (st_side == consumed and prev_side == -consumed))):
                consumed = None
        if day not in allowed_days or c_t < busy_until or dt.time() > B_CUTOFF:
            continue
        side = 1 if sig["bull"][k] else -1 if sig["bear"][k] else 0
        if not side or side == consumed:
            continue
        if k >= 20:
            avg = sum(fast["v"][k - 20:k]) / 20
            if avg and fast["v"][k] / avg < VOL_MIN:
                continue
        if gate and not regime.classify(b5, b15, dt).allows_entry:
            continue
        sq = ts_of(day, SQUARE_OFF)
        i_sp = bisect.bisect_right(spot["ts"], c_t - 60) - 1
        spot0 = spot["c"][i_sp] if i_sp >= 0 else fast["c"][k]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                legx = m.pr.open_leg(sym, day, "LONG" if side == 1 else "SHORT", c_t, sq, spot0)
        except Exception:  # noqa: BLE001
            legx = None
        mins = legx["mins"] if legx else None
        contract = (f"{sym} {legx['expiry']:%d %b} {float(legx['strike']):g} {'CALL' if side == 1 else 'PUT'}"
                    if legx else None)
        if mins is None or not lot:
            UNPRICED["B"] += 1
            continue
        jj = bisect.bisect_right(mins.ts, c_t - 60) - 1
        if jj < 0 or c_t - 60 - mins.ts[jj] > 300:
            UNPRICED["B"] += 1
            continue
        p0 = mins.c[jj]
        if p0 <= 0:
            continue
        best, reason, px, t_exit = p0, None, None, None
        stop = max(p0 - B_MAX_LOSS / lot, p0 * (1 - B_HARD))
        for i in range(jj + 1, len(mins.ts)):
            t = mins.ts[i]
            if t < c_t:
                continue
            o, h, l, c = mins.o[i], mins.h[i], mins.l[i], mins.c[i]
            if t >= sq:
                reason, px, t_exit = "SQUARE_OFF_15:15", o, t
                break
            if l <= stop:
                reason, px, t_exit = ("MAX_LOSS_HIT" if stop == p0 - B_MAX_LOSS / lot else "STOP_LOSS_HIT"), min(o, stop), t
                break
            if (best - p0) * lot > B_PP and l <= best * (1 - B_GIVEBACK):
                reason, px, t_exit = "PROFIT_PROTECTION_HIT", min(o, best * (1 - B_GIVEBACK)), t
                break
            if h >= p0 * (1 + B_TARGET):
                reason, px, t_exit = "TARGET_HIT", max(o, p0 * (1 + B_TARGET)), t
                break
            best = max(best, h)
            if (best - p0) * lot > B_PP and c <= best * (1 - B_GIVEBACK):
                reason, px, t_exit = "PROFIT_PROTECTION_HIT", c, t
                break
            kl = bisect.bisect_right(close_t, t) - 1                  # last 5-min candle closed by this minute
            si = sidx.get(t)
            if kl > k and si is not None and sig["st"][kl] is not None and (
                    (side == 1 and spot["l"][si] < sig["st"][kl]) or (side == -1 and spot["h"][si] > sig["st"][kl])):
                reason, px, t_exit = "SUPERTREND_REVERSAL_TICK", c, t
                break
        if reason is None:
            reason, px, t_exit = "DATA_END", mins.c[-1], mins.ts[-1]
        trades.append({"engine": "B", "sym": sym, "day": day, "t0": c_t, "t1": t_exit + 60, "p0": p0, "qty": lot,
                       "contract": contract,
                       "side": "CALL" if side == 1 else "PUT", "reason": reason, "exit": px,
                       "pnl": m.mod(p0, px, lot), "pnl_raw": (px - p0) * lot})
        consumed, consumed_k, busy_until = side, k, t_exit + 60
    return trades


# --------------------------------------------------------------------------- #
# Market chop gate - NIFTY's own 2 h efficiency ratio
# --------------------------------------------------------------------------- #
def nifty_er() -> dict:
    j = json.loads(Path("history/bt_walkforward_long/underlying_1m/NIFTY_1min.json").read_text())
    s = {"ts": [int(t) for t in j["timestamps"]], "o": j["opens"], "h": j["highs"], "l": j["lows"], "c": j["closes"],
         "v": [0] * len(j["timestamps"])}
    f = resample(s, 5)
    out = {}
    for k in range(24, len(f["ts"])):
        path = sum(abs(f["c"][i] - f["c"][i - 1]) for i in range(k - 23, k + 1))
        out[f["ts"][k] + 300] = abs(f["c"][k] - f["c"][k - 24]) / path if path else 0.0
        NIFTY_DIR[f["ts"][k] + 300] = 1 if f["c"][k] > f["c"][k - 24] else -1 if f["c"][k] < f["c"][k - 24] else 0
    return out


NIFTY_DIR: dict[int, int] = {}                                     # candle close -> NIFTY 2 h direction (+1/-1/0)


# --------------------------------------------------------------------------- #
# Portfolio - one account, both engines, in time order
# --------------------------------------------------------------------------- #
def portfolio(cands: list[dict], cfg: dict, ner: dict) -> dict:
    """cfg: engines ("AB"/"A"/"B"), slots, addons ("none"/"hedge"/"s1s2"), daily_stop (Rs or None),
    market_gate (bool), budget, min_premium, max_leg."""
    ner_t = sorted(ner)
    opens = []
    for i, x in enumerate(cands):
        if x["engine"] not in cfg["engines"]:
            continue
        if x["engine"] == "B" and cfg.get("b_sides") and x["side"] not in cfg["b_sides"]:
            continue
        opens.append((x["t0"], 0 if x["engine"] == "A" else 1, 0, "main", i))
        if x["engine"] == "A" and cfg["addons"] != "none" and x["hi"]:
            opens.append((x["hi"]["a_t"], 0, 1, "A", i))
        if x["engine"] == "A" and cfg["addons"] == "s1s2":
            if x["s2i"].get("added"):
                opens.append((x["s2i"]["b_t"], 0, 2, "B", i))
            if x["s1i"].get("readd"):
                opens.append((x["s1i"]["c_t"], 0, 3, "C", i))
    opens.sort()
    cash, held, took = cfg["budget"], {}, defaultdict(set)
    holding, realized, n = {}, defaultdict(float), defaultdict(int)
    holding_engine: dict[str, str] = {}                          # symbol -> engine holding it
    closes = []                                                    # (t, pnl, day) realised when the main leg closes

    def settle(t):
        nonlocal cash
        for key in [k for k, v in held.items() if v[0] <= t]:
            cash += held.pop(key)[1]
        while closes and closes[0][0] <= t:
            _t, p, d = heapq.heappop(closes)
            realized[d] += p
        for s in [s for s, until in holding.items() if until <= t]:
            del holding[s]
            holding_engine.pop(s, None)

    for t, _pri, _o, kind, i in opens:
        settle(t)
        x = cands[i]
        cost_main = x["p0"] * x["qty"]
        if kind == "main":
            d = x["day"]
            if x["sym"] in holding:
                n["skip_stock_held"] += 1
                continue
            if len(holding) >= cfg["slots"]:
                n["skip_slots"] += 1
                continue
            cap_e = cfg.get(f"slots_{x['engine'].lower()}")
            if cap_e is not None and sum(1 for e in holding_engine.values() if e == x["engine"]) >= cap_e:
                n[f"skip_slots_{x['engine']}"] += 1
                continue
            if x["p0"] < cfg["min_premium"] or cost_main > cfg["max_leg"]:
                n["skip_premium"] += 1
                continue
            if cfg["daily_stop"] is not None and realized[d] <= -cfg["daily_stop"]:
                n["skip_daily_stop"] += 1
                continue
            if cfg.get("a_follow") or cfg.get("b_follow"):
                j = bisect.bisect_right(ner_t, t) - 1
                mdir = NIFTY_DIR.get(ner_t[j], 0) if j >= 0 else 0
                if x["engine"] == "A" and cfg.get("a_follow") and mdir < 0:
                    n["skip_a_market_falling"] += 1
                    continue
                if x["engine"] == "B" and cfg.get("b_follow") and mdir != (1 if x["side"] == "CALL" else -1):
                    n["skip_b_against_market"] += 1
                    continue
            if cfg["market_gate"]:
                j = bisect.bisect_right(ner_t, t) - 1
                if j >= 0 and ner[ner_t[j]] < cfg.get("gate_th", 0.15):
                    n["skip_market_chop"] += 1
                    continue
            if cost_main > cash:
                n["skip_cash"] += 1
                continue
            cash -= cost_main
            took[i].add("main")
            n[f"took_{x['engine']}"] += 1
            end = max(x["t1"], t + 1)
            held[(i, "main")] = [end, cost_main]
            holding[x["sym"]] = end
            holding_engine[x["sym"]] = x["engine"]
            heapq.heappush(closes, (end, x["pnl"], d))          # main leg's P&L counts for the daily stop at its exit
            continue
        if "main" not in took[i] or (kind == "B" and "A" not in took[i]):
            continue
        qty = x["qty"]
        cost, exit_t = {"A": (x["hi"]["a_q"] * qty, x["hi"]["a_exit_t"]),
                        "B": (x["s2i"].get("b_q", 0) * qty, x["s2i"].get("b_exit_t", 0)),
                        "C": (x["s1i"].get("c_q", 0) * qty, x["s1i"].get("c_exit_t", 0))}[kind]
        if cost > cash:
            n[f"skip_cash_{kind}"] += 1
            continue
        cash -= cost
        took[i].add(kind)
        held[(i, kind)] = [max(exit_t, t + 1), cost]
        if kind == "B":
            held[(i, "A")][0] = max(x["s2i"]["a_exit_t"], t + 1)
    trades, day = [], defaultdict(float)
    for i, x in enumerate(cands):
        if "main" not in took[i]:
            continue
        p = x["pnl"]
        put_leg = (x["s2"] if "B" in took[i] else x["hedge"]) if "A" in took[i] else 0.0
        s1_leg = x["s1"] if "C" in took[i] else 0.0
        p += put_leg + s1_leg
        day[x["day"]] += p
        legs = "+".join(k for k in ("main", "A", "B", "C") if k in took[i]).replace("A", "hedge").replace(
            "B", "2nd PUT").replace("C", "S1 call")
        trades.append({**{k: v for k, v in x.items() if k not in ("hi", "s2i", "s1i", "hedge", "s1", "s2")},
                       "put_side_pnl": put_leg, "s1_pnl": s1_leg, "legs": legs, "total": p})
    return {"day": day, "trades": trades, "n": n}


def _dd(vals: list) -> float:
    eq = pk = dd = 0.0
    for v in vals:
        eq += v
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return dd


def stats(r: dict, days: list) -> dict:
    eq = sum(r["day"].get(d, 0.0) for d in days)
    dd = _dd([r["day"].get(d, 0.0) for d in days])
    dd_aug = _dd([r["day"].get(d, 0.0) for d in days if d < SPLIT])
    dd_sep = _dd([r["day"].get(d, 0.0) for d in days if d >= SPLIT])
    aug = sum(v for d, v in r["day"].items() if d < SPLIT)
    vals = [r["day"].get(d, 0.0) for d in days]
    return {"total": eq, "aug": aug, "sep": eq - aug, "dd": dd, "dd_aug": dd_aug, "dd_sep": dd_sep, "worst": min(vals), "best": max(vals),
            "win_days": sum(1 for v in vals if v > 0), "trades": len(r["trades"]),
            "won": sum(1 for t in r["trades"] if t["total"] > 0),
            "A": sum(1 for t in r["trades"] if t["engine"] == "A"), "B": sum(1 for t in r["trades"] if t["engine"] == "B")}


# --------------------------------------------------------------------------- #
def main() -> None:
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    days = sorted(rsim.allowed)
    by_sym_days = defaultdict(set)
    for d, syms in rsim.allowed.items():
        for s in syms:
            by_sym_days[s].add(d)
    print("engine A ...", flush=True)
    cand_a = engine_a()
    print(f"  {len(cand_a)} A candidates; engine B ...", flush=True)
    cand_b, cand_b_raw = [], []
    for i, sym in enumerate(sorted(by_sym_days)):
        cand_b += engine_b(sym, by_sym_days[sym], gate=True)
        cand_b_raw += engine_b(sym, by_sym_days[sym], gate=False)
        print(f"  B {i + 1}/{len(by_sym_days)} {sym}: {len(cand_b)} gated / {len(cand_b_raw)} ungated so far, "
              f"option calls {m.pr.calls}", flush=True)
    ner = nifty_er()
    cands = sorted(cand_a + cand_b, key=lambda x: (x["t0"], x["engine"]))
    base = {"budget": BUDGET, "min_premium": MIN_PREMIUM, "max_leg": MAX_LEG, "market_gate": False, "daily_stop": None}

    def run(name, cfg, pool=None):
        r = portfolio(pool or cands, {**base, **cfg}, ner)
        return name, cfg, r, stats(r, days)

    say("UNIFIED SUPER STRATEGY - 3 Aug - 29 Sep 2026, Rs 1,20,000 account, walk-forward HYBRID picks (47 stocks), "
        "modelled P&L after slippage")
    say(f"Candidates: engine A (pullback, Super Bollinger) {len(cand_a)}, engine B (trend momentum, SwingMomentum "
        f"intraday) {len(cand_b)} (without the momentum gate: {len(cand_b_raw)}). Unpriced signals skipped: {UNPRICED}. "
        f"Option data calls this run: {m.pr.calls}")
    rows = []
    rows.append(run("Super Bollinger as live (A, 2 slots, hedge, premium >= 5)",
                    {"engines": "A", "slots": 2, "addons": "hedge", "min_premium": 5.0, "max_leg": 1e9}))
    rows.append(run("Super Bollinger, 4 slots, hedge", {"engines": "A", "slots": 4, "addons": "hedge"}))
    rows.append(run("Super Bollinger, 4 slots, hedge + S1 + S2", {"engines": "A", "slots": 4, "addons": "s1s2"}))
    rows.append(run("SwingMomentum intraday alone (B, 5 slots)", {"engines": "B", "slots": 5, "addons": "none"}))
    rows.append(run("Swing (no momentum gate) intraday (B raw, 5 slots)", {"engines": "B", "slots": 5, "addons": "none"},
                    pool=sorted(cand_a + cand_b_raw, key=lambda x: (x["t0"], x["engine"]))))
    grid = []
    for slots in (3, 4, 5):
        for addons in ("hedge", "s1s2"):
            for stop in (None, 6000):
                for mg in (False, True):
                    name = (f"UNIFIED A+B {slots} slots, {'hedge+S1+S2' if addons == 's1s2' else 'hedge'}"
                            f"{', daily stop 6k' if stop else ''}{', market chop gate' if mg else ''}")
                    grid.append(run(name, {"engines": "AB", "slots": slots, "addons": addons, "daily_stop": stop,
                                           "market_gate": mg}))
    say("")
    hdr = (f"{'setup':62s} {'total':>9s} {'Aug':>8s} {'Sep':>8s} {'max dd':>8s} {'worst':>8s} {'win days':>8s} "
           f"{'trades':>6s} {'A/B':>7s} {'won%':>5s}")
    say(hdr)
    for name, _cfg, _r, s in rows + grid:
        say(f"{name:62s} {s['total']:>+9,.0f} {s['aug']:>+8,.0f} {s['sep']:>+8,.0f} {s['dd']:>+8,.0f} {s['worst']:>+8,.0f} "
            f"{s['win_days']:>3d}/{len(days):<4d} {s['trades']:>6d} {s['A']:>3d}/{s['B']:<3d} "
            f"{100 * s['won'] / max(1, s['trades']):>4.0f}%")
    # out-of-sample pick: best by August -> its September, and best by September -> its August
    by_aug = max(grid, key=lambda g: g[3]["aug"] / max(1.0, -g[3]["dd_aug"]) if g[3]["aug"] > 0 else g[3]["aug"])
    by_sep = max(grid, key=lambda g: g[3]["sep"] / max(1.0, -g[3]["dd_sep"]) if g[3]["sep"] > 0 else g[3]["sep"])
    say("")
    say(f"Picked on AUGUST (best Aug profit per unit of drawdown): {by_aug[0]} -> Aug {by_aug[3]['aug']:+,.0f}, "
        f"SEPTEMBER (unseen) {by_aug[3]['sep']:+,.0f}")
    say(f"Picked on SEPTEMBER: {by_sep[0]} -> Sep {by_sep[3]['sep']:+,.0f}, AUGUST (unseen) {by_sep[3]['aug']:+,.0f}")
    pos_both = [g for g in grid if g[3]["aug"] > 0 and g[3]["sep"] > 0]
    say(f"Unified configs positive in BOTH months: {len(pos_both)}/{len(grid)}")
    # day-wise for the August-picked config vs the baselines
    say("")
    say("Day-wise (Rs, modelled): August-picked unified config vs Super Bollinger as live vs SwingMomentum alone")
    say(f"{'day':10s} {'unified':>9s} {'SB live':>9s} {'SwingMom':>9s}")
    u_r, sb_r, b_r = by_aug[2], rows[0][2], rows[3][2]
    for d in days:
        say(f"{d.isoformat():10s} {u_r['day'].get(d, 0):>+9,.0f} {sb_r['day'].get(d, 0):>+9,.0f} {b_r['day'].get(d, 0):>+9,.0f}")
    say("")
    say(f"Skip counters (August-picked config): {dict(by_aug[2]['n'])}")
    ex = defaultdict(lambda: [0, 0.0])
    for t in by_aug[2]["trades"]:
        ex[(t["engine"], t["reason"])][0] += 1
        ex[(t["engine"], t["reason"])][1] += t["total"]
    say("Exits (August-picked config): " + "; ".join(f"{e} {r}: {n} ({p:+,.0f})" for (e, r), (n, p) in sorted(ex.items())))
    OUT.write_text("\n".join(lines) + "\n")
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "candidates.json").write_text(json.dumps(
        [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in x.items() if k not in ("hi", "s2i", "s1i")}
         for x in cands], default=str))


if __name__ == "__main__":
    main()
