"""
User request (29 Sep 2026): backtest the paper-only Bollinger Hold-Long
strategy over the last 30 days, once with MAX_LOSS_PROTECTION_RS = 3000
and once with 4500, and report PnL. Follow-up the same evening: also run a
"both sides" variant that additionally takes BEARISH triggers (buy ATM PE,
resting sell-stop: fills when a 1-min LOW touches the trigger, underlying
fill = min(trigger, 1-min open)) - its own SIDES variant, reported next to
the deployed long-only rules, never merged into them.

Rules mirrored from the live engine (Bollinger/trading_engine.py HOLD_LONG
profile + Bollinger/config.py HOLD_LONG_* block, as of 29 Sep 2026):
  - Signal: the deployed BB-ribbon + Vortex pending-order state machine on
    continuous 5-min underlying bars (bollinger_research.fires_with_
    snapshots, the validated port of Bollinger/signals.py).
  - Entry: BULLISH only (buy ATM CE), resting order - the pending trigger
    armed at the close of 5-min bar i-1 (same trading day) fills the moment
    a 1-min bar inside bar i touches it; underlying fill = max(trigger,
    1-min open). One open position per symbol, no capacity cap, no funds
    check (HOLD_LONG has neither).
  - Contract: ATM CE at the fill. Nearest expiry after today (an expiring-
    today contract is never bought); NSE rolls to the next expiry when it is
    within HOLD_LONG_ROLL_EXPIRY_WITHIN_TRADING_DAYS (2) trading days; MCX
    never rolls. Rs 5 minimum premium for NSE (MCX exempt).
  - Exit: MAX_LOSS_HIT when (entry - option price) * multiplier >= the cap
    (checked on each 1-min option LOW, filled at the cap level or the
    minute's open if it gapped through); otherwise DAILY_SQUARE_OFF at
    15:15 for NSE. MCX is exempt from the daily square-off and only closes
    at the Friday 23:25 square-off. No entries at/after 15:15 (NSE).

Option prices (real 1-min option OHLC, never synthetic):
  - Contracts still listed in Dhan's instrument master (NSE stock monthly
    29 SEP / 27 OCT, BANKNIFTY monthly, NIFTY 29 SEP / 06 OCT weeklies,
    MCX Oct+) - fetched directly by security_id.
  - Expired NIFTY weeklies - Dhan's /charts/rollingoption (expiry_code 1 =
    nearest weekly, 2 = next; validated 29 Sep against the listed 29 SEP /
    06 OCT contracts, within Rs 0.30), rebuilt for the fixed entry strike
    by picking, minute by minute, the ATM-offset series whose strike equals
    it.
  - Expired MCX options (Sep series) - not fetchable anywhere; those
    entries are counted as skipped and reported, never guessed.
The entry premium is the entry minute's option close adjusted by
0.5 * (underlying fill - underlying minute close).

PnL is reported raw and "modeled" (Bollinger/paper_book.modeled_slippage_pct
on both legs - the same number the live paper book logs as pnl_modeled).

Run (after 15:30 IST - shares the account's DH-904 budget with the live bot):
    HANDOFF_DHAN_ACCESS_TOKEN=... uv run python backtest_bollinger_hold_long_30day.py
Output: history/bt_bollinger_hold_long_30day/{trades.csv,summary.json}
"""
from __future__ import annotations

import bisect
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import walkforward_selector_eval as wf
from walkforward_selector_eval import IST, bcfg, bt, dhan_wrapper, _retry
from bollinger_research import fires_with_snapshots
from Bollinger.paper_book import modeled_slippage_pct

REPO_ROOT = Path(__file__).resolve().parent
OUT = REPO_ROOT / "history" / "bt_bollinger_hold_long_30day"
WINDOW_FROM = date(2026, 8, 31)
WINDOW_TO = date(2026, 9, 29)
MAX_LOSS_VARIANTS = (3000.0, 4500.0)
SIDES_VARIANTS = ("long", "both")  # long = deployed Hold-Long rules
UNDERLYING_DAYS = 85
OPTION_DAYS = 45
ROLL_DAYS = bcfg.HOLD_LONG_ROLL_EXPIRY_WITHIN_TRADING_DAYS
SQUARE_OFF = datetime.strptime(bcfg.HOLD_LONG_DAILY_SQUARE_OFF_TIME, "%H:%M").time()
MCX_FRIDAY_SQUARE_OFF = datetime.strptime(bcfg.MCX_FRIDAY_SQUARE_OFF_TIME, "%H:%M").time()
DELTA = 0.5
PACE = 1.3

