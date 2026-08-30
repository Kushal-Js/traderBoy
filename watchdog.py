"""
Standalone health-check watchdog for dhanboy.service - runs as its own
systemd unit (dhanboy-watchdog.service, set up directly on the droplet,
not tracked in git - same as dhanboy.service itself), independent of the
main app process so it can detect the app being down, including the kind
of incident that triggered this: a deliberate restart landing in a
transient Dhan auth-rejection window, self-healed by Restart=always after
~3.5 minutes with nothing recorded anywhere once journald's retention
window passed.

Polls GET /health every POLL_INTERVAL_SECONDS. When a health check starts
failing, tracks how long the outage lasts. If it exceeds
INCIDENT_THRESHOLD_SECONDS (whether it later recovers on its own or not),
appends a self-contained incident record to INCIDENT_LOG_PATH: start/end
time (IST, to avoid the UTC/IST mix-up that caused the original
investigation to misjudge when the incident actually happened), duration,
resolved/ongoing status, and the actual dhanboy.service journal output
for that window - so the record survives past journald's own retention
and has enough detail to diagnose without re-SSHing into the box.

Deliberately dependency-free (stdlib only) - runs via the same `uv run`
venv as the main app for convenience, but must stay cheap to import/run
on a ~1GB droplet regardless of what's installed there.
"""
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

HEALTH_URL = "http://127.0.0.1:8000/health"
SERVICE_NAME = "dhanboy.service"
POLL_INTERVAL_SECONDS = 5
INCIDENT_THRESHOLD_SECONDS = 30
# While an outage is still ongoing past the threshold, re-log an updated
# record at this cadence so a long/permanent outage isn't silent forever.
ONGOING_UPDATE_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 3

# Dated, history/-folder convention (user request, 31 Aug 2026) - matches
# trade_history.py's own history/<date>_<name>.log scheme, but implemented
# directly here (not by importing trade_history) since this script is
# deliberately dependency-free/stdlib-only, and incidents are a multi-line
# text block per entry, not JSONL - trade_history.py's append_jsonl isn't
# the right shape for this format anyway. Dated by the incident's own
# START time, not "now" - so a rare incident spanning midnight still lands
# entirely in one file instead of being split mid-record.
HISTORY_DIR = "/root/apps/traderBoy/history"


def incident_log_path(down_since: datetime) -> str:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    return os.path.join(HISTORY_DIR, f"{down_since.date().isoformat()}_incidents.log")


def check_health() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - any failure (refused, timeout, 5xx) counts as down
        return False


def journal_excerpt(start: datetime, end: datetime) -> str:
    """Pulls the actual dhanboy.service journal for the incident window -
    the real diagnostic content (tracebacks, Dhan error codes, etc.), not
    just the fact that it was down. Best-effort: if journalctl itself
    fails for some reason, the incident record still gets written with a
    note instead of silently losing the whole entry."""
    try:
        result = subprocess.run(
            [
                "journalctl", "-u", SERVICE_NAME,
                "--since", start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S"),
                "--until", (end + timedelta(seconds=2)).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S"),
                "--no-pager",
            ],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip() or "(no journal output captured for this window)"
    except Exception as exc:  # noqa: BLE001
        return f"(failed to capture journal excerpt: {exc})"


def write_incident(down_since: datetime, now: datetime, resolved: bool) -> None:
    duration = (now - down_since).total_seconds()
    status = "RESOLVED" if resolved else "ONGOING"
    header = (
        f"=== INCIDENT {down_since.isoformat()} -> {now.isoformat()} IST "
        f"(duration so far: {duration:.0f}s) [{status}] ==="
    )
    body = journal_excerpt(down_since, now)
    with open(incident_log_path(down_since), "a") as f:
        f.write(header + "\n" + body + "\n\n")


def main() -> None:
    down_since = None
    last_ongoing_log = None

    while True:
        now = datetime.now(IST)
        healthy = check_health()

        if healthy:
            if down_since is not None:
                duration = (now - down_since).total_seconds()
                if duration >= INCIDENT_THRESHOLD_SECONDS:
                    write_incident(down_since, now, resolved=True)
                down_since = None
                last_ongoing_log = None
        else:
            if down_since is None:
                down_since = now
            else:
                duration = (now - down_since).total_seconds()
                if duration >= INCIDENT_THRESHOLD_SECONDS and (
                    last_ongoing_log is None
                    or (now - last_ongoing_log).total_seconds() >= ONGOING_UPDATE_SECONDS
                ):
                    write_incident(down_since, now, resolved=False)
                    last_ongoing_log = now

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
