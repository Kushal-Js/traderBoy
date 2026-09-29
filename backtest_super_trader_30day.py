"""
Super Trader 30-day backtest (30 Sep 2026, user request) - rules live in
SuperTrader/strategy.py (the same functions a live engine would call).

Window: entries 31 Aug - 29 Sep 2026, the whole NSE F&O stock universe
(fno_ath_screener.fetch_fno_universe, every NIFTY 50 stock included).

Honest-timing rules (see trading-skills learnings/bollinger-backtest-
lookahead-bias-entry-timing.md and backtest-methodology.md):
  - signals and underlying exits are read on CLOSED 5-min bars; the order is
    priced at the option's 1-min OPEN of the next minute (bar close time);
  - MAX_LOSS_HIT is checked on every option 1-min LOW in between (filled at
    the cap level, or the minute's open if it gapped through);
  - 15:15 square-off priced at the option's own 15:15 minute (Dhan's equity
    5-min series stops at the 15:10 bar - its close IS 15:15);
  - portfolio-level: at most `max_concurrent` open trades across all
    stocks; simultaneous signals ranked by score; a signal that finds no
    free slot is skipped; max 2 entries per stock per day;
  - slippage on both legs with the live paper-book model (modeled PnL);
    brokerage/taxes NOT included.

Option prices are REAL 1-min prices, never synthetic:
  - days up to 28 Sep: Dhan /charts/rollingoption (expired contracts), per
    stock per day, rebuilt for the fixed entry strike from ATM-offset series
    (expiry_code 1 = nearest monthly incl. an expiry day's own contract,
    2 = next; verified 30 Sep: RBLBANK next-month ATM == listed 27 OCT, Rs 16.00);
  - 29 Sep (rolling data not published yet): the listed 27 OCT contracts.
  Contract = ATM at entry, nearest monthly expiry, rolled to the next one
  within 2 trading days of expiry (same rule as Super Bollinger).

Run (after 15:30 IST or before 09:15 - shares the account's data budget):
  HANDOFF_DHAN_ACCESS_TOKEN=... uv run python backtest_super_trader_30day.py fetch
  ... backtest_super_trader_30day.py signals
  ... backtest_super_trader_30day.py run [exit_mode ...]
Output: history/bt_super_trader_30day/
"""
from __future__ import annotations

import bisect
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import walkforward_selector_eval as wf
from walkforward_selector_eval import IST, _retry, dhan_wrapper
from fno_ath_screener import fetch_fno_universe
from Bollinger.paper_book import modeled_slippage_pct
from SuperTrader.strategy import Params, compute_features, entry_signal, exit_signal

OUT = Path(__file__).resolve().parent / "history" / "bt_super_trader_30day"
WINDOW_FROM, WINDOW_TO = date(2026, 8, 31), date(2026, 9, 29)
ROLLING_LAST_DAY = date(2026, 9, 28)
EXPIRIES = [date(2026, 8, 25), date(2026, 9, 29), date(2026, 10, 27)]
ROLL_DAYS = 2
UNDERLYING_DAYS = 75
MAX_OFFSET = 10
PACE = 0.4
HALF_SPLIT = "2026-09-15"


