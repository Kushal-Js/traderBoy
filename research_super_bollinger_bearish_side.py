"""
Super Bollinger's BEARISH side + the mirrored supervisor (30 Sep 2026, user
idea: "with this hedging based approach ... we can trade the other direction
too - buy PEs and hedge them with CEs").

Walk-forward HYBRID picks (weekly, past data only), Super Bollinger rules
unchanged (G': Rs 4,500 max loss, breakeven once +Rs 1,500, no entries from
14:00, 15:15 square-off, max 5 open). Three books:
  long  - CE on BULLISH triggers (the deployed strategy),
  short - ATM PE on BEARISH triggers only,
  both  - both sides sharing the same 5 slots.
On every trade (either side), mirrored rules from the live supervisor and the
scale-in paper variant:
  HEDGE   trade's open loss >= Rs 2,000 AND the stock >= 1 x ATR(14, 5m)
          against the trade from its entry spot -> 1 lot of the opposite ATM
          option (PE for a CE trade, CE for a PE trade); exit on a 40%
          giveback once +Rs 1,000, Rs 1,500 stop, 15:15. No hedge from 15:00.
  HADD    once hedged, Supertrend(10,3) 5m confirming the hedge's direction
          (bearish for a PE hedge, bullish for a CE hedge) before 14:30 -> a
          2nd hedge lot (own Rs 750 stop); both hedge lots out once trade +
          hedge PnL >= 0, else hedge stop/trail/15:15.
  TADD    trade +Rs 1,500 before 14:00 -> 1 more lot of the trade's option,
          sold when Supertrend turns against the trade on a bar closed after
          the add, else exits with the original.
Earlier evidence (29 Sep, hindsight watchlist, hold-to-close, no hedge): the
PE side lost -Rs 49.7k at Rs 4,500 max loss, negative even before slippage.

Periods: "sep" = 31 Aug - 29 Sep (HYBRID picks from selection_walkforward
.json, 30-day caches); "aug" = 3 Aug - 29 Sep from history/bt_walkforward_
long (needs research_selection_walkforward_aug.py fetch + run first), August
reported separately. Real option prices (OptionPricer); slippage on every leg.

Run: uv run python research_super_bollinger_bearish_side.py sep --cache-only   (market-hours safe: no Dhan calls)
     HANDOFF_DHAN_ACCESS_TOKEN=... uv run python research_super_bollinger_bearish_side.py aug   (after 15:30)
"""
from __future__ import annotations

import bisect
import io
import contextlib
import json
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime
from pathlib import Path

import pandas as pd

import backtest_super_bollinger_universe_30day as u
import backtest_super_trader_30day as st
import research_stock_selection_walkforward as r
from Bollinger.paper_book import modeled_slippage_pct
from SuperTrader.strategy import atr, supertrend
from walkforward_selector_eval import IST

MODE = sys.argv[1] if len(sys.argv) > 1 else "sep"
CACHE_ONLY = "--cache-only" in sys.argv
LONG_DIR = Path("history/bt_walkforward_long")
HEDGE_TRIGGER, HEDGE_ATR, HEDGE_STOP, TRAIL_ARM, GIVEBACK = 2000, 1.0, 1500, 1000, 0.40
ADD_AT, ADD_STOP = 1500, 750
HEDGE_CUTOFF, HADD_CUTOFF, TADD_CUTOFF = dtime(15, 0), dtime(14, 30), dtime(14, 0)

if MODE == "aug":
    r.FIVE, r.OUT, u.ROOT = LONG_DIR / "underlying", LONG_DIR, LONG_DIR
    u.WINDOW_FROM, u.WINDOW_TO = date(2026, 8, 3), date(2026, 9, 29)
    PICKS_FILE, SPLIT, SPLIT_NAMES = LONG_DIR / "selection_walkforward.json", "2026-08-31", ("Aug", "Sep")
else:
    PICKS_FILE, SPLIT, SPLIT_NAMES = Path("history/bt_super_bollinger_universe_30day/selection_walkforward.json"), \
        "2026-09-15", ("H1", "H2")
OUT = PICKS_FILE.parent

