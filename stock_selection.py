"""
HYBRID stock selection for Super Bollinger (30 Sep 2026, user request:
"use current ATH for Bollinger and deploy this Hybrid function for Super
Bollinger").

Why a separate selection: Super Bollinger's rules only make money on stocks
that trend in a way that suits them. Picked weekly with ATH alone (what the
Friday scheduler does for Swing/Bollinger), the same rules lost Rs 31k over
31 Aug - 29 Sep - inside the range of RANDOM 15-stock picks. HYBRID made
+Rs 46k with every week positive, above every random draw
(research_stock_selection_walkforward.py; trading-skills learnings/
stock-selection-walkforward-hindsight-bias.md).

HYBRID, using only data up to the latest completed session:
  1. liquidity gate - estimated ATM premium >= Rs 5 (0.4 x spot x 20-day
     vol x sqrt(15/365)) and 20-day average turnover >= Rs 50 cr;
  2. trend-quality pool - the top POOL (40) gated stocks by the deployed
     ATH composite score (fno_ath_screener.score_stock, unchanged);
  3. strategy fit - for each pool stock, the Super Bollinger rules replayed
     on its own 5-min bars over the last FIT_LOOKBACK_SESSIONS (20):
     resting BULLISH Bollinger/Vortex trigger touched before 14:00, exit
     at -1% (stop), at entry once +0.4% was reached (breakeven), else at
     the day's close; score = summed % result, needing >= FIT_MIN_TRADES
     (3) trades;
  4. the TOP_N (15) best fits; if fewer than TOP_N qualify, the rest are
     filled from the pool in ATH order.

The signal code is the LIVE Bollinger indicator functions (Bollinger/
signals.py) with the same pending-order state machine the backtests use,
so selection and trading read the market the same way.

SHADOW LIST (30 Sep 2026, NOT traded): next to the live list the weekly
job also picks a second list with the one rule that scored above HYBRID in
the 14-week comparison (research_selection_rules_longer.py, "HYBRID_F
lookback 40"): the same pool, but the fit is measured over the last
SHADOW_LOOKBACK_SESSIONS (40) with the live 1-hour-green entry filter
applied. It was 1 of 17 rules on 14 weeks - not proven - so it is only
recorded (data/super_bollinger_shadow_watchlist.json) and, a week later,
both lists are scored on the sessions that followed with the same
stock-price replay (data/super_bollinger_shadow_scores.jsonl; GET
/super-bollinger/watchlist/shadow). Nothing in this block can change the
live list.

CLI (after 15:30 IST or before 09:15 - ~210 daily + ~40 intraday REST calls):
    uv run python stock_selection.py            # print today's picks
    uv run python stock_selection.py --write    # also write data/super_bollinger_watchlist + data/unified_momentum_watchlist
    uv run python stock_selection.py --shadow   # also score/record the shadow list (+~40 intraday calls)
"""
from __future__ import annotations

import bisect
import json
import math
import statistics
import sys
import time
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
REPO_ROOT = Path(__file__).resolve().parent
SUPER_BOLLINGER_WATCHLIST_FILE = REPO_ROOT / "data" / "super_bollinger_watchlist"
# Unified Momentum (1 Oct 2026, real money) trades the same HYBRID picks from its own file.
UNIFIED_MOMENTUM_WATCHLIST_FILE = REPO_ROOT / "data" / "unified_momentum_watchlist"
HISTORY_DIR = REPO_ROOT / "history"

TOP_N = 15
POOL = 40
FIT_LOOKBACK_SESSIONS = 20
FIT_MIN_TRADES = 3
FIT_STOP_PCT = 1.0
FIT_BREAKEVEN_AFTER_PCT = 0.4
FIT_ENTRY_CUTOFF = dtime(14, 0)
INTRADAY_LOOKBACK_DAYS = 60
PREMIUM_GATE_RS = 5.0
TURNOVER_GATE_CR = 50.0
GATE_DAYS_TO_EXPIRY = 15
MIN_DAILY_BARS_FOR_ATH = 260

