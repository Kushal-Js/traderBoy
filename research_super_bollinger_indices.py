"""
Super Bollinger on NIFTY / BANKNIFTY (30 Sep 2026, user request: "include
indexes nifty and bank nifty also for our testing ... after market hours").

Super Bollinger live trades stocks only. This measures, over 3 Aug - 29 Sep,
with the unchanged Super Bollinger rules and real option prices:
  A. the two indices on their own - CE only, and CE + PE (bearish triggers);
  B. the HYBRID stock portfolio (weekly picks from the August walk-forward)
     with the indices ALWAYS allowed, sharing the same 5 slots - does adding
     them raise the total, or just take slots from better stock trades?
  C. the live hedge rule (Rs 2,000 CE loss + 1x ATR drop -> ATM PE, trail
     exit, Rs 1,500 PE stop) on the index CE trades.

Index option prices: NIFTY weeklies and BANKNIFTY monthlies from Dhan's
expired-options data (/charts/rollingoption, OPTIDX; expiry_code 1 = the
nearest expiry incl. an expiry day's own, 2 = next - validated for NIFTY on
29 Sep), rebuilt for the fixed entry strike; 29 Sep from the listed Oct
contracts. Same roll rule as live (next expiry within 2 trading days).

Run after the August stock run (needs its history/bt_walkforward_long cache
and HYBRID picks), after 15:30 IST:
    HANDOFF_DHAN_ACCESS_TOKEN=... uv run python research_super_bollinger_indices.py fetch
    HANDOFF_DHAN_ACCESS_TOKEN=... uv run python research_super_bollinger_indices.py run
"""
from __future__ import annotations

import bisect
import io
import contextlib
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import walkforward_selector_eval as wf
from walkforward_selector_eval import IST, dhan_wrapper, _retry
import backtest_super_bollinger_universe_30day as u
import backtest_super_trader_30day as st
import research_stock_selection_walkforward as r
import research_selection_walkforward_aug as aug
from Bollinger.paper_book import modeled_slippage_pct
from SuperTrader.strategy import atr as atr_series

LONG = aug.LONG
INDEX = {"NIFTY": ("13", "WEEK"), "BANKNIFTY": ("25", "MONTH")}
WINDOW_FROM, WINDOW_TO = date(2026, 8, 3), date(2026, 9, 29)
ROLL_DAYS = 2


def index_expiries(sym: str) -> list[date]:
    out, d = [], date(2026, 7, 20)
    while d <= date(2026, 11, 30):
        if d.weekday() == 1 and (sym == "NIFTY" or d == wf.last_tuesday(d.year, d.month)):
            out.append(d)
        d += timedelta(days=1)
    return out


def index_contract(sym: str, day: date) -> tuple[date, int]:
    ahead = [e for e in index_expiries(sym) if e >= day]
    idx = 1 if st.trading_days_to_expiry(ahead[0], day) <= ROLL_DAYS else 0
    return ahead[idx], idx + 1


