"""
Swing with hedge variants over the last 30 days (1 Oct 2026, user: "backtest for last 30 days with these
variants and show me day wise PnL for these" - after the 1 Oct hedge test on Swing's actual trades).

Universe: Swing's watchlist as of 1 Oct 15:36 IST (15 NSE stocks; COPPER, NATURALGAS, NIFTY, BANKNIFTY removed by
the user). HINDSIGHT: the list was picked by the weekly ATH screen, partly on data inside this window, so Swing's
own absolute P&L here is flattered. The hedge variants are compared on the SAME trades, so their differences are
the useful part.

Swing = its live settings on 1 Oct (droplet Swing.config): v3 entry on CLOSED 5-min candles (+ the 15-min layer,
signal code shared with research_swing_index_1min_vs_5min.py), NSE volume floor (entry candle volume >= 0.6 x the
average of the 20 bars before it), at most 5 positions at once, one per stock, re-entry rule as live since 33916b1
(the traded side is free again once the EMA200 regime is on the other side, OR the 5-min Supertrend is on the other
side on a closed candle after the entry candle, OR a fresh cross back after it; the block applies to the signal's
side only). Options basket: BULLISH -> ATM CE, BEARISH -> ATM PE of the nearest monthly expiry after the trade date,
1 lot, entry at the option's 1-min close of the minute the signal candle closes. Exits as live, on the option's
1-min OHLC (stops on the low first, a gap fills at the open): MAX_LOSS 4,500 -> PROFIT_PROTECTION (peak profit above
3,000, 2% off the best price) -> TARGET +35% -> STOP_LOSS -20% -> SUPERTREND_REVERSAL_TICK (the stock crossing the
last closed 5-min candle's Supertrend line against the trade, never on the entry candle) -> Friday and expiry-day
square-off 15:25. Positions carry overnight otherwise (as live).

Hedge variants (each added on top of the same Swing trades; legs use research_swing_hedge_scale_actual_trades.py's
hedge_leg: stop 1,500, 30% giveback trail once +1,000, squared off 15:15 the same day, one per trade):
  SB rule     the trade is down >= 1,800 (option low) AND the stock is >= 1 x ATR(14, 5-min) against the entry spot
              -> buy 1 lot of the opposite ATM option (same expiry). This is Super Bollinger's live hedge.
  1.5 ATR     the same with 1.5 x ATR.
  reverse     buy the opposite ATM option at Swing's own LOSING Supertrend-reversal exit (stop-and-reverse leg).

Option prices: Sep monthly (expired) from Dhan's rolling-option data (OPTSTK, expiry_code 1, ATM +/- k, rebuilt for
the strike held fixed, day by day for overnight holds; reuses history/bt_super_trader_30day/options); from 29 Sep the
27 Oct contracts' own 1-min data. Stock 1-min bars: history/bt_walkforward_long/underlying_1m (to 30 Sep) + 1 Oct
fetched. Dhan's stock minute data ends 15:14, so signals after that are not seen (the live bot builds them from
ticks). Data READ-ONLY, HANDOFF_DHAN_ACCESS_TOKEN only (never pin_totp), cached in history/bt_swing_hedge_30d/; run
after 15:30 IST. P&L is "modelled" (the paper books' slippage on every fill) unless marked raw. Swing trades count
on their exit day, hedge legs on their own day.

Run: HANDOFF_DHAN_ACCESS_TOKEN=<token> .venv/bin/python research_swing_hedge_variants_30day.py
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
OUT = Path("history/bt_swing_hedge_30d")
R.DIR = OUT
SYMS = ["ZYDUSLIFE", "SONACOMS", "DIVISLAB", "AUROPHARMA", "MOTHERSON", "APOLLOHOSP", "APLAPOLLO", "MCX", "BOSCHLTD",
        "LAURUSLABS", "OBEROIRLTY", "RBLBANK", "RADICO", "PHOENIXLTD", "MOTILALOFS"]
FIRST_DAY, LAST_DAY = date(2026, 9, 1), date(2026, 10, 1)
SEP_EXPIRY, OCT_EXPIRY = date(2026, 9, 29), date(2026, 10, 27)
MAX_LOSS, TARGET_PCT, HARD_STOP_PCT, PP_RS, PP_GIVEBACK = 4500.0, 0.35, 0.20, 3000.0, 0.02
VOL_MIN, SLOTS, SQUARE_OFF = 0.6, 5, dtime(15, 25)
MAX_OFFSET, PACE = 10, 1.2
HAVE_TOKEN = bool(os.environ.get("HANDOFF_DHAN_ACCESS_TOKEN"))
OLD_ROLLING = Path("history/bt_super_trader_30day/options")
OLD_LISTED = [Path("history/bt_swing_actual_hedge_3d/option"), Path("history/bt_swing_actual_hedge_1oct/option")]
OUTFILE = Path("research_results/2026-10-01_swing_hedge_variants_30day.txt")
CALLS, MISSING = {"n": 0}, []
ENTRY_GATE = None      # research hook: callable(sym, side, k, now) -> bool; None = Swing as live
RULES = {"SB rule": R.LIVE_RULE,
         "1.5 ATR": {"name": "1.5 ATR (1-min)", "loss": R.HEDGE_TRIGGER, "atr": 1.5, "atr_on": "minute", "st": False},
         "reverse": {"reverse": True}}


def _pace() -> None:
    CALLS["n"] += 1
    time.sleep(PACE)


# --------------------------------------------------------------------------- #
# Stock 1-min bars (with volume)
# --------------------------------------------------------------------------- #
def load_spot(sym: str) -> dict:
    bars: dict[int, tuple] = {}

    def add(ts, o, h, l, c, v):
        for t, a, b, x, y, z in zip(ts, o, h, l, c, v):
            if dtime(9, 15) <= datetime.fromtimestamp(t, IST).time() < dtime(15, 30):
                bars[int(t)] = (a, b, x, y, z or 0.0)

    j = json.loads(Path(f"history/bt_walkforward_long/underlying_1m/{sym}_1min.json").read_text())
    add(j["timestamps"], j["opens"], j["highs"], j["lows"], j["closes"], j["volumes"])
    fresh = OUT / "spot" / f"{sym}.json"
    if not fresh.exists() and HAVE_TOKEN:
        resp = R.dhan().intraday_minute_data(R.underlying(sym)["sid"], "NSE_EQ", "EQUITY", "2026-10-01", "2026-10-02", 1)
        _pace()
        d = (resp or {}).get("data") or {}
        if d.get("timestamp"):
            fresh.parent.mkdir(parents=True, exist_ok=True)
            fresh.write_text(json.dumps({"ts": d["timestamp"], "o": d["open"], "h": d["high"], "l": d["low"],
                                         "c": d["close"], "v": d.get("volume") or [0] * len(d["timestamp"])}))
    if fresh.exists():
        f = json.loads(fresh.read_text())
        add(f["ts"], f["o"], f["h"], f["l"], f["c"], f["v"])
    ts = sorted(bars)
    return {"ts": ts, "o": [bars[t][0] for t in ts], "h": [bars[t][1] for t in ts], "l": [bars[t][2] for t in ts],
            "c": [bars[t][3] for t in ts], "v": [bars[t][4] for t in ts]}


def resample(s: dict, minutes: int) -> dict:
    out = S.resample(s, minutes)
    vol: dict[int, float] = {}
    for t, v in zip(s["ts"], s["v"]):
        dt = datetime.fromtimestamp(t, IST)
        mins = dt.hour * 60 + dt.minute - (9 * 60 + 15)
        start = int(datetime.combine(dt.date(), dtime(9, 15), IST).timestamp()) + (mins // minutes) * minutes * 60
        vol[start] = vol.get(start, 0.0) + v
    out["v"] = [vol.get(t, 0.0) for t in out["ts"]]
    return out


def volume_ratio(v: list[float], k: int, lookback: int = 20) -> float | None:
    """Swing/signals._volume_ratio_at: the candle's volume vs the average of the `lookback` bars before it."""
    if k < lookback:
        return None
    avg = sum(v[k - lookback:k]) / lookback
    return v[k] / avg if avg else None


