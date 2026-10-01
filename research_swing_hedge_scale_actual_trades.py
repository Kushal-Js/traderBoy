"""
Super Bollinger's hedge + scale-in rules on SWING's actual trades (1 Oct 2026, user request:
"backtest SWING with hedging and scale in strategy we recently developed for Super Bollinger"
- "just do this backtest for yesterday and [to]day's trade" - "on Swing's actual trades").

Trades: every Swing trade (paper book + real SwingIndex trades) ENTERED 28 Sep - 1 Oct 2026 (the
user widened it to the last 4 trading days), NSE options only (stocks, NIFTY, BANKNIFTY; the MCX
COPPER/NATURALGAS trades are listed but not tested - other session hours, no MCX hedge rules). history/bt_swing_actual_hedge/trades.json, pulled from
the droplet. Positions still open count for the hedge/added legs only - their own P&L is not final.

Rules = Super Bollinger's live settings, mirrored for PUT trades:
  HEDGE  the trade is down >= 1,800 (option low) AND the stock has moved >= 1 x ATR(14, 5-min)
         against it (CALL: below the entry spot, PUT: above) -> buy 1 lot of the opposite ATM option
         (same expiry) at that minute's close. Stop 1,500; 30% giveback trail once +1,000; one hedge
         per trade.
  S1     after the hedge fired, before 14:00, when the stock is back at its entry price (CALL: 1-min
         high >= entry spot; PUT: 1-min low <= entry spot) and Supertrend(10,3) on the last closed
         5-min bar agrees with the trade -> one more lot of the same option. It exits with the
         original; own max loss 4,500.
  S2     on top of the hedge: a 2nd hedge lot when Supertrend turns against the trade (own 750 stop);
         when the two hedge lots together reach +4,000 the 2nd lot is sold and the 1st rides the
         trail with a floor at its cost. If the 1st lot exits first, both go.
  Overnight: the hedge and added lots are squared off at 15:15 the same day; Swing's own position
  keeps its own exits (its recorded P&L is unchanged).
Fills on 1-minute OHLC with conservative intra-minute ordering (stop before target), modelled
slippage on every leg (Bollinger.paper_book.modeled_slippage_pct). Two days of trades - an
illustration of what the rules would have done, not evidence that they work.

  fetch  download the 1-minute data needed (Dhan intraday charts, READ-ONLY). Needs
         HANDOFF_DHAN_ACCESS_TOKEN in the environment; never pin_totp (that would mint a new session
         and collide with the live bot's). Paced PACE_SECONDS per request - the account's data budget
         is shared with the live bot.
  run    cache only; prints the result.

Run:  HANDOFF_DHAN_ACCESS_TOKEN=<token> .venv/bin/python research_swing_hedge_scale_actual_trades.py fetch
      .venv/bin/python research_swing_hedge_scale_actual_trades.py run
"""
from __future__ import annotations

import bisect
import json
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pandas as pd

from Bollinger.paper_book import modeled_slippage_pct
from SuperTrader.strategy import atr, supertrend

IST = timezone(timedelta(hours=5, minutes=30))
DIR = Path("history/bt_swing_actual_hedge")
TRADES = DIR / "trades.json"
DAYS = ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01")
UNDERLYING_FROM, FETCH_TO = "2026-09-22", "2026-10-02"
REFRESH = "--refresh" in sys.argv          # re-download even when cached (today's data was partial)
MCX = ("COPPER", "NATURALGAS", "CRUDEOIL", "GOLD", "SILVER")      # a few sessions of warm-up for ATR / Supertrend
PACE_SECONDS = 1.5

HEDGE_TRIGGER, HEDGE_STOP, ARM, GIVEBACK = 1800, 1500, 1000, 0.30
MAX_LOSS, B_STOP, S2_TARGET = 4500, 750, 4000
S1_CUTOFF, SQUARE_OFF = dtime(14, 0), dtime(15, 15)


# --------------------------------------------------------------------------- #
# Instruments (local Dhan master - no network)
# --------------------------------------------------------------------------- #
def master() -> pd.DataFrame:
    """The last few daily masters together (newest wins) - contracts that expired on 29 Sep are only in
    the older ones."""
    cols = ["SEM_EXM_EXCH_ID", "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL", "SEM_LOT_UNITS",
            "SEM_CUSTOM_SYMBOL", "SEM_STRIKE_PRICE", "SEM_SERIES"]
    frames = []
    for f in sorted(Path(".").glob("Dependencies\\all_instrument *.csv"))[-3:]:
        df = pd.read_csv(f, low_memory=False, usecols=cols)
        frames.append(df[df["SEM_EXM_EXCH_ID"] == "NSE"])
    # ids repeat across segments (an equity and an option can share one), so de-duplicate on id + name
    return pd.concat(frames).drop_duplicates(["SEM_SMST_SECURITY_ID", "SEM_CUSTOM_SYMBOL"], keep="last")


