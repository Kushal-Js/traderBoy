"""
Walk-forward STOCK SELECTION for Super Bollinger (30 Sep 2026, user
request: "work on stock selection first and then run the strategy on a
smaller subset").

Why: Super Bollinger's 15-stock watchlist was picked on 27 Sep using data
that covers most of its own backtest window (hindsight), and the same
rules on all 210 F&O stocks lose money - so the edge depends on WHICH
stocks it trades. This measures selection rules honestly:

  every week, rank the F&O universe using ONLY data up to the previous
  week's last trading day, keep the top TOP_N, and trade just those next
  week with the unchanged Super Bollinger rules (backtest_super_bollinger_
  universe_30day.simulate: real option prices, max 5 open, BULLISH/CE).

Selection rules (fixed before any result was seen):
  TREND_ATH      - the deployed ATH()/trend score (fno_ath_screener), the
                   method family that produced today's watchlist;
  TREND_ATH_GATE - same, only stocks passing the liquidity gates (ATM
                   premium >= Rs 5, 20-day turnover >= Rs 50 cr);
  MOMENTUM       - 20-day return / 20-day volatility (risk-adjusted),
                   positive only, gated;
  STRATEGY_FIT   - how these same rules did on the stock over the prior 20
                   sessions (underlying proxy on 5-min bars: resting
                   BULLISH trigger, -1% stop, breakeven after +0.4%, else
                   15:15), min 3 proxy trades, gated;
  RANDOM_k       - TOP_N random gated stocks each week (3 seeds) - the bar
                   every rule has to clear.
  HYBRID         - (added after the first results) ATH's top 40 among gated
                   stocks = trend-quality pool, ranked by STRATEGY_FIT; filled
                   up to TOP_N in ATH order if fewer than TOP_N have >= 3
                   proxy trades;
  *_DAILY        - the same rule re-ranked every trading day on data up to
                   the previous day, instead of weekly.
Compared against the whole universe (no selection) and the hindsight
watchlist. Reported per week and per half.
"""
from __future__ import annotations

import bisect
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import walkforward_selector_eval as wf
from walkforward_selector_eval import IST, bt
from bollinger_research import fires_with_snapshots
import backtest_super_bollinger_universe_30day as u
import backtest_super_trader_30day as st
from fno_ath_screener import fetch_fno_universe

TOP_N = 15
CAP = 5
DAILY = Path("history/walkforward_20260928/daily")
FIVE = Path("history/bt_super_trader_30day/underlying")
OUT = Path("history/bt_super_bollinger_universe_30day")


def trading_days() -> list[date]:
    n = json.loads((FIVE / "_NIFTY_5min.json").read_text())
    return sorted({datetime.fromtimestamp(t, IST).date() for t in n["timestamps"]})


def weekly_as_of(days: list[date]) -> dict[date, date]:
    """window day -> the last trading day BEFORE that day's week (Mon-Fri)."""
    out = {}
    for d in days:
        if not (u.WINDOW_FROM <= d <= u.WINDOW_TO):
            continue
        monday = d - timedelta(days=d.weekday())
        prior = [x for x in days if x < monday]
        out[d] = prior[-1]
    return out


# --------------------------------------------------------------------------- #
# Selection rules
# --------------------------------------------------------------------------- #
def momentum_scores(dailies: dict, as_of: date) -> dict[str, float]:
    out = {}
    for sym, d in dailies.items():
        sl = wf.daily_slice(d, as_of)
        c = sl["close"]
        if len(c) < 25:
            continue
        vol = wf.vol_from_closes(c)
        ret = c[-1] / c[-21] - 1
        if vol and ret > 0:
            out[sym] = ret / vol
    return out


def fit_proxy(sym: str) -> dict[date, float]:
    """Per-day underlying-proxy PnL (%) of the Super Bollinger rules on 5-min bars."""
    fast = json.loads((FIVE / f"{sym}_5min.json").read_text())
    if not fast.get("closes") or len(fast["closes"]) < 300:
        return {}
    sig = bt.compute_signals(fast)
    _f, snap = fires_with_snapshots(fast, sig)
    ts, o, h, l, c = fast["timestamps"], fast["opens"], fast["highs"], fast["lows"], fast["closes"]
    days = [datetime.fromtimestamp(t, IST).date() for t in ts]
    res, n = defaultdict(float), defaultdict(int)
    i = 1
    while i < len(ts):
        p = snap[i - 1]
        t_i = datetime.fromtimestamp(ts[i], IST)
        if (p and p[0] == "BULLISH" and days[i - 1] == days[i] and t_i.time() < dtime(14, 0) and h[i] >= p[1]):
            e = max(p[1], o[i])
            peak, out, j = 0.0, None, i
            while j < len(ts) and days[j] == days[i]:
                lo = (l[j] - e) / e * 100
                if lo <= -1.0:
                    out = -1.0
                    break
                if peak >= 0.4 and lo <= 0:
                    out = 0.0
                    break
                peak = max(peak, (h[j] - e) / e * 100)
                j += 1
            if out is None:
                out = (c[j - 1] - e) / e * 100
            res[days[i]] += out
            n[days[i]] += 1
            while j < len(ts) and days[j] == days[i] and j < i + 1:
                j += 1
            i = max(j, i + 1)
            continue
        i += 1
    return {d: (res[d], n[d]) for d in res}


