"""
Scalper (BANKNIFTY options on Swing's rules, 1-minute candles) traded with TWO lots - two exit variants over the
last 30 days (1 Oct 2026, user: "back test this bank nifty scalper strategy with two lots ... In one variant, let
it flow with the current rules of entry and exit. In the other variant, just try with a sell of one lot when the
current rule profit is reached and hold another lot to ride the momentum until super trend reversal comes back
... last 30 days and show me PNL report").

Window: every trading day 1 Sep - 1 Oct 2026. Entries exactly as the live Scalper (Scalper/signals.py = Swing v3
on CLOSED 1-min candles + the 15-min layer from its last closed candle; signal code shared with
research_swing_index_1min_vs_5min.py, which was replayed against the live module with 0 mismatches):
BULLISH -> ATM CE, BEARISH -> ATM PE of the nearest expiry (rolled on expiry day), one position at a time,
same-side re-entry only after the EMA200 regime has been on the other side, no entries from 15:25 or once
the day's realised loss reaches the daily stop. Entry price = the option's 1-min close of the minute the
signal candle closes.

Size: 2 lots x 30 (today's BANKNIFTY lot) = 60 for every day. The live rupee limits are per 1 lot, so the main
runs keep the same PRICE levels as live by doubling them: max loss Rs 3,000 (50 points), profit protection
arms above Rs 6,000 peak (100 points), daily stop Rs 6,000. The "same rupees" runs keep the live numbers
(1,500 / 3,000 / 3,000) on 2 lots instead (stops twice as tight in points).

  A  "as live"      both lots exit together: MAX_LOSS -> TARGET +35% -> PROFIT_PROTECTION (peak above the arm,
                    2% off the best price) -> STOP_LOSS -20% -> SUPERTREND_REVERSAL (index crossing the last
                    closed 1-min candle's Supertrend line, never on the entry candle) -> 15:25 square-off.
  B  "split at exit" when A would book profit (PROFIT_PROTECTION or TARGET) only ONE lot is sold; the other lot
                    rides with no stops until the Supertrend reversal or 15:25. Losses before that close both.
  C  "split at arm"  one lot is sold the moment the peak profit first reaches the profit-protection level
                    (Rs 3,000 per lot = +100 points); the other rides to the Supertrend reversal / 15:25.
                    (A second reading of "sell one lot when the current rule profit is reached".)

Price exits use the option's 1-minute OHLC (stops on the low first, then the best price / target on the high;
a gap through a level fills at the open) - closer to the live tick checks than 1-min closes. Supertrend
reversal and the square-off exit at that minute's option close. P&L raw and "modelled" (the paper books'
slippage on both fills: 0.10 / price, 0.5% floor - about Rs 5 per unit per side at a Rs 1,000 premium).

Option prices: Sep monthly (expired) from Dhan's /charts/rollingoption (OPTIDX, expiry_code 1, ATM +/- k,
rebuilt for the strike held fixed); from 29 Sep the Oct monthly from the listed contract's 1-min data.
Data READ-ONLY with HANDOFF_DHAN_ACCESS_TOKEN only (never pin_totp); fetched lazily while running and cached
in history/bt_scalper_2lot/ (reuses history/bt_super_trader_30day and bt_swing_index_tf caches). Run after
15:30 IST (the download shares the live bot's data-API budget):
    HANDOFF_DHAN_ACCESS_TOKEN=<token> .venv/bin/python research_scalper_banknifty_2lot_variants.py
Without a token it runs on the cache only and lists the signals it could not price.
"""
from __future__ import annotations

import bisect
import json
import os
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import research_swing_hedge_scale_actual_trades as R
import research_swing_index_1min_vs_5min as S