# --------------------------------------------------------------------------- #
# Option prices
# --------------------------------------------------------------------------- #
def _rolling(sym: str, day: date, ot: str, offset: int) -> dict | None:
    tag = "ATM" if offset == 0 else f"ATM{offset:+d}"
    for p in (OUT / "rolling" / sym / f"{day}_c1_{ot}_{tag}.json", OLD_ROLLING / sym / f"{day}_c1_{ot}_{tag}.json"):
        if p.exists():
            return json.loads(p.read_text())
    if not HAVE_TOKEN:
        return None
    leg = {}
    for attempt in range(3):
        try:
            resp = R.dhan().expired_options_data(
                security_id=R.underlying(sym)["sid"], exchange_segment="NSE_FNO", instrument_type="OPTSTK",
                expiry_flag="MONTH", expiry_code=1, strike=tag, drv_option_type="CALL" if ot == "CE" else "PUT",
                required_data=["open", "high", "low", "close", "strike", "spot"],
                from_date=day.isoformat(), to_date=(day + timedelta(days=1)).isoformat(), interval=1)
            CALLS["n"] += 1
            leg = (((resp or {}).get("data") or {}).get("data") or {}).get(ot.lower()) or {}
            if leg.get("timestamp"):
                break
        except Exception as exc:  # noqa: BLE001
            print(f"   rolling {sym} {day} {ot} {tag}: {exc!r} (attempt {attempt + 1})", flush=True)
        time.sleep(PACE * (2 + 2 * attempt))
    out = {"timestamps": [int(t) for t in leg.get("timestamp") or []], "opens": leg.get("open") or [],
           "highs": leg.get("high") or [], "lows": leg.get("low") or [], "closes": leg.get("close") or [],
           "strikes": leg.get("strike") or [], "spots": leg.get("spot") or []}
    p = OUT / "rolling" / sym / f"{day}_c1_{ot}_{tag}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out))
    time.sleep(PACE)
    return out