if CACHE_ONLY:
    def _no_fetch(*_a, **_k):
        raise RuntimeError("cache-only run - no Dhan calls")
    st._retry = _no_fetch
    st._save = lambda *_a, **_k: None
    _df = pd.read_csv(sorted(Path(".").glob("Dependencies\\all_instrument *.csv"))[-1], low_memory=False)
    st.dhan_wrapper.instruments = lambda: _df
    _orig_listed = st.OptionPricer.listed_contract

    def _listed_safe(self, *a, **k):
        try:
            return _orig_listed(self, *a, **k)
        except RuntimeError:
            return None, None
    st.OptionPricer.listed_contract = _listed_safe
else:
    r.wf.authenticate()
    st.dhan_wrapper.client.Dhan.dhan_http.timeout = 90
u.wf.authenticate = lambda: None

pr = st.OptionPricer()
_feat: dict = {}


def feats(sym):
    if sym not in _feat:
        b = json.loads((u.ROOT / "underlying" / f"{sym}_5min.json").read_text())
        m = json.loads((u.ROOT / "underlying_1m" / f"{sym}_1min.json").read_text())
        _feat[sym] = ({"ts": b["timestamps"], "st": supertrend(b["highs"], b["lows"], b["closes"], 10, 3.0),
                       "atr": atr(b["highs"], b["lows"], b["closes"], 14)}, m)
    return _feat[sym]


def bar_at(f, t):
    k = bisect.bisect_right(f["ts"], t - 300) - 1
    return k if k >= 0 else None


def spot_at(m, t):
    i = bisect.bisect_right(m["timestamps"], t) - 1
    return m["closes"][i] if i >= 0 else None


def mod(p_in, p_out, qty):
    return (p_out * (1 - modeled_slippage_pct(p_out)) - p_in * (1 + modeled_slippage_pct(p_in))) * qty


def hhmm(t):
    return datetime.fromtimestamp(t, IST).time()


def leg(sym, d, side, t, sq, spot):
    try:
        x = pr.open_leg(sym, d, side, t, sq, spot)
    except Exception:  # noqa: BLE001
        return None
    return x["mins"] if x else None