# Shadow list (recorded and scored, never traded) - see the module docstring.
SHADOW_RULE = "HYBRID_F40"
SHADOW_LOOKBACK_SESSIONS = 40
SHADOW_INTRADAY_LOOKBACK_DAYS = 85     # 40 sessions + holidays; Dhan serves up to 90 days of 5-min bars per call
SHADOW_TIME_BUDGET_SECONDS = 600       # the weekly job must reach its restart step even if Dhan is slow
SHADOW_FILE = REPO_ROOT / "data" / "super_bollinger_shadow_watchlist.json"
SHADOW_SCORES_FILE = REPO_ROOT / "data" / "super_bollinger_shadow_scores.jsonl"
LIVE_RULE = f"HYBRID ({FIT_LOOKBACK_SESSIONS} sessions, plain fit)"


# --------------------------------------------------------------------------- #
# Liquidity gate (identical to walkforward_selector_eval.gate_check)
# --------------------------------------------------------------------------- #
def _vol(closes: list[float], window: int = 20) -> Optional[float]:
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(len(closes) - window, len(closes))]
    return statistics.pstdev(rets) * math.sqrt(252)


def liquidity_gate(daily: dict) -> tuple[bool, Optional[float], Optional[float]]:
    """daily: {"close","high","low","volume"} up to the as-of session."""
    c, v = daily["close"], daily["volume"]
    if len(c) < 25:
        return False, None, None
    turnover_cr = sum(vv * cc for vv, cc in zip(v[-20:], c[-20:])) / 20 / 1e7
    sigma = _vol(c)
    prem = 0.4 * c[-1] * sigma * math.sqrt(GATE_DAYS_TO_EXPIRY / 365.0) if sigma else None
    ok = prem is not None and prem >= PREMIUM_GATE_RS and turnover_cr >= TURNOVER_GATE_CR
    return ok, prem, turnover_cr


# --------------------------------------------------------------------------- #
# Strategy fit (Super Bollinger rules replayed on the stock's own 5-min bars)
# --------------------------------------------------------------------------- #
def pending_snapshots(fast: dict) -> list:
    """The Bollinger/Vortex pending order as it stands at the END of every
    5-min bar - (side, trigger, stop) or None. Indicators from the live
    Bollinger.signals module; state machine = bollinger_research.
    fires_with_snapshots (the port validated against the live replay)."""
    from Bollinger import config as bcfg, signals as bsig
    highs, lows, closes = fast["highs"], fast["lows"], fast["closes"]
    n = len(closes)
    sma = bsig._compute_sma(closes, bcfg.BB_PERIOD)
    vip, vim = bsig._compute_vortex(highs, lows, closes, bcfg.VORTEX_PERIOD)
    sh, sl = bsig._compute_fractal_swings(highs, lows, bcfg.SWING_FRACTAL_LOOKBACK)
    vb = [sma[i] is not None and vip[i] is not None and vim[i] is not None and closes[i] > sma[i] and vip[i] > vim[i]
          for i in range(n)]
    vr = [sma[i] is not None and vip[i] is not None and vim[i] is not None and closes[i] < sma[i] and vim[i] > vip[i]
          for i in range(n)]
    L, M = bcfg.SWING_FRACTAL_LOOKBACK, bcfg.MIN_PULLBACK_CANDLES
    pending, ext, ah, al = None, None, None, None
    snap = [None] * n
    for i in range(n):
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
                if pending[0] == "BULLISH":
                    ah = None
                else:
                    al = None
                pending, ext = None, None
        snap[i] = tuple(pending) if pending else None
    return snap