def _day_bounds(day: date) -> tuple[int, int]:
    return (int(datetime.combine(day, dtime(9, 15), IST).timestamp()),
            int(datetime.combine(day, dtime(15, 30), IST).timestamp()))


def rolling_day(sym: str, day: date, ot: str, strike: float | None, from_t: int) -> tuple[float, dict] | None:
    """One day of 1-min bars of the Sep-expiry option at `strike` (None = the ATM strike at from_t), from
    from_t to the day's end, rebuilt from the ATM +/- k rolling series."""
    atm = _rolling(sym, day, ot, 0)
    if not atm or not atm["timestamps"]:
        return None
    if strike is None:
        j = bisect.bisect_right(atm["timestamps"], from_t) - 1
        if j < 0 or from_t - atm["timestamps"][j] > 300:
            return None
        strike = atm["strikes"][j]
    rows: dict[int, tuple] = {}

    def take(s):
        for i, t in enumerate(s["timestamps"]):
            if s["strikes"][i] == strike and t not in rows:
                rows[t] = (s["opens"][i], s["highs"][i], s["lows"][i], s["closes"][i])

    take(atm)
    _, end = _day_bounds(day)
    need = [(t, s) for t, s in zip(atm["timestamps"], atm["strikes"]) if from_t - 60 <= t < end and s != strike]
    if need:
        up = _rolling(sym, day, ot, 1)
        if up and up["timestamps"]:
            take(up)
            pos = {t: i for i, t in enumerate(atm["timestamps"])}
            steps = [up["strikes"][i] - atm["strikes"][pos[t]] for i, t in enumerate(up["timestamps"]) if t in pos]
            step = next((x for x in steps if x), None)
            if step:
                offsets = sorted({round((strike - s) / step) for t, s in need if t not in rows} - {0, 1}, key=abs)
                for off in offsets:
                    if abs(off) > MAX_OFFSET:
                        continue
                    if all(t in rows for t, _s in need):
                        break
                    s = _rolling(sym, day, ot, off)
                    if s:
                        take(s)
    ts = sorted(t for t in rows if t >= from_t - 60)
    return strike, {"ts": ts, "o": [rows[t][0] for t in ts], "h": [rows[t][1] for t in ts],
                    "l": [rows[t][2] for t in ts], "c": [rows[t][3] for t in ts]}


