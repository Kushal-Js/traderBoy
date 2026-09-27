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
  8. Restarts dhanboy.service - UNCONDITIONALLY, per explicit user
     instruction. The bot's own broker-reconciliation-on-restart logic
     (verified live 17 Sep 2026, see TRADING_JOURNAL.md) recovers any
     position still open across the restart; the one known, already-
     accepted tradeoff is that a recovered position's trailing-stop
     memory (highest_price) resets to the broker's reported entry price.

ONE safety net this script DOES still apply - NOT a position holdback,
the user never asked for that to be skipped, just a basic defense against
acting on a broken scan: if the composite scan successfully scores fewer
than MIN_SUCCESSFUL_SCORES stocks (e.g. a Dhan-wide outage that day),
abort BEFORE touching any file, log why, and leave both watchlists
exactly as they were. An unrelated API failure should never silently
produce an empty/garbage watchlist.

Read-only Dhan calls only; the only writes this script performs are its
own log file and the two watchlist text files.

HOW TO RUN (normally fired by the systemd timer, not by hand):
    uv run python weekly_watchlist_refresh.py
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import requests

from Options.dhan_client import dhan_wrapper
from Swing import config as swing_config
from fno_ath_screener import get_top_n_candidates, explain, DEFAULT_TOP_N

IST = ZoneInfo("Asia/Kolkata")
HISTORY_DIR = REPO_ROOT / "history"
DATA_DIR = REPO_ROOT / "data"
SWING_WATCHLIST_FILE = DATA_DIR / "watchlist"
BOLLINGER_WATCHLIST_FILE = DATA_DIR / "bollinger_watchlist"
MIN_SUCCESSFUL_SCORES = 50  # abort (no file changes) if the scan can't score at least this many stocks

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


def restart_bot() -> None:
    subprocess.run(["systemctl", "restart", "dhanboy.service"], check=True)


def main() -> None:
    dhan_wrapper.authenticate()
    now = datetime.now(IST)
    ts = now.strftime("%Y%m%d_%H%M%S")
    HISTORY_DIR.mkdir(exist_ok=True)
    log_path = HISTORY_DIR / f"{now.strftime('%Y-%m-%d')}_weekly_watchlist_refresh.log"
    log_lines = [f"=== Weekly watchlist refresh - {now.strftime('%Y-%m-%d %H:%M:%S')} IST ==="]

    def log(msg: str) -> None:
        print(msg)
        log_lines.append(msg)

    log("Checking live positions across all packages (audit trail only - NOT a gate on this run)...")
    for pkg, pos in snapshot_live_positions().items():
        log(f"  {pkg}: {pos}")

    log("\nScoring full F&O universe (composite Minervini+ATH+Stage2, fno_ath_screener.get_top_n_candidates)...")
    top_n, failed = get_top_n_candidates(
        n=DEFAULT_TOP_N, progress_callback=lambda d, t: log(f"  ...{d}/{t} done"))
    log(f"Scored {len(top_n)} top candidates ({len(failed)} symbols failed/skipped out of the full universe).")

    if len(top_n) < min(DEFAULT_TOP_N, MIN_SUCCESSFUL_SCORES):
        log(f"\nABORTING: only {len(top_n)} candidates scored (need >= {MIN_SUCCESSFUL_SCORES}) - "
            f"this looks like a broad data/API failure, not a normal day. Leaving both watchlists "
            f"untouched, NOT restarting.")
        log_path.write_text("\n".join(log_lines) + "\n")
        return

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

    log("\nRestarting dhanboy.service (unconditional, per explicit user instruction - broker "
        "reconciliation on restart recovers any position still open; its trailing-stop memory "
        "resets to the broker's reported entry price, an already-accepted tradeoff)...")
    try:
        restart_bot()
        log("Restart command issued successfully.")
    except Exception as exc:  # noqa: BLE001
        log(f"RESTART FAILED: {exc}. Watchlist files were already updated - both stores re-read "
            f"the file on every monitor tick regardless, so the new watchlist is live even without "
            f"a restart; a manual restart is only needed for anything that specifically requires one.")

    log_path.write_text("\n".join(log_lines) + "\n")
    print(f"\nFull report written to {log_path}")


if __name__ == "__main__":
    main()
