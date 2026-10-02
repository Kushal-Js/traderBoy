"""
PIN+TOTP failure cap (2 Oct 2026, user request: "build and deploy the 4-try cap").

Dhan support on MadeForTrade: TOTP login errors appear "usually when 5 consecutive failed attempts happen at the
server"; the lock's duration is not published. On 2 Oct 00:00 IST the bot got 4 "Invalid TOTP" in a row - one short.
Dhan counts per ACCOUNT, so every process on this machine (bot, weekly job, one-off scripts) shares one count in
config.DHAN_TOTP_FAILURE_FILE (data/, gitignored), and a file lock lets only one of them send a PIN+TOTP request at
a time (the check, the request and the bookkeeping happen under the same lock).

- A failure = Dhan answered without a token ("Invalid TOTP", wrong PIN ...). Network errors and unreadable
  responses are not counted (Dhan may never have seen the attempt).
- After config.DHAN_TOTP_MAX_CONSECUTIVE_FAILURES (4) in a row, no PIN+TOTP request is sent for
  config.DHAN_TOTP_PAUSE_SECONDS (6 h): slot() raises LoginPaused. After the pause ONE try is allowed; a failure
  pauses again. A success - or `python3 dhan_fallback_token.py login-reset` - clears the count.
- Meanwhile the bot runs on the fallback access token if one is stored (DhanWrapper).
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import config


class LoginPaused(RuntimeError):
    """No PIN+TOTP request was sent: the consecutive-failure cap is reached and its pause has not ended."""


def _path() -> Path:
    return Path(config.DHAN_TOTP_FAILURE_FILE)


def _read() -> dict:
    try:
        s = json.loads(_path().read_text())
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(s: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(s))
    os.replace(tmp, path)


def state() -> dict:
    """{"consecutive", "paused_until" (epoch or None), "last_error", "last_failure_at"} - no lock needed to read."""
    s = _read()
    return {"consecutive": int(s.get("consecutive") or 0), "paused_until": s.get("paused_until"),
            "last_error": s.get("last_error"), "last_failure_at": s.get("last_failure_at")}


def paused(now: Optional[float] = None) -> Optional[float]:
    """The epoch the pause ends at while PIN+TOTP is paused, else None."""
    s = state()
    until = s["paused_until"]
    now = time.time() if now is None else now
    if s["consecutive"] >= config.DHAN_TOTP_MAX_CONSECUTIVE_FAILURES and until and float(until) > now:
        return float(until)
    return None


def reset() -> bool:
    """Clears the count and any pause. True if there was something to clear."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        had = bool(_read().get("consecutive"))
        _write({"consecutive": 0})
    return had


class slot:
    """`with login_budget.slot() as s:` around ONE PIN+TOTP request. Holds the machine-wide lock for the block,
    raises LoginPaused on entry (no request) while paused; the caller reports s.success() or s.failure(error)."""

    def __enter__(self) -> "slot":
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = open(path.with_name(path.name + ".lock"), "w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        until = paused()
        if until is not None:
            self._release()
            s = state()
            raise LoginPaused(
                f"PIN+TOTP login paused until {datetime.fromtimestamp(until, ZoneInfo(config.MARKET_TZ)):%d %b %H:%M} IST "
                f"after {s['consecutive']} consecutive failed attempts - Dhan locks the account at 5; last error: "
                f"{s['last_error']}")
        return self

    def success(self) -> None:
        if _read().get("consecutive"):
            _write({"consecutive": 0})

    def failure(self, error: str) -> dict:
        s = _read()
        n = int(s.get("consecutive") or 0) + 1
        now = time.time()
        s = {"consecutive": n, "last_error": str(error)[:200], "last_failure_at": now,
             "paused_until": now + config.DHAN_TOTP_PAUSE_SECONDS if n >= config.DHAN_TOTP_MAX_CONSECUTIVE_FAILURES
             else None}
        _write(s)
        return s

    def _release(self) -> None:
        try:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
        finally:
            self._lock.close()

    def __exit__(self, *exc) -> bool:
        self._release()
        return False
