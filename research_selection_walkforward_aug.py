"""
August extension of the stock-selection walk-forward (30 Sep 2026, user
request: "run the August walk-forward extension after market close").

HYBRID was designed after seeing the 31 Aug - 29 Sep results, so that month
can't test it fairly. This re-runs every selection rule over 3 Aug - 29 Sep
(about 9 weekly decisions) with the same code
(research_stock_selection_walkforward.py + backtest_super_bollinger_
universe_30day.simulate), reporting AUGUST (3 - 28 Aug, never used to design
HYBRID) separately from SEPTEMBER.

Needs longer history than the 30-day cache, written to history/
bt_walkforward_long/: 5-min bars from late May (fit lookback + indicator
warm-up for the early-August decisions, two requests per stock) and 1-min
bars from 31 Jul. Option prices reuse the existing expired-options cache.

Run after 15:30 IST (shares the live bot's Dhan data budget):
    HANDOFF_DHAN_ACCESS_TOKEN=... uv run python research_selection_walkforward_aug.py fetch
    HANDOFF_DHAN_ACCESS_TOKEN=... uv run python research_selection_walkforward_aug.py run
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

import walkforward_selector_eval as wf
from walkforward_selector_eval import dhan_wrapper, _retry
from fno_ath_screener import fetch_fno_universe
import backtest_super_bollinger_universe_30day as u
import research_stock_selection_walkforward as r

LONG = Path("history/bt_walkforward_long")
FIVE_CHUNKS = [("2026-05-25", "2026-08-09"), ("2026-08-10", "2026-09-30")]
ONE_MIN_FROM, ONE_MIN_TO = "2026-07-31", "2026-09-30"
AUG_END = date(2026, 8, 31)
RULES = ["HYBRID", "STRATEGY_FIT", "TREND_ATH", "TREND_ATH_GATE", "MOMENTUM"] + [f"RANDOM_{k}" for k in range(1, 11)]


def _get(sid: str, seg: str, inst: str, frm: str, to: str, interval: int) -> dict:
    for attempt in range(4):
        try:
            resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data, security_id=sid, exchange_segment=seg,
                          instrument_type=inst, from_date=frm, to_date=to, interval=interval)
            d = (resp or {}).get("data") or {}
            if d.get("close"):
                return d
        except Exception as exc:  # noqa: BLE001
            print(f"    {sid} {frm}..{to} {interval}m failed: {exc!r}", flush=True)
        time.sleep(3 * (attempt + 1))
    return {}


def _bars(parts: list[dict]) -> dict:
    rows = {}
    for d in parts:
        for i, t in enumerate(d.get("timestamp") or []):
            rows[int(t)] = (d["open"][i], d["high"][i], d["low"][i], d["close"][i], (d.get("volume") or [0] * len(d["close"]))[i])
    ts = sorted(rows)
    return {"timestamps": ts, "opens": [rows[t][0] for t in ts], "highs": [rows[t][1] for t in ts],
            "lows": [rows[t][2] for t in ts], "closes": [rows[t][3] for t in ts], "volumes": [rows[t][4] for t in ts]}


def fetch() -> None:
    universe = fetch_fno_universe()
    (LONG / "underlying").mkdir(parents=True, exist_ok=True)
    (LONG / "underlying_1m").mkdir(parents=True, exist_ok=True)
    nf = LONG / "underlying" / "_NIFTY_5min.json"
    if not nf.exists():
        nf.write_text(json.dumps(_bars([_get("13", "IDX_I", "INDEX", a, b, 5) for a, b in FIVE_CHUNKS])))
    for k, sym in enumerate(universe):
        try:
            sid = dhan_wrapper._equity_security_id(sym)
        except Exception:  # noqa: BLE001
            continue
        f5 = LONG / "underlying" / f"{sym}_5min.json"
        if not f5.exists():
            b = _bars([_get(sid, "NSE_EQ", "EQUITY", a, bb, 5) for a, bb in FIVE_CHUNKS])
            if b["closes"]:
                f5.write_text(json.dumps(b))
            time.sleep(0.4)
        f1 = LONG / "underlying_1m" / f"{sym}_1min.json"
        if not f1.exists():
            b = _bars([_get(sid, "NSE_EQ", "EQUITY", ONE_MIN_FROM, ONE_MIN_TO, 1)])
            if b["closes"]:
                f1.write_text(json.dumps(b))
            time.sleep(0.4)
        if (k + 1) % 25 == 0:
            print(f"  fetched {k + 1}/{len(universe)}", flush=True)
    print("fetch done", flush=True)


def run() -> None:
    r.FIVE = LONG / "underlying"
    r.OUT = LONG
    u.ROOT = LONG
    u.WINDOW_FROM, u.WINDOW_TO = date(2026, 8, 3), date(2026, 9, 29)
    sys.argv = [sys.argv[0]] + RULES
    r.main()
    report = json.loads((LONG / "selection_walkforward.json").read_text())
    print("\n=== August (3-28 Aug, never used to design HYBRID) vs September ===", flush=True)
    for rule, v in report.items():
        wk = v["weekly_by_decision_date"]
        aug = sum(x for k, x in wk.items() if date.fromisoformat(k) < date(2026, 8, 28))
        sep = sum(x for k, x in wk.items() if date.fromisoformat(k) >= date(2026, 8, 28))
        print(f"{rule:16s} total {v['summary'].get('net', 0):>9,}  Aug {aug:>9,}  Sep {sep:>9,}  "
              f"weeks+ {sum(1 for x in wk.values() if x > 0)}/{len(wk)}", flush=True)


if __name__ == "__main__":
    wf.authenticate()
    dhan_wrapper.client.Dhan.dhan_http.timeout = 90
    phase = sys.argv[1] if len(sys.argv) > 1 else "run"
    fetch() if phase == "fetch" else run()