IST = R.IST
OUT = Path("history/bt_scalper_2lot")
R.DIR = OUT                                         # S sets it on import; listed contracts cache here
FIRST_DAY, LAST_DAY = date(2026, 9, 1), date(2026, 10, 1)
SEP_EXPIRY = date(2026, 9, 29)
LOT, LOTS = 30, 2
QTY = LOT * LOTS
TARGET_PCT, HARD_STOP_PCT, GIVEBACK = 0.35, 0.20, 0.02
SQUARE_OFF = dtime(15, 25)
MAX_OFFSET = 8
PACE = 0.5
HAVE_TOKEN = bool(os.environ.get("HANDOFF_DHAN_ACCESS_TOKEN"))
OLD_ROLLING = Path("history/bt_super_trader_30day/options/BANKNIFTY")
OLD_LISTED = Path("history/bt_swing_index_tf/option")
OUTFILE = Path("research_results/2026-10-01_scalper_banknifty_2lot_variants.txt")
MISSING: list[str] = []
CALLS = {"n": 0}


def _day_end(d: date) -> int:
    return int(datetime.combine(d, dtime(15, 30), IST).timestamp())


# --------------------------------------------------------------------------- #
# Spot
# --------------------------------------------------------------------------- #
def load_spot() -> dict:
    bars: dict[int, tuple] = {}

    def add(ts, o, h, l, c):
        for t, a, b, x, y in zip(ts, o, h, l, c):
            hm = datetime.fromtimestamp(t, IST).time()
            if dtime(9, 15) <= hm < dtime(15, 30):
                bars[int(t)] = (a, b, x, y)

    long_ = json.loads(Path("history/bt_walkforward_long/underlying_1m/BANKNIFTY_1min.json").read_text())
    add(long_["timestamps"], long_["opens"], long_["highs"], long_["lows"], long_["closes"])
    tf = json.loads(Path("history/bt_swing_index_tf/spot/BANKNIFTY.json").read_text())
    add(tf["ts"], tf["o"], tf["h"], tf["l"], tf["c"])
    fresh = OUT / "spot" / "BANKNIFTY.json"
    last = max(bars)
    if HAVE_TOKEN and (not fresh.exists() or datetime.fromtimestamp(last, IST).date() < LAST_DAY
                       or datetime.fromtimestamp(last, IST).time() < dtime(15, 29)):
        resp = R.dhan().intraday_minute_data("25", "IDX_I", "INDEX", "2026-09-25", "2026-10-02", 1)
        CALLS["n"] += 1
        d = (resp or {}).get("data") or {}
        if d.get("timestamp"):
            fresh.parent.mkdir(parents=True, exist_ok=True)
            fresh.write_text(json.dumps({"ts": d["timestamp"], "o": d["open"], "h": d["high"], "l": d["low"],
                                         "c": d["close"]}))
        time.sleep(PACE)
    if fresh.exists():
        f = json.loads(fresh.read_text())
        add(f["ts"], f["o"], f["h"], f["l"], f["c"])
    ts = sorted(bars)
    return {"ts": ts, "o": [bars[t][0] for t in ts], "h": [bars[t][1] for t in ts], "l": [bars[t][2] for t in ts],
            "c": [bars[t][3] for t in ts]}


# --------------------------------------------------------------------------- #
# Option prices
# --------------------------------------------------------------------------- #
def _rolling(day: date, ot: str, offset: int) -> dict | None:
    tag = "ATM" if offset == 0 else f"ATM{offset:+d}"
    for p in (OUT / "rolling" / f"{day}_{ot}_{tag}.json", OLD_ROLLING / f"{day}_c1_{ot}_{tag}.json"):
        if p.exists():
            return json.loads(p.read_text())
    if not HAVE_TOKEN:
        return None
    leg = {}
    for attempt in range(3):
        try:
            resp = R.dhan().expired_options_data(
                security_id="25", exchange_segment="NSE_FNO", instrument_type="OPTIDX", expiry_flag="MONTH",
                expiry_code=1, strike=tag, drv_option_type="CALL" if ot == "CE" else "PUT",
                required_data=["open", "high", "low", "close", "strike", "spot"],
                from_date=day.isoformat(), to_date=(day + timedelta(days=1)).isoformat(), interval=1)
            CALLS["n"] += 1
            leg = (((resp or {}).get("data") or {}).get("data") or {}).get(ot.lower()) or {}
            if leg.get("timestamp"):
                break
        except Exception as exc:  # noqa: BLE001
            print(f"   rolling {day} {ot} {tag}: {exc!r} (attempt {attempt + 1})", flush=True)
        time.sleep(PACE * (2 + 2 * attempt))
    out = {"timestamps": [int(t) for t in leg.get("timestamp") or []], "opens": leg.get("open") or [],
           "highs": leg.get("high") or [], "lows": leg.get("low") or [], "closes": leg.get("close") or [],
           "strikes": leg.get("strike") or [], "spots": leg.get("spot") or []}
    p = OUT / "rolling" / f"{day}_{ot}_{tag}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out))
    time.sleep(PACE)
    return out