WATCHLIST = ["NIFTY", "BANKNIFTY", "ZYDUSLIFE", "SONACOMS", "DIVISLAB", "AUROPHARMA", "MOTHERSON", "APOLLOHOSP",
             "APLAPOLLO", "MCX", "BOSCHLTD", "LAURUSLABS", "OBEROIRLTY", "RBLBANK", "RADICO", "PHOENIXLTD",
             "MOTILALOFS", "COPPER", "NATURALGAS"]
INDEX = {"NIFTY": ("13", "NIFTY-", "WEEK"), "BANKNIFTY": ("25", "BANKNIFTY-", "MONTH")}
MCX_MULTIPLIER = {}
for _line in (REPO_ROOT / "data" / "mcx_config").read_text().splitlines():
    _p = _line.strip().split(",")
    if len(_p) >= 3 and not _p[0].startswith("#") and _p[2].strip():
        MCX_MULTIPLIER[_p[0].strip().upper()] = int(_p[2])


def _load(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _series(data: dict) -> dict:
    return {"opens": data.get("open") or [], "highs": data.get("high") or [], "lows": data.get("low") or [],
            "closes": data.get("close") or [], "timestamps": [int(t) for t in data.get("timestamp") or []]}


def fetch_continuous(cache: Path, sid: str, seg: str, inst: str, interval: int, days: int) -> dict:
    cached = _load(cache)
    if cached is not None:
        return cached
    data = {}
    for attempt in range(4):
        data = dhan_wrapper.fetch_continuous_intraday(sid, seg, inst, interval, lookback_days_override=days)
        if data.get("close"):
            break
        time.sleep(4 * (attempt + 1))
    result = _series(data)
    if result["closes"]:
        _save(cache, result)
    time.sleep(PACE)
    return result


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #
def trading_days_to_expiry(expiry: date, today: date) -> int:
    days, d = 0, today
    while d < expiry:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def listed_options(sym: str):
    df = dhan_wrapper.instruments()
    if sym in INDEX:
        rows = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTIDX")
                  & df["SEM_TRADING_SYMBOL"].astype(str).str.startswith(INDEX[sym][1])]
    elif sym in MCX_MULTIPLIER:
        rows = df[(df["SEM_EXM_EXCH_ID"] == "MCX") & (df["SEM_INSTRUMENT_NAME"] == "OPTFUT")
                  & df["SEM_TRADING_SYMBOL"].astype(str).str.startswith(sym + "-")]
    else:
        opts = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")]
        rows = opts[opts["SEM_TRADING_SYMBOL"].map(
            lambda s: dhan_wrapper._underlying_from_trading_symbol(str(s)) == sym)]
    rows = rows[rows["SEM_OPTION_TYPE"].isin(("CE", "PE"))].copy()
    rows["expiry"] = rows["SEM_EXPIRY_DATE"].map(lambda s: datetime.strptime(str(s)[:10], "%Y-%m-%d").date())
    return rows


def expiry_schedule(sym: str, listed) -> list[date]:
    """Every expiry the window can touch - listed ones plus the expired NSE
    ones (last Tuesday monthly; every Tuesday for NIFTY). Expired MCX expiries
    are unknown and deliberately absent (see resolve_contract)."""
    exps = set(listed["expiry"])
    if sym in MCX_MULTIPLIER:
        return sorted(exps)
    d = WINDOW_FROM - timedelta(days=10)
    while d <= WINDOW_TO:
        if sym == "NIFTY" and d.weekday() == 1:
            exps.add(d)
        if sym != "NIFTY" and d == wf.last_tuesday(d.year, d.month):
            exps.add(d)
        d += timedelta(days=1)
    return sorted(exps)


