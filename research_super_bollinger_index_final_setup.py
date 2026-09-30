"""
NIFTY and BANKNIFTY with the setup that is live since 30 Sep 2026 18:54 IST
(user: "backtest Nifty and Bank Nifty with this same strategy (Super
Bollinger + hedge + Scale In) and show me PnL over last 30 days").

Exactly research_super_bollinger_final_setup_trades.py, for the two indices:
  entry filter   the index's last closed 1-hour candle is green
  call           Super Bollinger CE, 1 lot (max loss 4,500, breakeven after +1,500, no entry from 14:00, 15:15)
  PUT side (S2)  hedge at CE loss 1,800 (+1 ATR), 2nd PUT lot on Supertrend-bearish, sell one lot at combined
                 +4,000, ride the other on the 30% trail with a purchase-price floor
  call side (S1) one more call when the index is back at its trigger (Supertrend bullish, before 14:00), own
                 4,500 max loss, exits with the original
Indices on their own (one position per index, so the 5 slots never bind).
Index option prices as in research_super_bollinger_indices.py (NIFTY
weeklies, BANKNIFTY monthlies, fixed entry strike, modeled slippage).

CACHE ONLY by default - no Dhan calls. A leg whose prices are not in the cache
is counted and listed instead of guessed:
  "no option data"  the call itself could not be priced -> trade left out
  "unpriced hedge"  the hedge fired but its PUT could not be priced -> PUT side counted as 0
  "partial"         the leg's strike was rebuilt from an incomplete set of cached files
`fetch` downloads exactly those missing legs first (read-only market data,
after 15:30 IST only, hand-off token - never pin_totp).

Window: 3 Aug - 30 Sep; "last 30 days" = 31 Aug - 30 Sep (22 sessions).

Run: uv run python research_super_bollinger_index_final_setup.py
     HANDOFF_DHAN_ACCESS_TOKEN=... uv run python research_super_bollinger_index_final_setup.py fetch
"""
from __future__ import annotations

import bisect
import contextlib
import csv
import io
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

FETCH = "fetch" in sys.argv[1:]
import backtest_super_trader_30day as _st0          # noqa: E402
import walkforward_selector_eval as _wf0            # noqa: E402
_real_save, _real_retry, _real_auth = _st0._save, _wf0._retry, _wf0.authenticate   # before the cache-only switches below

with contextlib.redirect_stdout(io.StringIO()):
    import research_super_bollinger_chop_filter_resim as rsim      # cache-only switches + long-history dirs
    import research_super_bollinger_indices as ix
rs, m, u, r = rsim.rs, rsim.m, rsim.u, rsim.r
st = ix.st
LAST_30_FROM = "2026-08-31"
u.WINDOW_TO = date(2026, 9, 30)
EMPTY = {"timestamps": [], "opens": [], "highs": [], "lows": [], "closes": [], "strikes": [], "spots": []}


def _no_network(*_a, **_k):
    raise RuntimeError("cache-only run - no Dhan calls")


if FETCH:
    _real_auth()
    ix.dhan_wrapper.client.Dhan.dhan_http.timeout = 90
    ix._retry, st._save = _real_retry, _real_save     # index option data only; the stock paths stay cache-only
else:
    ix._retry = _no_network