def listed(sym: str, ot: str, spot: float) -> tuple[str, int, dict] | None:
    m = R.mdf()
    typ = "CALL" if ot == "CE" else "PUT"
    rows = m[m["SEM_CUSTOM_SYMBOL"].astype(str).str.startswith(f"{sym} 27 OCT ")
             & m["SEM_CUSTOM_SYMBOL"].astype(str).str.endswith(f" {typ}")]
    if rows.empty:
        return None
    r = rows.iloc[(rows["SEM_STRIKE_PRICE"].astype(float) - spot).abs().argsort().iloc[0]]
    name, sid, lot = r["SEM_CUSTOM_SYMBOL"], str(int(r["SEM_SMST_SECURITY_ID"])), int(r["SEM_LOT_UNITS"])
    fname = name.replace(" ", "_") + ".json"
    data = None
    for p in [OUT / "listed" / fname] + [d / fname for d in OLD_LISTED]:
        if p.exists():
            data = json.loads(p.read_text())
            break
    if data is None and HAVE_TOKEN:
        resp = R.dhan().intraday_minute_data(sid, "NSE_FNO", "OPTSTK", "2026-09-29", "2026-10-02", 1)
        _pace()
        d = (resp or {}).get("data") or {}
        if d.get("timestamp"):
            data = {"ts": [int(t) for t in d["timestamp"]], "o": d["open"], "h": d["high"], "l": d["low"], "c": d["close"]}
            p = OUT / "listed" / fname
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data))
    return (name, lot, data) if data and data.get("ts") else None


_LOTS: dict[str, int] = {}


def sep_lot(sym: str) -> int | None:
    if sym not in _LOTS:
        m = R.mdf()
        rows = m[m["SEM_CUSTOM_SYMBOL"].astype(str).str.startswith(f"{sym} 29 SEP ")]
        if rows.empty:
            rows = m[m["SEM_CUSTOM_SYMBOL"].astype(str).str.startswith(f"{sym} 27 OCT ")]
        _LOTS[sym] = int(rows.iloc[0]["SEM_LOT_UNITS"]) if not rows.empty else None
    return _LOTS[sym]


class OptionLeg:
    """The traded contract's 1-min bars, extended day by day while the position is open."""

    def __init__(self, sym: str, ot: str, day: date, entry_t: int, spot: float):
        self.sym, self.ot, self.ok = sym, ot, False
        self.bars: dict[int, tuple] = {}
        if day < SEP_EXPIRY:
            self.expiry = SEP_EXPIRY
            got = rolling_day(sym, day, ot, None, entry_t)
            if got:
                self.strike, d = got
                self.lot = sep_lot(sym)
                self.name = f"{sym} 29 SEP {self.strike:g} {'CALL' if ot == 'CE' else 'PUT'}"
                self._add(d)
                self.days = {day}
                self.ok = bool(self.bars) and bool(self.lot)
        else:
            self.expiry = OCT_EXPIRY
            got = listed(sym, ot, spot)
            if got:
                self.name, self.lot, d = got
                self.strike = float(self.name.split(" ")[3])
                self._add(d)
                self.days = None                      # the listed series already covers every day
                self.ok = bool(self.bars)
        self._sorted()

    def _add(self, d: dict) -> None:
        for t, o, h, l, c in zip(d["ts"], d["o"], d["h"], d["l"], d["c"]):
            self.bars[int(t)] = (o, h, l, c)

    def _sorted(self) -> None:
        self.ts = sorted(self.bars)

    def ensure_day(self, day: date) -> None:
        if self.days is None or day in self.days or day > self.expiry:
            return
        self.days.add(day)
        got = rolling_day(self.sym, day, self.ot, self.strike, _day_bounds(day)[0])
        if got:
            self._add(got[1])
            self._sorted()

    def bar(self, t: int) -> tuple | None:
        return self.bars.get(t)

    def close_at_or_before(self, t: int, max_back: int = 300) -> float | None:
        j = bisect.bisect_right(self.ts, t) - 1
        return self.bars[self.ts[j]][3] if j >= 0 and t - self.ts[j] <= max_back else None

    def series(self) -> R.Series:
        return R.Series({"ts": self.ts, "o": [self.bars[t][0] for t in self.ts], "h": [self.bars[t][1] for t in self.ts],
                         "l": [self.bars[t][2] for t in self.ts], "c": [self.bars[t][3] for t in self.ts]})


def hedge_series(tr: dict, hp: dict) -> R.Series | None:
    """The opposite ATM option (same expiry as the trade) from the hedge minute to that day's end."""
    ot = "PE" if tr["side"] == 1 else "CE"
    day = date.fromisoformat(hp["day"])
    if tr["expiry"] == SEP_EXPIRY:
        got = rolling_day(tr["sym"], day, ot, None, hp["t"])
        if not got:
            return None
        d = got[1]
    else:
        got = listed(tr["sym"], ot, hp["spot"])
        if not got:
            return None
        d = got[2]
    return R.Series(d) if d and d["ts"] else None