def _load(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def _save(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


# --------------------------------------------------------------------------- #
# Underlying 5-min bars
# --------------------------------------------------------------------------- #
def underlying(sym: str) -> dict | None:
    cache = OUT / "underlying" / f"{sym}_5min.json"
    cached = _load(cache)
    if cached is not None:
        return cached if cached.get("closes") else None
    try:
        sid = dhan_wrapper._equity_security_id(sym)
    except Exception:  # noqa: BLE001
        return None
    data = {}
    for attempt in range(3):
        data = dhan_wrapper.fetch_continuous_intraday(sid, "NSE_EQ", "EQUITY", 5, lookback_days_override=UNDERLYING_DAYS)
        if data.get("close"):
            break
        time.sleep(3 * (attempt + 1))
    bars = {"timestamps": data.get("timestamp") or [], "opens": data.get("open") or [], "highs": data.get("high") or [],
            "lows": data.get("low") or [], "closes": data.get("close") or [], "volumes": data.get("volume") or []}
    _save(cache, bars)
    time.sleep(PACE)
    return bars if bars["closes"] else None


# --------------------------------------------------------------------------- #
# Option prices
# --------------------------------------------------------------------------- #
def trading_days_to_expiry(expiry: date, today: date) -> int:
    days, d = 0, today
    while d < expiry:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def contract_expiry(day: date) -> tuple[date, int]:
    """(expiry, rolling expiry_code) - nearest monthly expiry >= day, rolled
    within ROLL_DAYS trading days; code counts from expiries >= day."""
    ahead = [e for e in EXPIRIES if e >= day]
    idx = 1 if trading_days_to_expiry(ahead[0], day) <= ROLL_DAYS else 0
    return ahead[idx], idx + 1


class Minutes:
    def __init__(self, ts, o, h, l, c):
        self.ts, self.o, self.h, self.l, self.c = ts, o, h, l, c
        self.pos = {t: i for i, t in enumerate(ts)}

    def price_at(self, t: int, max_back: int = 300):
        """Open of minute t, else the last close within max_back seconds."""
        i = self.pos.get(t)
        if i is not None:
            return self.o[i]
        j = bisect.bisect_right(self.ts, t) - 1
        if j >= 0 and t - self.ts[j] <= max_back:
            return self.c[j]
        return None


class OptionPricer:
    def __init__(self):
        self.calls = 0
        self._lots: dict[str, int] = {}
        self._listed = None

    # ---- rolling (expired) ----
    def _rolling(self, sym: str, day: date, code: int, ot: str, offset: int) -> dict:
        tag = "ATM" if offset == 0 else f"ATM{offset:+d}"
        cache = OUT / "options" / sym / f"{day}_c{code}_{ot}_{tag}.json"
        cached = _load(cache)
        if cached is not None:
            return cached
        sid = dhan_wrapper._equity_security_id(sym)
        try:
            resp = _retry(dhan_wrapper.client.Dhan.expired_options_data, security_id=sid, exchange_segment="NSE_FNO",
                          instrument_type="OPTSTK", expiry_flag="MONTH", expiry_code=code, strike=tag,
                          drv_option_type="CALL" if ot == "CE" else "PUT",
                          required_data=["open", "high", "low", "close", "strike", "spot"],
                          from_date=day.isoformat(), to_date=(day + timedelta(days=1)).isoformat(), interval=1)
            leg = (((resp or {}).get("data") or {}).get("data") or {}).get(ot.lower()) or {}
        except Exception as exc:  # noqa: BLE001
            print(f"    rolling fetch failed {sym} {day} c{code} {ot} {tag}: {exc!r}", flush=True)
            leg = {}
        self.calls += 1
        out = {"timestamps": [int(t) for t in leg.get("timestamp") or []], "opens": leg.get("open") or [],
               "highs": leg.get("high") or [], "lows": leg.get("low") or [], "closes": leg.get("close") or [],
               "strikes": leg.get("strike") or [], "spots": leg.get("spot") or []}
        _save(cache, out)
        time.sleep(PACE)
        return out

    def rolling_fixed_strike(self, sym: str, day: date, code: int, ot: str, entry_t: int, until_t: int):
        """(strike, Minutes) for the ATM strike at entry_t, held fixed."""
        atm = self._rolling(sym, day, code, ot, 0)
        if not atm["timestamps"]:
            return None, None
        j = bisect.bisect_right(atm["timestamps"], entry_t) - 1
        if j < 0 or entry_t - atm["timestamps"][j] > 600:
            return None, None
        strike = atm["strikes"][j]
        rows = {}

        def take(s):
            for i, t in enumerate(s["timestamps"]):
                if s["strikes"][i] == strike and t not in rows:
                    rows[t] = (s["opens"][i], s["highs"][i], s["lows"][i], s["closes"][i])

        take(atm)
        need = [(t, s) for t, s in zip(atm["timestamps"], atm["strikes"]) if entry_t <= t <= until_t and s != strike]
        if need:
            up = self._rolling(sym, day, code, ot, 1)
            direction = 1
            if up["strikes"] and atm["strikes"] and up["strikes"][0] < atm["strikes"][0]:
                direction = -1
            take(up)
            above = any(s > strike for _t, s in need)   # ATM moved up -> our strike is now below ATM
            below = any(s < strike for _t, s in need)
            for sign, wanted in ((-1, above), (1, below)):
                if not wanted:
                    continue
                for k in range(1, MAX_OFFSET + 1):
                    missing = [t for t, s in need if t not in rows and ((s > strike) if sign == -1 else (s < strike))]
                    if not missing:
                        break
                    off = sign * k * direction
                    if off == 1:
                        continue  # already have ATM+1
                    take(self._rolling(sym, day, code, ot, off))
        ts = sorted(rows)
        return strike, Minutes(ts, [rows[t][0] for t in ts], [rows[t][1] for t in ts], [rows[t][2] for t in ts],
                               [rows[t][3] for t in ts])

    # ---- listed (29 Sep) ----
    def listed_contract(self, sym: str, expiry: date, ot: str, spot: float):
        df = dhan_wrapper.instruments()
        if self._listed is None:
            self._listed = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")]
        rows = self._listed[(self._listed["SEM_TRADING_SYMBOL"].astype(str).str.startswith(sym + "-"))
                            & (self._listed["SEM_EXPIRY_DATE"].astype(str).str.startswith(expiry.isoformat()))
                            & (self._listed["SEM_OPTION_TYPE"] == ot)]
        if rows.empty:
            return None, None
        rows = rows.assign(dist=(rows["SEM_STRIKE_PRICE"] - spot).abs()).sort_values("dist")
        r = rows.iloc[0]
        sid = str(int(r["SEM_SMST_SECURITY_ID"]))
        cache = OUT / "options" / sym / f"listed_{sid}.json"
        data = _load(cache)
        if data is None:
            resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data, security_id=sid, exchange_segment="NSE_FNO",
                          instrument_type="OPTSTK", from_date="2026-09-29", to_date="2026-09-30", interval=1)
            d = (resp or {}).get("data") or {}
            data = {"timestamps": [int(t) for t in d.get("timestamp") or []], "opens": d.get("open") or [],
                    "highs": d.get("high") or [], "lows": d.get("low") or [], "closes": d.get("close") or []}
            self.calls += 1
            _save(cache, data)
            time.sleep(PACE)
        return float(r["SEM_STRIKE_PRICE"]), Minutes(data["timestamps"], data["opens"], data["highs"], data["lows"],
                                                     data["closes"])

    def lot(self, sym: str) -> int | None:
        if sym not in self._lots:
            df = dhan_wrapper.instruments()
            rows = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")
                      & (df["SEM_TRADING_SYMBOL"].astype(str).str.startswith(sym + "-"))]
            self._lots[sym] = int(float(rows.iloc[0]["SEM_LOT_UNITS"])) if not rows.empty else None
        return self._lots[sym]

    def open_leg(self, sym: str, day: date, side: str, entry_t: int, until_t: int, spot: float):
        ot = "CE" if side == "LONG" else "PE"
        expiry, code = contract_expiry(day)
        if day <= ROLLING_LAST_DAY:
            strike, mins = self.rolling_fixed_strike(sym, day, code, ot, entry_t, until_t)
        else:
            strike, mins = self.listed_contract(sym, expiry, ot, spot)
        if mins is None or not mins.ts:
            return None
        return {"ot": ot, "expiry": expiry, "strike": strike, "mins": mins}


