"""
Weekly F&O composite watchlist refresh - scheduled job (droplet-side
systemd timer, `dhanboy-weekly-watchlist-refresh.timer`, NOT tracked in
git - same convention as `dhanboy-morning-refresh.timer`/`shadow-
evaluator.timer` themselves). Runs Friday 00:00 IST (Thursday 18:30 UTC -
the droplet's own clock is UTC, see trading-skills' feedback-droplet-utc-
timestamps.md).

User request (27 Sep 2026): "Create a scheduler which runs on every week
on Friday night 12 [midnight IST] and call this ATH method and based on
it's result, update SWING and BOLLINGER watchlist (match both watchlists
15 stocks with ATH method stocks list result and replace any stock which
is not matched in them across to keep them consistent on weekly basis,
MCX and Index entries are not to be replaced)."

EXPLICIT USER DECISION ON AUTONOMY (27 Sep 2026, asked directly given the
real-money stakes of an unattended recurring watchlist change): fully
autonomous, no holdback for open positions - "since we already have...
Friday square-off... there won't be any open position... the bot would
take care of any open position and would close them based on already
defined logic... there shouldn't be any holdback." Friday midnight was
specifically chosen BECAUSE every position (NSE equity 15:25 IST, MCX
23:25 IST, NIFTY/BANKNIFTY daily 15:25 IST) is already flat by then. This
script still CHECKS AND LOGS live positions before acting, purely for the
audit trail - it is never a gate, per that explicit instruction.

What it does, in order:
  1. Calls fno_ath_screener.get_top_n_candidates(15) - THE global common
     function (see that module's own docstring for the full composite
     formula). Reuses the bot's own cached Dhan session by running
     directly on the droplet as a systemd oneshot (same pattern as
     shadow_evaluator.py) - no pin_totp collision risk, since that risk
     is specific to a genuinely external/local machine with no access to
     the droplet's cached token file (see incidents/2026-09-21-local-
     backtest-dhan-session-collision.md).
  2. Reads the CURRENT data/watchlist and data/bollinger_watchlist.
  3. Splits each into PROTECTED (MCX commodities, live-checked via
     dhan_wrapper.is_mcx_commodity - the same data-driven check the rest
     of the codebase already uses, not a hardcoded symbol list - and
     index symbols, via Swing.config.INDEX_SYMBOLS) vs EQUITY. Protected
     entries are carried forward UNCHANGED every run, never touched.
  4. New equity portion = exactly the top-N symbols from step 1, for
     BOTH watchlists (identical - the composite score doesn't distinguish
     between the two strategies).
  5. Writes a full diff report (added/removed per watchlist, each new
     stock's composite score/explanation, and the live-position snapshot)
     to history/<date>_weekly_watchlist_refresh.log.
  6. Backs up both watchlist files (.bak.<timestamp>, same convention
     used for every manual edit this session) before overwriting.
  7. Writes the new watchlist files.
  7b. (added 30 Sep 2026, user request) Super Bollinger's OWN list,
     data/super_bollinger_watchlist, from the HYBRID selection
     (stock_selection.run_live - ATH top 40 among liquid stocks, ranked by
     how the Super Bollinger rules have done on each over the last 20
     sessions). Swing/Bollinger above keep plain ATH. Best-effort: a
     failure or fewer than 10 picks leaves last week's Super Bollinger list
     in place and never blocks the ATH update or the restart.
     (1 Oct 2026, user request) the same picks are also written to Unified
     Momentum's own list, data/unified_momentum_watchlist (the real-money
     strategy; Super Bollinger is paper from 1 Oct) - same rules: fewer
     than 10 picks or a failure keeps last week's list. The pipeline does
     not import Super Bollinger: removing that strategy later only means
     dropping its write (UnifiedMomentum keeps working unchanged).
  7c. (added 30 Sep 2026) the SHADOW list: a second Super Bollinger pick
     (40-session fit with the 1-hour filter, stock_selection.run_shadow)
     that is only recorded and, a week later, scored against the live list
     (Unified Momentum's since 1 Oct - identical picks)
     (data/super_bollinger_shadow_watchlist.json / _shadow_scores.jsonl).
     Never traded; best-effort, time-boxed, cannot change any watchlist.
  8. Restarts dhanboy.service THROUGH safe_restart.py (user request 30 Sep
     2026; until then a plain `systemctl restart`): snapshot of every
     strategy's positions, checks that no order/exit is in flight and that
     every real Super Bollinger position has its broker stop and is in the
     state file, restart, wait for /health, restart report in this job's
     log. If a check fails the restart is SKIPPED, not forced - the new
     watchlists are already live (every store re-reads its file on each
     monitor tick) and the 08:00 IST morning refresh restarts the bot
     anyway. If safe_restart.py itself cannot run, falls back to the plain
     restart.

ONE safety net this script DOES still apply - NOT a position holdback,
the user never asked for that to be skipped, just a basic defense against
acting on a broken scan: if the composite scan successfully scores fewer
than MIN_SUCCESSFUL_SCORES stocks (e.g. a Dhan-wide outage that day),
abort BEFORE touching any file, log why, and leave both watchlists
exactly as they were. An unrelated API failure should never silently
produce an empty/garbage watchlist.

FAILED RUN -> SAFE RETRY (2 Oct 2026, user: "fix this so that watchlist
function can resume safely"). The first scheduled run (Fri 2 Oct 00:00
IST) met Dhan refusing every data call (DH-902 "not subscribed to Data
APIs"): it restarted the bot for memory first, scored 0 of 213, aborted
and exited 0 - systemd showed success and nothing would have retried for
a week. Now:
  - Before any restart or file change, one small NIFTY daily-history call
    (data_probe) checks that Dhan returns candles. No data (or the login
    fails) -> abort with NO bot restart.
  - Every abort for missing data (probe, login, or the scored-count net
    above) writes data/weekly_watchlist_refresh_pending.json and exits
    EXIT_RETRY_LATER (75), so systemd records the run as failed.
  - `--retry` (droplet-side dhanboy-weekly-watchlist-retry.timer/.service,
    not in git; the unit only starts while the pending file exists) re-runs
    the whole job, but only weekdays 00:00-07:00 IST or at weekends - never
    while a market can be open, finished well before the 08:00 IST morning
    refresh. A completed run deletes the pending file; the Friday schedule
    is unchanged. Runs never overlap (lock file in history/).
  - The day's log file is appended to, so every attempt stays on record.

Read-only Dhan calls only; the only writes this script performs are its
own log file, the pending-retry file and the watchlist text files.

HOW TO RUN (normally fired by the systemd timers, not by hand):
    uv run python weekly_watchlist_refresh.py            # the Friday run
    uv run python weekly_watchlist_refresh.py --retry    # only if a run is pending, only in the retry window
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import requests

from Options.dhan_client import dhan_wrapper
from Swing import config as swing_config
from fno_ath_screener import get_top_n_candidates, explain, DEFAULT_TOP_N
import stock_selection

IST = ZoneInfo("Asia/Kolkata")
HISTORY_DIR = REPO_ROOT / "history"
DATA_DIR = REPO_ROOT / "data"
SWING_WATCHLIST_FILE = DATA_DIR / "watchlist"
BOLLINGER_WATCHLIST_FILE = DATA_DIR / "bollinger_watchlist"
MIN_SUCCESSFUL_SCORES = 50  # abort (no file changes) if the scan can't score at least this many stocks
PENDING_FILE = DATA_DIR / "weekly_watchlist_refresh_pending.json"   # a run that found no Dhan data, to retry
LOCK_FILE = HISTORY_DIR / ".weekly_watchlist_refresh.lock"
EXIT_RETRY_LATER = 75       # EX_TEMPFAIL: systemd shows the run as failed; the retry timer runs it again
RETRY_WEEKDAY_BEFORE_HOUR = 7   # IST: weekday retries start 00:00-06:59 only (markets shut, before 08:00 restart)

POSITION_ENDPOINTS = [
    ("options", "/positions"), ("swing", "/swing/positions"),
    ("bollinger", "/bollinger/positions"), ("luxury", "/luxury/positions"),
]


def read_watchlist_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return [ln.strip().upper() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def split_protected_vs_equity(symbols: list[str]) -> tuple[list[str], list[str]]:
    """protected = MCX commodities (live-checked, data-driven - same
    dhan_wrapper.is_mcx_commodity() the rest of the codebase already
    uses) or index symbols (Swing.config.INDEX_SYMBOLS). NEVER replaced
    by this job, per explicit user instruction. Everything else is
    'equity' and gets fully replaced with the new top-N each run."""
    protected, equity = [], []
    for sym in symbols:
        if sym in swing_config.INDEX_SYMBOLS or dhan_wrapper.is_mcx_commodity(sym):
            protected.append(sym)
        else:
            equity.append(sym)
    return protected, equity


def backup_file(path: Path, ts: str) -> None:
    if not path.exists():
        return
    backup_path = path.with_name(f"{path.name}.bak.{ts}")
    backup_path.write_text(path.read_text())


def write_watchlist_file(path: Path, protected: list[str], new_equity: list[str]) -> None:
    lines = protected + new_equity
    path.write_text("\n".join(lines) + "\n")


def snapshot_live_positions() -> dict:
    """Read-only check across all 4 packages, for the audit trail ONLY -
    never a gate on whether this job proceeds (explicit user instruction,
    27 Sep 2026: 'there shouldn't be any holdback')."""
    positions = {}
    for pkg, endpoint in POSITION_ENDPOINTS:
        try:
            resp = requests.get(f"http://localhost:8000{endpoint}", timeout=5)
            positions[pkg] = resp.json().get("live_positions", [])
        except Exception as exc:  # noqa: BLE001
            positions[pkg] = f"check failed: {exc}"
    return positions


SAFE_RESTART = REPO_ROOT / "safe_restart.py"
SAFE_RESTART_TIMEOUT_SECONDS = 300


def restart_bot(log=print) -> str:
    """Restart via safe_restart.py; returns "restarted" | "restarted_needs_review" | "skipped_checks_failed" |
    "restarted_health_not_back" | "restarted_plain". Its output goes to `log`."""
    try:
        proc = subprocess.run([sys.executable, str(SAFE_RESTART)], cwd=str(REPO_ROOT), capture_output=True, text=True,
                              timeout=SAFE_RESTART_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - the tool itself could not run: do what this job did before
        log(f"safe_restart.py could not be run ({exc!r}) - falling back to a plain restart")
        subprocess.run(["systemctl", "restart", "dhanboy.service"], check=True)
        return "restarted_plain"
    for line in (proc.stdout + proc.stderr).splitlines():
        log(f"  safe_restart: {line}")
    return {0: "restarted", 1: "restarted_needs_review", 2: "skipped_checks_failed",
            3: "restarted_health_not_back"}.get(proc.returncode, f"safe_restart_exit_{proc.returncode}")


# 1 Oct 2026 (price-path audit, section 5): this job is a second Python process with its own instrument
# master and the whole F&O universe's candles, started while the bot sits at its end-of-day size (one
# full-day run peaked at 420 MB and pushed 92 MB into swap on a 961 MB droplet). When memory is short, the
# bot is restarted FIRST (safe_restart.py - every market is closed at Friday 00:00 IST) so the scoring runs
# next to a freshly started, smaller bot. The usual restart at the end still loads the new lists.
MIN_AVAILABLE_MB_BEFORE_SCORING = int(os.getenv("WEEKLY_REFRESH_MIN_AVAILABLE_MB", "450"))


def available_memory_mb() -> Optional[int]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def data_probe() -> Optional[str]:
    """One small daily-history call (the endpoint the scoring uses) for NIFTY. None when Dhan returns candles,
    otherwise why not - e.g. DH-902 'User has not subscribed to Data APIs' (1-2 Oct 2026)."""
    today = datetime.now(IST)
    try:
        resp = dhan_wrapper.client.Dhan.historical_daily_data(
            security_id=dhan_wrapper.NIFTY_SECURITY_ID, exchange_segment="IDX_I", instrument_type="INDEX",
            from_date=(today - timedelta(days=10)).strftime("%Y-%m-%d"), to_date=today.strftime("%Y-%m-%d"))
    except Exception as exc:  # noqa: BLE001
        return f"the NIFTY history call raised {exc!r}"
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict) and data.get("close"):
        return None
    detail = resp.get("remarks") if isinstance(resp, dict) else resp
    return f"no NIFTY daily candles - Dhan said: {str(detail)[:300]}"


def retry_blocked_reason(now: datetime) -> Optional[str]:
    """--retry runs only while a failed run is pending, and only when no market can be open."""
    if not PENDING_FILE.exists():
        return "no failed run is pending"
    if now.weekday() >= 5 or now.hour < RETRY_WEEKDAY_BEFORE_HOUR:
        return None
    return (f"outside the retry window (weekdays 00:00-{RETRY_WEEKDAY_BEFORE_HOUR:02d}:00 IST, weekends any time) - "
            f"a market can be open")


def record_pending(reason: str, now: datetime) -> dict:
    try:
        prev = json.loads(PENDING_FILE.read_text())
    except (OSError, ValueError):
        prev = {}
    rec = {"since": prev.get("since") or now.isoformat(timespec="seconds"),
           "attempts": int(prev.get("attempts") or 0) + 1,
           "last_attempt": now.isoformat(timespec="seconds"), "last_reason": reason}
    PENDING_FILE.parent.mkdir(exist_ok=True)
    PENDING_FILE.write_text(json.dumps(rec, indent=2) + "\n")
    return rec


def clear_pending() -> Optional[dict]:
    """Deletes the pending-retry file; returns what it held (None if nothing was pending)."""
    try:
        rec = json.loads(PENDING_FILE.read_text())
    except (OSError, ValueError):
        rec = None
    PENDING_FILE.unlink(missing_ok=True)
    return rec


def acquire_run_lock():
    """An open, exclusively locked file (keep it open for the whole run), or None if another run holds it."""
    HISTORY_DIR.mkdir(exist_ok=True)
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def main(argv: Optional[list[str]] = None) -> int:
    retry = "--retry" in (sys.argv[1:] if argv is None else argv)
    now = datetime.now(IST)
    if retry:
        blocked = retry_blocked_reason(now)
        if blocked:
            print(f"Retry not started: {blocked}.")
            return 0
    lock = acquire_run_lock()
    if lock is None:
        print("Another weekly watchlist refresh is running - not starting a second one.")
        return 0
    ts = now.strftime("%Y%m%d_%H%M%S")
    log_path = HISTORY_DIR / f"{now.strftime('%Y-%m-%d')}_weekly_watchlist_refresh.log"
    log_lines = [f"=== Weekly watchlist refresh{' - RETRY of a failed run' if retry else ''} - "
                 f"{now.strftime('%Y-%m-%d %H:%M:%S')} IST ==="]

    def log(msg: str) -> None:
        print(msg)
        log_lines.append(msg)

    def write_log() -> None:
        with open(log_path, "a") as fh:     # append: a retry on the same day keeps the earlier attempt's record
            fh.write("\n".join(log_lines) + "\n\n")

    def give_up_for_now(reason: str) -> int:
        rec = record_pending(reason, now)
        log(f"\nABORTING: {reason}. Leaving every watchlist untouched, NOT restarting. Marked for retry "
            f"({PENDING_FILE.name}, attempt {rec['attempts']}, pending since {rec['since']}): the retry timer re-runs "
            f"this job hourly on weekdays 00:20-06:20 IST and at weekends until a run completes.")
        write_log()
        return EXIT_RETRY_LATER

    try:
        dhan_wrapper.authenticate()
    except Exception as exc:  # noqa: BLE001
        return give_up_for_now(f"Dhan login failed ({exc!r})")
    problem = data_probe()
    if problem:
        return give_up_for_now(f"Dhan market data is not available - {problem}")
    log("Dhan data probe OK (NIFTY daily candles returned).")

    avail = available_memory_mb()
    if avail is not None and avail < MIN_AVAILABLE_MB_BEFORE_SCORING:
        log(f"Only {avail} MB available (< {MIN_AVAILABLE_MB_BEFORE_SCORING}) - restarting the bot first so the "
            f"scoring does not push it into swap")
        log(f"  pre-scoring restart: {restart_bot()} - now {available_memory_mb()} MB available")

    log("Checking live positions across all packages (audit trail only - NOT a gate on this run)...")
    for pkg, pos in snapshot_live_positions().items():
        log(f"  {pkg}: {pos}")

    log("\nScoring full F&O universe (composite Minervini+ATH+Stage2, fno_ath_screener.get_top_n_candidates)...")
    top_n, failed = get_top_n_candidates(
        n=DEFAULT_TOP_N, progress_callback=lambda d, t: log(f"  ...{d}/{t} done"))
    log(f"Scored {len(top_n)} top candidates ({len(failed)} symbols failed/skipped out of the full universe).")

    if len(top_n) < min(DEFAULT_TOP_N, MIN_SUCCESSFUL_SCORES):
        return give_up_for_now(f"only {len(top_n)} candidates scored (need >= {MIN_SUCCESSFUL_SCORES}) - this looks "
                               f"like a broad data/API failure, not a normal day")

    new_equity_symbols = [r["symbol"] for r in top_n]
    log(f"\nNew equity watchlist (top {len(new_equity_symbols)} by composite score): {new_equity_symbols}")
    for i, r in enumerate(top_n, 1):
        log(f"  {i:2d}. {r['symbol']} (score {r['total_score']:.1f}/100): {explain(r)}")

    for label, path in [("Swing", SWING_WATCHLIST_FILE), ("Bollinger", BOLLINGER_WATCHLIST_FILE)]:
        current = read_watchlist_file(path)
        protected, old_equity = split_protected_vs_equity(current)
        added = sorted(set(new_equity_symbols) - set(old_equity))
        removed = sorted(set(old_equity) - set(new_equity_symbols))
        log(f"\n{label} watchlist - protected (never touched): {protected}")
        log(f"{label} equity - old: {sorted(old_equity)}")
        log(f"{label} equity - new: {sorted(new_equity_symbols)}")
        log(f"{label} ADDED: {added}")
        log(f"{label} REMOVED: {removed}")

        backup_file(path, ts)
        write_watchlist_file(path, protected, new_equity_symbols)
        log(f"{label} watchlist file updated ({len(protected)} protected + {len(new_equity_symbols)} equity "
            f"= {len(protected) + len(new_equity_symbols)} total).")

    log("\nSuper Bollinger + Unified Momentum watchlists - HYBRID selection (ATH top 40 -> strategy fit, "
        "stock_selection.py)...")
    try:
        old_super = read_watchlist_file(stock_selection.SUPER_BOLLINGER_WATCHLIST_FILE)
        old_um = read_watchlist_file(stock_selection.UNIFIED_MOMENTUM_WATCHLIST_FILE)
        picks = stock_selection.run_live(log)
        new_super = [p_["symbol"] for p_ in picks]
        if len(new_super) < 10:
            log(f"HYBRID: only {len(new_super)} picks - keeping last week's lists: Super Bollinger {old_super}, "
                f"Unified Momentum {old_um}")
        else:
            log(f"Super Bollinger ADDED: {sorted(set(new_super) - set(old_super))}")
            log(f"Super Bollinger REMOVED: {sorted(set(old_super) - set(new_super))}")
            stock_selection.write_watchlist(new_super, stock_selection.SUPER_BOLLINGER_WATCHLIST_FILE)
            log(f"Super Bollinger watchlist file updated ({len(new_super)} stocks).")
            log(f"Unified Momentum ADDED: {sorted(set(new_super) - set(old_um))}")
            log(f"Unified Momentum REMOVED: {sorted(set(old_um) - set(new_super))}")
            stock_selection.write_watchlist(new_super, stock_selection.UNIFIED_MOMENTUM_WATCHLIST_FILE)  # the real one
            log(f"Unified Momentum watchlist file updated ({len(new_super)} stocks).")
    except Exception as exc:  # noqa: BLE001
        log(f"HYBRID selection FAILED ({exc!r}) - keeping last week's Super Bollinger and Unified Momentum lists; "
            f"the ATH watchlists above were already updated normally.")

    log("\nSuper Bollinger SHADOW list (recorded and scored only - never traded, never written to a watchlist)...")
    try:
        # scored against the live list Unified Momentum trades (the same picks Super Bollinger's file gets)
        stock_selection.run_shadow(log, read_watchlist_file(stock_selection.UNIFIED_MOMENTUM_WATCHLIST_FILE))
    except Exception as exc:  # noqa: BLE001
        log(f"Shadow list step FAILED ({exc!r}) - nothing else is affected.")

    cleared = clear_pending()
    if cleared:
        log(f"\nCompleted the run pending since {cleared.get('since')} ({cleared.get('attempts')} failed attempt(s), "
            f"last: {cleared.get('last_reason')}) - retry marker removed.")

    log("\nRestarting dhanboy.service through safe_restart.py (snapshot, checks, restart, /health, restart report)...")
    try:
        outcome = restart_bot(log)
        log({"restarted": "Restarted; the restart report is clean.",
             "restarted_needs_review": "Restarted, but the restart report says NEEDS REVIEW - see the lines above.",
             "skipped_checks_failed": "NOT restarted: a safe-restart check failed (see above). The new watchlists are "
                                      "already live - every store re-reads its file on each monitor tick - and the "
                                      "08:00 IST morning refresh restarts the bot.",
             "restarted_health_not_back": "Restart issued but /health did not come back within 2 minutes - check "
                                          "journalctl -u dhanboy.service.",
             "restarted_plain": "Restarted with a plain systemctl restart (safe_restart.py could not run)."}
            .get(outcome, f"safe_restart.py ended unexpectedly ({outcome}) - check the bot."))
    except Exception as exc:  # noqa: BLE001
        log(f"RESTART FAILED: {exc}. Watchlist files were already updated - both stores re-read "
            f"the file on every monitor tick regardless, so the new watchlist is live even without "
            f"a restart; a manual restart is only needed for anything that specifically requires one.")

    write_log()
    print(f"\nFull report written to {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