# --------------------------------------------------------------------------- #
# Swing simulation (one portfolio, 5 slots)
# --------------------------------------------------------------------------- #
def simulate(data: dict) -> list[dict]:
    days = sorted({datetime.fromtimestamp(t, IST).date() for s in data.values() for t in s["spot"]["ts"]
                   if FIRST_DAY <= datetime.fromtimestamp(t, IST).date() <= LAST_DAY})
    state = {s: {"last_k": None, "consumed": None, "consumed_k": None, "pos": None} for s in data}
    trades = []
    for day in days:
        d0, _ = _day_bounds(day)
        friday, expiry_day = day.weekday() == 4, day in (SEP_EXPIRY, OCT_EXPIRY)
        for minute in range(375):
            t = d0 + minute * 60
            now = t + 60
            hhmm = datetime.fromtimestamp(now, IST).time()
            # exits first (as live: exits on every tick, entries on candle close)
            for sym, st in state.items():
                pos = st["pos"]
                if pos is None:
                    continue
                pos["opt"].ensure_day(day)
                spot = data[sym]["spot"]
                i = data[sym]["idx"].get(t)
                b = pos["opt"].bar(t)
                reason = px = None
                if b is not None:
                    o, h, l, c = b
                    stop = max(pos["p0"] - MAX_LOSS / pos["qty"], pos["p0"] * (1 - HARD_STOP_PCT))
                    peak_before = (pos["best"] - pos["p0"]) * pos["qty"]
                    pp_level = pos["best"] * (1 - PP_GIVEBACK)
                    if l <= stop:
                        px = min(o, stop)
                        reason = "MAX_LOSS_HIT" if stop == pos["p0"] - MAX_LOSS / pos["qty"] else "STOP_LOSS_HIT"
                    elif peak_before > PP_RS and l <= pp_level:
                        px, reason = min(o, pp_level), "PROFIT_PROTECTION_HIT"
                    elif h >= pos["p0"] * (1 + TARGET_PCT):
                        px, reason = max(o, pos["p0"] * (1 + TARGET_PCT)), "TARGET_HIT"
                    else:
                        pos["best"] = max(pos["best"], h)
                        if (pos["best"] - pos["p0"]) * pos["qty"] > PP_RS and c <= pos["best"] * (1 - PP_GIVEBACK):
                            px, reason = c, "PROFIT_PROTECTION_HIT"
                if reason is None and i is not None and st["last_k"] is not None and st["last_k"] > pos["k_in"]:
                    line = data[sym]["sig"]["st"][st["last_k"]]
                    if line is not None and ((pos["side"] == 1 and spot["l"][i] < line)
                                             or (pos["side"] == -1 and spot["h"][i] > line)):
                        px, reason = pos["opt"].close_at_or_before(t), "SUPERTREND_REVERSAL_TICK"
                if reason is None and hhmm >= SQUARE_OFF and (friday or day == pos["opt"].expiry):
                    px, reason = pos["opt"].close_at_or_before(t), ("FRIDAY_SQUARE_OFF" if friday else
                                                                    "EXPIRY_DAY_SQUARE_OFF")
                if reason and px is not None:
                    pos.update({"t1": now, "exit": px, "reason": reason, "pnl_raw": (px - pos["p0"]) * pos["qty"],
                                "pnl": R.mod(pos["p0"], px, pos["qty"])})
                    trades.append(pos)
                    st["pos"] = None
            # candle closes: signal state + entries
            candidates = []
            for sym, st in state.items():
                k = data[sym]["close_at"].get(now)
                if k is None:
                    continue
                st["last_k"] = k
                sig, fast = data[sym]["sig"], data[sym]["fast"]
                if st["consumed"] is not None:
                    side, kin = st["consumed"], st["consumed_k"]
                    reg, line = sig["regime"][k], sig["st"][k]
                    st_side = None if line is None else (1 if fast["c"][k] > line else -1)
                    prev_line = sig["st"][k - 1] if k > 0 else None
                    prev_side = None if prev_line is None else (1 if fast["c"][k - 1] > prev_line else -1)
                    fresh_cross = st_side == side and prev_side == -side
                    if (reg is not None and (1 if reg else -1) != side) or (k > kin and (
                            (st_side is not None and st_side != side) or fresh_cross)):
                        st["consumed"] = None
                side = 1 if sig["bull"][k] else -1 if sig["bear"][k] else 0
                if not side or st["pos"] is not None or side == st["consumed"]:
                    continue
                if hhmm > SQUARE_OFF or (hhmm == SQUARE_OFF and (friday or expiry_day)):
                    continue
                vr = volume_ratio(fast["v"], k)
                if vr is not None and vr < VOL_MIN:
                    continue
                if ENTRY_GATE is not None and not ENTRY_GATE(sym, side, k, now):
                    continue
                candidates.append((sym, side, k))
            for sym, side, k in candidates:
                if sum(1 for s in state.values() if s["pos"] is not None) >= SLOTS:
                    break
                spot = data[sym]["spot"]
                i = data[sym]["idx"].get(t)
                spot_px = spot["c"][i] if i is not None else data[sym]["fast"]["c"][k]
                leg = OptionLeg(sym, "CE" if side == 1 else "PE", day, t, spot_px)
                p0 = leg.close_at_or_before(t) if leg.ok else None
                if not p0:                        # Swing has no minimum premium (unlike Super Bollinger)
                    MISSING.append(f"{sym} {datetime.fromtimestamp(now, IST):%d %b %H:%M} {'CE' if side == 1 else 'PE'}")
                    continue
                state[sym]["pos"] = {"sym": sym, "side": side, "typ": "CALL" if side == 1 else "PUT", "contract": leg.name,
                                     "expiry": leg.expiry, "qty": leg.lot, "t0": now, "p0": p0, "best": p0, "k_in": k,
                                     "opt": leg}
                state[sym]["consumed"], state[sym]["consumed_k"] = side, k
    for sym, st in state.items():
        pos = st["pos"]
        if pos is not None:
            px = pos["opt"].close_at_or_before(pos["opt"].ts[-1])
            pos.update({"t1": None, "exit": px, "reason": "OPEN (marked)", "pnl_raw": (px - pos["p0"]) * pos["qty"],
                        "pnl": R.mod(pos["p0"], px, pos["qty"])})
            trades.append(pos)
    return trades