def rolling_series(day: date, ot: str, entry_t: int) -> tuple[str, dict] | None:
    """The Sep-expiry ATM strike at entry_t, held fixed until the day's end, rebuilt from ATM +/- k."""
    atm = _rolling(day, ot, 0)
    if not atm or not atm["timestamps"]:
        return None
    j = bisect.bisect_right(atm["timestamps"], entry_t) - 1
    if j < 0 or entry_t - atm["timestamps"][j] > 300:
        return None
    strike = atm["strikes"][j]
    rows: dict[int, tuple] = {}

    def take(s):
        for i, t in enumerate(s["timestamps"]):
            if s["strikes"][i] == strike and t not in rows:
                rows[t] = (s["opens"][i], s["highs"][i], s["lows"][i], s["closes"][i])

    take(atm)
    end = _day_end(day)
    need = [t for t, s in zip(atm["timestamps"], atm["strikes"]) if entry_t <= t < end and s != strike]
    for k in range(1, MAX_OFFSET + 1):
        if all(t in rows for t in need):
            break
        for off in (k, -k):
            s = _rolling(day, ot, off)
            if s:
                take(s)
    ts = sorted(rows)
    name = f"BANKNIFTY 29 SEP {int(strike)} {'CALL' if ot == 'CE' else 'PUT'}"
    return name, {"ts": ts, "o": [rows[t][0] for t in ts], "h": [rows[t][1] for t in ts],
                  "l": [rows[t][2] for t in ts], "c": [rows[t][3] for t in ts]}


def listed_series(day: date, ot: str, spot: float) -> tuple[str, dict] | None:
    c = S.atm("BANKNIFTY", ot, spot, day)
    name = c["symbol"]
    p = OUT / "listed" / (name.replace(" ", "_") + ".json")
    complete = lambda d: d and d["ts"] and datetime.fromtimestamp(d["ts"][-1], IST) >= datetime.combine(
        LAST_DAY, dtime(15, 25), IST)
    data = json.loads(p.read_text()) if p.exists() else None
    if not complete(data):
        old = OLD_LISTED / (name.replace(" ", "_") + ".json")
        if old.exists():
            data = json.loads(old.read_text())
        if not complete(data) and HAVE_TOKEN:
            resp = R.dhan().intraday_minute_data(c["sid"], "NSE_FNO", "OPTIDX", "2026-09-29", "2026-10-02", 1)
            CALLS["n"] += 1
            d = (resp or {}).get("data") or {}
            if d.get("timestamp"):
                data = {"ts": [int(t) for t in d["timestamp"]], "o": d["open"], "h": d["high"], "l": d["low"],
                        "c": d["close"]}
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(data))
            time.sleep(PACE)
    return (name, data) if data and data.get("ts") else None


_SERIES: dict[tuple, tuple | None] = {}


