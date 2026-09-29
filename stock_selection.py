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

CLI (after 15:30 IST or before 09:15 - ~210 daily + ~40 intraday REST calls):
    uv run python stock_selection.py            # print today's picks
    uv run python stock_selection.py --write    # also write data/super_bollinger_watchlist
"""
from __future__ import annotations

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


def fit_by_day(fast: dict) -> dict[date, tuple[float, int]]:
    """{session: (summed % result, trades)} of the Super Bollinger rules on
    this stock's own 5-min bars (underlying proxy - see module docstring)."""
    if not fast.get("closes") or len(fast["closes"]) < 300:
        return {}
    snap = pending_snapshots(fast)
    ts, o, h, l, c = fast["timestamps"], fast["opens"], fast["highs"], fast["lows"], fast["closes"]
    days = [datetime.fromtimestamp(t, IST).date() for t in ts]
    res: dict[date, list] = {}
    i = 1
    while i < len(ts):
        p = snap[i - 1]
        if (p and p[0] == "BULLISH" and days[i - 1] == days[i]
                and datetime.fromtimestamp(ts[i], IST).time() < FIT_ENTRY_CUTOFF and h[i] >= p[1]):
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
                  top_n: int = TOP_N, pool_size: int = POOL) -> list[dict]:
    """dailies: {symbol: daily bars up to the as-of session}; get_fast(symbol)
    -> 5-min bars (or None); sessions: trading days up to the as-of session
    (the last FIT_LOOKBACK_SESSIONS are the fit window). Returns the picks,
    best first, with why each was picked."""
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
    look = set(sessions[-FIT_LOOKBACK_SESSIONS:])
    fit = {}
    for s in pool:
        fast = get_fast(s)
        per = fit_by_day(fast) if fast else {}
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
def run_live(log: Callable[[str], None] = print, pace_seconds: float = 0.25) -> list[dict]:
    from Options.dhan_client import dhan_wrapper
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
    sessions_seen: set[date] = set()

    def get_fast(sym: str) -> Optional[dict]:
        sid = dhan_wrapper._equity_security_id(sym)
        data = {}
        for attempt in range(3):
            data = dhan_wrapper.fetch_continuous_intraday(sid, "NSE_EQ", "EQUITY", 5,
                                                          lookback_days_override=INTRADAY_LOOKBACK_DAYS)
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

    # sessions need the fast series; fetch the pool lazily inside hybrid_select,
    # then recompute the session list from what was seen (completed days only).
    fasts: dict[str, Optional[dict]] = {}

    def cached_fast(sym: str) -> Optional[dict]:
        if sym not in fasts:
            fasts[sym] = get_fast(sym)
        return fasts[sym]

    # First pass fills `fasts` for the pool and the session list; second pass
    # scores with the complete session list (no further Dhan calls).
    hybrid_select(dailies, cached_fast, sorted(sessions_seen))
    picks = hybrid_select(dailies, cached_fast, sorted(sessions_seen))
    log(f"HYBRID: {len(dailies)} stocks scored, fit window = last {FIT_LOOKBACK_SESSIONS} of "
        f"{len(sessions_seen)} completed sessions")
    for i, p in enumerate(picks, 1):
        log(f"  {i:2d}. {p['symbol']:12s} fit {p['fit_pct']:+6.2f}% over {p['fit_trades']} trades, "
            f"ATH {p['ath_score']:.1f} ({p['source']})")
    return picks


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
            log(f"Wrote {len(picks)} symbols to {SUPER_BOLLINGER_WATCHLIST_FILE}")
    HISTORY_DIR.mkdir(exist_ok=True)
    out = HISTORY_DIR / f"{datetime.now(IST).strftime('%Y-%m-%d')}_super_bollinger_hybrid_selection.log"
    out.write_text("\n".join(lines) + "\n" + json.dumps(picks, indent=2) + "\n")


if __name__ == "__main__":
    main()