class IndexPricer(st.OptionPricer):
    """st.OptionPricer + NIFTY/BANKNIFTY (OPTIDX) support."""

    def _rolling(self, sym, day, code, ot, offset):
        if sym not in INDEX:
            return super()._rolling(sym, day, code, ot, offset)
        tag = "ATM" if offset == 0 else f"ATM{offset:+d}"
        cache = st.OUT / "options" / sym / f"{day}_c{code}_{ot}_{tag}.json"
        cached = st._load(cache)
        if cached is not None:
            return cached
        sid, flag = INDEX[sym]
        try:
            resp = _retry(dhan_wrapper.client.Dhan.expired_options_data, security_id=sid, exchange_segment="NSE_FNO",
                          instrument_type="OPTIDX", expiry_flag=flag, expiry_code=code, strike=tag,
                          drv_option_type="CALL" if ot == "CE" else "PUT",
                          required_data=["open", "high", "low", "close", "strike", "spot"],
                          from_date=day.isoformat(), to_date=(day + timedelta(days=1)).isoformat(), interval=1)
            leg = (((resp or {}).get("data") or {}).get("data") or {}).get(ot.lower()) or {}
        except Exception as exc:  # noqa: BLE001
            print(f"    index rolling fetch failed {sym} {day} c{code} {ot} {tag}: {exc!r}", flush=True)
            leg = {}
        self.calls += 1
        out = {"timestamps": [int(t) for t in leg.get("timestamp") or []], "opens": leg.get("open") or [],
               "highs": leg.get("high") or [], "lows": leg.get("low") or [], "closes": leg.get("close") or [],
               "strikes": leg.get("strike") or [], "spots": leg.get("spot") or []}
        st._save(cache, out)
        time.sleep(st.PACE)
        return out

    def _index_rows(self, sym):
        df = dhan_wrapper.instruments()
        return df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTIDX")
                  & df["SEM_TRADING_SYMBOL"].astype(str).str.startswith(sym + "-")]

    def lot(self, sym):
        if sym not in INDEX:
            return super().lot(sym)
        if sym not in self._lots:
            rows = self._index_rows(sym)
            self._lots[sym] = int(float(rows.iloc[0]["SEM_LOT_UNITS"])) if not rows.empty else None
        return self._lots[sym]

    def open_leg(self, sym, day, side, entry_t, until_t, spot):
        if sym not in INDEX:
            return super().open_leg(sym, day, side, entry_t, until_t, spot)
        ot = "CE" if side == "LONG" else "PE"
        expiry, code = index_contract(sym, day)
        if day <= st.ROLLING_LAST_DAY:
            strike, mins = self.rolling_fixed_strike(sym, day, code, ot, entry_t, until_t)
        else:
            rows = self._index_rows(sym)
            rows = rows[(rows["SEM_EXPIRY_DATE"].astype(str).str.startswith(expiry.isoformat())) & (rows["SEM_OPTION_TYPE"] == ot)]
            if rows.empty:
                return None
            rows = rows.assign(dist=(rows["SEM_STRIKE_PRICE"] - spot).abs()).sort_values("dist")
            row = rows.iloc[0]
            sid = str(int(row["SEM_SMST_SECURITY_ID"]))
            cache = st.OUT / "options" / sym / f"listed_{sid}.json"
            data = st._load(cache)
            if data is None:
                resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data, security_id=sid, exchange_segment="NSE_FNO",
                              instrument_type="OPTIDX", from_date=day.isoformat(),
                              to_date=(day + timedelta(days=1)).isoformat(), interval=1)
                d = (resp or {}).get("data") or {}
                data = {"timestamps": [int(t) for t in d.get("timestamp") or []], "opens": d.get("open") or [],
                        "highs": d.get("high") or [], "lows": d.get("low") or [], "closes": d.get("close") or []}
                self.calls += 1
                st._save(cache, data)
            strike = float(row["SEM_STRIKE_PRICE"])
            mins = st.Minutes(data["timestamps"], data["opens"], data["highs"], data["lows"], data["closes"])
        if mins is None or not mins.ts:
            return None
        return {"ot": ot, "expiry": expiry, "strike": strike, "mins": mins}


def fetch() -> None:
    (LONG / "underlying").mkdir(parents=True, exist_ok=True)
    (LONG / "underlying_1m").mkdir(parents=True, exist_ok=True)
    for sym, (sid, _flag) in INDEX.items():
        f5, f1 = LONG / "underlying" / f"{sym}_5min.json", LONG / "underlying_1m" / f"{sym}_1min.json"
        if not f5.exists():
            f5.write_text(json.dumps(aug._bars([aug._get(sid, "IDX_I", "INDEX", a, b, 5) for a, b in aug.FIVE_CHUNKS])))
        if not f1.exists():
            f1.write_text(json.dumps(aug._bars([aug._get(sid, "IDX_I", "INDEX", aug.ONE_MIN_FROM, aug.ONE_MIN_TO, 1)])))
        print(sym, "5m bars", len(json.loads(f5.read_text())["closes"]), "1m bars", len(json.loads(f1.read_text())["closes"]), flush=True)