def option_for(day: date, side: int, entry_t: int, spot: float) -> tuple[str, dict] | None:
    ot = "CE" if side == 1 else "PE"
    key = (day, ot, entry_t if day < SEP_EXPIRY else round(spot / 100))
    if key not in _SERIES:
        _SERIES[key] = rolling_series(day, ot, entry_t) if day < SEP_EXPIRY else listed_series(day, ot, spot)
    return _SERIES[key]


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
CONFIGS = [
    {"key": "A", "name": "A  as live (both lots together)", "mode": "all", "ml": 3000, "arm": 6000, "daily": 6000},
    {"key": "B", "name": "B  sell 1 lot at the profit exit, ride 1", "mode": "split_exit", "ml": 3000, "arm": 6000,
     "daily": 6000},
    {"key": "C", "name": "C  sell 1 lot when the profit level is reached, ride 1", "mode": "split_arm", "ml": 3000,
     "arm": 6000, "daily": 6000},
    {"key": "A$", "name": "A  as live, live rupee limits on 2 lots", "mode": "all", "ml": 1500, "arm": 3000,
     "daily": 3000},
    {"key": "B$", "name": "B  split at the profit exit, live rupee limits", "mode": "split_exit", "ml": 1500,
     "arm": 3000, "daily": 3000},
    {"key": "C$", "name": "C  split at the profit level, live rupee limits", "mode": "split_arm", "ml": 1500,
     "arm": 3000, "daily": 3000},
]


def bar_at(s: dict, t: int) -> int:
    j = bisect.bisect_right(s["ts"], t) - 1
    return j if j >= 0 and s["ts"][j] == t else -1