def resolve_contract(sym: str, day: date, spot: float, listed, schedule: list[date], opt_type: str):
    """(expiry, strike, security_id or None, lot/multiplier, rolling expiry_code or None) - or a skip reason."""
    later = [e for e in schedule if e > day]
    if not later:
        return "no_expiry"
    is_mcx = sym in MCX_MULTIPLIER
    # MCX: the front contract on `day` was an already-expired (delisted) Sep
    # series if the earliest still-listed expiry is more than ~a month out.
    if is_mcx and (min(listed["expiry"]) - day).days > 30:
        return "mcx_front_expired_unfetchable"
    idx = 0
    if not is_mcx and trading_days_to_expiry(later[0], day) <= ROLL_DAYS and len(later) > 1:
        idx = 1
    expiry = later[idx]
    rows = listed[(listed["expiry"] == expiry) & (listed["SEM_OPTION_TYPE"] == opt_type)]
    if not rows.empty:
        rows = rows.assign(dist=(rows["SEM_STRIKE_PRICE"] - spot).abs()).sort_values("dist")
        r = rows.iloc[0]
        lot = MCX_MULTIPLIER[sym] if is_mcx else int(float(r["SEM_LOT_UNITS"]))
        return (expiry, float(r["SEM_STRIKE_PRICE"]), str(int(r["SEM_SMST_SECURITY_ID"])), lot, None)
    if sym != "NIFTY":
        return "expired_contract_unfetchable"
    # Expired NIFTY weekly: strike from the rolling ATM series, priced below.
    # expiry_code counts from the contract expiring TODAY on an expiry day
    # (verified 29 Sep: code 1 on a Tuesday = that day's weekly).
    near = [e for e in schedule if e >= day]
    code = near.index(expiry) + 1
    step = 50.0
    lot = int(float(listed.iloc[0]["SEM_LOT_UNITS"]))
    return (expiry, round(spot / step) * step, None, lot, code)


# --------------------------------------------------------------------------- #
# Option price series
# --------------------------------------------------------------------------- #
def option_series_listed(sym: str, sid: str) -> dict:
    seg, inst = (("MCX_COMM", "OPTFUT") if sym in MCX_MULTIPLIER
                 else ("NSE_FNO", "OPTIDX" if sym in INDEX else "OPTSTK"))
    return fetch_continuous(OUT / sym.lower() / f"OPT_{sid}_1min.json", sid, seg, inst, 1, OPTION_DAYS)


def _rolling(sym: str, day: date, code: int, offset: int, opt_type: str = "CE") -> dict:
    tag = "ATM" if offset == 0 else f"ATM{offset:+d}"
    suffix = "" if opt_type == "CE" else "_PE"
    cache = OUT / sym.lower() / "rolling" / f"{day}_c{code}_{tag}{suffix}.json"
    cached = _load(cache)
    if cached is not None:
        return cached
    sid, _, flag = INDEX[sym]
    resp = _retry(dhan_wrapper.client.Dhan.expired_options_data, security_id=sid, exchange_segment="NSE_FNO",
                  instrument_type="OPTIDX", expiry_flag=flag, expiry_code=code, strike=tag,
                  drv_option_type="CALL" if opt_type == "CE" else "PUT",
                  required_data=["open", "high", "low", "close", "strike"], from_date=day.isoformat(),
                  to_date=(day + timedelta(days=1)).isoformat(), interval=1)
    ce = (((resp or {}).get("data") or {}).get("data") or {}).get(opt_type.lower()) or {}
    result = {**_series(ce), "strikes": ce.get("strike") or []}
    if result["closes"]:
        _save(cache, result)
    time.sleep(PACE)
    return result