class CachedIndexPricer(ix.IndexPricer):
    """IndexPricer that stays in the cache (or, with `fetch`, downloads only
    what is missing) and remembers every leg it could not price."""

    def __init__(self):
        super().__init__()
        self.misses: list[tuple] = []
        self.fetched = 0

    def _rolling(self, sym, day, code, ot, offset):
        if sym not in ix.INDEX:
            return super()._rolling(sym, day, code, ot, offset)
        tag = "ATM" if offset == 0 else f"ATM{offset:+d}"
        cache = st.OUT / "options" / sym / f"{day}_c{code}_{ot}_{tag}.json"
        if cache.exists():
            return super()._rolling(sym, day, code, ot, offset)
        if FETCH:
            out = super()._rolling(sym, day, code, ot, offset)
            if out["timestamps"]:
                self.fetched += 1
                return out
            cache.unlink(missing_ok=True)      # never leave an empty file behind: it would look like "cached"
        self.misses.append((sym, day, ot, tag))
        return dict(EMPTY)

    def _listed_minutes(self, sym, sid, day):
        """1-min bars of a listed contract for ONE day (the parent's cache is per contract, not per day)."""
        lo = int(datetime.combine(day, datetime.min.time(), ix.IST).timestamp())
        for cache in (st.OUT / "options" / sym / f"listed_{sid}_{day}.json", st.OUT / "options" / sym / f"listed_{sid}.json"):
            data = st._load(cache)
            if data and any(lo <= t < lo + 86400 for t in data["timestamps"]):
                return data
        if not FETCH:
            return None
        resp = ix._retry(ix.dhan_wrapper.client.Dhan.intraday_minute_data, security_id=sid, exchange_segment="NSE_FNO",
                         instrument_type="OPTIDX", from_date=day.isoformat(), to_date=(day + timedelta(days=1)).isoformat(),
                         interval=1)
        d = (resp or {}).get("data") or {}
        data = {"timestamps": [int(t) for t in d.get("timestamp") or []], "opens": d.get("open") or [],
                "highs": d.get("high") or [], "lows": d.get("low") or [], "closes": d.get("close") or []}
        time.sleep(st.PACE)
        if not data["timestamps"]:
            return None
        self.fetched += 1
        st._save(st.OUT / "options" / sym / f"listed_{sid}_{day}.json", data)
        return data

    def open_leg(self, sym, day, side, entry_t, until_t, spot):
        ot = "CE" if side == "LONG" else "PE"
        if sym not in ix.INDEX or day <= st.ROLLING_LAST_DAY:
            return super().open_leg(sym, day, side, entry_t, until_t, spot)
        expiry, _code = ix.index_contract(sym, day)
        rows = self._index_rows(sym)
        rows = rows[(rows["SEM_EXPIRY_DATE"].astype(str).str.startswith(expiry.isoformat())) & (rows["SEM_OPTION_TYPE"] == ot)]
        if rows.empty:
            self.misses.append((sym, day, ot, "listed: contract not in the instrument file"))
            return None
        row = rows.assign(dist=(rows["SEM_STRIKE_PRICE"] - spot).abs()).sort_values("dist").iloc[0]
        try:
            data = self._listed_minutes(sym, str(int(row["SEM_SMST_SECURITY_ID"])), day)
        except Exception as exc:  # noqa: BLE001
            print(f"    listed fetch failed {sym} {day} {ot}: {exc!r}", flush=True)
            data = None
        if not data:
            self.misses.append((sym, day, ot, "listed"))
            return None
        return {"ot": ot, "expiry": expiry, "strike": float(row["SEM_STRIKE_PRICE"]),
                "mins": st.Minutes(data["timestamps"], data["opens"], data["highs"], data["lows"], data["closes"])}


pricer = CachedIndexPricer()
m.pr = pricer
idx = list(ix.INDEX)


def run(gate):
    """-> rows (one per call trade), stats, legs with missing cache files."""
    u._cand_cache.clear()
    pricer.misses.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        trades, stats = u.simulate(idx, r.CAP, pricer, "INDEX_FINAL", "long", None, symbol_gate=gate)
    sim_misses = list(pricer.misses)
    rows = []
    for t in trades:
        pricer.misses.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            ev = rs.prep(t)
            p2, i2 = rs.put_side(ev, True, 4000)
            p1, i1 = rs.call_side(ev, confirm=True, lot2_exit="with", arm=0, pair_cap=False, reentry=False)
        d = date.fromisoformat(t["day"])
        ce_partial = any(x[0] == t["symbol"] and x[1] == d and x[2] == "CE" for x in sim_misses + pricer.misses)
        pe_missing = [x for x in pricer.misses if x[2] == "PE"]
        note = ""
        if ev["th"] is not None:
            if ev["hm"] is None:
                note = "hedge fired, PUT not in cache (counted 0)"
            else:
                note = "hedge" + (" + 2nd lot" if i2.get("added") else "") + (", one sold at target" if i2.get("booked") else "")
                if pe_missing:
                    note += " [partial PUT prices]"
        call = float(t["pnl_modeled"])
        rows.append({"day": t["day"], "symbol": t["symbol"], "contract": t["contract"], "entry_time": t["entry_time"],
                     "exit_time": t["exit_time"], "qty": t["qty"], "entry": t["entry"], "exit": t["exit"],
                     "exit_reason": t["reason"], "call_pnl": round(call), "put_side_pnl": round(p2), "put_side": note,
                     "extra_call_pnl": round(p1), "extra_call": "re-added" if i1.get("readd") else "",
                     "trade_total": round(call + p2 + p1), "call_prices": "partial" if ce_partial else "full",
                     "hedge_fired": ev["th"] is not None, "hedge_priced": ev["hm"] is not None})
    return rows, stats, sim_misses


