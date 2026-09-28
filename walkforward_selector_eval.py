"""
Walk-forward evaluation of WATCHLIST SELECTION RULES for the live Bollinger
strategy (28 Sep 2026). Instead of backtesting a fixed stock list over the
same window it was picked from (hindsight bias), every rule is re-run as of
each past Friday close using only data up to that day, and its picks are
scored on the FOLLOWING week only.

Rules compared: ATH (fno_ath_screener.score_stock, exactly as the weekly
watchlist job uses it), ATH + tradability gates, BOLLINGER_FIT (strategy-
specific, see score_fit), HYBRID (ATH top-40 re-ranked by fit), and RANDOM
(many random 15-stock draws - the null baseline any rule must beat).

Two P&L tiers:
  option    - real ATM option 1-min premiums, only for the current front-
              month series (expired contracts are not in Dhan's instrument
              master), so roughly the last month. Ground truth.
  synthetic - premium modeled as P0*(1 + leverage*underlying_return) with
              P0 from a Black-Scholes ATM approximation. Covers the full
              ~90-day intraday window; validated against the option tier
              on the overlapping weeks.

Entry timing matches LIVE (Bollinger/signals.py trims the still-forming bar
and only acts on the newest CLOSED bar): a fire on 5-min bar i is entered at
the first 1-min bar at or after bar i's close. The older backtest scripts
entered during bar i itself using bar i's full high/low - a lookahead bias;
--compare-lag quantifies it on the current watchlist.

Not modeled (same as every Bollinger backtest here): MAX_CONCURRENT_TRADES
capacity/backlog, liquid-strike selection, funds check.

Phases (auth via HANDOFF_DHAN_ACCESS_TOKEN, never pin_totp):
    uv run python walkforward_selector_eval.py daily    --tag TAG
    uv run python walkforward_selector_eval.py intraday --tag TAG [--symbols A,B] [--compare-lag]
    uv run python walkforward_selector_eval.py evaluate --tag TAG
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env", override=False)

# The backtest module parses sys.argv and prints a banner at import time.
_saved_argv = sys.argv
sys.argv = [sys.argv[0]]
with contextlib.redirect_stdout(io.StringIO()):
    import backtest_bollinger_exact_slippage_9symbols_30day as bt  # noqa: E402
sys.argv = _saved_argv

from Options import config as ocfg  # noqa: E402
from Options.dhan_client import dhan_wrapper, _retry  # noqa: E402
from Bollinger import config as bcfg  # noqa: E402
from Swing.position_store import hard_stop_for, price_past_hard_stop, unrealized_pnl_rs  # noqa: E402
from fno_ath_screener import fetch_fno_universe, score_stock as ath_score_stock  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

TOP_N = 15
INTRADAY_LOOKBACK_DAYS = 90
DAILY_LOOKBACK_DAYS = 650
MIN_DAILY_BARS_FOR_ATH = 260
RANDOM_DRAWS = 1000
RANDOM_SEED = 7
PREMIUM_GATE_RS = 5.0
TURNOVER_GATE_CR = 50.0
FIT_LOOKBACK_TRADING_DAYS = 20
FIT_MIN_TRADES = 3
HYBRID_ATH_POOL = 40
ATM_DELTA = 0.5
GATE_DAYS_TO_EXPIRY = 15
PACE_SECONDS = 1.2
INTERVAL = bcfg.SIGNAL_INTERVAL_MINUTES
FRIDAY_SQUARE_OFF_TIME = datetime.strptime(bcfg.FRIDAY_SQUARE_OFF_TIME, "%H:%M").time()


# --------------------------------------------------------------------------- #
# Auth / caching / fetching
# --------------------------------------------------------------------------- #
def authenticate() -> None:
    handoff = os.environ.get("HANDOFF_DHAN_ACCESS_TOKEN")
    if not handoff:
        raise SystemExit("HANDOFF_DHAN_ACCESS_TOKEN not set - refusing to fall back to pin_totp "
                         "(would collide with the live droplet bot's session).")
    ocfg.DHAN_AUTH_MODE = "access_token"
    ocfg.DHAN_ACCESS_TOKEN = handoff
    with contextlib.redirect_stdout(io.StringIO()):
        dhan_wrapper.authenticate()


def tag_root(tag: str) -> Path:
    root = REPO_ROOT / "history" / f"walkforward_{tag}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_json(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def fetch_intraday_cached(cache_file: Path, security_id: str, segment: str, instrument: str,
                          interval: int, days: int) -> dict:
    cached = _read_json(cache_file)
    if cached is not None:
        return cached
    data = {}
    for attempt in range(4):
        data = dhan_wrapper.fetch_continuous_intraday(
            security_id, segment, instrument, interval, lookback_days_override=days)
        if data.get("close"):
            break
        time.sleep(5 * (attempt + 1))
    result = {"opens": data.get("open") or [], "highs": data.get("high") or [],
              "lows": data.get("low") or [], "closes": data.get("close") or [],
              "volumes": data.get("volume") or [], "timestamps": data.get("timestamp") or []}
    # Never cache an empty/failed response - it would poison every later run.
    if result["closes"]:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result))
    time.sleep(PACE_SECONDS)
    return result


def fetch_daily_cached(root: Path, symbol: str) -> dict | None:
    cache_file = root / "daily" / f"{symbol}.json"
    cached = _read_json(cache_file)
    if cached is not None:
        return cached
    try:
        security_id = dhan_wrapper._equity_security_id(symbol)
    except ValueError:
        return None
    now = datetime.now(IST)
    resp = _retry(dhan_wrapper.client.Dhan.historical_daily_data,
                  security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
                  from_date=(now - timedelta(days=DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
                  to_date=now.strftime("%Y-%m-%d"))
    d = (resp.get("data") or {}) if isinstance(resp, dict) else {}
    result = {"close": d.get("close") or [], "high": d.get("high") or [], "low": d.get("low") or [],
              "volume": d.get("volume") or [], "timestamp": d.get("timestamp") or []}
    if result["close"]:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result))
    time.sleep(0.3)
    return result if result["close"] else None


def build_option_index() -> dict:
    df = dhan_wrapper.instruments()
    opts = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")].copy()

    def underlying(s):
        try:
            return dhan_wrapper._underlying_from_trading_symbol(str(s))
        except Exception:  # noqa: BLE001
            return None

    opts["underlying"] = opts["SEM_TRADING_SYMBOL"].map(underlying)
    index = {}
    for sym, group in opts.groupby("underlying"):
        nearest = group["SEM_EXPIRY_DATE"].min()
        index[sym] = (group[group["SEM_EXPIRY_DATE"] == nearest].copy(), str(nearest))
    return index


# --------------------------------------------------------------------------- #
# Signal stream - exact port of run_backtest's entry_signal_for_bar, emitted
# as a stream of fires instead of being consumed inside one simulation.
# --------------------------------------------------------------------------- #
def compute_fires(fast: dict, signals: dict) -> list[dict]:
    highs, lows, closes, ts = fast["highs"], fast["lows"], fast["closes"], fast["timestamps"]
    valid_bullish, valid_bearish = signals["valid_bullish"], signals["valid_bearish"]
    swing_high, swing_low = signals["swing_high"], signals["swing_low"]
    lookback, min_pullback = bcfg.SWING_FRACTAL_LOOKBACK, bcfg.MIN_PULLBACK_CANDLES

    pending = None  # [side, trigger, stop]
    anchor_high = anchor_low = None
    pullback_extreme = None
    fires = []
    for i in range(len(closes)):
        if i < lookback:
            continue
        j = i - lookback
        if swing_high[j] and valid_bullish[j]:
            anchor_high = (j, highs[j])
        if swing_low[j] and valid_bearish[j]:
            anchor_low = (j, lows[j])
        if not valid_bullish[i]:
            anchor_high = None
        if not valid_bearish[i]:
            anchor_low = None

        if pending is not None:
            still_valid = valid_bullish[i] if pending[0] == "BULLISH" else valid_bearish[i]
            if not still_valid:
                pending = None
                pullback_extreme = None

        if valid_bullish[i] and anchor_high is not None:
            swing_idx, swing_price = anchor_high
            if i > swing_idx:
                streak = 0
                for k in range(swing_idx + 1, i + 1):
                    streak = streak + 1 if closes[k] < closes[k - 1] else 0
                if streak >= min_pullback:
                    if pending is None or pending[0] != "BULLISH":
                        pending = ["BULLISH", swing_price, lows[i]]
                        pullback_extreme = lows[i]
                    else:
                        if swing_price > pending[1]:
                            pending[1] = swing_price
                        pullback_extreme = min(pullback_extreme, lows[i])
                        pending[2] = pullback_extreme

        if valid_bearish[i] and anchor_low is not None:
            swing_idx, swing_price = anchor_low
            if i > swing_idx:
                streak = 0
                for k in range(swing_idx + 1, i + 1):
                    streak = streak + 1 if closes[k] > closes[k - 1] else 0
                if streak >= min_pullback:
                    if pending is None or pending[0] != "BEARISH":
                        pending = ["BEARISH", swing_price, highs[i]]
                        pullback_extreme = highs[i]
                    else:
                        if swing_price < pending[1]:
                            pending[1] = swing_price
                        pullback_extreme = max(pullback_extreme, highs[i])
                        pending[2] = pullback_extreme

        if pending is not None:
            hit = (pending[0] == "BULLISH" and highs[i] >= pending[1]) or \
                  (pending[0] == "BEARISH" and lows[i] <= pending[1])
            if hit:
                fires.append({"idx": i, "bar_start": ts[i], "side": pending[0],
                              "trigger": pending[1], "stop": pending[2], "bar_close": closes[i]})
                if pending[0] == "BULLISH":
                    anchor_high = None
                else:
                    anchor_low = None
                pending = None
                pullback_extreme = None
    return fires


# --------------------------------------------------------------------------- #
# Premium models
# --------------------------------------------------------------------------- #
def last_tuesday(year: int, month: int) -> date:
    d = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


def days_to_monthly_expiry(d: date) -> int:
    exp = last_tuesday(d.year, d.month)
    if d > exp:
        nxt = d.replace(day=28) + timedelta(days=7)
        exp = last_tuesday(nxt.year, nxt.month)
    return max((exp - d).days, 1)


def daily_vol_asof(daily: dict, as_of: date, window: int = 20) -> float | None:
    closes = [c for c, t in zip(daily["close"], daily["timestamp"])
              if datetime.fromtimestamp(t, tz=IST).date() < as_of]
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(len(closes) - window, len(closes))]
    return statistics.pstdev(rets) * math.sqrt(252)


def atm_premium_estimate(spot: float, sigma: float, days_to_expiry: int) -> float:
    return 0.4 * spot * sigma * math.sqrt(days_to_expiry / 365.0)


# --------------------------------------------------------------------------- #
# Simulation (one symbol, one tier, one entry-timing mode)
# --------------------------------------------------------------------------- #
def simulate(symbol: str, fires: list[dict], master: dict, *, tier: str, lag: bool,
             entry_from: date, lot_size: int, daily: dict | None = None,
             front_df=None, option_cache_dir: Path | None = None) -> list[dict]:
    m_ts, m_close = master["timestamps"], master["closes"]
    if not m_ts:
        return []
    last_ts_of_date = {}
    for t in m_ts:
        last_ts_of_date[datetime.fromtimestamp(t, tz=IST).date()] = t

    bar_secs = INTERVAL * 60
    queue = sorted(fires, key=lambda f: f["bar_start"])
    q = 0
    option_series: dict[str, dict] = {}
    trades = []
    pos = None

    def premium_at(t, underlying_now):
        if tier == "option":
            return bt.price_at_or_before(pos["opt_ts"], pos["opt_close"], t)
        r = (underlying_now / pos["spot0"] - 1.0) * (1 if pos["side"] == "BULLISH" else -1)
        return max(pos["entry_price"] * (1.0 + pos["leverage"] * r), 0.05)

    def close(t, dt, clean, reason):
        slip = bt.slippage_pct_for(clean)
        filled = clean * (1 - slip)
        pnl = unrealized_pnl_rs("LONG", pos["entry_price"], filled, pos["qty"])
        trades.append({
            "symbol": symbol, "tier": tier, "lag": lag, "side": pos["side"],
            "entry_ts": pos["entry_ts"], "entry_date": pos["entry_date"].isoformat(),
            "entry_price": round(pos["entry_price"], 4), "exit_ts": t,
            "exit_price": round(filled, 4), "exit_reason": reason, "pnl": round(pnl, 2),
            "contract": pos.get("contract"),
        })

    for t, spot in zip(m_ts, m_close):
        dt = datetime.fromtimestamp(t, tz=IST)
        d = dt.date()

        if pos is not None:
            prem = premium_at(t, spot)
            if prem is not None:
                pos["best"] = max(pos["best"], prem)
                loss_rs = -unrealized_pnl_rs("LONG", pos["entry_price"], prem, pos["qty"])
                reason = None
                if loss_rs >= bcfg.MAX_LOSS_PROTECTION_RS:
                    reason = "MAX_LOSS_HIT"
                else:
                    favorable = pos["best"] - pos["entry_price"]
                    if not pos["trail_armed"] and favorable >= pos["trail_dist"]:
                        pos["trail_armed"] = True
                        pos["trail_price"] = pos["best"] - pos["trail_dist"]
                    elif pos["trail_armed"]:
                        cand = pos["best"] - pos["trail_dist"]
                        if cand - pos["trail_price"] >= pos["trail_step"]:
                            pos["trail_price"] = cand
                    active = pos["trail_price"] if pos["trail_armed"] else pos["hard_stop"]
                    if price_past_hard_stop("LONG", prem, active):
                        reason = "TRAILING_STOP_HIT" if pos["trail_armed"] else "STOP_LOSS_HIT"
                    if (reason is None and bcfg.FRIDAY_SQUARE_OFF_ENABLED and dt.weekday() == 4
                            and (dt.time() >= FRIDAY_SQUARE_OFF_TIME or t == last_ts_of_date[d])):
                        reason = "FRIDAY_SQUARE_OFF"
                if reason:
                    close(t, dt, prem, reason)
                    pos = None

        # Collect fires that became actionable by t. Live only ever acts on
        # the NEWEST confirmed bar, so only the latest one still inside its
        # window counts; anything older is gone for good.
        candidate = None
        while q < len(queue):
            f = queue[q]
            actionable = f["bar_start"] + (bar_secs if lag else 0)
            if actionable > t:
                break
            if t < actionable + bar_secs and datetime.fromtimestamp(f["bar_start"], tz=IST).date() == d:
                candidate = f
            q += 1

        if pos is not None or candidate is None or d < entry_from:
            continue
        if lag and dt.weekday() == 4 and (dt.time() >= FRIDAY_SQUARE_OFF_TIME or t == last_ts_of_date[d]):
            continue

        side = candidate["side"]
        ref = candidate["bar_close"] if lag else spot
        stop_pct = max(abs(candidate["trigger"] - candidate["stop"]) / ref if ref else 0.0, bcfg.MIN_STOP_PCT)
        pos = {"side": side, "entry_ts": t, "entry_date": d, "spot0": spot,
               "qty": lot_size * bcfg.QUANTITY_LOTS}

        if tier == "option":
            opt_type = "CE" if side == "BULLISH" else "PE"
            try:
                opt = bt.nearest_for_strike_ref(front_df, opt_type, spot)
            except Exception:  # noqa: BLE001
                pos = None
                continue
            sid = opt["security_id"]
            if sid not in option_series:
                option_series[sid] = fetch_intraday_cached(
                    option_cache_dir / f"OPT_{sid}_1min.json", sid, "NSE_FNO", "OPTSTK", 1,
                    INTRADAY_LOOKBACK_DAYS)
            series = option_series[sid]
            entry_price = bt.price_at_or_before(series["timestamps"], series["closes"], t)
            if not entry_price:
                pos = None
                continue
            pos.update({"opt_ts": series["timestamps"], "opt_close": series["closes"],
                        "contract": opt["trading_symbol"], "qty": opt["lot_size"] * bcfg.QUANTITY_LOTS})
        else:
            sigma = daily_vol_asof(daily, d) if daily else None
            if not sigma:
                pos = None
                continue
            entry_price = atm_premium_estimate(spot, sigma, days_to_monthly_expiry(d))
            pos["leverage"] = ATM_DELTA * spot / entry_price

        trail_dist = entry_price * stop_pct * bcfg.TRAILING_STOP_FRACTION
        pos.update({"entry_price": entry_price, "best": entry_price,
                    "hard_stop": hard_stop_for("LONG", entry_price, stop_pct),
                    "trail_dist": trail_dist, "trail_step": trail_dist * bcfg.TRAILING_STEP_FRACTION,
                    "trail_armed": False, "trail_price": None})
    return trades


# --------------------------------------------------------------------------- #
# Per-symbol intraday phase
# --------------------------------------------------------------------------- #
def run_symbol(root: Path, symbol: str, option_index: dict, compare_lag: bool) -> dict | None:
    out_file = root / "trades" / f"{symbol}.json"
    cached = _read_json(out_file)
    if cached is not None and (not compare_lag or "option_nolag" in cached):
        return cached

    sym_dir = root / "intraday" / symbol.lower()
    try:
        sid = dhan_wrapper._equity_security_id(symbol)
    except ValueError:
        return None
    fast = fetch_intraday_cached(sym_dir / f"{symbol}_{INTERVAL}min_{INTRADAY_LOOKBACK_DAYS}d.json",
                                 sid, "NSE_EQ", "EQUITY", INTERVAL, INTRADAY_LOOKBACK_DAYS)
    master = fetch_intraday_cached(sym_dir / f"{symbol}_1min_{INTRADAY_LOOKBACK_DAYS}d.json",
                                   sid, "NSE_EQ", "EQUITY", 1, INTRADAY_LOOKBACK_DAYS)
    if not fast["closes"] or not master["closes"]:
        return None

    signals = bt.compute_signals(fast)
    fires = compute_fires(fast, signals)
    daily = fetch_daily_cached(root, symbol)

    result = {"symbol": symbol, "fires": len(fires)}
    # Trend-on flag per 5-min bar, stored compactly for the fit score.
    result["trend_on"] = [[t, 1 if (vb or vr) else 0] for t, vb, vr in
                          zip(fast["timestamps"], signals["valid_bullish"], signals["valid_bearish"])]

    lot_size = 1
    front = option_index.get(symbol)
    if front is not None:
        lot_size = int(float(front[0]["SEM_LOT_UNITS"].iloc[0]))
    result["lot_size"] = lot_size

    first_day = datetime.fromtimestamp(fast["timestamps"][0], tz=IST).date()
    result["synthetic"] = simulate(symbol, fires, master, tier="synthetic", lag=True,
                                   entry_from=first_day + timedelta(days=5), lot_size=lot_size, daily=daily)

    if front is not None:
        front_df, front_expiry = front
        tradable_from = bt.previous_monthly_expiry_cutoff(front_expiry) + timedelta(days=1)
        result["option_from"] = tradable_from.isoformat()
        result["option"] = simulate(symbol, fires, master, tier="option", lag=True,
                                    entry_from=tradable_from, lot_size=lot_size,
                                    front_df=front_df, option_cache_dir=sym_dir)
        if compare_lag:
            result["option_nolag"] = simulate(symbol, fires, master, tier="option", lag=False,
                                              entry_from=tradable_from, lot_size=lot_size,
                                              front_df=front_df, option_cache_dir=sym_dir)
            result["synthetic_nolag"] = simulate(symbol, fires, master, tier="synthetic", lag=False,
                                                 entry_from=first_day + timedelta(days=5),
                                                 lot_size=lot_size, daily=daily)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(result))
    return result


# --------------------------------------------------------------------------- #
# Selection rules (all strictly as-of the last trading day before the week)
# --------------------------------------------------------------------------- #
def prepare_daily(daily: dict) -> dict:
    daily = dict(daily)
    daily["dates"] = [datetime.fromtimestamp(t, tz=IST).date() for t in daily["timestamp"]]
    return daily


def daily_slice(daily: dict, as_of: date) -> dict:
    n = sum(1 for d in daily["dates"] if d <= as_of)
    return {key: daily[key][:n] for key in ("close", "high", "low", "volume")}


def vol_from_closes(closes: list[float], window: int = 20) -> float | None:
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(len(closes) - window, len(closes))]
    return statistics.pstdev(rets) * math.sqrt(252)


def ath_scores(dailies: dict, as_of: date) -> dict[str, float]:
    scores = {}
    for sym, daily in dailies.items():
        sl = daily_slice(daily, as_of)
        if len(sl["close"]) < MIN_DAILY_BARS_FOR_ATH:
            continue
        try:
            r = ath_score_stock(sym, sl)
        except Exception:  # noqa: BLE001
            r = None
        if r is not None:
            scores[sym] = r["total_score"]
    return scores


def gate_check(daily: dict, as_of: date) -> tuple[bool, float | None, float | None]:
    sl = daily_slice(daily, as_of)
    if len(sl["close"]) < 25:
        return False, None, None
    spot = sl["close"][-1]
    turnover_cr = sum(v * c for v, c in zip(sl["volume"][-20:], sl["close"][-20:])) / 20 / 1e7
    sigma = vol_from_closes(sl["close"])
    prem = atm_premium_estimate(spot, sigma, GATE_DAYS_TO_EXPIRY) if sigma else None
    ok = prem is not None and prem >= PREMIUM_GATE_RS and turnover_cr >= TURNOVER_GATE_CR
    return ok, prem, turnover_cr


def pct_ranks(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values, key=lambda s: values[s])
    n = len(ordered)
    return {s: (i / (n - 1) if n > 1 else 0.5) for i, s in enumerate(ordered)}


def fit_scores(day_stats: dict, candidates: list[str], lookback_days: list[date]) -> dict[str, float]:
    proxy_total, hit_rate, trend_on = {}, {}, {}
    for sym in candidates:
        stats = day_stats.get(sym)
        if not stats:
            continue
        on = sum(stats["trend"].get(d, (0, 0))[0] for d in lookback_days)
        bars = sum(stats["trend"].get(d, (0, 0))[1] for d in lookback_days)
        if not bars:
            continue
        pnl = sum(stats["syn_pnl"].get(d, 0.0) for d in lookback_days)
        n = sum(stats["syn_n"].get(d, 0) for d in lookback_days)
        wins = sum(stats["syn_wins"].get(d, 0) for d in lookback_days)
        trend_on[sym] = on / bars
        proxy_total[sym] = pnl
        hit_rate[sym] = wins / n if n >= FIT_MIN_TRADES else 0.0
    if not proxy_total:
        return {}
    r_pnl, r_hit, r_trend = pct_ranks(proxy_total), pct_ranks(hit_rate), pct_ranks(trend_on)
    return {s: 0.5 * r_pnl[s] + 0.25 * r_hit[s] + 0.25 * r_trend[s] for s in proxy_total}


# --------------------------------------------------------------------------- #
# Evaluate phase
# --------------------------------------------------------------------------- #
RULES = ("ATH (deployed)", "ATH + gates", "BOLLINGER_FIT", "HYBRID")
TIERS = ("synthetic", "option")


def load_results(root: Path) -> dict:
    out = {}
    for f in sorted((root / "trades").glob("*.json")):
        r = json.loads(f.read_text())
        out[r["symbol"]] = r
    return out


def week_key(d: date) -> tuple[int, int]:
    return d.isocalendar()[:2]


def evaluate(root: Path) -> dict:
    results = load_results(root)
    dailies = {f.stem: prepare_daily(json.loads(f.read_text())) for f in (root / "daily").glob("*.json")}
    universe = sorted(s for s in results if s in dailies)
    print(f"[evaluate] {len(universe)} symbols with both intraday results and daily data", flush=True)

    # Per-symbol per-day aggregates, computed once.
    day_stats = {}
    trading_days = set()
    for sym in universe:
        r = results[sym]
        trend = defaultdict(lambda: [0, 0])
        for t, flag in r["trend_on"]:
            d = datetime.fromtimestamp(t, tz=IST).date()
            trend[d][0] += flag
            trend[d][1] += 1
        trading_days.update(trend)
        syn_pnl, syn_n, syn_wins = defaultdict(float), defaultdict(int), defaultdict(int)
        for tr in r["synthetic"]:
            d = date.fromisoformat(tr["entry_date"])
            syn_pnl[d] += tr["pnl"]
            syn_n[d] += 1
            syn_wins[d] += tr["pnl"] > 0
        day_stats[sym] = {"trend": {d: tuple(v) for d, v in trend.items()},
                          "syn_pnl": syn_pnl, "syn_n": syn_n, "syn_wins": syn_wins}
    trading_days = sorted(trading_days)
    option_from = max((date.fromisoformat(r["option_from"]) for r in results.values() if "option_from" in r),
                      default=None)

    # (tier, week) -> symbol -> [pnl, trades, wins]
    cell = {tier: defaultdict(lambda: defaultdict(lambda: [0.0, 0, 0])) for tier in TIERS}
    for sym in universe:
        for tier in TIERS:
            for tr in results[sym].get(tier, []):
                c = cell[tier][week_key(date.fromisoformat(tr["entry_date"]))][sym]
                c[0] += tr["pnl"]
                c[1] += 1
                c[2] += tr["pnl"] > 0

    by_week = defaultdict(list)
    for d in trading_days:
        by_week[week_key(d)].append(d)
    wkeys = sorted(by_week)

    rng = random.Random(RANDOM_SEED)
    weeks = []
    for prev, cur in zip(wkeys, wkeys[1:]):
        as_of = by_week[prev][-1]
        lookback = [d for d in trading_days if d <= as_of][-FIT_LOOKBACK_TRADING_DAYS:]
        first_syn = min((date.fromisoformat(results[s]["synthetic"][0]["entry_date"])
                         for s in universe if results[s]["synthetic"]), default=None)
        if len(lookback) < FIT_LOOKBACK_TRADING_DAYS or first_syn is None or lookback[0] < first_syn:
            continue
        ath = ath_scores({s: dailies[s] for s in universe}, as_of)
        gates = {s: gate_check(dailies[s], as_of) for s in universe}
        gated = [s for s in universe if gates[s][0]]
        ath_rank = sorted(ath, key=lambda s: ath[s], reverse=True)
        fit = fit_scores(day_stats, gated, lookback)
        picks = {
            "ATH (deployed)": ath_rank[:TOP_N],
            "ATH + gates": [s for s in ath_rank if gates[s][0]][:TOP_N],
            "BOLLINGER_FIT": sorted(fit, key=lambda s: fit[s], reverse=True)[:TOP_N],
            "HYBRID": sorted([s for s in ath_rank[:HYBRID_ATH_POOL] if s in fit],
                             key=lambda s: fit[s], reverse=True)[:TOP_N],
        }
        days = by_week[cur]
        weeks.append({
            "key": cur, "as_of": as_of, "days": days, "picks": picks,
            "random": [rng.sample(universe, min(TOP_N, len(universe))) for _ in range(RANDOM_DRAWS)],
            "gate_fail": sorted(s for s in picks["ATH (deployed)"] if not gates[s][0]),
            "option_covered": option_from is not None and days[-1] >= option_from,
            "option_partial": option_from is not None and days[0] < option_from <= days[-1],
        })
        print(f"[evaluate] week of {days[0]} selected (as of {as_of})", flush=True)

    report = {"universe_size": len(universe), "option_from": option_from.isoformat() if option_from else None,
              "tiers": {}}
    for tier in TIERS:
        tw = [w for w in weeks if tier == "synthetic" or w["option_covered"]]
        rows = {}
        for name in RULES:
            weekly, trades, wins = [], 0, 0
            for w in tw:
                cells = [cell[tier][w["key"]][s] for s in w["picks"][name]]
                weekly.append(sum(c[0] for c in cells))
                trades += sum(c[1] for c in cells)
                wins += sum(c[2] for c in cells)
            rows[name] = {"weekly": weekly, "total": sum(weekly), "trades": trades,
                          "win_rate": wins / trades * 100 if trades else 0.0,
                          "per_trade": sum(weekly) / trades if trades else 0.0,
                          "positive_weeks": sum(1 for x in weekly if x > 0),
                          "worst_week": min(weekly) if weekly else 0.0}
        draw_weekly = [[sum(cell[tier][w["key"]][s][0] for s in w["random"][k]) for w in tw]
                       for k in range(RANDOM_DRAWS)]
        draw_totals = sorted(sum(x) for x in draw_weekly)
        for row in rows.values():
            row["random_percentile"] = (sum(1 for x in draw_totals if x < row["total"]) / len(draw_totals) * 100
                                        if draw_totals and tw else None)
        rows["RANDOM (median of 1000)"] = {
            "weekly": [statistics.median(dw[i] for dw in draw_weekly) for i in range(len(tw))],
            "total": draw_totals[len(draw_totals) // 2] if tw else 0.0,
            "p10": draw_totals[len(draw_totals) // 10] if tw else 0.0,
            "p90": draw_totals[len(draw_totals) * 9 // 10] if tw else 0.0,
        }
        report["tiers"][tier] = {
            "weeks": [{"week_start": w["days"][0].isoformat(), "as_of": w["as_of"].isoformat(),
                       "partial": tier == "option" and w["option_partial"]} for w in tw],
            "rows": rows,
        }
    report["picks"] = [{"week_start": w["days"][0].isoformat(), "as_of": w["as_of"].isoformat(),
                        "ath_failing_gates": w["gate_fail"], **w["picks"]} for w in weeks]

    pairs = [(cell["synthetic"][w["key"]][s][0], cell["option"][w["key"]][s][0])
             for w in weeks if w["option_covered"] and not w["option_partial"]
             for s in universe if "option" in results[s]]
    if len(pairs) > 3:
        xs, ys = zip(*pairs)
        report["synthetic_vs_option_corr"] = statistics.correlation(xs, ys)
        report["synthetic_vs_option_n"] = len(pairs)

    lag_rows = {}
    for sym, r in results.items():
        if "option_nolag" in r:
            lag_rows[sym] = {"lag_fixed": sum(t["pnl"] for t in r["option"]),
                             "original_timing": sum(t["pnl"] for t in r["option_nolag"]),
                             "trades_fixed": len(r["option"]), "trades_original": len(r["option_nolag"])}
    report["lag_comparison"] = lag_rows
    return report


def print_report(report: dict) -> None:
    print(f"\nUniverse: {report['universe_size']} symbols | option tier from {report['option_from']}")
    for tier, data in report["tiers"].items():
        print(f"\n=== {tier.upper()} TIER - {len(data['weeks'])} walk-forward weeks ===")
        print(f"{'Rule':<26}{'Total':>12}{'Trades':>8}{'Win%':>7}{'Rs/trade':>10}{'+weeks':>8}{'Worst wk':>11}{'vs random':>11}")
        for name, row in data["rows"].items():
            if name.startswith("RANDOM"):
                print(f"{name:<26}{row['total']:>12,.0f}   (p10 {row['p10']:,.0f} / p90 {row['p90']:,.0f})")
                continue
            print(f"{name:<26}{row['total']:>12,.0f}{row['trades']:>8}{row['win_rate']:>6.1f}%"
                  f"{row['per_trade']:>10,.0f}{row['positive_weeks']:>5}/{len(row['weekly']):<2}"
                  f"{row['worst_week']:>11,.0f}{row['random_percentile']:>9.0f}th")
    if "synthetic_vs_option_corr" in report:
        print(f"\nSynthetic vs real-option P&L correlation: {report['synthetic_vs_option_corr']:.2f} "
              f"over {report['synthetic_vs_option_n']} symbol-weeks")
    if report["lag_comparison"]:
        fixed = sum(r["lag_fixed"] for r in report["lag_comparison"].values())
        orig = sum(r["original_timing"] for r in report["lag_comparison"].values())
        print(f"\nEntry-timing bias ({len(report['lag_comparison'])} symbols, option tier): "
              f"original timing Rs {orig:,.0f} -> live-faithful timing Rs {fixed:,.0f}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["daily", "intraday", "evaluate"])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--compare-lag", action="store_true")
    args = ap.parse_args()
    root = tag_root(args.tag)

    if args.phase == "evaluate":
        report = evaluate(root)
        (root / "report.json").write_text(json.dumps(report, indent=2, default=str))
        print_report(report)
        return

    authenticate()
    symbols = args.symbols.split(",") if args.symbols else fetch_fno_universe()
    if args.phase == "daily":
        ok = 0
        for i, s in enumerate(symbols):
            if fetch_daily_cached(root, s):
                ok += 1
            if (i + 1) % 25 == 0:
                print(f"[daily] {i + 1}/{len(symbols)}", flush=True)
        print(f"[daily] done: {ok}/{len(symbols)} symbols cached", flush=True)
        return

    option_index = build_option_index()
    for i, s in enumerate(symbols):
        t0 = time.time()
        try:
            r = run_symbol(root, s, option_index, args.compare_lag)
        except Exception as e:  # noqa: BLE001
            print(f"[intraday] {i + 1}/{len(symbols)} {s}: FAILED {e!r}", flush=True)
            continue
        if r is None:
            print(f"[intraday] {i + 1}/{len(symbols)} {s}: no data", flush=True)
            continue
        opt = r.get("option", [])
        print(f"[intraday] {i + 1}/{len(symbols)} {s}: fires={r['fires']} synthetic={len(r['synthetic'])} "
              f"option={len(opt)} (Rs {sum(t['pnl'] for t in opt):,.0f}) {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