def option_series_rolling(sym: str, day: date, code: int, strike: float, opt_type: str = "CE") -> dict:
    """Fixed-strike 1-min series for one day, stitched from ATM-offset series.
    The direction of "ATM+1" is read off the data (higher strike for CALL;
    not assumed for PUT) before choosing which offsets to fetch."""
    atm = _rolling(sym, day, code, 0, opt_type)
    if not atm["closes"]:
        return {"opens": [], "highs": [], "lows": [], "closes": [], "timestamps": []}
    step = 50.0
    direction = 1
    if opt_type != "CE":
        up = _rolling(sym, day, code, 1, opt_type)
        if up["strikes"] and atm["strikes"] and up["strikes"][0] < atm["strikes"][0]:
            direction = -1
    offsets = sorted({direction * int(round((strike - s) / step)) for s in atm["strikes"] if s})
    rows = {}
    for off in offsets:
        s = atm if off == 0 else _rolling(sym, day, code, off, opt_type)
        for i, t in enumerate(s["timestamps"]):
            if s["strikes"][i] == strike:
                rows[t] = (s["opens"][i], s["highs"][i], s["lows"][i], s["closes"][i])
    ts = sorted(rows)
    return {"timestamps": ts, "opens": [rows[t][0] for t in ts], "highs": [rows[t][1] for t in ts],
            "lows": [rows[t][2] for t in ts], "closes": [rows[t][3] for t in ts]}


class Bars:
    def __init__(self, d: dict):
        self.ts, self.o, self.h, self.l, self.c = (d["timestamps"], d["opens"] or d["closes"],
                                                   d["highs"] or d["closes"], d["lows"] or d["closes"], d["closes"])
        self.pos = {t: i for i, t in enumerate(self.ts)}

    def at_or_before(self, t):
        i = bisect.bisect_right(self.ts, t) - 1
        return i if i >= 0 else None


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
def load_underlying(sym: str) -> tuple[dict, dict] | None:
    d = OUT / sym.lower()
    if sym in INDEX:
        sid, seg, inst = INDEX[sym][0], "IDX_I", "INDEX"
    elif sym in MCX_MULTIPLIER:
        sid, seg, inst = dhan_wrapper.get_mcx_futures_contract(sym).security_id, "MCX_COMM", "FUTCOM"
    else:
        sid, seg, inst = dhan_wrapper._equity_security_id(sym), "NSE_EQ", "EQUITY"
    fast = fetch_continuous(d / f"{sym}_5min.json", str(sid), seg, inst, 5, UNDERLYING_DAYS)
    master = fetch_continuous(d / f"{sym}_1min.json", str(sid), seg, inst, 1, UNDERLYING_DAYS)
    if not fast["closes"] or not master["closes"]:
        return None
    return fast, master