def hedge_pnl(trades: list[dict], pricer: IndexPricer, stop_rs=1500, trig=2000, arm=1000, giveback=0.40) -> dict:
    """The live hedge rule on each CE trade (see SuperBollinger/supervisor.py). Returns {trade idx: pnl}."""
    out = {}
    for k, t in enumerate(trades):
        if t.get("side", "LONG") != "LONG":
            continue
        sym, d = t["symbol"], date.fromisoformat(t["day"])
        m = json.loads((LONG / "underlying_1m" / f"{sym}_1min.json").read_text())
        f5 = json.loads((LONG / "underlying" / f"{sym}_5min.json").read_text())
        a5 = atr_series(f5["highs"], f5["lows"], f5["closes"], 14)
        t0 = int(datetime.combine(d, datetime.strptime(t["entry_time"], "%H:%M").time(), IST).timestamp())
        t1 = int(datetime.combine(d, datetime.strptime(t["exit_time"], "%H:%M").time(), IST).timestamp())
        sq = int(datetime.combine(d, dtime(15, 15), IST).timestamp())
        spot0 = m["closes"][bisect.bisect_right(m["timestamps"], t0) - 1]
        ce = pricer.open_leg(sym, d, "LONG", t0, sq, spot0)
        if not ce:
            continue
        cm, p0, qty = ce["mins"], t["entry"], t["qty"]
        th = None
        for i, tt in enumerate(cm.ts):
            if tt <= t0 or tt > t1 or (p0 - cm.l[i]) * qty < trig or datetime.fromtimestamp(tt, IST).strftime("%H:%M") >= "15:00":
                continue
            kb = bisect.bisect_right(f5["timestamps"], tt - 300) - 1
            spot = m["closes"][bisect.bisect_right(m["timestamps"], tt) - 1]
            if kb >= 0 and a5[kb] is not None and spot0 - spot >= a5[kb]:
                th = tt
                break
        if th is None:
            continue
        pe = pricer.open_leg(sym, d, "SHORT", th, sq, m["closes"][bisect.bisect_right(m["timestamps"], th) - 1])
        if not pe:
            continue
        pm = pe["mins"]
        i0 = bisect.bisect_right(pm.ts, th) - 1
        if i0 < 0 or th - pm.ts[i0] > 300:
            continue
        q0 = pm.c[i0]
        pnl = lambda px: (px * (1 - modeled_slippage_pct(px)) - q0 * (1 + modeled_slippage_pct(q0))) * qty  # noqa: E731
        peak, res = 0.0, None
        for j in range(i0 + 1, len(pm.ts)):
            if pm.ts[j] >= sq:
                res = pnl(pm.o[j]); break
            if (q0 - pm.l[j]) * qty >= stop_rs:
                res = pnl(q0 - stop_rs / qty); break
            peak = max(peak, (pm.h[j] - q0) * qty)
            if peak >= arm and (pm.c[j] - q0) * qty <= peak * (1 - giveback):
                res = pnl(pm.c[j]); break
        out[k] = res if res is not None else pnl(pm.c[-1])
    return out


def report(label, tr, as_of=None, extra=0.0):
    s = u.summarize(tr)
    side = defaultdict(lambda: [0, 0.0])
    sym = defaultdict(lambda: [0, 0.0])
    for t in tr:
        side[t.get("side", "LONG")][0] += 1
        side[t.get("side", "LONG")][1] += t["pnl_modeled"]
        sym[t["symbol"]][0] += 1
        sym[t["symbol"]][1] += t["pnl_modeled"]
    aug_pnl = sum(t["pnl_modeled"] for t in tr if t["day"] < "2026-08-31")
    print(f"{label:48s} trades {s.get('n', 0):3d} win {s.get('win_pct', 0)}% net {s.get('net', 0):>9,}  (Aug {aug_pnl:>9,.0f} / Sep {s.get('net', 0) - aug_pnl:>9,.0f})"
          f"  dd {s.get('max_dd', 0):>9,}  | by side {dict((k, [n, round(v)]) for k, (n, v) in side.items())}"
          + (f" | index legs {dict((k, [n, round(v)]) for k, (n, v) in sym.items() if k in INDEX)}" if tr else ""), flush=True)


def run() -> None:
    u.ROOT = LONG
    u.WINDOW_FROM, u.WINDOW_TO = WINDOW_FROM, WINDOW_TO
    r.FIVE = LONG / "underlying"
    u.wf.authenticate = lambda: None
    pricer = IndexPricer()
    idx = list(INDEX)
    print("\n=== A. indices alone (3 Aug - 29 Sep) ===", flush=True)
    for sides in ("long", "both"):
        u._cand_cache.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            tr, _ = u.simulate(idx, 5, pricer, f"INDICES_{sides}", sides)
        report(f"NIFTY+BANKNIFTY, {'CE only' if sides == 'long' else 'CE + PE'}", tr)
        if sides == "long" and tr:
            hp = hedge_pnl(tr, pricer)
            print(f"   + live hedge rule on these CE trades: {len(hp)} hedges, hedge PnL {sum(hp.values()):+,.0f}", flush=True)

    rep = json.loads((LONG / "selection_walkforward.json").read_text())
    days = r.trading_days()
    as_of = r.weekly_as_of(days)
    print("\n=== B. HYBRID stocks + indices, shared 5 slots (CE only) ===", flush=True)
    for rule in [k for k in ("HYBRID", "HYBRID_TOP10", "HYBRID_TOP20") if k in rep]:
        picks = {date.fromisoformat(k): set(v) for k, v in rep[rule]["picks"].items()}
        for with_idx in (False, True):
            allowed = {d: picks[a] | (set(idx) if with_idx else set()) for d, a in as_of.items()}
            syms = sorted(set().union(*allowed.values()))
            u._cand_cache.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                tr, _ = u.simulate(syms, 5, pricer, rule, "long", allowed)
            report(f"{rule} {'+ NIFTY/BANKNIFTY' if with_idx else '(stocks only)'}", tr)
    print(f"\noption calls made: {pricer.calls}", flush=True)


if __name__ == "__main__":
    wf.authenticate()
    dhan_wrapper.client.Dhan.dhan_http.timeout = 90
    fetch() if (sys.argv[1:] or ["run"])[0] == "fetch" else run()
