"""
Swing on NIFTY / BANKNIFTY: its live rules on 5-minute candles (as deployed) vs the same rules on
1-minute candles (1 Oct 2026, user: "For Nifty and BankNifty, just re run SWING with 1 min time frame
rather than 5 min candle for last couple of days and show me PnL").

Days: 29 Sep, 30 Sep and 1 Oct 2026 (today up to the data's last minute). Signal = the live v3 entry
(Swing/trading_engine._evaluate_entry_signal), computed on the index's own spot candles:
  BULLISH: ((15-min Supertrend green) OR (trend-aware: fast EMA200 > 15-min EMA200 and the gap has widened
            over 8 fast candles) OR (fast EMA200 > 15-min EMA200)) AND fast close crossed above the fast
            Supertrend(10,3)
           OR (Day Range Bull: today's open > yesterday's close, fast close > today's open, fast close above
            the fast Supertrend, RSI(14) crossing above 60 - AND the trend-aware leg)
  BEARISH: the mirror.
  "fast" = 5-min (live) or 1-min (this test); the 15-min layer is unchanged. Entries on a CLOSED fast candle
  (live SWING_ENTRY_TIMING=bar_close), the 15-min layer from its last CLOSED candle. Re-entry rule as live:
  after a trade on one side, that side is blocked until the EMA200 regime has been seen on the other side.
Exits = live Swing for options: max loss 4,500; target +35%; profit protection once peak > 3,000, 2%
  giveback; hard stop -20%; Supertrend reversal on the tick (live SWING_EXIT_TIMING=tick: the spot crossing
  the last closed fast candle's Supertrend line against the trade, not on the entry candle); index daily
  square-off 15:25. Price exits checked every minute on the option's 1-min close in BOTH runs, so the only
  difference between the runs is the candle length the signals use.
Option = ATM CE / PE of the nearest expiry after the trade date (as live), 1 lot (NIFTY 65, BANKNIFTY 30),
  priced at the 1-min close of the minute the signal candle closes. P&L shown raw and with the modelled
  slippage of Swing's paper book.
Data: Dhan intraday 1-min (READ-ONLY), HANDOFF_DHAN_ACCESS_TOKEN only (never pin_totp), cached under
  history/bt_swing_index_tf/. Three days - an illustration, not evidence.

Run: HANDOFF_DHAN_ACCESS_TOKEN=<token> .venv/bin/python research_swing_index_1min_vs_5min.py
"""
from __future__ import annotations

import bisect
import sys
from datetime import date, datetime, time as dtime

import research_swing_hedge_scale_actual_trades as R
from Options.dhan_client import _compute_ema, _compute_rsi, _compute_supertrend
from Swing import config as sc

IST = R.IST
R.DIR = R.Path("history/bt_swing_index_tf")
DAYS = ("2026-09-29", "2026-09-30", "2026-10-01")
SPOT_FROM, FETCH_TO = "2026-09-01", "2026-10-02"
OPT_FROM = "2026-09-29"
REFRESH = "--refresh" in sys.argv
INDEX = {"NIFTY": {"sid": "13", "lot": 65}, "BANKNIFTY": {"sid": "25", "lot": 30}}

MAX_LOSS = sc.MAX_LOSS_PROTECTION_RS                    # 4,500
TARGET_PCT = sc.TARGET_PCT_NON_COPPER                   # 0.35
HARD_STOP_PCT = sc.HARD_STOP_LOSS_PCT                   # 0.20
PP_RS = sc.PROFIT_PROTECTION_RS_OPTIONS                 # 3,000
PP_GIVEBACK = sc.PROFIT_PROTECTION_GIVEBACK_PCT_OPTIONS  # 0.02
GAP_LOOKBACK = sc.REGIME_GAP_WIDENING_LOOKBACK_CANDLES  # 8
SQUARE_OFF = dtime(15, 25)


