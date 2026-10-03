#!/usr/bin/env python3
"""
Server backstop for the 09:05 IST pre-open warm-up (3 Oct 2026, user: "a scheduler should be placed at server for
warm ups at 09:05 AM IST"). Run by the droplet's dhanboy-warmup-check.service (systemd timer, 09:06 IST Mon-Fri).

The warm-up itself runs INSIDE the bot - it fills the bot's own candle caches and WebSocket subscriptions, which no
other process can do. The bot starts it by the clock at 09:05 (official_candles thread). This job makes sure it
happened:
  1. POST /official-candles/prewarm - the bot answers "running" (the clock started it), "done", or "started" (the
     clock trigger never fired - it starts it now); "outside_window" on a weekend.
  2. Polls GET /official-candles/prewarm every POLL_SECONDS until today's result is in (at most WAIT_SECONDS).
  3. Logs the result. Exit 0 = done with no failed loads (or nothing to do), 1 = done with failed loads,
     2 = not done in time / bot not answering (the bot's own loops still download what they need at 09:15).
Everything is appended to history/<date>_warmup_check.log and printed to the systemd journal. Standard library only.

    cd ~/apps/traderBoy && python3 warmup_check.py
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
BASE_URL = "http://127.0.0.1:8000"
POLL_SECONDS = 5
WAIT_SECONDS = 480


def log(msg: str) -> None:
    line = f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST | {msg}"
    print(line, flush=True)
    try:
        path = REPO / "history" / f"{datetime.now(IST):%Y-%m-%d}_warmup_check.log"
        path.parent.mkdir(exist_ok=True)
        with path.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def call(method: str, path: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(BASE_URL + path, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def summary(last: dict) -> str:
    keys = ("mode", "source", "listed", "loaded", "failed", "skipped", "seconds", "added_um_stocks", "added_bases")
    return ", ".join(f"{k}={last.get(k)}" for k in keys)


def main() -> int:
    try:
        first = call("POST", "/official-candles/prewarm")
    except Exception as exc:  # noqa: BLE001
        log(f"bot not answering POST /official-candles/prewarm ({exc!r}) - warm-up not confirmed; the bot's loops "
            "download what they need at 09:15")
        return 2
    status = first.get("status")
    log(f"warm-up request -> {status} (running={first.get('running')}, done_today={first.get('done_today')})")
    if status == "outside_window":
        log("not a warm-up time (weekend or outside 09:05-15:15) - nothing to do")
        return 0
    deadline = time.monotonic() + WAIT_SECONDS
    state = first
    while not state.get("done_today") or state.get("running"):
        if time.monotonic() > deadline:
            log(f"warm-up not finished after {WAIT_SECONDS} s (running={state.get('running')}) - check "
                "GET /official-candles/prewarm and the journal")
            return 2
        time.sleep(POLL_SECONDS)
        try:
            state = call("GET", "/official-candles/prewarm")
        except Exception as exc:  # noqa: BLE001
            log(f"status poll failed ({exc!r}) - retrying")
    last = state.get("last") or {}
    who = "the bot's own 09:05 trigger" if last.get("source") == "clock" else "this check (the clock trigger had not)"
    log(f"warm-up done by {who}: {summary(last)}")
    if last.get("failed"):
        log(f"{last.get('failed')} base(s) failed to load - their loops download them at 09:15 as before")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