# --------------------------------------------------------------------------- #
# Hedge legs
# --------------------------------------------------------------------------- #
def add_hedges(trades: list[dict], data: dict) -> None:
    R.DAYS = tuple(sorted({datetime.fromtimestamp(t["t0"], IST).date().isoformat() for t in trades}
                          | {datetime.fromtimestamp(t["t1"], IST).date().isoformat() for t in trades if t["t1"]}))
    for tr in trades:
        und, b5 = data[tr["sym"]]["und"], data[tr["sym"]]["b5"]
        opt = tr["opt"].series()
        tdict = {"p0": tr["p0"], "qty": tr["qty"], "typ": tr["typ"], "t0": tr["t0"], "t1": tr["t1"],
                 "base": tr["pnl"], "reason": tr["reason"]}
        tr["hedges"] = {}
        for name, rule in RULES.items():
            hp = R.reverse_point(tdict, und, b5) if rule.get("reverse") else R.hedge_point(tdict, opt, und, b5, rule)
            if not hp:
                continue
            hs = hedge_series(tr, hp)
            if hs is None:
                MISSING.append(f"hedge {name} {tr['contract']} {datetime.fromtimestamp(hp['t'], IST):%d %b %H:%M}")
                continue
            pnl, info = R.hedge_leg(hp, hs, tr["qty"], b5, -1 if tr["side"] == 1 else 1, add=False)
            if info.get("no_price"):
                MISSING.append(f"hedge {name} {tr['contract']} no price at {datetime.fromtimestamp(hp['t'], IST):%H:%M}")
                continue
            tr["hedges"][name] = {"pnl": pnl, "day": hp["day"], "t": hp["t"], "exit": info.get("exit")}