MASTER = None


def mdf() -> pd.DataFrame:
    global MASTER
    if MASTER is None:
        MASTER = master()
    return MASTER


def contract(custom_symbol: str) -> dict:
    row = mdf()[mdf()["SEM_CUSTOM_SYMBOL"] == custom_symbol]
    if row.empty:
        raise ValueError(f"not in the master: {custom_symbol}")
    r = row.iloc[-1]
    return {"symbol": custom_symbol, "sid": str(int(r["SEM_SMST_SECURITY_ID"])), "lot": int(r["SEM_LOT_UNITS"]),
            "strike": float(r["SEM_STRIKE_PRICE"]), "inst": r["SEM_INSTRUMENT_NAME"], "seg": "NSE_FNO"}


def atm_opposite(trade_symbol: str, spot: float) -> dict:
    """The opposite-type option of the same underlying and expiry, strike nearest to spot."""
    sym, dd, mon, _strike, typ = trade_symbol.split(" ")
    other = "PUT" if typ == "CALL" else "CALL"
    prefix = f"{sym} {dd} {mon} "
    rows = mdf()[mdf()["SEM_CUSTOM_SYMBOL"].astype(str).str.startswith(prefix)
                 & mdf()["SEM_CUSTOM_SYMBOL"].astype(str).str.endswith(f" {other}")]
    if rows.empty:
        raise ValueError(f"no {other} strikes for {prefix}")
    r = rows.iloc[(rows["SEM_STRIKE_PRICE"].astype(float) - spot).abs().argsort().iloc[0]]
    return contract(r["SEM_CUSTOM_SYMBOL"])


def underlying(sym: str) -> dict:
    if sym in ("NIFTY", "BANKNIFTY"):
        return {"symbol": sym, "sid": "13" if sym == "NIFTY" else "25", "seg": "IDX_I", "inst": "INDEX"}
    row = mdf()[(mdf()["SEM_INSTRUMENT_NAME"] == "EQUITY") & (mdf()["SEM_TRADING_SYMBOL"] == sym)
                & (mdf()["SEM_SERIES"] == "EQ")]
    if row.empty:
        raise ValueError(f"no NSE equity row for {sym}")
    return {"symbol": sym, "sid": str(int(row.iloc[-1]["SEM_SMST_SECURITY_ID"])), "seg": "NSE_EQ", "inst": "EQUITY"}


# --------------------------------------------------------------------------- #
# Data cache
# --------------------------------------------------------------------------- #
def path_for(kind: str, name: str) -> Path:
    return DIR / kind / (name.replace(" ", "_").replace("/", "_") + ".json")


def load(kind: str, name: str) -> dict | None:
    p = path_for(kind, name)
    return json.loads(p.read_text()) if p.exists() else None


_DHAN = None


def dhan():
    global _DHAN
    if _DHAN is None:
        token = os.environ.get("HANDOFF_DHAN_ACCESS_TOKEN")
        if not token:
            raise SystemExit("HANDOFF_DHAN_ACCESS_TOKEN not set - refusing to fall back to pin_totp "
                             "(it would collide with the live bot's session).")
        client_id = os.environ.get("DHAN_CLIENT_ID")
        if not client_id:
            for line in Path(".env").read_text().splitlines():
                if line.startswith("DHAN_CLIENT_ID="):
                    client_id = line.split("=", 1)[1].strip().strip('"').strip("'")
        from dhanhq import DhanContext, dhanhq
        _DHAN = dhanhq(DhanContext(client_id, token))
    return _DHAN