# --------------------------------------------------------------------------- #
# Per-trade overlays
# --------------------------------------------------------------------------- #
def overlays(tr: dict, cov: dict) -> dict:
    """Extra PnL of each overlay on one trade: {"HEDGE": x, "HEDGE+HADD": y, "TADD": z}."""
    sym, d, side = tr["symbol"], date.fromisoformat(tr["day"]), tr["side"]
    sgn = 1 if side == "LONG" else -1          # +1: trade profits when the stock rises
    f, m = feats(sym)
    t0 = int(datetime.combine(d, datetime.strptime(tr["entry_time"], "%H:%M").time(), IST).timestamp())
    t1 = int(datetime.combine(d, datetime.strptime(tr["exit_time"], "%H:%M").time(), IST).timestamp())
    sq = int(datetime.combine(d, dtime(15, 15), IST).timestamp())
    spot0 = spot_at(m, t0)
    tm = leg(sym, d, side, t0, sq, spot0)
    out = {"HEDGE": 0.0, "HEDGE+HADD": 0.0, "TADD": 0.0}
    if tm is None:
        cov["trade_unpriced"] += 1
        return out
    p0, qty, exit_px = float(tr["entry"]), int(tr["qty"]), float(tr["exit"])

    # ---- TADD: add to the trade at +1,500, sell on a Supertrend flip against it ----
    lvl = p0 + ADD_AT / qty
    ia = next((i for i, t in enumerate(tm.ts) if t0 < t < t1 and hhmm(t) < TADD_CUTOFF and tm.h[i] >= lvl), None)
    if ia is not None:
        fill, ta = max(lvl, tm.o[ia]), tm.ts[ia]
        px = exit_px
        for j, t in enumerate(tm.ts):
            if t <= ta:
                continue
            if t >= t1:
                break
            k = bar_at(f, t)
            if k is not None and f["ts"][k] + 300 > ta and f["st"][k] == -sgn:
                px = tm.c[j]
                break
        out["TADD"] = mod(fill, px, qty)
        cov["tadd"] += 1

    # ---- HEDGE: opposite option when the trade is down 2k and the stock moved 1 ATR against it ----
    th = None
    for i, t in enumerate(tm.ts):
        if t <= t0 or t > t1 or hhmm(t) >= HEDGE_CUTOFF or (p0 - tm.l[i]) * qty < HEDGE_TRIGGER:
            continue
        k, s = bar_at(f, t), spot_at(m, t)
        if k is not None and f["atr"][k] is not None and s is not None and sgn * (spot0 - s) >= HEDGE_ATR * f["atr"][k]:
            th = t
            break
    if th is None:
        return out
    cov["hedges"] += 1
    hside = "SHORT" if side == "LONG" else "LONG"
    hm = leg(sym, d, hside, th, sq, spot_at(m, th))
    if hm is None:
        cov["hedge_unpriced"] += 1
        return out
    i0 = bisect.bisect_right(hm.ts, th) - 1
    if i0 < 0 or th - hm.ts[i0] > 300:
        cov["hedge_unpriced"] += 1
        return out
    q0 = hm.c[i0]

    def run(with_add: bool) -> float:
        legs = [{"q": q0, "open": True, "stop": q0 - HEDGE_STOP / qty, "pnl": 0.0}]
        peak, added = 0.0, False
        for j in range(i0 + 1, len(hm.ts)):
            t = hm.ts[j]
            live = [x for x in legs if x["open"]]
            if not live:
                break
            if t >= sq:
                for x in live:
                    x["open"], x["pnl"] = False, mod(x["q"], hm.o[j], qty)
                break
            for x in live:
                if hm.l[j] <= x["stop"]:
                    x["open"], x["pnl"] = False, mod(x["q"], min(x["stop"], hm.o[j]), qty)
            if not legs[0]["open"]:
                for x in legs:
                    if x["open"]:
                        x["open"], x["pnl"] = False, mod(x["q"], hm.c[j], qty)
                break
            peak = max(peak, (hm.h[j] - q0) * qty)
            trail = peak >= TRAIL_ARM and (hm.c[j] - q0) * qty <= peak * (1 - GIVEBACK)
            if with_add and added:
                ci = bisect.bisect_right(tm.ts, t) - 1
                trade_now = (exit_px - p0) * qty if t >= t1 else ((tm.c[ci] - p0) * qty if ci >= 0 else 0.0)
                hedge_now = sum((hm.c[j] - x["q"]) * qty if x["open"] else x["pnl"] for x in legs)
                if trade_now + hedge_now >= 0 or trail:
                    for x in legs:
                        if x["open"]:
                            x["open"], x["pnl"] = False, mod(x["q"], hm.c[j], qty)
                    break
            elif trail:
                for x in legs:
                    if x["open"]:
                        x["open"], x["pnl"] = False, mod(x["q"], hm.c[j], qty)
                break
            if with_add and not added and hhmm(t) < HADD_CUTOFF:
                k = bar_at(f, t)
                if k is not None and f["st"][k] == -sgn:     # Supertrend in the hedge's direction
                    added = True
                    cov["hadd"] += 1
                    legs.append({"q": hm.c[j], "open": True, "stop": hm.c[j] - ADD_STOP / qty, "pnl": 0.0})
        for x in legs:
            if x["open"]:
                x["pnl"] = mod(x["q"], hm.c[-1], qty)
        return sum(x["pnl"] for x in legs)

    out["HEDGE"] = run(False)
    out["HEDGE+HADD"] = run(True)
    return out


# --------------------------------------------------------------------------- #
# Books
# --------------------------------------------------------------------------- #
def summarize(name, trades, ov):
    variants = {"base": lambda t, o: 0.0, "+hedge": lambda t, o: o["HEDGE"],
                "+hedge+hadd": lambda t, o: o["HEDGE+HADD"], "+tadd": lambda t, o: o["TADD"],
                "+hedge+hadd+tadd": lambda t, o: o["HEDGE+HADD"] + o["TADD"]}
    res = {}
    for vname, extra in variants.items():
        daily, by_side = defaultdict(float), defaultdict(float)
        for t, o in zip(trades, ov):
            v = float(t["pnl_modeled"]) + extra(t, o)
            daily[t["day"]] += v
            by_side[t["side"]] += v
        eq = pk = dd = 0.0
        for k in sorted(daily):
            eq += daily[k]
            pk = max(pk, eq)
            dd = min(dd, eq - pk)
        a = sum(v for k, v in daily.items() if k < SPLIT)
        b = sum(v for k, v in daily.items() if k >= SPLIT)
        res[vname] = {"total": round(sum(daily.values())), SPLIT_NAMES[0]: round(a), SPLIT_NAMES[1]: round(b),
                      "CE_side": round(by_side["LONG"]), "PE_side": round(by_side["SHORT"]), "max_dd": round(dd),
                      "worst_day": round(min(daily.values())) if daily else 0,
                      "green_days": f"{sum(v > 0 for v in daily.values())}/{len(daily)}"}
        x = res[vname]
        print(f"  {name:6s} {vname:18s} total {x['total']:>+9,}  {SPLIT_NAMES[0]} {x[SPLIT_NAMES[0]]:>+9,}  "
              f"{SPLIT_NAMES[1]} {x[SPLIT_NAMES[1]]:>+9,}  CE {x['CE_side']:>+9,}  PE {x['PE_side']:>+9,}  "
              f"dd {x['max_dd']:>9,}  worst {x['worst_day']:>+8,}  green {x['green_days']}", flush=True)
    return res