# --------------------------------------------------------------------------- #
def main() -> None:
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    data = {}
    for sym in SYMS:
        spot = load_spot(sym)
        fast, slow = resample(spot, 5), resample(spot, 15)
        und = R.Series(spot)
        data[sym] = {"spot": spot, "idx": {t: i for i, t in enumerate(spot["ts"])}, "fast": fast,
                     "sig": S.signals(fast, slow, 5), "close_at": {t + 300: k for k, t in enumerate(fast["ts"])},
                     "und": und, "b5": R.five_min(und)}
    trades = simulate(data)
    add_hedges(trades, data)

    say("SWING + HEDGE VARIANTS, 1 Sep - 1 Oct 2026 (15-stock watchlist of 1 Oct; modelled P&L after slippage)")
    say(f"Dhan calls this run: {CALLS['n']}; option data {'downloaded as needed' if HAVE_TOKEN else 'CACHE ONLY'}")
    if MISSING:
        say(f"Not priced ({len(MISSING)}): " + "; ".join(MISSING[:25]) + (" ..." if len(MISSING) > 25 else ""))
    by_day: dict[str, dict] = {}
    for tr in trades:
        dkey = datetime.fromtimestamp(tr["t1"] or tr["t0"], IST).date().isoformat()
        d = by_day.setdefault(dkey, {"n": 0, "w": 0, "swing": 0.0, **{k: 0.0 for k in RULES}, **{f"{k}#": 0 for k in RULES}})
        d["n"] += 1
        d["w"] += tr["pnl"] > 0
        d["swing"] += tr["pnl"]
        for name, h in tr["hedges"].items():
            hd = by_day.setdefault(h["day"], {"n": 0, "w": 0, "swing": 0.0, **{k: 0.0 for k in RULES},
                                              **{f"{k}#": 0 for k in RULES}})
            hd[name] += h["pnl"]
            hd[f"{name}#"] += 1
    say("")
    say(f"{'day':10s} {'trades':>6s} {'won':>4s} {'Swing':>9s} | {'+SB rule':>9s} {'(legs)':>8s} | {'+1.5 ATR':>9s} "
        f"{'(legs)':>8s} | {'+reverse':>9s} {'(legs)':>8s}")
    cum = {k: 0.0 for k in ("swing", *RULES)}
    peak = {k: 0.0 for k in cum}
    dd = {k: 0.0 for k in cum}
    win_days = {k: 0 for k in cum}
    worst = {k: (0.0, "") for k in cum}
    for day in sorted(by_day):
        d = by_day[day]
        tot = {"swing": d["swing"], **{k: d["swing"] + d[k] for k in RULES}}
        for k, v in tot.items():
            cum[k] += v
            peak[k] = max(peak[k], cum[k])
            dd[k] = min(dd[k], cum[k] - peak[k])
            win_days[k] += v > 0
            if v < worst[k][0]:
                worst[k] = (v, day)
        legs = lambda k: f"{d[k]:+,.0f}/{d[k + '#']}"
        say(f"{day:10s} {d['n']:>6d} {d['w']:>4d} {d['swing']:>+9,.0f} | {tot['SB rule']:>+9,.0f} {legs('SB rule'):>8s} | "
            f"{tot['1.5 ATR']:>+9,.0f} {legs('1.5 ATR'):>8s} | {tot['reverse']:>+9,.0f} {legs('reverse'):>8s}")
    n_days = len(by_day)
    say("")
    say(f"{'variant':12s} {'total':>10s} {'hedge legs':>11s} {'hedges':>6s} {'stopped':>7s} {'win days':>9s} "
        f"{'worst day':>18s} {'max drawdown':>13s}")
    for k in ("swing", *RULES):
        legs = [tr["hedges"][k] for tr in trades if k in tr.get("hedges", {})] if k != "swing" else []
        name = "Swing alone" if k == "swing" else k
        say(f"{name:12s} {cum[k]:>+10,.0f} {sum(h['pnl'] for h in legs):>+11,.0f} {len(legs):>6d} "
            f"{sum(1 for h in legs if h['exit'] == 'stop'):>7d} {win_days[k]:>4d}/{n_days:<4d} "
            f"{worst[k][0]:>+10,.0f} {worst[k][1][5:]:>7s} {dd[k]:>+13,.0f}")
    closed = [t for t in trades if t["t1"]]
    say("")
    say(f"Swing: {len(trades)} trades ({len(closed)} closed, {sum(1 for t in closed if t['pnl'] > 0)} won), raw "
        f"{sum(t['pnl_raw'] for t in trades):+,.0f}, modelled {sum(t['pnl'] for t in trades):+,.0f}; exits: " +
        ", ".join(f"{r} {sum(1 for t in trades if t['reason'] == r)}" for r in sorted({t['reason'] for t in trades})))
    say("Hindsight caveat: the 15 stocks are 1 Oct's list (picked partly on this window's data) - Swing's own total is "
        "flattered; compare the variants with each other.")
    OUTFILE.write_text("\n".join(lines) + "\n")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "trades.json").write_text(json.dumps([{k: v for k, v in t.items() if k != "opt"} for t in trades],
                                                default=str))


if __name__ == "__main__":
    main()