def simulate(spot: dict, sig: dict, cfg: dict) -> list[dict]:
    close_at = {t + 60: k for k, t in enumerate(spot["ts"])}
    trades, pos, consumed, last_k = [], None, None, None
    realised: dict[date, float] = {}

    def close_leg(qty, t_out, px, reason):
        leg = {"qty": qty, "t_out": t_out, "exit": px, "reason": reason, "pnl": (px - pos["p0"]) * qty,
               "pnl_modeled": R.mod(pos["p0"], px, qty)}
        pos["legs"].append(leg)
        realised[pos["day"]] = realised.get(pos["day"], 0.0) + leg["pnl"]

    for i, t in enumerate(spot["ts"]):
        now = t + 60
        day = datetime.fromtimestamp(t, IST).date()
        if not (FIRST_DAY <= day <= LAST_DAY):
            continue
        hhmm = datetime.fromtimestamp(now, IST).time()
        if now in close_at:
            last_k = close_at[now]
            reg = sig["regime"][last_k]
            if consumed is not None and reg is not None and (1 if reg else -1) != consumed:
                consumed = None
        if pos is not None and pos["day"] != day:          # no data after the last bar of a day: close at the last price
            close_leg(pos["open"], pos["last_t"], pos["last_px"], "DAY_END_NO_DATA")
            trades.append(pos)
            pos = None
        if pos is not None:
            j = bar_at(pos["opt"], t)
            c = pos["last_px"]
            if j >= 0:
                o, h, l, c = (pos["opt"][x][j] for x in ("o", "h", "l", "c"))
                pos["last_t"], pos["last_px"] = now, c
                p0, full = pos["p0"], pos["open"] == QTY
                if full:
                    best0 = pos["best"]
                    ml_level = p0 - cfg["ml"] / QTY
                    pp_level = best0 * (1 - GIVEBACK)
                    armed = (best0 - p0) * QTY > cfg["arm"]
                    reason = px = None
                    if l <= ml_level:
                        reason, px = "MAX_LOSS_HIT", min(o, ml_level)
                    elif armed and l <= pp_level:
                        reason, px = "PROFIT_PROTECTION_HIT", min(o, pp_level)
                    elif l <= p0 * (1 - HARD_STOP_PCT):
                        reason, px = "STOP_LOSS_HIT", min(o, p0 * (1 - HARD_STOP_PCT))
                    else:
                        pos["best"] = max(best0, h)
                        if h >= p0 * (1 + TARGET_PCT):
                            reason, px = "TARGET_HIT", max(o, p0 * (1 + TARGET_PCT))
                        elif cfg["mode"] == "split_arm" and (pos["best"] - p0) * QTY > cfg["arm"]:
                            reason, px = "PROFIT_LEVEL_REACHED", max(o, p0 + cfg["arm"] / QTY)
                    if reason in ("PROFIT_PROTECTION_HIT", "TARGET_HIT", "PROFIT_LEVEL_REACHED") and cfg["mode"] != "all":
                        close_leg(LOT, now, px, reason)
                        pos["open"] = QTY - LOT
                        pos["split_at"] = now
                    elif reason == "PROFIT_LEVEL_REACHED":
                        pass
                    elif reason:
                        close_leg(QTY, now, px, reason)
                        pos["open"] = 0
            if pos["open"] and last_k is not None and last_k > pos["k_in"] and sig["st"][last_k] is not None:
                line = sig["st"][last_k]
                if (pos["side"] == 1 and spot["l"][i] < line) or (pos["side"] == -1 and spot["h"][i] > line):
                    close_leg(pos["open"], now, c, "SUPERTREND_REVERSAL_TICK" if pos["open"] == QTY
                              else "RUNNER_SUPERTREND_REVERSAL")
                    pos["open"] = 0
            if pos["open"] and hhmm >= SQUARE_OFF:
                close_leg(pos["open"], now, c, "DAILY_SQUARE_OFF" if pos["open"] == QTY else "RUNNER_SQUARE_OFF")
                pos["open"] = 0
            if not pos["open"]:
                trades.append(pos)
                pos = None
        if pos is None and now in close_at and hhmm < SQUARE_OFF and realised.get(day, 0.0) > -cfg["daily"]:
            k = close_at[now]
            side = 1 if sig["bull"][k] else -1 if sig["bear"][k] else 0
            if side and (consumed is None or consumed != side):
                got = option_for(day, side, t, spot["c"][i])
                j = bar_at(got[1], t) if got else -1
                if j < 0 and got:
                    jj = bisect.bisect_right(got[1]["ts"], t) - 1
                    j = jj if jj >= 0 and t - got[1]["ts"][jj] <= 120 else -1
                if j < 0:
                    MISSING.append(f"{datetime.fromtimestamp(now, IST):%d %b %H:%M} {'CE' if side == 1 else 'PE'}")
                else:
                    pos = {"contract": got[0], "side": side, "day": day, "t_in": now, "k_in": k,
                           "p0": got[1]["c"][j], "best": got[1]["c"][j], "opt": got[1], "open": QTY, "legs": [],
                           "split_at": None, "last_t": now, "last_px": got[1]["c"][j]}
                    consumed = side
    if pos is not None:
        close_leg(pos["open"], pos["last_t"], pos["last_px"], "OPEN_AT_DATA_END")
        trades.append(pos)
    for tr in trades:
        tr["pnl"] = sum(lg["pnl"] for lg in tr["legs"])
        tr["pnl_modeled"] = sum(lg["pnl_modeled"] for lg in tr["legs"])
    return trades


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def stats(trades: list[dict]) -> dict:
    by_day: dict[date, list[float]] = {}
    for tr in trades:
        d = by_day.setdefault(tr["day"], [0.0, 0.0, 0])
        d[0] += tr["pnl"]; d[1] += tr["pnl_modeled"]; d[2] += 1
    eq = peak = dd = 0.0
    for d in sorted(by_day):
        eq += by_day[d][1]; peak = max(peak, eq); dd = min(dd, eq - peak)
    return {"n": len(trades), "wins": sum(1 for t in trades if t["pnl_modeled"] > 0),
            "raw": sum(t["pnl"] for t in trades), "mod": sum(t["pnl_modeled"] for t in trades), "dd": dd,
            "days": by_day, "best_day": max((v[1] for v in by_day.values()), default=0),
            "worst_day": min((v[1] for v in by_day.values()), default=0),
            "pos_days": sum(1 for v in by_day.values() if v[1] > 0)}