def hourly_candles(fast: dict) -> tuple[list[int], list[bool]]:
    """(end timestamp, green?) of every 60-minute candle, anchored at 09:15
    like the live entry filter (SuperBollinger/entry_filters.py): green =
    closed above its own open."""
    ends, green, key = [], [], None
    o0 = c0 = None
    for t, o, c in zip(fast["timestamps"], fast["opens"], fast["closes"]):
        dt = datetime.fromtimestamp(t, IST)
        k = (dt.date(), (dt.hour * 60 + dt.minute - 555) // 60)
        if k != key:
            if key is not None:
                green.append(c0 > o0)
            key, o0 = k, o
            ends.append(t + 300)
        else:
            ends[-1] = t + 300
        c0 = c
    if key is not None:
        green.append(c0 > o0)
    return ends, green


def fit_by_day(fast: dict, hour_filter: bool = False) -> dict[date, tuple[float, int]]:
    """{session: (summed % result, trades)} of the Super Bollinger rules on
    this stock's own 5-min bars (underlying proxy - see module docstring).
    hour_filter=True also applies the live 1-hour-green entry filter: no
    entry while the last CLOSED 60-minute candle is red."""
    if not fast.get("closes") or len(fast["closes"]) < 300:
        return {}
    snap = pending_snapshots(fast)
    ends, green = hourly_candles(fast) if hour_filter else ([], [])
    ts, o, h, l, c = fast["timestamps"], fast["opens"], fast["highs"], fast["lows"], fast["closes"]
    days = [datetime.fromtimestamp(t, IST).date() for t in ts]
    res: dict[date, list] = {}
    i = 1
    while i < len(ts):
        p = snap[i - 1]
        if (p and p[0] == "BULLISH" and days[i - 1] == days[i]
                and datetime.fromtimestamp(ts[i], IST).time() < FIT_ENTRY_CUTOFF and h[i] >= p[1]):
            if hour_filter:
                k = bisect.bisect_right(ends, ts[i]) - 1
                if k < 0 or not green[k]:
                    i += 1
                    continue
            e = max(p[1], o[i])
            peak, out, j = 0.0, None, i
            while j < len(ts) and days[j] == days[i]:
                lo = (l[j] - e) / e * 100
                if lo <= -FIT_STOP_PCT:
                    out = -FIT_STOP_PCT
                    break
                if peak >= FIT_BREAKEVEN_AFTER_PCT and lo <= 0:
                    out = 0.0
                    break
                peak = max(peak, (h[j] - e) / e * 100)
                j += 1
            if out is None:
                out = (c[j - 1] - e) / e * 100
            r = res.setdefault(days[i], [0.0, 0])
            r[0] += out
            r[1] += 1
            i = max(j, i + 1)
            continue
        i += 1
    return {d: (v[0], v[1]) for d, v in res.items()}


# --------------------------------------------------------------------------- #
# HYBRID
# --------------------------------------------------------------------------- #
def hybrid_select(dailies: dict[str, dict], get_fast: Callable[[str], Optional[dict]], sessions: list[date],
                  top_n: int = TOP_N, pool_size: int = POOL, lookback: int = FIT_LOOKBACK_SESSIONS,
                  hour_filter: bool = False) -> list[dict]:
    """dailies: {symbol: daily bars up to the as-of session}; get_fast(symbol)
    -> 5-min bars (or None); sessions: trading days up to the as-of session
    (the last `lookback` are the fit window). Returns the picks, best first,
    with why each was picked. The defaults are the LIVE rule; lookback /
    hour_filter are only changed for the shadow list."""
    from fno_ath_screener import score_stock
    gated = {s: d for s, d in dailies.items() if liquidity_gate(d)[0]}
    ath = {}
    for s, d in gated.items():
        if len(d["close"]) < MIN_DAILY_BARS_FOR_ATH:
            continue
        try:
            r = score_stock(s, d)
        except Exception:  # noqa: BLE001
            r = None
        if r is not None:
            ath[s] = r["total_score"]
    pool = sorted(ath, key=lambda s: ath[s], reverse=True)[:pool_size]
    look = set(sessions[-lookback:])
    fit = {}
    for s in pool:
        fast = get_fast(s)
        per = fit_by_day(fast, hour_filter) if fast else {}
        tot = sum(v[0] for d, v in per.items() if d in look)
        cnt = sum(v[1] for d, v in per.items() if d in look)
        fit[s] = (tot, cnt)
    qualified = [s for s in pool if fit[s][1] >= FIT_MIN_TRADES]
    ranked = sorted(qualified, key=lambda s: fit[s][0], reverse=True)[:top_n]
    picks = [{"symbol": s, "source": "strategy_fit", "fit_pct": round(fit[s][0], 2), "fit_trades": fit[s][1],
              "ath_score": round(ath[s], 1)} for s in ranked]
    for s in pool:
        if len(picks) >= top_n:
            break
        if s not in ranked:
            picks.append({"symbol": s, "source": "ath_fill", "fit_pct": round(fit[s][0], 2),
                          "fit_trades": fit[s][1], "ath_score": round(ath[s], 1)})
    return picks


# --------------------------------------------------------------------------- #
# Live run (Dhan calls; caller authenticates)
# --------------------------------------------------------------------------- #
_LAST: dict = {}   # the daily bars of the latest run_live, reused by run_shadow in the same process


def _fetch_dailies(log: Callable[[str], None], pace_seconds: float) -> dict[str, dict]:
    from fno_ath_screener import fetch_daily_series, fetch_fno_universe
    universe = fetch_fno_universe()
    log(f"HYBRID: fetching daily bars for {len(universe)} F&O stocks...")
    dailies = {}
    for k, s in enumerate(universe):
        try:
            d = fetch_daily_series(s)
        except Exception as exc:  # noqa: BLE001
            d = None
            log(f"  {s}: daily fetch failed ({exc})")
        if d:
            dailies[s] = d
        time.sleep(pace_seconds)
        if (k + 1) % 50 == 0:
            log(f"  ...{k + 1}/{len(universe)} daily series fetched")
    return dailies


def _fast_fetcher(lookback_days: int, sessions_seen: set, deadline: Optional[float] = None) -> Callable[[str], Optional[dict]]:
    """get_fast(symbol) -> that stock's 5-min bars over `lookback_days`
    (fetched once per symbol, None if Dhan has nothing or `deadline` - a
    time.monotonic() value - has passed). Completed sessions are collected
    into `sessions_seen`."""
    from Options.dhan_client import dhan_wrapper
    fasts: dict[str, Optional[dict]] = {}

    def get_fast(sym: str) -> Optional[dict]:
        if deadline is not None and time.monotonic() > deadline:
            return None
        sid = dhan_wrapper._equity_security_id(sym)
        data = {}
        for attempt in range(3):
            data = dhan_wrapper.fetch_continuous_intraday(sid, "NSE_EQ", "EQUITY", 5,
                                                          lookback_days_override=lookback_days)
            if data.get("close"):
                break
            time.sleep(3 * (attempt + 1))
        if not data.get("close"):
            return None
        fast = {"timestamps": data["timestamp"], "opens": data["open"], "highs": data["high"],
                "lows": data["low"], "closes": data["close"]}
        today = datetime.now(IST).date()
        for t in fast["timestamps"]:
            d = datetime.fromtimestamp(t, IST).date()
            if d < today or datetime.now(IST).time() >= dtime(15, 30):
                sessions_seen.add(d)
        return fast

    def cached_fast(sym: str) -> Optional[dict]:
        if sym not in fasts:
            fasts[sym] = get_fast(sym)
        return fasts[sym]

    return cached_fast


def run_live(log: Callable[[str], None] = print, pace_seconds: float = 0.25) -> list[dict]:
    dailies = _fetch_dailies(log, pace_seconds)
    sessions_seen: set[date] = set()
    # sessions need the fast series; fetch the pool lazily inside hybrid_select,
    # then recompute the session list from what was seen (completed days only).
    cached_fast = _fast_fetcher(INTRADAY_LOOKBACK_DAYS, sessions_seen)

    # First pass fills the fetcher's cache for the pool and the session list;
    # second pass scores with the complete session list (no further Dhan calls).
    hybrid_select(dailies, cached_fast, sorted(sessions_seen))
    picks = hybrid_select(dailies, cached_fast, sorted(sessions_seen))
    _LAST["dailies"] = dailies
    log(f"HYBRID: {len(dailies)} stocks scored, fit window = last {FIT_LOOKBACK_SESSIONS} of "
        f"{len(sessions_seen)} completed sessions")
    for i, p in enumerate(picks, 1):
        log(f"  {i:2d}. {p['symbol']:12s} fit {p['fit_pct']:+6.2f}% over {p['fit_trades']} trades, "
            f"ATH {p['ath_score']:.1f} ({p['source']})")
    return picks


# --------------------------------------------------------------------------- #
# Shadow list - recorded and scored, never traded
# --------------------------------------------------------------------------- #
def load_shadow_record(path: Optional[Path] = None) -> Optional[dict]:
    path = path or SHADOW_FILE          # resolved at call time (a test or a relocation can point it elsewhere)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def score_lists(record: dict, get_fast: Callable[[str], Optional[dict]], sessions: list[date]) -> Optional[dict]:
    """How last week's two lists did on the completed sessions AFTER they
    were picked: the same stock-price replay as the fit, with the 1-hour
    filter (what the bot trades). None if no session has completed since."""
    as_of = date.fromisoformat(record["as_of"])
    days = [d for d in sessions if d > as_of]
    if not days:
        return None
    wanted = set(days)
    out = {"as_of": record["as_of"], "scored_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
           "sessions": [d.isoformat() for d in days]}
    lists = {k: record[k]["symbols"] for k in ("live", "shadow")}
    for name, symbols in lists.items():
        pct, trades, no_data, per_symbol = 0.0, 0, [], {}
        for s in symbols:
            fast = get_fast(s)
            if not fast:
                no_data.append(s)
                continue
            per = fit_by_day(fast, hour_filter=True)
            p = sum(v[0] for d, v in per.items() if d in wanted)
            n = sum(v[1] for d, v in per.items() if d in wanted)
            pct += p
            trades += n
            per_symbol[s] = round(p, 2)
        out[name] = {"rule": record[name]["rule"], "pct": round(pct, 2), "trades": trades, "no_data": no_data,
                     "per_symbol": per_symbol}
    out["in_both"] = sorted(set(lists["live"]) & set(lists["shadow"]))
    return out


def run_shadow(log: Callable[[str], None] = print, live_symbols: Optional[list[str]] = None,
               pace_seconds: float = 0.25, record_path: Optional[Path] = None,
               scores_path: Optional[Path] = None) -> Optional[dict]:
    """1. scores the lists recorded last time on the sessions since; 2. picks
    this week's shadow list and records it next to the live list. Reads Dhan,
    writes only its own two files - the live watchlist is never touched."""
    record_path, scores_path = record_path or SHADOW_FILE, scores_path or SHADOW_SCORES_FILE
    deadline = time.monotonic() + SHADOW_TIME_BUDGET_SECONDS
    dailies = _LAST.get("dailies") or _fetch_dailies(log, pace_seconds)
    sessions_seen: set[date] = set()
    get_fast = _fast_fetcher(SHADOW_INTRADAY_LOOKBACK_DAYS, sessions_seen, deadline)
    hybrid_select(dailies, get_fast, [], lookback=SHADOW_LOOKBACK_SESSIONS, hour_filter=True)   # fills cache + sessions
    sessions = sorted(sessions_seen)
    if not sessions:
        log("SHADOW: no 5-min history came back - nothing scored or recorded this week")
        return None

    previous = load_shadow_record(record_path)
    if previous:
        score = score_lists(previous, get_fast, sessions)
        if score is None:
            log(f"SHADOW: no completed session since the lists of {previous['as_of']} - nothing to score yet")
        else:
            scores_path.parent.mkdir(parents=True, exist_ok=True)
            with scores_path.open("a") as fh:
                fh.write(json.dumps(score) + "\n")
            log(f"SHADOW SCORE for the lists picked {score['as_of']} over {len(score['sessions'])} session(s) "
                f"({score['sessions'][0]} .. {score['sessions'][-1]}), stock-price replay with the 1-hour filter: "
                f"LIVE {score['live']['pct']:+.2f}% ({score['live']['trades']} trades) vs "
                f"SHADOW {score['shadow']['pct']:+.2f}% ({score['shadow']['trades']} trades); "
                f"{len(score['in_both'])} stocks were on both lists")

    picks = hybrid_select(dailies, get_fast, sessions, lookback=SHADOW_LOOKBACK_SESSIONS, hour_filter=True)
    window = min(SHADOW_LOOKBACK_SESSIONS, len(sessions))
    log(f"SHADOW ({SHADOW_RULE}, NOT traded): fit window = last {window} of {len(sessions)} completed sessions, "
        f"1-hour filter on")
    for i, p in enumerate(picks, 1):
        log(f"  {i:2d}. {p['symbol']:12s} fit {p['fit_pct']:+6.2f}% over {p['fit_trades']} trades, "
            f"ATH {p['ath_score']:.1f} ({p['source']})")
    if len(picks) < 10:
        log(f"SHADOW: only {len(picks)} picks - looks like a data problem, not recorded")
        return None
    live = list(live_symbols or [])
    shadow = [p["symbol"] for p in picks]
    record = {"as_of": sessions[-1].isoformat(), "picked_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
              "fit_sessions_used": window,
              "live": {"rule": LIVE_RULE, "symbols": live},
              "shadow": {"rule": f"{SHADOW_RULE} ({SHADOW_LOOKBACK_SESSIONS} sessions, fit with the 1-hour filter)",
                         "symbols": shadow, "picks": picks},
              "only_live": sorted(set(live) - set(shadow)), "only_shadow": sorted(set(shadow) - set(live))}
    record_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = record_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1))
    tmp.replace(record_path)
    log(f"SHADOW recorded to {record_path.name}: only on the live list {record['only_live']}, "
        f"only on the shadow list {record['only_shadow']}")
    return record