# --------------------------------------------------------------------------- #
# Portfolio simulation
# --------------------------------------------------------------------------- #
def planned_exit_t(f, i_entry_bar: int, side: str, stop: float, p: Params) -> int:
    """Underlying-only exit time for a trade entered after bar i_entry_bar
    closed (for knowing how far to fetch option prices)."""
    peak = f.h[i_entry_bar] if side == "LONG" else f.l[i_entry_bar]
    d = f.day[i_entry_bar]
    sq = int(datetime.combine(d, p.square_off_time, IST).timestamp())
    for i in range(i_entry_bar + 1, len(f.c)):
        if f.day[i] != d:
            break
        peak = max(peak, f.h[i]) if side == "LONG" else min(peak, f.l[i])
        t_close = f.ts[i] + 300
        if t_close >= sq:
            return sq
        if exit_signal(f, i, side, stop, peak, p):
            return t_close
    return sq


def simulate(feats: dict, p: Params, pricer: OptionPricer, label: str) -> tuple[list[dict], dict]:
    events = defaultdict(list)   # bar close time -> [(sym, bar index)]
    for sym, f in feats.items():
        for i, t in enumerate(f.ts):
            if WINDOW_FROM <= f.day[i] <= WINDOW_TO:
                events[t + 300].append((sym, i))
    trades, open_pos, stats = [], {}, defaultdict(int)
    entries_today = defaultdict(int)
    peak_open, peak_capital = 0, 0.0

    def close(sym, t, px, reason):
        pos = open_pos.pop(sym)
        e_mod = pos["p0"] * (1 + modeled_slippage_pct(pos["p0"]))
        x_mod = px * (1 - modeled_slippage_pct(px))
        trades.append({"run": label, "symbol": sym, "side": pos["side"], "contract": pos["name"], "day": pos["day"].isoformat(),
                       "entry_time": datetime.fromtimestamp(pos["t0"], IST).strftime("%H:%M"),
                       "exit_time": datetime.fromtimestamp(t, IST).strftime("%H:%M"),
                       "entry": round(pos["p0"], 2), "exit": round(px, 2), "qty": pos["qty"], "reason": reason,
                       "score": round(pos["score"], 2), "rvol": round(pos["rvol"], 2),
                       "pnl_raw": round((px - pos["p0"]) * pos["qty"], 2),
                       "pnl_modeled": round((x_mod - e_mod) * pos["qty"], 2),
                       "hold_min": round((t - pos["t0"]) / 60)})

    last_day = None
    for T in sorted(events):
        dT = datetime.fromtimestamp(T, IST)
        d = dT.date()
        if d != last_day:
            if last_day is not None:
                print(f"  [{label}] {last_day} done: {len(trades)} trades so far, {pricer.calls} option calls", flush=True)
            last_day = d
        # 1) manage open positions: max loss on option minutes up to T, then bar-close exits
        for sym in list(open_pos):
            pos = open_pos[sym]
            m = pos["mins"]
            level = pos["p0"] - p.max_loss_rs / pos["qty"]
            hit = None
            for k in range(bisect.bisect_left(m.ts, pos["checked"]), bisect.bisect_left(m.ts, T)):
                if m.l[k] <= level:
                    hit = (m.ts[k], min(level, m.o[k]))
                    break
            pos["checked"] = T
            if hit:
                close(sym, hit[0], hit[1], "MAX_LOSS_HIT")
                continue
            f = feats[sym]
            if pos["day"] != d or dT.time() >= p.square_off_time:
                px = m.price_at(T if pos["day"] == d else pos["sq"]) or m.c[-1]
                close(sym, T if pos["day"] == d else pos["sq"], px, "DAILY_SQUARE_OFF")
                continue
            i = next((i for s, i in events[T] if s == sym), None)
            if i is None:
                continue
            pos["peak"] = max(pos["peak"], f.h[i]) if pos["side"] == "LONG" else min(pos["peak"], f.l[i])
            reason = exit_signal(f, i, pos["side"], pos["stop"], pos["peak"], p)
            if reason:
                px = m.price_at(T)
                if px is None:
                    continue  # no print yet - retry at the next bar close
                close(sym, T, px, reason)
        # 2) new entries on bars that closed at T
        if dT.time() > p.last_entry_time:
            continue
        cands = []
        for sym, i in events[T]:
            if sym in open_pos or entries_today[(sym, d)] >= p.max_entries_per_symbol_per_day:
                continue
            sig = entry_signal(feats[sym], i, p)
            if sig:
                cands.append((sig["score"], sym, i, sig))
        cands.sort(reverse=True)
        for _score, sym, i, sig in cands:
            stats["signals"] += 1
            if len(open_pos) >= p.max_concurrent:
                stats["skipped_capacity_full"] += 1
                continue
            f = feats[sym]
            qty = pricer.lot(sym)
            if not qty:
                stats["skipped_no_lot"] += 1
                continue
            until = planned_exit_t(f, i, sig["side"], sig["stop"], p)
            leg = pricer.open_leg(sym, d, sig["side"], T, until, f.c[i])
            if leg is None:
                stats["skipped_no_option_data"] += 1
                continue
            p0 = leg["mins"].price_at(T)
            if p0 is None:
                stats["skipped_no_option_print"] += 1
                continue
            if p0 < p.min_premium_rs:
                stats["skipped_premium_below_min"] += 1
                continue
            entries_today[(sym, d)] += 1
            open_pos[sym] = {"side": sig["side"], "p0": p0, "qty": qty, "t0": T, "day": d, "mins": leg["mins"],
                             "checked": T + 60, "stop": sig["stop"],
                             "peak": f.h[i] if sig["side"] == "LONG" else f.l[i], "score": sig["score"],
                             "rvol": sig["rvol"], "sq": int(datetime.combine(d, p.square_off_time, IST).timestamp()),
                             "name": f"{sym} {leg['expiry']:%d %b} {leg['strike']:g} {leg['ot']}"}
            stats["entered"] += 1
        peak_open = max(peak_open, len(open_pos))
        peak_capital = max(peak_capital, sum(pp["p0"] * pp["qty"] for pp in open_pos.values()))
    for sym in list(open_pos):
        pos = open_pos[sym]
        close(sym, pos["sq"], pos["mins"].price_at(pos["sq"]) or pos["mins"].c[-1], "DAILY_SQUARE_OFF")
    stats["peak_open_positions"] = peak_open
    stats["peak_premium_deployed_rs"] = round(peak_capital)
    return trades, dict(stats)