def main() -> None:
    lines: list[str] = []
    say = lambda s="": (print(s), lines.append(s))
    spot = load_spot()
    sig = S.signals(spot, S.resample(spot, 15), 1)
    days = sorted({datetime.fromtimestamp(t, IST).date() for t in spot["ts"]
                   if FIRST_DAY <= datetime.fromtimestamp(t, IST).date() <= LAST_DAY})
    last = datetime.fromtimestamp(spot["ts"][-1], IST)
    results = {cfg["key"]: simulate(spot, sig, cfg) for cfg in CONFIGS}
    hm = lambda t: datetime.fromtimestamp(t, IST).strftime("%d %b %H:%M")
    say(f"SCALPER BANKNIFTY, 2 lots (qty {QTY}) - {days[0]:%d %b} .. {days[-1]:%d %b %Y} ({len(days)} trading days, "
        f"spot data to {last:%d %b %H:%M})")
    say(f"Option data: {'downloaded as needed' if HAVE_TOKEN else 'CACHE ONLY'}; {CALLS['n']} Dhan calls this run; "
        f"{len(set(MISSING))} signal(s) could not be priced")
    say()
    say(f"{'variant':58s} {'trades':>6s} {'wins':>5s} {'P&L raw':>10s} {'after costs':>12s} {'max DD':>9s} "
        f"{'best day':>9s} {'worst day':>10s} {'+days':>6s}")
    for cfg in CONFIGS:
        s = stats(results[cfg["key"]])
        say(f"{cfg['name']:58s} {s['n']:>6d} {s['wins']:>5d} {s['raw']:>+10,.0f} {s['mod']:>+12,.0f} {s['dd']:>+9,.0f} "
            f"{s['best_day']:>+9,.0f} {s['worst_day']:>+10,.0f} {s['pos_days']:>3d}/{len(s['days']):<2d}")
    for key in ("A", "B", "C"):
        tr = results[key]
        say()
        say(f"=== {next(c['name'] for c in CONFIGS if c['key'] == key)} - {len(tr)} trades ===")
        for t in tr:
            legs = "; ".join(f"{lg['qty']}@{lg['exit']:.2f} {hm(lg['t_out'])[7:]} {lg['reason']}" for lg in t["legs"])
            say(f"  {t['contract']:28s} {hm(t['t_in'])}  in {t['p0']:8.2f}  best {t['best']:8.2f} | {legs} | "
                f"{t['pnl']:+8,.0f} ({t['pnl_modeled']:+8,.0f})")
    say()
    say("Per day, after costs (A / B / C):")
    sa, sb, sc_ = (stats(results[k])["days"] for k in ("A", "B", "C"))
    for d in days:
        g = lambda s: s.get(d, [0, 0, 0])
        say(f"  {d:%a %d %b}  A {g(sa)[1]:>+8,.0f} ({g(sa)[2]})   B {g(sb)[1]:>+8,.0f} ({g(sb)[2]})   "
            f"C {g(sc_)[1]:>+8,.0f} ({g(sc_)[2]})")
    for key in ("B", "C"):
        runs = [lg for t in results[key] for lg in t["legs"] if lg["reason"].startswith("RUNNER")]
        split = [t for t in results[key] if t["split_at"]]
        say()
        say(f"{key}: {len(split)} trades split; runner lots: {len(runs)}, raw {sum(r['pnl'] for r in runs):+,.0f}, "
            f"after costs {sum(r['pnl_modeled'] for r in runs):+,.0f}; runners that ended below entry: "
            f"{sum(1 for r in runs if r['pnl'] < 0)}; best runner {max((r['pnl'] for r in runs), default=0):+,.0f}, "
            f"worst {min((r['pnl'] for r in runs), default=0):+,.0f}")
    if MISSING:
        say()
        say("Signals without option data (skipped): " + ", ".join(sorted(set(MISSING))))
    OUTFILE.parent.mkdir(exist_ok=True)
    OUTFILE.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