def simulate(sym: str, fast: dict, master: dict, max_loss: float, listed, schedule, skips: dict,
             sides: str = "long") -> list[dict]:
    is_mcx = sym in MCX_MULTIPLIER
    signals = bt.compute_signals(fast)
    _fires, snap = fires_with_snapshots(fast, signals)
    fts = fast["timestamps"]
    m = Bars(master)
    trades, pos, consumed = [], None, set()
    opt_cache: dict = {}
    # The NSE equity 1-min series ends at 15:14 (index/option series run to
    # 15:29), so the 15:15 square-off is taken on each day's LAST underlying
    # bar, priced from the option's own 15:15 minute.
    last_of_day = {}
    for t in m.ts:
        last_of_day[datetime.fromtimestamp(t, IST).date()] = t

    def opt_for(contract, day, opt_type):
        expiry, strike, sid, lot, code = contract
        key = sid or (day, code, strike, opt_type)
        if key not in opt_cache:
            s = option_series_listed(sym, sid) if sid else option_series_rolling(sym, day, code, strike, opt_type)
            opt_cache[key] = Bars(s) if s["closes"] else None
        return opt_cache[key]

    def close(t, px, reason):
        lot = pos["lot"]
        e_mod = pos["p0"] * (1 + modeled_slippage_pct(pos["p0"]))
        x_mod = px * (1 - modeled_slippage_pct(px))
        trades.append({"sides": sides, "variant": int(max_loss), "side": pos["side"], "symbol": sym, "contract": pos["name"], "day": pos["day"].isoformat(),
                       "entry_time": datetime.fromtimestamp(pos["t0"], IST).strftime("%Y-%m-%d %H:%M"),
                       "exit_time": datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d %H:%M"),
                       "entry": round(pos["p0"], 2), "exit": round(px, 2), "qty": lot, "reason": reason,
                       "pnl_raw": round((px - pos["p0"]) * lot, 2), "pnl_modeled": round((x_mod - e_mod) * lot, 2),
                       "hold_min": round((t - pos["t0"]) / 60)})

    for k, t in enumerate(m.ts):
        dt = datetime.fromtimestamp(t, IST)
        d = dt.date()
        if d > WINDOW_TO:
            break

        if pos is not None:
            ob = pos["opt"]
            i = ob.pos.get(t)
            if i is not None and not (not is_mcx and dt.time() >= SQUARE_OFF):
                if ob.l[i] <= pos["p0"] - max_loss / pos["lot"]:
                    close(t, min(pos["p0"] - max_loss / pos["lot"], ob.o[i]), "MAX_LOSS_HIT")
                    pos = None
            if pos is not None and not is_mcx and (dt.time() >= SQUARE_OFF or t == last_of_day[d]):
                sq = int(datetime.combine(d, SQUARE_OFF, IST).timestamp())
                j = ob.pos.get(sq)
                px = ob.o[j] if j is not None else ob.c[ob.at_or_before(sq)]
                close(sq, px, "DAILY_SQUARE_OFF")
                pos = None
            elif pos is not None and is_mcx and dt.weekday() == 4 and dt.time() >= MCX_FRIDAY_SQUARE_OFF:
                j = ob.pos.get(t)
                close(t, ob.o[j] if j is not None else ob.c[ob.at_or_before(t)], "FRIDAY_SQUARE_OFF")
                pos = None

        if d < WINDOW_FROM or pos is not None:
            continue
        # (an entry on the 15:14 last bar would be squared off a minute later)
        if not is_mcx and (dt.time() >= SQUARE_OFF or t == last_of_day[d]):
            continue
        if is_mcx and dt.weekday() == 4 and dt.time() >= MCX_FRIDAY_SQUARE_OFF:
            continue
        bar_i = bisect.bisect_right(fts, t) - 1
        if bar_i < 1 or datetime.fromtimestamp(fts[bar_i - 1], IST).date() != d:
            continue
        p = snap[bar_i - 1]
        if p is None or fts[bar_i - 1] in consumed or (sides == "long" and p[0] != "BULLISH"):
            continue
        bull = p[0] == "BULLISH"
        if (bull and m.h[k] < p[1]) or (not bull and m.l[k] > p[1]):
            continue
        consumed.add(fts[bar_i - 1])
        fill_u = max(p[1], m.o[k]) if bull else min(p[1], m.o[k])
        opt_type = "CE" if bull else "PE"
        contract = resolve_contract(sym, d, fill_u, listed, schedule, opt_type)
        if isinstance(contract, str):
            skips[contract] += 1
            continue
        ob = opt_for(contract, d, opt_type)
        i = ob.at_or_before(t) if ob else None
        if i is None or datetime.fromtimestamp(ob.ts[i], IST).date() != d:
            skips["option_price_missing"] += 1
            continue
        p0 = max(ob.c[i] + (1 if bull else -1) * DELTA * (fill_u - m.c[k]), 0.05)
        if not is_mcx and p0 < bcfg.MIN_ATM_PREMIUM_RS:
            skips["premium_below_5"] += 1
            continue
        expiry, strike, _sid, lot, _code = contract
        pos = {"opt": ob, "p0": p0, "lot": lot, "t0": t, "day": d, "side": "LONG_CE" if bull else "SHORT_PE",
               "name": f"{sym} {expiry:%d %b} {strike:g} {opt_type}"}

    if pos is not None:
        j = pos["opt"].at_or_before(m.ts[-1])
        close(pos["opt"].ts[j], pos["opt"].c[j], "OPEN_AT_WINDOW_END")
    return trades