def fetch(kind: str, inst: dict, from_date: str) -> dict | None:
    """1-minute bars for one instrument (cached). Dhan answers a rate-limit with status=failure, so retry."""
    cached = load(kind, inst["symbol"])
    if cached is not None and not REFRESH:
        return cached
    for attempt in range(4):
        time.sleep(PACE_SECONDS * (1 + 2 * attempt))
        try:
            resp = dhan().intraday_minute_data(inst["sid"], inst["seg"], inst["inst"], from_date, FETCH_TO, 1)
        except Exception as exc:  # noqa: BLE001
            print(f"   {inst['symbol']}: {exc!r} (attempt {attempt + 1})")
            continue
        data = resp.get("data") or {} if isinstance(resp, dict) else {}
        if isinstance(data, dict) and data.get("timestamp"):
            out = {"ts": [int(t) for t in data["timestamp"]], "o": data["open"], "h": data["high"], "l": data["low"],
                   "c": data["close"]}
            p = path_for(kind, inst["symbol"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(out))
            print(f"   {kind:10s} {inst['symbol']}: {len(out['ts'])} bars")
            return out
        print(f"   {inst['symbol']}: no data ({resp.get('remarks') if isinstance(resp, dict) else resp}) "
              f"(attempt {attempt + 1})")
    return None


# --------------------------------------------------------------------------- #
# Trades and series
# --------------------------------------------------------------------------- #
def ts_of(iso: str | None) -> int | None:
    return int(datetime.fromisoformat(iso).timestamp()) if iso else None


EXCLUDED: list[dict] = []


def trades() -> list[dict]:
    out = []
    EXCLUDED.clear()
    for j in json.loads(TRADES.read_text()):
        sym_full = j.get("option_trading_symbol") or j.get("trading_symbol") or ""
        t0 = ts_of(j["opened_at"])
        if datetime.fromtimestamp(t0, IST).date().isoformat() not in DAYS:
            continue                                   # entered before the window (overnight carry) - not in scope
        base = j.get("pnl_modeled") if j.get("pnl_modeled") is not None else j.get("pnl")
        why = ("MCX" if sym_full.split(" ")[0] in MCX else
               None if sym_full.endswith((" CALL", " PUT")) else "not an option")
        try:
            c = None if why else contract(sym_full)
        except ValueError:
            why = "contract not in the master"
        if why:
            EXCLUDED.append({"symbol": sym_full, "book": j["_book"], "t0": t0, "t1": ts_of(j.get("closed_at")),
                             "base": base, "why": why})
            continue
        qty = int(j.get("quantity") or j.get("pnl_multiplier") or c["lot"])
        out.append({"book": j["_book"], "symbol": sym_full.split(" ")[0], "contract": c, "qty": qty,
                    "typ": "CALL" if sym_full.endswith("CALL") else "PUT", "p0": float(j["entry_price"]), "t0": t0,
                    "t1": ts_of(j.get("closed_at")), "exit_px": j.get("exit_price"), "reason": j.get("exit_reason"),
                    "base": None if base is None else float(base)})
    return out


class Series:
    """1-min bars (bar START epochs) + 5-min ATR / Supertrend from the same data."""

    def __init__(self, d: dict):
        self.ts, self.o, self.h, self.l, self.c = d["ts"], d["o"], d["h"], d["l"], d["c"]

    def idx(self, t: int) -> int:
        """Last bar that has STARTED at or before t."""
        return bisect.bisect_right(self.ts, t) - 1


def five_min(s: Series) -> dict:
    b = {"ts": [], "h": [], "l": [], "c": []}
    key = None
    for t, h, l, c in zip(s.ts, s.h, s.l, s.c):
        k = t - (t % 300)
        if k != key:
            key = k
            b["ts"].append(k); b["h"].append(h); b["l"].append(l); b["c"].append(c)
        else:
            b["h"][-1] = max(b["h"][-1], h); b["l"][-1] = min(b["l"][-1], l); b["c"][-1] = c
    b["atr"] = atr(b["h"], b["l"], b["c"], 14)
    b["st"] = supertrend(b["h"], b["l"], b["c"], 10, 3.0)
    return b


def closed_bar(b: dict, t: int) -> int | None:
    """The last 5-min bar that has CLOSED by t."""
    k = bisect.bisect_right(b["ts"], t - 300) - 1
    return k if k >= 0 else None


def mod(p_in: float, p_out: float, qty: int) -> float:
    return (p_out * (1 - modeled_slippage_pct(p_out)) - p_in * (1 + modeled_slippage_pct(p_in))) * qty


def at(day: str, hm: dtime) -> int:
    return int(datetime.combine(date.fromisoformat(day), hm, IST).timestamp())


# --------------------------------------------------------------------------- #
# Overlays
# --------------------------------------------------------------------------- #
def hedge_point(tr: dict, opt: Series, und: Series, b5: dict) -> dict | None:
    """First minute the hedge rule fires (only on days in scope, 09:15-15:15, while the trade is open)."""
    p0, qty, sign = tr["p0"], tr["qty"], (1 if tr["typ"] == "CALL" else -1)
    i_spot0 = und.idx(tr["t0"])
    if i_spot0 < 0:
        return None
    spot0 = und.c[i_spot0]
    t_end = tr["t1"] if tr["t1"] is not None else opt.ts[-1] + 60
    for i, t in enumerate(opt.ts):
        if t <= tr["t0"] or t >= t_end:
            continue
        day = datetime.fromtimestamp(t, IST).date().isoformat()
        if day not in DAYS or t >= at(day, SQUARE_OFF):
            continue
        if (p0 - opt.l[i]) * qty < HEDGE_TRIGGER:
            continue
        k, j = closed_bar(b5, t), und.idx(t)
        if k is None or b5["atr"][k] is None or j < 0:
            continue
        if sign * (spot0 - und.c[j]) >= b5["atr"][k]:
            return {"t": t, "day": day, "spot": und.c[j], "spot0": spot0, "atr": b5["atr"][k]}
    return None


def hedge_leg(hp: dict, hm: Series, qty: int, b5: dict, against: int, add: bool) -> tuple[float, dict]:
    """The hedge (lot A) and, with add=True, S2's 2nd lot B. `against` = the Supertrend value that is
    against the base trade (CALL trade: -1, PUT trade: +1). -> (pnl, info)."""
    i0 = hm.idx(hp["t"])
    if i0 < 0 or hp["t"] - hm.ts[i0] > 300:
        return 0.0, {"no_price": True}
    q0, sq = hm.c[i0], at(hp["day"], SQUARE_OFF)
    a_stop, peak, pnl, b, booked = q0 - HEDGE_STOP / qty, 0.0, 0.0, None, False
    info = {"q0": q0, "t": hm.ts[i0], "added": False, "booked": False}
    for j in range(i0 + 1, len(hm.ts)):
        t = hm.ts[j]
        if t >= sq:
            pnl += mod(q0, hm.o[j], qty) + (mod(b["q"], hm.o[j], qty) if b and b["open"] else 0.0)
            return pnl, {**info, "exit": "15:15", "exit_t": t}
        if hm.l[j] <= a_stop:
            px = min(a_stop, hm.o[j])
            pnl += mod(q0, px, qty) + (mod(b["q"], hm.c[j], qty) if b and b["open"] else 0.0)
            return pnl, {**info, "exit": "floor" if booked else "stop", "exit_t": t}
        if b and b["open"] and hm.l[j] <= b["q"] - B_STOP / qty:
            pnl += mod(b["q"], min(b["q"] - B_STOP / qty, hm.o[j]), qty)
            b["open"] = False
        if b and b["open"] and not booked:
            lvl = (S2_TARGET / qty + q0 + b["q"]) / 2
            if hm.h[j] >= lvl:
                pnl += mod(b["q"], max(lvl, hm.o[j]), qty)
                b["open"], booked, info["booked"] = False, True, True
                a_stop = max(a_stop, q0)
        peak = max(peak, (hm.h[j] - q0) * qty)
        if peak >= ARM and (hm.c[j] - q0) * qty <= peak * (1 - GIVEBACK):
            pnl += mod(q0, hm.c[j], qty) + (mod(b["q"], hm.c[j], qty) if b and b["open"] else 0.0)
            return pnl, {**info, "exit": "trail", "exit_t": t, "peak": round(peak)}
        if add and b is None:
            k = closed_bar(b5, t)
            if k is not None and b5["st"][k] == against:
                b = {"q": hm.c[j], "open": True}
                info["added"] = True
    pnl += mod(q0, hm.c[-1], qty) + (mod(b["q"], hm.c[-1], qty) if b and b["open"] else 0.0)
    return pnl, {**info, "exit": "data_end", "exit_t": hm.ts[-1]}


def s1_leg(tr: dict, hp: dict, opt: Series, und: Series, b5: dict) -> tuple[float, dict]:
    """S1: one more lot of the trade's own option when the stock is back at its entry price."""
    sign, qty = (1 if tr["typ"] == "CALL" else -1), tr["qty"]
    sq, cutoff = at(hp["day"], SQUARE_OFF), at(hp["day"], S1_CUTOFF)
    t_end = tr["t1"] if tr["t1"] is not None else opt.ts[-1] + 60
    ia = None
    for i, t in enumerate(opt.ts):
        if t <= hp["t"] or t >= min(cutoff, t_end):
            continue
        j = und.idx(t)
        if j < 0:
            continue
        back = und.h[j] >= hp["spot0"] if sign == 1 else und.l[j] <= hp["spot0"]
        k = closed_bar(b5, t)
        if back and k is not None and b5["st"][k] == sign:
            ia = i
            break
    if ia is None:
        return 0.0, {}
    p2 = opt.c[ia]
    info = {"t": opt.ts[ia], "p2": p2}
    for j in range(ia + 1, len(opt.ts)):
        t = opt.ts[j]
        if tr["t1"] is not None and t >= tr["t1"] and tr["t1"] < sq:      # exits with the original
            return mod(p2, float(tr["exit_px"]), qty), {**info, "exit": "with original"}
        if t >= sq:
            return mod(p2, opt.o[j], qty), {**info, "exit": "15:15"}
        if opt.l[j] <= p2 - MAX_LOSS / qty:
            return mod(p2, min(p2 - MAX_LOSS / qty, opt.o[j]), qty), {**info, "exit": "max loss"}
    return mod(p2, opt.c[-1], qty), {**info, "exit": "data_end"}


# --------------------------------------------------------------------------- #
def do_fetch() -> None:
    trs = trades()
    print(f"{len(trs)} trades in scope; fetching underlyings and the traded contracts (paced {PACE_SECONDS}s)")
    for sym in sorted({t["symbol"] for t in trs}):
        fetch("underlying", underlying(sym), UNDERLYING_FROM)
    for name in sorted({t["contract"]["symbol"] for t in trs}):
        fetch("option", contract(name), DAYS[0])
    print("hedge contracts:")
    for tr in trs:
        u, o = load("underlying", tr["symbol"]), load("option", tr["contract"]["symbol"])
        if not u or not o:
            continue
        und = Series(u)
        hp = hedge_point(tr, Series(o), und, five_min(und))
        if hp:
            fetch("option", atm_opposite(tr["contract"]["symbol"], hp["spot"]), DAYS[0])
    print("fetch done")


def do_run() -> None:
    trs = trades()
    rows, missing = [], []
    for tr in trs:
        u, o = load("underlying", tr["symbol"]), load("option", tr["contract"]["symbol"])
        if not u or not o:
            missing.append(tr["contract"]["symbol"])
            EXCLUDED.append({"symbol": tr["contract"]["symbol"], "book": tr["book"], "t0": tr["t0"], "t1": tr["t1"],
                             "base": tr["base"], "why": "no price data - expired contract" if " 29 SEP " in
                             tr["contract"]["symbol"] else "no price data"})
            continue
        und, opt = Series(u), Series(o)
        b5 = five_min(und)
        hp = hedge_point(tr, opt, und, b5)
        r = {"tr": tr, "hp": hp, "hedge": 0.0, "s2": 0.0, "s1": 0.0, "hinfo": {}, "s2info": {}, "s1info": {}}
        if hp:
            hc = atm_opposite(tr["contract"]["symbol"], hp["spot"])
            hd = load("option", hc["symbol"])
            r["hedge_contract"] = hc["symbol"]
            if hd is None:
                missing.append(hc["symbol"])
            else:
                against = -1 if tr["typ"] == "CALL" else 1
                r["hedge"], r["hinfo"] = hedge_leg(hp, Series(hd), tr["qty"], b5, against, add=False)
                s2_total, r["s2info"] = hedge_leg(hp, Series(hd), tr["qty"], b5, against, add=True)
                r["s2"] = s2_total - r["hedge"]
            r["s1"], r["s1info"] = s1_leg(tr, hp, opt, und, b5)
        rows.append(r)

    hhmm = lambda t: datetime.fromtimestamp(t, IST).strftime("%d %b %H:%M") if t else "open"
    money = lambda v: "open" if v is None else f"{v:+,.0f}"
    print("Swing trades entered 28 Sep - 1 Oct (NSE options). base = Swing's own recorded P&L (paper: modelled "
          "with slippage; real: the actual P&L); hedge / S1 / S2 = legs the Super Bollinger rules add, after "
          "modelled slippage.\n")
    head = (f"{'trade':34s} {'book':6s} {'in':>12s} {'out':>12s} {'base':>8s} | {'hedge':>7s} {'S1':>7s} {'S2':>7s} "
            f"| {'w/ hedge+S1':>11s}  detail")
    grand = {"base": 0.0, "hedge": 0.0, "s1": 0.0, "s2": 0.0, "n": 0, "wins": 0}
    by_day: dict = {}
    for r in rows:
        by_day.setdefault(datetime.fromtimestamp(r["tr"]["t0"], IST).date().isoformat(), []).append(r)
    for day in sorted(by_day):
        print(f"=== {day} ===\n{head}")
        tot = {"base": 0.0, "hedge": 0.0, "s1": 0.0, "s2": 0.0}
        for r in sorted(by_day[day], key=lambda x: x["tr"]["t0"]):
            tr = r["tr"]
            detail = ""
            if r["hp"]:
                h = r["hinfo"]
                detail = (f"hedge {hhmm(r['hp']['t'])[7:]} {r.get('hedge_contract', '?')} @ {h.get('q0', 0):.2f} -> "
                          f"{h.get('exit', 'NO DATA')}"
                          + (f"; S1 add {hhmm(r['s1info']['t'])[7:]} @ {r['s1info']['p2']:.2f} -> {r['s1info']['exit']}"
                             if r["s1info"] else "; S1 no add")
                          + ("; S2 2nd lot" + (" booked" if r["s2info"].get("booked") else "")
                             if r["s2info"].get("added") else ""))
            base = tr["base"]
            combined = None if base is None else base + r["hedge"] + r["s1"]
            print(f"{tr['contract']['symbol']:34s} {tr['book']:6s} {hhmm(tr['t0']):>12s} {hhmm(tr['t1']):>12s} "
                  f"{money(base):>8s} | {r['hedge']:>+7,.0f} {r['s1']:>+7,.0f} {r['s2']:>+7,.0f} | {money(combined):>11s}  "
                  f"{detail}")
            tot["base"] += base or 0.0
            for k in ("hedge", "s1", "s2"):
                tot[k] += r[k]
            if base is not None:
                grand["n"] += 1
                grand["wins"] += base > 0
        print(f"{'day total':34s} {'':6s} {'':>12s} {'':>12s} {tot['base']:>+8,.0f} | {tot['hedge']:>+7,.0f} "
              f"{tot['s1']:>+7,.0f} {tot['s2']:>+7,.0f} | {tot['base'] + tot['hedge'] + tot['s1']:>+11,.0f}\n")
        for k in tot:
            grand[k] += tot[k]
    print(f"{len(rows)} NSE option trades ({grand['n']} closed, {grand['wins']} winners; {len(rows) - grand['n']} still "
          f"open), hedge fired on {sum(1 for r in rows if r['hp'])}, S1 added on {sum(1 for r in rows if r['s1info'])}, "
          f"S2 2nd lot on {sum(1 for r in rows if r['s2info'].get('added'))}")
    print(f"Swing as traded (closed trades):  {grand['base']:+,.0f}")
    print(f"  + hedge:                         {grand['base'] + grand['hedge']:+,.0f}   (hedge legs {grand['hedge']:+,.0f})")
    print(f"  + hedge + S1:                    {grand['base'] + grand['hedge'] + grand['s1']:+,.0f}   "
          f"(S1 legs {grand['s1']:+,.0f})")
    print(f"  + hedge + S1 + S2:               {grand['base'] + grand['hedge'] + grand['s1'] + grand['s2']:+,.0f}   "
          f"(S2 legs {grand['s2']:+,.0f})")
    all_base = grand["base"] + sum(e["base"] or 0 for e in EXCLUDED)
    print(f"\nAll Swing trades entered 28 Sep - 1 Oct (tested + not tested, closed): {all_base:+,.0f}")
    if EXCLUDED:
        print(f"\nNot tested ({len(EXCLUDED)} trades, P&L {sum(e['base'] or 0 for e in EXCLUDED):+,.0f}):")
        for e in sorted(EXCLUDED, key=lambda x: x["t0"]):
            print(f"  {e['symbol']:34s} {e['book']:10s} {hhmm(e['t0']):>12s} {hhmm(e['t1']):>12s} {money(e['base']):>8s}  "
                  f"({e['why']})")
    if missing:
        print(f"\nNO PRICE DATA (trade left out / leg not priced): {sorted(set(missing))}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "fetch":
        do_fetch()
    do_run()
