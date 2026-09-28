"""
Research simulator for the Bollinger strategy's entry/exit design (28 Sep
2026, after the lookahead-bias finding in walkforward_selector_eval.py).
Runs on already-cached data only (CACHE_ONLY) - never calls Dhan - so it
is safe during market hours.

Axes, each switchable independently:
  entry_mode  lookahead   - old backtests: enter inside the firing bar
                            knowing its full high/low (NOT achievable live)
              bar_close   - current live engine: act only after the 5-min
                            bar closes
              resting     - a real resting stop order: the pending trigger
                            known at the END of bar i-1 fires the moment a
                            1-min bar in bar i crosses it (achievable live
                            via the tick feed; no knowledge of bar i needed)
  stop_basis  premium     - current live: stop % applied to option premium
              underlying  - the video's intent: stop/trailing measured on
                            the underlying's own price, exit priced from
                            the option at that moment
  hold        swing       - current live: carry overnight, Friday square-off
              intraday    - flat by the last bar of every day

Option-tier P&L uses real cached 1-min option closes; an intra-minute fill
(trigger or stop touched inside a minute) is priced as that minute's option
close adjusted by delta*(fill underlying - minute close underlying). Every
exit is worsened by the same inverse-to-premium slippage model as the other
Bollinger backtests.

Anti-overfitting protocol: variants are compared on the SYNTHETIC tier
before 28 Aug (design window). The chosen variant is then reported on the
held-out window (28 Aug onward) in both tiers.
"""
from __future__ import annotations

import bisect
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import walkforward_selector_eval as wf
from walkforward_selector_eval import IST, bcfg, bt

ROOT = wf.REPO_ROOT / "history" / "walkforward_dev"
SPLIT_DATE = date(2026, 8, 28)
DELTA = 0.5
BAR = wf.INTERVAL * 60


@dataclass(frozen=True)
class Variant:
    name: str
    entry_mode: str = "bar_close"
    stop_basis: str = "premium"
    hold: str = "swing"
    min_stop_pct: float = bcfg.MIN_STOP_PCT
    trail_frac: float = bcfg.TRAILING_STOP_FRACTION
    step_frac: float = bcfg.TRAILING_STEP_FRACTION
    entry_slippage: bool = False


class Series:
    def __init__(self, d: dict):
        self.ts = d["timestamps"]
        self.close = d["closes"]
        self.high = d.get("highs") or d["closes"]
        self.low = d.get("lows") or d["closes"]
        self.open = d.get("opens") or d["closes"]

    def at(self, t):
        i = bisect.bisect_right(self.ts, t) - 1
        return self.close[i] if i >= 0 else None


def fires_with_snapshots(fast: dict, signals: dict):
    """wf.compute_fires, plus the pending order as it stood at the END of
    every bar (what a resting order during the NEXT bar would be)."""
    highs, lows, closes, ts = fast["highs"], fast["lows"], fast["closes"], fast["timestamps"]
    vb, vr = signals["valid_bullish"], signals["valid_bearish"]
    sh, sl = signals["swing_high"], signals["swing_low"]
    L, M = bcfg.SWING_FRACTAL_LOOKBACK, bcfg.MIN_PULLBACK_CANDLES
    pending = None
    ah = al = None
    ext = None
    fires, snap = [], [None] * len(closes)
    for i in range(len(closes)):
        if i >= L:
            j = i - L
            if sh[j] and vb[j]:
                ah = (j, highs[j])
            if sl[j] and vr[j]:
                al = (j, lows[j])
            if not vb[i]:
                ah = None
            if not vr[i]:
                al = None
            if pending is not None and not (vb[i] if pending[0] == "BULLISH" else vr[i]):
                pending, ext = None, None
            if vb[i] and ah is not None and i > ah[0]:
                streak = 0
                for k in range(ah[0] + 1, i + 1):
                    streak = streak + 1 if closes[k] < closes[k - 1] else 0
                if streak >= M:
                    if pending is None or pending[0] != "BULLISH":
                        pending, ext = ["BULLISH", ah[1], lows[i]], lows[i]
                    else:
                        pending[1] = max(pending[1], ah[1])
                        ext = min(ext, lows[i])
                        pending[2] = ext
            if vr[i] and al is not None and i > al[0]:
                streak = 0
                for k in range(al[0] + 1, i + 1):
                    streak = streak + 1 if closes[k] > closes[k - 1] else 0
                if streak >= M:
                    if pending is None or pending[0] != "BEARISH":
                        pending, ext = ["BEARISH", al[1], highs[i]], highs[i]
                    else:
                        pending[1] = min(pending[1], al[1])
                        ext = max(ext, highs[i])
                        pending[2] = ext
            if pending is not None and ((pending[0] == "BULLISH" and highs[i] >= pending[1]) or
                                        (pending[0] == "BEARISH" and lows[i] <= pending[1])):
                fires.append({"idx": i, "bar_start": ts[i], "side": pending[0], "trigger": pending[1],
                              "stop": pending[2], "bar_close": closes[i]})
                if pending[0] == "BULLISH":
                    ah = None
                else:
                    al = None
                pending, ext = None, None
        snap[i] = tuple(pending) if pending else None
    return fires, snap