def load_picks(rule: str) -> dict:
    """Walk-forward weekly picks: from the selection run's JSON, else (BEAR_HYBRID
    on the September window) computed here with the same select() code."""
    data = json.loads(PICKS_FILE.read_text())
    if rule in data:
        return {date.fromisoformat(k): set(v) for k, v in data[rule]["picks"].items()}
    extra = OUT / "bear_hybrid_picks.json"
    if rule == "BEAR_HYBRID" and extra.exists() and MODE == "sep":
        return {date.fromisoformat(k): set(v) for k, v in json.loads(extra.read_text()).items()}
    from fno_ath_screener import fetch_fno_universe
    universe = fetch_fno_universe()
    dailies = {p.stem: r.wf.prepare_daily(json.loads(p.read_text())) for p in r.DAILY.glob("*.json")}
    days = r.trading_days()
    return {dd: r.select(rule, dd, dailies, universe, {}, days) for dd in sorted(set(r.weekly_as_of(days).values()))}


def main():
    days = r.trading_days()
    as_of = r.weekly_as_of(days)
    print(f"mode={MODE} cache_only={CACHE_ONLY} window {u.WINDOW_FROM}..{u.WINDOW_TO}", flush=True)
    report, books = {}, {}
    # (book, sides, watchlist rule): the PE book on the bullish-selected HYBRID list (counter-trend) and on
    # its own bearish-selected BEAR_HYBRID list; "both" = CE + PE sharing HYBRID's 5 slots.
    for book, sides, rule in (("long", "long", "HYBRID"), ("short@HYBRID", "short", "HYBRID"),
                              ("short@BEAR", "short", "BEAR_HYBRID"), ("both@HYBRID", "both", "HYBRID")):
        picks = load_picks(rule)
        allowed = {d: picks[a] for d, a in as_of.items() if a in picks}
        syms = sorted(set().union(*picks.values()))
        with contextlib.redirect_stdout(io.StringIO()):
            trades, stats = u.simulate(syms, r.CAP, pr, f"{rule}_{sides}", sides, allowed)
        cov = defaultdict(int)
        with contextlib.redirect_stdout(io.StringIO()):
            ov = [overlays(t, cov) for t in trades]
        books[book] = (trades, ov)
        n_ce = sum(t["side"] == "LONG" for t in trades)
        print(f"\n[{book}] {len(syms)} symbols | trades {len(trades)} (CE {n_ce}, PE {len(trades) - n_ce}) | skipped no "
              f"option data {stats.get('skipped_no_option_data', 0)} | overlay coverage {dict(cov)} | option fetch "
              f"attempts {pr.calls}", flush=True)
        report[book] = {"rule": rule, "sides": sides, "summary": summarize(book[:6], trades, ov), "stats": dict(stats),
                        "coverage": dict(cov),
                        "trades": [{**t, **{k: round(v) for k, v in o.items()}} for t, o in zip(trades, ov)]}
    lt, lo = books["long"]
    bt_, bo = books["short@BEAR"]
    print("\n[long@HYBRID + short@BEAR, separate 5-slot books - funds not shared]", flush=True)
    report["long+short@BEAR"] = {"summary": summarize("L+S", lt + bt_, lo + bo)}
    name = f"bearish_side_{MODE}{'_cacheonly' if CACHE_ONLY else ''}.json"
    (OUT / name).write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwritten {OUT / name}")


if __name__ == "__main__":
    main()