def summary(rows, label):
    if not rows:
        print(f"{label}: no trades")
        return
    daily = defaultdict(float)
    for x in rows:
        daily[x["day"]] += x["trade_total"]
    cum = peak = dd = 0.0
    for d in sorted(daily):
        cum += daily[d]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    tot = lambda k: sum(x[k] for x in rows)  # noqa: E731
    print(f"{label}: trades {len(rows)}, winning {sum(x['trade_total'] > 0 for x in rows)} | calls {tot('call_pnl'):+,} | "
          f"PUT side {tot('put_side_pnl'):+,} | extra call {tot('extra_call_pnl'):+,} | TOTAL {tot('trade_total'):+,} | "
          f"max drawdown {dd:,.0f} | worst day {min(daily.values()):+,.0f} | best day {max(daily.values()):+,.0f} | "
          f"green days {sum(v > 0 for v in daily.values())}/{len(daily)}")
    for s in idx:
        sub = [x for x in rows if x["symbol"] == s]
        print(f"    {s:10s} trades {len(sub):2d}, winning {sum(x['trade_total'] > 0 for x in sub):2d} | calls "
              f"{sum(x['call_pnl'] for x in sub):+,} | PUT side {sum(x['put_side_pnl'] for x in sub):+,} | extra call "
              f"{sum(x['extra_call_pnl'] for x in sub):+,} | total {sum(x['trade_total'] for x in sub):+,}")


print(f"lot sizes: {', '.join(f'{s} {pricer.lot(s)}' for s in idx)}")
for name, gate in (("WITH the 1-hour-green entry filter (the live setup)", rsim.candle_gate(60)), ("WITHOUT the entry filter (reference)", None)):
    rows, stats, sim_misses = run(gate)
    last30 = [x for x in rows if x["day"] >= LAST_30_FROM]
    print(f"\n================ {name} ================")
    print(f"signals refused by the 1-hour filter {stats.get('skipped_symbol_gate', 0)} | calls left out, no option data "
          f"{stats.get('skipped_no_option_data', 0) + stats.get('skipped_no_option_print', 0)} | hedges fired "
          f"{sum(x['hedge_fired'] for x in rows)} (priced {sum(x['hedge_priced'] for x in rows)}) | 2nd PUT lots "
          f"{sum('2nd lot' in x['put_side'] for x in rows)} | call re-adds {sum(bool(x['extra_call']) for x in rows)} | "
          f"calls priced from partial cache {sum(x['call_prices'] == 'partial' for x in rows)}")
    summary(last30, "LAST 30 DAYS (31 Aug - 30 Sep)")
    summary([x for x in rows if x["day"] < LAST_30_FROM], "earlier (3 - 28 Aug)        ")
    summary(rows, "whole window (3 Aug - 30 Sep)")
    if gate is not None:
        with open("research_results/2026-09-30_index_final_setup_trades.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(f"\n{'day':10s} {'index':10s} {'contract':28s} {'in':>5s} {'out':>5s} {'qty':>4s} {'entry':>8s} {'exit':>8s} {'call exit':20s} "
          f"{'call':>8s} {'PUT side':>9s} {'extra call':>10s} {'TOTAL':>8s}  notes")
    cum = 0
    for x in rows:
        cum += x["trade_total"]
        notes = "; ".join(n for n in (x["put_side"], x["extra_call"] and "call re-added", x["call_prices"] == "partial" and "partial call prices") if n)
        print(f"{x['day']:10s} {x['symbol']:10s} {x['contract']:28s} {x['entry_time']:>5s} {x['exit_time']:>5s} {x['qty']:>4d} "
              f"{x['entry']:>8.2f} {x['exit']:>8.2f} {x['exit_reason']:20s} {x['call_pnl']:>+8,} {x['put_side_pnl']:>+9,} "
              f"{x['extra_call_pnl']:>+10,} {x['trade_total']:>+8,}  {notes}   [running {cum:+,}]")
if FETCH:
    print(f"\nindex option files downloaded: {pricer.fetched}")
