"""
Automated WS-candle-reconstruction live parity check (added 22 Sep 2026,
user request - see trading-skills' designs/ws-candle-reconstruction-parity-
results.md for the full investigation this completes). Settles the one
question the REST-replay parity backtest structurally cannot: does
underlying_candle_feed.py's real WS-tick-derived OPEN price match a real
REST candle's own open, not just close/volume (already proven via 8
symbols/568 bars at 100% match in the REST-replay backtest).

MAKES NO DHAN CALLS ITSELF AND NEEDS NO CREDENTIALS - this only calls the
LIVE bot's own already-authenticated HTTP API on localhost, via the
/debug/underlying-feed/* endpoints (main.py). Zero session-collision risk
(see incidents/2026-09-21-local-backtest-dhan-session-collision.md) -
there is nothing here for that incident's own root cause to touch.

Run every 5 min via a systemd timer (droplet-side, not tracked in git,
same pattern as shadow-evaluator.timer) - self-contained via a per-date
JSON state file, so each invocation is idempotent and safe to run
repeatedly:
  1. At/after SUBSCRIBE_TIME_IST (11:00 - "less trade frequency", user's
     own framing), if not already done today: POST /debug/underlying-
     feed/subscribe with TEST_SYMBOLS, so real WS ticks start
     accumulating for them.
  2. At/after REPORT_TIME_IST (15:40 - 10 min past the 15:30 close, well
     past NSE's last trade), if not already done today: GET
     /debug/underlying-feed/parity/{symbol} for each test symbol
     (computed server-side, comparing the day's real WS-reconstructed
     bars against a real REST fetch for the same day - the live-tick
     comparison this whole investigation needed), write the combined
     report to history/<date>_ws_candle_parity_report.json, and print a
     summary (both to stdout/journal and the report file).

State file: history/<date>_ws_candle_parity_state.json (which of the
two steps have run today - prevents re-subscribing or re-reporting on
every 5-min tick).

Run: uv run python3 ws_candle_parity_check.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
HISTORY = REPO_ROOT / "history"
IST = ZoneInfo("Asia/Kolkata")

BASE_URL = "http://localhost:8000"
TEST_SYMBOLS = ["RELIANCE", "TCS", "MAHABANK", "IDEA", "HDFCBANK", "ICICIBANK", "SBIN", "ITC"]
SUBSCRIBE_TIME_IST = dtime(11, 0)
REPORT_TIME_IST = dtime(15, 40)


def _http_json(method: str, path: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _state_path(today: str) -> Path:
    return HISTORY / f"{today}_ws_candle_parity_state.json"


def _load_state(today: str) -> dict:
    p = _state_path(today)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {"subscribed": False, "reported": False}


def _save_state(today: str, state: dict) -> None:
    _state_path(today).write_text(json.dumps(state))


def maybe_subscribe(now_ist: datetime, today: str, state: dict) -> bool:
    if state["subscribed"] or now_ist.time() < SUBSCRIBE_TIME_IST:
        return False
    try:
        result = _http_json("POST", "/debug/underlying-feed/subscribe", {"symbols": TEST_SYMBOLS})
        print(f"[ws-parity] subscribed: {result}")
        state["subscribed"] = True
        _save_state(today, state)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[ws-parity] subscribe FAILED (will retry next tick): {exc}")
        return False


def maybe_report(now_ist: datetime, today: str, state: dict) -> bool:
    if state["reported"] or now_ist.time() < REPORT_TIME_IST or not state["subscribed"]:
        return False
    results = {}
    all_ok = True
    for i, sym in enumerate(TEST_SYMBOLS):
        if i > 0:
            # Same rate-limit discipline as backtest_ws_candle_reconstruction_
            # parity.py's own CALL_PACE_SECONDS - the first real run of this
            # script hit a transient 500 on 3/8 symbols with zero pacing
            # between them (the endpoint itself now retries too, but pacing
            # here means most requests never need to).
            time.sleep(1.5)
        try:
            # 60s timeout, not the default 30s - the endpoint itself now
            # retries up to 4x with backoff (3s/6s/12s/15s) on a transient
            # Dhan rate-limit blip, which can push a single call past 30s.
            results[sym] = _http_json("GET", f"/debug/underlying-feed/parity/{sym}?day={today}", timeout=60.0)
        except urllib.error.HTTPError as exc:
            results[sym] = {"error": f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}"}
            all_ok = False
        except Exception as exc:  # noqa: BLE001
            results[sym] = {"error": str(exc)}
            all_ok = False

    report_path = HISTORY / f"{today}_ws_candle_parity_report.json"
    report_path.write_text(json.dumps(results, indent=2, default=str))

    print(f"\n{'='*90}\nWS-candle LIVE-TICK parity report - {today}\n{'='*90}")
    for sym, r in results.items():
        if "error" in r:
            print(f"{sym}: ERROR - {r['error']}")
            continue
        print(f"{sym}: real={r['real_bar_count']} recon={r['recon_bar_count']} matched={r['matched_bars']} "
              f"close_match(<0.05%)={r['close_matches_within_0.05pct']} "
              f"open+close_exact={r['open_and_close_exact_matches']} "
              f"vol_match(<1%)={r['volume_matches_within_1pct']}")
        if r["matched_bars"] == 0:
            print(f"  ⚠ {sym}: zero matched bars - check subscription actually caught real ticks (see snapshot endpoint)")
    print(f"\nFull report written to {report_path}")
    print("STATUS: " + ("DATA COLLECTED" if all_ok else "SOME SYMBOLS FAILED - inspect above"))

    state["reported"] = True
    _save_state(today, state)
    return True


def main() -> None:
    now_ist = datetime.now(IST)
    today = now_ist.date().isoformat()
    state = _load_state(today)

    if now_ist.weekday() >= 5:
        print(f"[ws-parity] {today} is a weekend - nothing to do.")
        return

    subscribed_now = maybe_subscribe(now_ist, today, state)
    reported_now = maybe_report(now_ist, today, state)
    if not subscribed_now and not reported_now:
        print(f"[ws-parity] {now_ist.strftime('%H:%M IST')} - nothing due yet "
              f"(subscribed={state['subscribed']}, reported={state['reported']})")


if __name__ == "__main__":
    main()