def summarize(trades: list[dict], key: str = "pnl_modeled") -> dict:
    if not trades:
        return {"n": 0}
    pnl = [t[key] for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    daily = defaultdict(float)
    for t in trades:
        daily[t["day"]] += t[key]
    eq = peak = dd = 0.0
    for d in sorted(daily):
        eq += daily[d]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"n": len(trades), "win_pct": round(len(wins) / len(trades) * 100, 1), "net": round(sum(pnl), 2),
            "net_raw": round(sum(t["pnl_raw"] for t in trades), 2),
            "avg_win": round(statistics.mean(wins), 2) if wins else 0.0,
            "avg_loss": round(statistics.mean(losses), 2) if losses else 0.0,
            "best_day": round(max(daily.values()), 2), "worst_day": round(min(daily.values()), 2),
            "green_days": sum(1 for v in daily.values() if v > 0), "days": len(daily), "max_dd": round(dd, 2)}


def main():
    wf.authenticate()
    symbols = sys.argv[1].split(",") if len(sys.argv) > 1 else WATCHLIST
    all_trades, skips_by_variant = [], {}
    loaded = {}
    for sym in symbols:
        try:
            u = load_underlying(sym)
        except Exception as exc:  # noqa: BLE001
            print(f"[{sym}] underlying fetch FAILED: {exc!r}", flush=True)
            continue
        if u is None:
            print(f"[{sym}] no underlying data", flush=True)
            continue
        listed = listed_options(sym)
        loaded[sym] = (u, listed, expiry_schedule(sym, listed))
        print(f"[{sym}] 5m={len(u[0]['closes'])} 1m={len(u[1]['closes'])} listed expiries="
              f"{sorted({e.isoformat() for e in listed['expiry']})[:3]}", flush=True)

    for sides in SIDES_VARIANTS:
        for ml in MAX_LOSS_VARIANTS:
            skips = defaultdict(int)
            for sym, ((fast, master), listed, schedule) in loaded.items():
                tr = simulate(sym, fast, master, ml, listed, schedule, skips, sides)
                all_trades += tr
                print(f"  [{sides} max_loss {ml:.0f}] {sym}: {len(tr)} trades, "
                      f"modeled Rs {sum(t['pnl_modeled'] for t in tr):,.0f}", flush=True)
            skips_by_variant[f"{sides}_{int(ml)}"] = dict(skips)

    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
        w.writeheader()
        w.writerows(all_trades)
    summary = {}
    for sides in SIDES_VARIANTS:
        for ml in MAX_LOSS_VARIANTS:
            name = f"{sides}_{int(ml)}"
            tr = [t for t in all_trades if t["sides"] == sides and t["variant"] == int(ml)]
            reasons = defaultdict(lambda: [0, 0.0])
            per_sym = defaultdict(lambda: [0, 0.0])
            per_side = defaultdict(lambda: [0, 0.0])
            daily = defaultdict(lambda: {"n": 0, "long": 0.0, "short": 0.0, "total": 0.0, "raw": 0.0})
            for t in tr:
                for bucket, key in ((reasons, t["reason"]), (per_sym, t["symbol"]), (per_side, t["side"])):
                    bucket[key][0] += 1
                    bucket[key][1] += t["pnl_modeled"]
                row = daily[t["day"]]
                row["n"] += 1
                row["long" if t["side"] == "LONG_CE" else "short"] += t["pnl_modeled"]
                row["total"] += t["pnl_modeled"]
                row["raw"] += t["pnl_raw"]
            summary[name] = {
                "overall": summarize(tr),
                "by_side": {k: {**summarize([t for t in tr if t["side"] == k])} for k in per_side},
                "by_reason": {k: [n, round(p, 2)] for k, (n, p) in reasons.items()},
                "by_symbol": {k: [n, round(p, 2)] for k, (n, p) in per_sym.items()},
                "daily": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in sorted(daily.items())},
                "skipped_entries": skips_by_variant[name]}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    for name, v in summary.items():
        print(f"\n=== {name}: {v['overall']}")
        print(f"    by side: {v['by_side']}")
        print(f"    skipped: {v['skipped_entries']}")
        print(f"    {'day':<12}{'trades':>7}{'long':>11}{'short':>11}{'total':>11}{'cumulative':>12}")
        cum = 0.0
        for d, row in v["daily"].items():
            cum += row["total"]
            print(f"    {d:<12}{row['n']:>7}{row['long']:>11,.0f}{row['short']:>11,.0f}{row['total']:>11,.0f}{cum:>12,.0f}")


if __name__ == "__main__":
    main()
