#!/usr/bin/env python3
"""
The 08:00 IST morning restart, through safe_restart.py (1 Oct 2026, user
request). Run by the droplet's dhanboy-morning-refresh.service (systemd timer
at 02:30 UTC); until 1 Oct that unit ran a plain `systemctl restart`.

Why the morning restart exists: the bot authenticates to Dhan once at start
and the token has a ~24h cap, so it must be restarted every day before the
09:15 open. That makes this restart MANDATORY, unlike a deploy restart:
  1. safe_restart.py (snapshot, checks, restart, /health, restart reports).
     Exit 0 or 1 (restarted; 1 = a report needs review) -> done.
  2. Exit 2 = a check refused (e.g. an order still in flight) -> wait
     RETRY_SECONDS and try again, up to MAX_ATTEMPTS times (~10 minutes).
  3. Still refused -> safe_restart.py --force: the fresh token matters more
     than the check at 08:10 with the market still closed; the restart
     memory / live-state files carry the positions' state across it. Logged
     loudly.
  4. Exit 3 (/health did not come back) -> one more forced attempt, then give
     up and leave it to the watchdog / a human (logged).
  If the bot does not answer /health at all, there is nothing to protect:
  straight to the forced restart.
Everything is appended to history/<date>_morning_refresh.log and printed to
the systemd journal. Standard library only.

    cd ~/apps/traderBoy && python3 morning_refresh.py
"""
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
MAX_ATTEMPTS = 10
RETRY_SECONDS = 60
SAFE_RESTART_TIMEOUT = 300


def log(msg: str) -> None:
    line = f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST | {msg}"
    print(line, flush=True)
    try:
        path = REPO / "history" / f"{datetime.now(IST):%Y-%m-%d}_morning_refresh.log"
        path.parent.mkdir(exist_ok=True)
        with path.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def safe_restart(*extra: str) -> int:
    try:
        proc = subprocess.run([sys.executable, str(REPO / "safe_restart.py"), *extra], cwd=str(REPO),
                              capture_output=True, text=True, timeout=SAFE_RESTART_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        log(f"safe_restart.py {' '.join(extra)} could not run: {exc!r}")
        return 99
    for line in (proc.stdout + proc.stderr).splitlines():
        log(f"  safe_restart: {line}")
    return proc.returncode


def bot_up() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:8000/health", timeout=5) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    log("morning refresh: restarting the bot through safe_restart.py (fresh Dhan session before the open)")
    attempts = MAX_ATTEMPTS
    if not bot_up():
        log("the bot does not answer /health - nothing to protect, going straight to a forced restart")
        attempts = 0
    for attempt in range(1, attempts + 1):
        code = safe_restart()
        if code in (0, 1):
            log("restarted" + (" - a restart report NEEDS REVIEW (see above)" if code == 1 else " - reports clean"))
            return code
        if code != 2:
            break
        log(f"a safe-restart check refused (attempt {attempt}/{MAX_ATTEMPTS}) - retrying in {RETRY_SECONDS}s")
        time.sleep(RETRY_SECONDS)
    log("FORCING the restart (safe_restart.py --force): the daily token refresh cannot be skipped")
    code = safe_restart("--force")
    if code == 3:
        log("/health did not come back - one more forced attempt")
        code = safe_restart("--force")
    if code in (0, 1):
        log("restarted (forced)" + (" - a restart report NEEDS REVIEW" if code == 1 else ""))
        return code
    if code == 99:
        log("safe_restart.py cannot run at all - falling back to a plain systemctl restart")
        subprocess.run(["systemctl", "restart", "dhanboy.service"], check=False)
        return 4
    log(f"MORNING RESTART FAILED (safe_restart exit {code}) - check journalctl -u dhanboy.service before 09:15")
    return 5


if __name__ == "__main__":
    sys.exit(main())