def summarize(tr: list[dict]) -> dict:
    if not tr:
        return {"n": 0}
    pnl = [t["pnl_modeled"] for t in tr]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    daily = defaultdict(float)
    for t in tr:
        daily[t["day"]] += t["pnl_modeled"]
    eq = peak = dd = 0.0
    for d in sorted(daily):
        eq += daily[d]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"n": len(tr), "win_pct": round(len(wins) / len(tr) * 100, 1), "net": round(sum(pnl)),
            "net_raw": round(sum(t["pnl_raw"] for t in tr)),
            "avg_win": round(statistics.mean(wins)) if wins else 0, "avg_loss": round(statistics.mean(losses)) if losses else 0,
            "green_days": sum(1 for v in daily.values() if v > 0), "days": len(daily), "max_dd": round(dd),
            "H1": round(sum(v for k, v in daily.items() if k < HALF_SPLIT)),
            "H2": round(sum(v for k, v in daily.items() if k >= HALF_SPLIT)),
            "median_hold_min": statistics.median(t["hold_min"] for t in tr)}


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "run"
    wf.authenticate()
    dhan_wrapper.client.Dhan.dhan_http.timeout = 90  # this local research process only
    universe = fetch_fno_universe()
    print(f"universe: {len(universe)} F&O stocks", flush=True)
    if phase == "fetch":
        ok = 0
        for k, sym in enumerate(universe):
            if underlying(sym):
                ok += 1
            if (k + 1) % 25 == 0:
                print(f"  fetched {k + 1}/{len(universe)} ({ok} with data)", flush=True)
        print(f"fetch done: {ok}/{len(universe)}", flush=True)
        return

    base = Params()
    feats = {}
    for sym in universe:
        bars = underlying(sym)
        if bars and len(bars["closes"]) > 300:
            feats[sym] = compute_features(bars, base)
    print(f"features for {len(feats)} stocks", flush=True)

    if phase == "signals":
        per_day, per_side = defaultdict(int), defaultdict(int)
        for sym, f in feats.items():
            for i in range(len(f.ts)):
                if WINDOW_FROM <= f.day[i] <= WINDOW_TO:
                    s = entry_signal(f, i, base)
                    if s:
                        per_day[f.day[i].isoformat()] += 1
                        per_side[s["side"]] += 1
        print("signals per side:", dict(per_side), "total:", sum(per_side.values()))
        print("signals per day:", dict(sorted(per_day.items())))
        return

    modes = sys.argv[2:] or ["supertrend", "chandelier", "ema"]
    pricer = OptionPricer()
    all_trades, report = [], {}
    for mode in modes:
        p = replace(base, exit_mode=mode)
        t0 = time.time()
        tr, stats = simulate(feats, p, pricer, mode)
        all_trades += tr
        reasons = defaultdict(lambda: [0, 0.0])
        sides = defaultdict(lambda: [0, 0.0])
        daily = defaultdict(float)
        for t in tr:
            reasons[t["reason"]][0] += 1
            reasons[t["reason"]][1] += t["pnl_modeled"]
            sides[t["side"]][0] += 1
            sides[t["side"]][1] += t["pnl_modeled"]
            daily[t["day"]] += t["pnl_modeled"]
        report[mode] = {"summary": summarize(tr), "stats": stats,
                        "by_reason": {k: [n, round(v)] for k, (n, v) in reasons.items()},
                        "by_side": {k: [n, round(v)] for k, (n, v) in sides.items()},
                        "daily": {k: round(v) for k, v in sorted(daily.items())}}
        print(f"[{mode}] {report[mode]['summary']} stats={stats} ({time.time() - t0:.0f}s, "
              f"{pricer.calls} option calls so far)", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "trades.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(all_trades[0].keys()))
        w.writeheader()
        w.writerows(all_trades)
    (OUT / "summary.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