def load_symbol(sym: str) -> dict | None:
    d = ROOT / "intraday" / sym.lower()
    try:
        fast = json.loads((d / f"{sym}_{wf.INTERVAL}min_90d.json").read_text())
        master = json.loads((d / f"{sym}_1min_90d.json").read_text())
        daily = wf.prepare_daily(json.loads((ROOT / "daily" / f"{sym}.json").read_text()))
    except FileNotFoundError:
        return None
    signals = bt.compute_signals(fast)
    fires, snap = fires_with_snapshots(fast, signals)
    return {"sym": sym, "fast": fast, "m": Series(master), "daily": daily, "fires": fires, "snap": snap,
            "opt_dir": d}


_front = None


def front_for(sym):
    global _front
    if _front is None:
        _front = wf.build_option_index()
    return _front.get(sym)


def simulate(ctx: dict, v: Variant, tier: str, day_from: date, day_to: date, stats: dict) -> list[dict]:
    sym, m, fast = ctx["sym"], ctx["m"], ctx["fast"]
    fts = fast["timestamps"]
    front = front_for(sym) if tier == "option" else None
    if tier == "option" and front is None:
        return []
    last_of_day = {}
    for t in m.ts:
        last_of_day[datetime.fromtimestamp(t, tz=IST).date()] = t
    fire_by_bar = {f["bar_start"]: f for f in ctx["fires"]}
    opt_cache: dict[str, Series | None] = {}
    trades, pos = [], None

    def opt_series(sid):
        if sid not in opt_cache:
            p = ctx["opt_dir"] / f"OPT_{sid}_1min.json"
            opt_cache[sid] = Series(json.loads(p.read_text())) if p.exists() else None
        return opt_cache[sid]

    def premium(t, und_close, und_fill):
        """Option price at minute t, adjusted to an intra-minute underlying fill level."""
        sign = 1 if pos["side"] == "BULLISH" else -1
        if tier == "option":
            px = pos["opt"].at(t)
            if px is None:
                return None
            return max(px + sign * DELTA * (und_fill - und_close), 0.05)
        r = (und_fill / pos["spot0"] - 1.0) * sign
        return max(pos["p0"] * (1.0 + pos["lev"] * r), 0.05)

    def close_pos(t, prem, reason):
        slip = bt.slippage_pct_for(prem)
        filled = prem * (1 - slip)
        pnl = (filled - pos["p0"]) * pos["qty"]
        trades.append({"sym": sym, "side": pos["side"], "entry_ts": pos["t0"], "exit_ts": t,
                       "day": pos["d0"].isoformat(), "p0": pos["p0"], "exit": filled, "pnl": pnl, "reason": reason})

    for k, t in enumerate(m.ts):
        dt = datetime.fromtimestamp(t, tz=IST)
        d = dt.date()
        if d > day_to:
            break
        hi, lo, cl, op = m.high[k], m.low[k], m.close[k], m.open[k]

        # ---- manage open position ----
        if pos is not None:
            bull = pos["side"] == "BULLISH"
            reason, fill = None, cl
            if v.stop_basis == "underlying":
                fav = hi if bull else lo
                pos["best_u"] = max(pos["best_u"], fav) if bull else min(pos["best_u"], fav)
                move = (pos["best_u"] - pos["spot0"]) if bull else (pos["spot0"] - pos["best_u"])
                if not pos["armed"] and move >= pos["trail_dist"]:
                    pos["armed"] = True
                    pos["trail"] = pos["best_u"] - pos["trail_dist"] if bull else pos["best_u"] + pos["trail_dist"]
                elif pos["armed"]:
                    cand = pos["best_u"] - pos["trail_dist"] if bull else pos["best_u"] + pos["trail_dist"]
                    if (cand - pos["trail"] if bull else pos["trail"] - cand) >= pos["step"]:
                        pos["trail"] = cand
                level = pos["trail"] if pos["armed"] else pos["hard"]
                if (bull and lo <= level) or (not bull and hi >= level):
                    reason = "TRAILING_STOP_HIT" if pos["armed"] else "STOP_LOSS_HIT"
                    fill = min(level, op) if bull else max(level, op)
                prem = premium(t, cl, fill)
                if prem is not None and reason is None and (pos["p0"] - prem) * pos["qty"] >= bcfg.MAX_LOSS_PROTECTION_RS:
                    reason = "MAX_LOSS_HIT"
            else:
                prem = premium(t, cl, cl)
                if prem is not None:
                    pos["best_p"] = max(pos["best_p"], prem)
                    if (pos["p0"] - prem) * pos["qty"] >= bcfg.MAX_LOSS_PROTECTION_RS:
                        reason = "MAX_LOSS_HIT"
                    else:
                        if not pos["armed"] and pos["best_p"] - pos["p0"] >= pos["trail_dist"]:
                            pos["armed"], pos["trail"] = True, pos["best_p"] - pos["trail_dist"]
                        elif pos["armed"] and pos["best_p"] - pos["trail_dist"] - pos["trail"] >= pos["step"]:
                            pos["trail"] = pos["best_p"] - pos["trail_dist"]
                        if prem <= (pos["trail"] if pos["armed"] else pos["hard"]):
                            reason = "TRAILING_STOP_HIT" if pos["armed"] else "STOP_LOSS_HIT"
            if prem is not None and reason is None:
                eod = t == last_of_day[d]
                if v.hold == "intraday" and (eod or dt.time() >= wf.FRIDAY_SQUARE_OFF_TIME):
                    reason = "EOD_SQUARE_OFF"
                elif dt.weekday() == 4 and (dt.time() >= wf.FRIDAY_SQUARE_OFF_TIME or eod):
                    reason = "FRIDAY_SQUARE_OFF"
            if reason and prem is not None:
                close_pos(t, prem, reason)
                pos = None

        if d < day_from:
            continue
        bar_i = bisect.bisect_right(fts, t) - 1
        if bar_i < 1:
            continue
        cand = None
        if v.entry_mode == "lookahead":
            f = fire_by_bar.get(fts[bar_i])
            if f:
                cand, fill_u, ref = f, cl, cl
        elif v.entry_mode == "bar_close":
            f = fire_by_bar.get(fts[bar_i - 1])
            if f and f["bar_start"] + BAR <= t < f["bar_start"] + 2 * BAR:
                cand, fill_u, ref = f, cl, f["bar_close"]
        else:  # resting stop order from the pending state at the end of the previous bar
            p = ctx["snap"][bar_i - 1]
            if p is not None and datetime.fromtimestamp(fts[bar_i - 1], tz=IST).date() == d:
                side_, trig, stop = p
                if (side_ == "BULLISH" and hi >= trig) or (side_ == "BEARISH" and lo <= trig):
                    fill_u = max(trig, op) if side_ == "BULLISH" else min(trig, op)
                    cand, ref = {"side": side_, "trigger": trig, "stop": stop, "bar_start": fts[bar_i]}, fill_u
        if cand is None:
            continue
        used = ctx.setdefault("_used", set())
        key = (v.name, tier, cand["bar_start"])
        if key in used:
            continue
        used.add(key)
        if pos is not None or datetime.fromtimestamp(cand["bar_start"], tz=IST).date() != d:
            continue
        if dt.time() >= wf.FRIDAY_SQUARE_OFF_TIME or t == last_of_day[d]:
            continue

        side = cand["side"]
        stop_pct = max(abs(cand["trigger"] - cand["stop"]) / ref if ref else 0.0, v.min_stop_pct)
        pos = {"side": side, "t0": t, "d0": d, "spot0": fill_u, "armed": False, "trail": None}
        if tier == "option":
            try:
                opt = bt.nearest_for_strike_ref(front[0], "CE" if side == "BULLISH" else "PE", fill_u)
            except Exception:  # noqa: BLE001
                pos = None
                continue
            ser = opt_series(opt["security_id"])
            if ser is None:
                stats["uncached"] += 1
                pos = None
                continue
            pos["opt"], pos["qty"] = ser, opt["lot_size"] * bcfg.QUANTITY_LOTS
            p0 = premium(t, cl, fill_u)
            if p0 is None:
                pos = None
                continue
        else:
            sigma = wf.vol_from_closes(wf.daily_slice(ctx["daily"], d - timedelta(days=1))["close"])
            if not sigma:
                pos = None
                continue
            p0 = wf.atm_premium_estimate(fill_u, sigma, wf.days_to_monthly_expiry(d))
            fr = front_for(sym)
            pos["qty"] = (int(float(fr[0]["SEM_LOT_UNITS"].iloc[0])) if fr else 1) * bcfg.QUANTITY_LOTS
            pos["lev"] = DELTA * fill_u / p0
        if v.entry_slippage:
            p0 = p0 * (1 + bt.slippage_pct_for(p0))
        pos["p0"] = p0
        stats["entries"] += 1
        if v.stop_basis == "underlying":
            dist = fill_u * stop_pct
            pos["hard"] = fill_u - dist if side == "BULLISH" else fill_u + dist
            pos["best_u"] = fill_u
            pos["trail_dist"] = dist * v.trail_frac
            pos["step"] = pos["trail_dist"] * v.step_frac
        else:
            pos["hard"] = p0 * (1 - stop_pct)
            pos["best_p"] = p0
            pos["trail_dist"] = p0 * stop_pct * v.trail_frac
            pos["step"] = pos["trail_dist"] * v.step_frac
    return trades


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "total": 0.0, "win": 0.0, "per": 0.0, "avg_w": 0.0, "avg_l": 0.0, "hold": 0.0, "dd": 0.0}
    w = [t["pnl"] for t in trades if t["pnl"] > 0]
    lo = [t["pnl"] for t in trades if t["pnl"] <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    daily = defaultdict(float)
    for t in trades:
        daily[t["day"]] += t["pnl"]
    for d in sorted(daily):
        eq += daily[d]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    total = sum(t["pnl"] for t in trades)
    return {"n": len(trades), "total": total, "win": len(w) / len(trades) * 100, "per": total / len(trades),
            "avg_w": statistics.mean(w) if w else 0.0, "avg_l": statistics.mean(lo) if lo else 0.0,
            "hold": statistics.median((t["exit_ts"] - t["entry_ts"]) / 60 for t in trades), "dd": dd}


def run(variants: list[Variant], symbols: list[str]):
    ctxs = [c for c in (load_symbol(s) for s in symbols) if c]
    first = min(datetime.fromtimestamp(c["fast"]["timestamps"][0], tz=IST).date() for c in ctxs) + timedelta(days=7)
    last = max(datetime.fromtimestamp(c["m"].ts[-1], tz=IST).date() for c in ctxs)
    out = {}
    for v in variants:
        row = {}
        for label, tier, a, b in (("design (synthetic, <28 Aug)", "synthetic", first, SPLIT_DATE - timedelta(days=1)),
                                  ("holdout (synthetic, >=28 Aug)", "synthetic", SPLIT_DATE, last),
                                  ("holdout (real options, >=28 Aug)", "option", SPLIT_DATE, last)):
            stats = {"entries": 0, "uncached": 0}
            tr = []
            for c in ctxs:
                c.pop("_used", None)
                tr += simulate(c, v, tier, a, b, stats)
            row[label] = {**summarize(tr), **stats, "trades": tr}
        out[v.name] = row
    return out, first, last


def print_table(out: dict, window: str):
    print(f"\n--- {window} ---")
    print(f"{'variant':<44}{'trades':>7}{'win%':>7}{'net Rs':>12}{'Rs/trade':>10}{'avg win':>9}{'avg loss':>10}{'hold':>6}{'max DD':>10}")
    for name, row in out.items():
        r = row[window]
        extra = f"  ({r['uncached']} skipped: option not cached)" if r.get("uncached") else ""
        print(f"{name:<44}{r['n']:>7}{r['win']:>6.1f}%{r['total']:>12,.0f}{r['per']:>10,.0f}{r['avg_w']:>9,.0f}"
              f"{r['avg_l']:>10,.0f}{r['hold']:>5.0f}m{r['dd']:>10,.0f}{extra}")


WATCHLIST = ["ZYDUSLIFE", "SONACOMS", "DIVISLAB", "AUROPHARMA", "MOTHERSON", "APOLLOHOSP", "APLAPOLLO", "MCX",
             "BOSCHLTD", "LAURUSLABS", "OBEROIRLTY", "RBLBANK", "RADICO", "PHOENIXLTD", "MOTILALOFS"]

if __name__ == "__main__":
    wf.authenticate()  # instrument master only (lot sizes / strikes); no market-data calls below
    variants = [
        Variant("0 old backtest (lookahead, premium stop)", "lookahead", "premium"),
        Variant("1 live today (bar close, premium stop)", "bar_close", "premium"),
        Variant("2 resting order, premium stop", "resting", "premium"),
        Variant("3 bar close, underlying stop", "bar_close", "underlying"),
        Variant("4 resting order, underlying stop", "resting", "underlying"),
        Variant("5 resting, underlying stop, intraday", "resting", "underlying", "intraday"),
        Variant("6 bar close, underlying stop, intraday", "bar_close", "underlying", "intraday"),
    ]
    out, first, last = run(variants, WATCHLIST)
    print(f"Data {first} .. {last}; split at {SPLIT_DATE}")
    for w in ("design (synthetic, <28 Aug)", "holdout (synthetic, >=28 Aug)", "holdout (real options, >=28 Aug)"):
        print_table(out, w)
    (ROOT / "research_round1.json").write_text(json.dumps(
        {k: {w: {kk: vv for kk, vv in r.items() if kk != "trades"} for w, r in row.items()} for k, row in out.items()},
        indent=2, default=str))