def shadow_status(last: int = 12) -> dict:
    """For GET /super-bollinger/watchlist/shadow: the lists being compared
    this week and the weekly scores so far."""
    scores = []
    try:
        for line in SHADOW_SCORES_FILE.read_text().splitlines():
            if line.strip():
                scores.append(json.loads(line))
    except (OSError, ValueError):
        pass
    total = {k: round(sum(s[k]["pct"] for s in scores), 2) for k in ("live", "shadow")} if scores else None
    return {"traded": False, "note": "the shadow list is recorded and scored only - Super Bollinger trades the live list",
            "current": load_shadow_record(), "weeks_scored": len(scores), "total_pct": total,
            "scores": [{k: ({x: y for x, y in v.items() if x != "per_symbol"} if isinstance(v, dict) else v)
                        for k, v in s.items()} for s in scores[-last:]]}


def write_watchlist(symbols: list[str], path: Path = SUPER_BOLLINGER_WATCHLIST_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_name(f"{path.name}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}").write_text(path.read_text())
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(f"{s}\n" for s in symbols))
    tmp.replace(path)


def main() -> None:
    from Options.dhan_client import dhan_wrapper
    dhan_wrapper.authenticate()
    lines = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        lines.append(msg)

    picks = run_live(log)
    if "--write" in sys.argv:
        if len(picks) < 10:
            log(f"NOT writing: only {len(picks)} picks - looks like a data problem, keeping the current list")
        else:
            write_watchlist([p["symbol"] for p in picks])
            write_watchlist([p["symbol"] for p in picks], UNIFIED_MOMENTUM_WATCHLIST_FILE)
            log(f"Wrote {len(picks)} symbols to {SUPER_BOLLINGER_WATCHLIST_FILE} and {UNIFIED_MOMENTUM_WATCHLIST_FILE}")
    if "--shadow" in sys.argv:
        current = [ln.strip().upper() for ln in SUPER_BOLLINGER_WATCHLIST_FILE.read_text().splitlines()
                   if ln.strip() and not ln.startswith("#")] if SUPER_BOLLINGER_WATCHLIST_FILE.exists() else []
        run_shadow(log, current)
    HISTORY_DIR.mkdir(exist_ok=True)
    out = HISTORY_DIR / f"{datetime.now(IST).strftime('%Y-%m-%d')}_super_bollinger_hybrid_selection.log"
    out.write_text("\n".join(lines) + "\n" + json.dumps(picks, indent=2) + "\n")


if __name__ == "__main__":
    main()