def resample(s: dict, minutes: int) -> dict:
    """1-min bars -> `minutes` bars anchored at 09:15 each day (bar START epochs)."""
    out = {"ts": [], "o": [], "h": [], "l": [], "c": []}
    key = None
    for t, o, h, l, c in zip(s["ts"], s["o"], s["h"], s["l"], s["c"]):
        dt = datetime.fromtimestamp(t, IST)
        mins = dt.hour * 60 + dt.minute - (9 * 60 + 15)
        k = (dt.date(), mins // minutes)
        if k != key:
            key = k
            start = int(datetime.combine(dt.date(), dtime(9, 15), IST).timestamp()) + (mins // minutes) * minutes * 60
            out["ts"].append(start); out["o"].append(o); out["h"].append(h); out["l"].append(l); out["c"].append(c)
        else:
            out["h"][-1] = max(out["h"][-1], h); out["l"][-1] = min(out["l"][-1], l); out["c"][-1] = c
    return out


def signals(fast: dict, slow: dict, fast_min: int) -> dict:
    """Per fast bar k (known at its close ts[k] + fast_min*60): entry flags, Supertrend line, regime side."""
    n = len(fast["ts"])
    ema_f = _compute_ema(fast["c"], 200)
    ema_s = _compute_ema(slow["c"], 200)
    st_f = _compute_supertrend(fast["h"], fast["l"], fast["c"], period=10, multiplier=3.0)
    st_s = _compute_supertrend(slow["h"], slow["l"], slow["c"], period=10, multiplier=3.0)
    slow_end = [t + 900 for t in slow["ts"]]
    rsi = _compute_rsi(fast["c"], 14)
    regime, gap = [None] * n, [None] * n
    st15_above = [None] * n
    for k in range(n):
        j = bisect.bisect_right(slow_end, fast["ts"][k] + fast_min * 60) - 1      # last CLOSED 15-min candle
        if j < 0:
            continue
        if ema_f[k] is not None and ema_s[j] is not None:
            regime[k] = ema_f[k] > ema_s[j]
            gap[k] = ema_f[k] - ema_s[j]
        if st_s[j] is not None:
            st15_above[k] = slow["c"][j] > st_s[j]
    above = [None if st_f[k] is None else fast["c"][k] > st_f[k] for k in range(n)]
    day_open, prev_close, days = {}, {}, []
    for k in range(n):
        d = datetime.fromtimestamp(fast["ts"][k], IST).date()
        if d not in day_open:
            day_open[d] = fast["o"][k]
            prev_close[d] = fast["c"][k - 1] if k > 0 else None
    bull, bear = [False] * n, [False] * n
    for k in range(1, n):
        if regime[k] is None or above[k] is None or above[k - 1] is None:
            continue
        crossed_up, crossed_dn = (not above[k - 1]) and above[k], above[k - 1] and not above[k]
        widened = k >= GAP_LOOKBACK and gap[k - GAP_LOOKBACK] is not None and (
            gap[k] > gap[k - GAP_LOOKBACK] if regime[k] else gap[k] < gap[k - GAP_LOOKBACK])
        trend_bull, trend_bear = regime[k] and widened, (not regime[k]) and widened
        d = datetime.fromtimestamp(fast["ts"][k], IST).date()
        o, pc = day_open[d], prev_close[d]
        rsi_up = rsi[k] is not None and rsi[k - 1] is not None and rsi[k - 1] <= 60 < rsi[k]
        rsi_dn = rsi[k] is not None and rsi[k - 1] is not None and rsi[k - 1] >= 40 > rsi[k]
        dr_bull = pc is not None and o > pc and fast["c"][k] > o and above[k] and rsi_up
        dr_bear = pc is not None and o < pc and fast["c"][k] < o and (not above[k]) and rsi_dn
        bull[k] = ((st15_above[k] is True) or trend_bull or regime[k]) and crossed_up or (dr_bull and trend_bull)
        bear[k] = ((st15_above[k] is False) or trend_bear or (not regime[k])) and crossed_dn or (dr_bear and trend_bear)
    return {"bull": bull, "bear": bear, "st": st_f, "regime": regime}


def atm(index: str, opt_type: str, spot: float, on: date) -> dict:
    m = R.mdf()
    rows = m[(m["SEM_INSTRUMENT_NAME"] == "OPTIDX") & m["SEM_TRADING_SYMBOL"].astype(str).str.startswith(f"{index}-")
             & m["SEM_CUSTOM_SYMBOL"].astype(str).str.endswith(" CALL" if opt_type == "CE" else " PUT")]
    exp = {}
    for cs in rows["SEM_CUSTOM_SYMBOL"].astype(str):
        dd, mon = cs.split(" ")[1:3]
        exp[cs] = datetime.strptime(f"{dd} {mon} 2026", "%d %b %Y").date()
    rows = rows.assign(_exp=[exp[c] for c in rows["SEM_CUSTOM_SYMBOL"].astype(str)])
    rows = rows[rows["_exp"] > on]
    rows = rows[rows["_exp"] == rows["_exp"].min()]
    r = rows.iloc[(rows["SEM_STRIKE_PRICE"].astype(float) - spot).abs().argsort().iloc[0]]
    return {"symbol": r["SEM_CUSTOM_SYMBOL"], "sid": str(int(r["SEM_SMST_SECURITY_ID"])), "lot": int(r["SEM_LOT_UNITS"])}


def option_series(c: dict) -> R.Series | None:
    d = R.load("option", c["symbol"])
    if d is None:
        d = R.fetch("option", {"symbol": c["symbol"], "sid": c["sid"], "seg": "NSE_FNO", "inst": "OPTIDX"}, OPT_FROM)
    return R.Series(d) if d else None


def simulate(index: str, spot: dict, fast_min: int) -> list[dict]:
    fast = spot if fast_min == 1 else resample(spot, fast_min)
    slow = resample(spot, 15)
    sig = signals(fast, slow, fast_min)
    close_at = {t + fast_min * 60: k for k, t in enumerate(fast["ts"])}      # fast candle k is complete at this time
    trades, pos, consumed, last_k = [], None, None, None
    for i, t in enumerate(spot["ts"]):
        now = t + 60                                                             # the moment this minute closes
        day = datetime.fromtimestamp(t, IST).date()
        if day.isoformat() not in DAYS:
            continue
        hhmm = datetime.fromtimestamp(now, IST).time()
        if now in close_at:
            last_k = close_at[now]
            reg = sig["regime"][last_k]
            if consumed is not None and reg is not None and (1 if reg else -1) != consumed:
                consumed = None                                                  # the regime left the traded side
        if pos is not None:
            j = pos["opt"].idx(t)
            px = pos["opt"].c[j] if j >= 0 and pos["opt"].ts[j] >= pos["t_in"] - 60 else None
            reason = None
            if px is not None:
                pos["best"] = max(pos["best"], px)
                pnl = (px - pos["p0"]) * pos["qty"]
                peak = (pos["best"] - pos["p0"]) * pos["qty"]
                if -pnl >= MAX_LOSS:
                    reason = "MAX_LOSS_HIT"
                elif px >= pos["p0"] * (1 + TARGET_PCT):
                    reason = "TARGET_HIT"
                elif peak > PP_RS and px <= pos["best"] * (1 - PP_GIVEBACK):
                    reason = "PROFIT_PROTECTION_HIT"
                elif px <= pos["p0"] * (1 - HARD_STOP_PCT):
                    reason = "STOP_LOSS_HIT"
            if reason is None and last_k is not None and last_k > pos["k_in"] and sig["st"][last_k] is not None:
                line = sig["st"][last_k]
                if (pos["side"] == 1 and spot["l"][i] < line) or (pos["side"] == -1 and spot["h"][i] > line):
                    reason = "SUPERTREND_REVERSAL_TICK"
            if reason is None and hhmm >= SQUARE_OFF:
                reason = "INDEX_DAILY_SQUARE_OFF"
            if reason and px is not None:
                pos.update({"t_out": now, "exit": px, "reason": reason, "pnl": (px - pos["p0"]) * pos["qty"],
                            "pnl_modeled": R.mod(pos["p0"], px, pos["qty"])})
                trades.append(pos)
                pos = None
        if pos is None and now in close_at and hhmm < SQUARE_OFF:
            k = close_at[now]
            side = 1 if sig["bull"][k] else -1 if sig["bear"][k] else 0
            if side and (consumed is None or consumed != side):
                c = atm(index, "CE" if side == 1 else "PE", spot["c"][i], day)
                opt = option_series(c)
                j = opt.idx(t) if opt else -1
                if opt and j >= 0 and t - opt.ts[j] <= 300:
                    pos = {"index": index, "contract": c["symbol"], "side": side, "qty": c["lot"], "t_in": now,
                           "k_in": k, "p0": opt.c[j], "best": opt.c[j], "opt": opt}
                    consumed = side
    if pos is not None:
        trades.append({**pos, "t_out": None, "exit": None, "reason": "open", "pnl": None, "pnl_modeled": None})
    return trades


def main() -> None:
    results = {}
    for index, meta in INDEX.items():
        R.REFRESH = REFRESH
        spot = R.fetch("spot", {"symbol": index, "sid": meta["sid"], "seg": "IDX_I", "inst": "INDEX"}, SPOT_FROM)
        R.REFRESH = False
        if not spot:
            print(f"{index}: no spot data")
            continue
        for fm in (5, 1):
            results[(index, fm)] = simulate(index, spot, fm)
    hm = lambda t: datetime.fromtimestamp(t, IST).strftime("%d %b %H:%M") if t else "open"
    print("\nSWING on NIFTY / BANKNIFTY - live rules on 5-min candles (as deployed) vs 1-min candles, "
          f"{DAYS[0]} .. {DAYS[-1]} (1 Oct up to the data's last minute)\n")
    summary = []
    for (index, fm), trades in results.items():
        closed = [t for t in trades if t["pnl"] is not None]
        raw, modeled = sum(t["pnl"] for t in closed), sum(t["pnl_modeled"] for t in closed)
        wins = sum(1 for t in closed if t["pnl_modeled"] > 0)
        summary.append((index, fm, len(trades), len(closed), wins, raw, modeled))
        print(f"=== {index} - {fm}-min candles: {len(trades)} trades ===")
        for t in trades:
            pnl = "open" if t["pnl"] is None else f"{t['pnl']:+8,.0f} ({t['pnl_modeled']:+8,.0f})"
            ex = "" if t["exit"] is None else f"{t['exit']:8.2f}"
            print(f"  {t['contract']:26s} {hm(t['t_in']):>12s} -> {hm(t['t_out']):>12s}  {t['p0']:8.2f} -> {ex:>8s}  "
                  f"{t['reason']:24s} {pnl}")
        print()
    print(f"{'index':10s} {'candles':>7s} {'trades':>6s} {'wins':>5s} | {'P&L raw':>9s} {'after slippage':>14s}")
    for index, fm, n, nc, wins, raw, modeled in summary:
        print(f"{index:10s} {fm:>5d}-m {n:>6d} {wins:>5d} | {raw:>+9,.0f} {modeled:>+14,.0f}")
    for fm in (5, 1):
        rows = [s for s in summary if s[1] == fm]
        print(f"{'both':10s} {fm:>5d}-m {sum(s[2] for s in rows):>6d} {sum(s[4] for s in rows):>5d} | "
              f"{sum(s[5] for s in rows):>+9,.0f} {sum(s[6] for s in rows):>+14,.0f}")


if __name__ == "__main__":
    main()