def _fit_scores(syms, as_of, fits, days, min_trades=3):
    look = [d for d in days if d <= as_of][-20:]
    sc = {}
    for s in syms:
        per = fits.get(s, {})
        tot = sum(per.get(d, (0, 0))[0] for d in look)
        cnt = sum(per.get(d, (0, 0))[1] for d in look)
        if cnt >= min_trades:
            sc[s] = tot
    return sc


def select(rule: str, as_of: date, dailies: dict, universe: list[str], fits: dict, days: list[date], seed: int = 0):
    rule = rule.replace("_DAILY", "")
    gated = [s for s in universe if s in dailies and wf.gate_check(dailies[s], as_of)[0]]
    if rule == "HYBRID":
        ath = wf.ath_scores({s: dailies[s] for s in gated}, as_of)
        pool = sorted(ath, key=lambda s: ath[s], reverse=True)[:40]
        fit = _fit_scores(pool, as_of, fits, days)
        ranked = sorted(fit, key=lambda s: fit[s], reverse=True)[:TOP_N]
        ranked += [s for s in pool if s not in ranked][:TOP_N - len(ranked)]
        return set(ranked)
    if rule == "TREND_ATH":
        sc = wf.ath_scores({s: dailies[s] for s in universe if s in dailies}, as_of)
    elif rule == "TREND_ATH_GATE":
        sc = wf.ath_scores({s: dailies[s] for s in gated}, as_of)
    elif rule == "MOMENTUM":
        sc = momentum_scores({s: dailies[s] for s in gated}, as_of)
    elif rule == "STRATEGY_FIT":
        sc = _fit_scores(gated, as_of, fits, days)
    elif rule.startswith("RANDOM"):
        rng = random.Random(f"{seed}-{as_of}")
        return set(rng.sample(gated, min(TOP_N, len(gated))))
    else:
        raise ValueError(rule)
    return set(sorted(sc, key=lambda s: sc[s], reverse=True)[:TOP_N])


def main():
    wf.authenticate()
    u.wf.authenticate = lambda: None
    st.dhan_wrapper.client.Dhan.dhan_http.timeout = 90
    universe = fetch_fno_universe()
    dailies = {p.stem: wf.prepare_daily(json.loads(p.read_text())) for p in DAILY.glob("*.json")}
    days = trading_days()
    as_of = weekly_as_of(days)
    decision_dates = sorted(set(as_of.values()))
    print("decision dates:", [d.isoformat() for d in decision_dates], flush=True)
    fits = {}
    for s in universe:
        if (FIVE / f"{s}_5min.json").exists():
            fits[s] = fit_proxy(s)
    rules = sys.argv[1:] or ["TREND_ATH", "TREND_ATH_GATE", "MOMENTUM", "STRATEGY_FIT", "RANDOM_1", "RANDOM_2", "RANDOM_3"]
    pricer = st.OptionPricer()
    report = {}
    daily_as_of = {d: max(x for x in days if x < d) for d in as_of}
    for rule in rules:
        seed = int(rule.split("_")[1]) if rule.startswith("RANDOM") else 0
        amap = daily_as_of if rule.endswith("_DAILY") else as_of
        picks = {dd: select(rule, dd, dailies, universe, fits, days, seed) for dd in sorted(set(amap.values()))}
        allowed = {d: picks[a] for d, a in amap.items()}
        syms = sorted(set().union(*picks.values()))
        tr, stats = u.simulate(syms, CAP, pricer, rule, "long", allowed)
        weekly = defaultdict(float)
        for t in tr:
            weekly[as_of[date.fromisoformat(t["day"])].isoformat()] += t["pnl_modeled"]  # always by week
        report = json.loads((OUT / "selection_walkforward.json").read_text()) if (OUT / "selection_walkforward.json").exists() else report
        report[rule] = {"summary": u.summarize(tr), "stats": stats,
                        "weekly_by_decision_date": {k: round(v) for k, v in sorted(weekly.items())},
                        "picks": {k.isoformat(): sorted(v) for k, v in picks.items()}}
        print(f"[{rule}] {report[rule]['summary']} weekly={report[rule]['weekly_by_decision_date']} "
              f"calls={pricer.calls}", flush=True)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "selection_walkforward.json").write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
