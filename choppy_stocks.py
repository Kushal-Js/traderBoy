"""
Manually-maintained "choppy stocks" exclusion list for the Options
strategy - stocks the user has chosen to skip entirely for new Options
entries. New positions are never opened in any symbol on this list;
existing open positions (if any) are unaffected - this only gates new
entries, same as every other capacity/dedup gate in this file's callers.

HISTORY (see NOTES.md entries #55/#56 for the full story): originally
built (31 Aug 2026) as an automatic weekly scan for every NSE stock-option
underlying with lot size > 6000 units. The user reviewed that scan's
output (14 stocks) and decided, same day, to keep only 3 of them
(IDEA, YESBANK, SAGILITY) and switch to maintaining the list BY HAND from
here on - no more automatic scanning, no more weekly refresh. This file
now just seeds that starting list once and provides a thin read layer;
all the lot-size-scanning/scheduling logic from the original build was
removed, not left dormant, so there's no unused machinery to confuse a
future reader about which behavior is actually live.

Stored on disk at choppy/choppy_stocks.json - a gitignored, server-only
runtime data folder, same convention as history/ (see trade_history.py's
own module docstring: runtime data lives outside git, only source does).
To change the list, edit that file directly on the server - a plain
{"stocks": [...]} JSON array, e.g.:
    ssh -i ~/.ssh/dhanboy_droplet root@<droplet-ip>
    nano ~/apps/traderBoy/choppy/choppy_stocks.json
Takes effect on the very next webhook alert - is_choppy() reads the file
fresh every time (deliberately not cached - see its own docstring), so no
restart is needed after a manual edit.

FAILS OPEN, not closed: if the file is missing, unreadable, or corrupt,
is_choppy() returns False for everything (nothing excluded) rather than
risk a bug in this module silently blocking every real Options entry.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("choppy_stocks")

CHOPPY_DIR = Path("choppy")
CHOPPY_FILE = CHOPPY_DIR / "choppy_stocks.json"

# Seeded 31 Aug 2026 - the 3 stocks the user chose to keep from that day's
# one-time lot-size scan (14 candidates, lot size > 6000 - see NOTES.md
# entry #55 for the full list). Written to disk once by
# ensure_choppy_list_exists(), only if choppy_stocks.json doesn't already
# exist - never overwritten automatically after that.
DEFAULT_CHOPPY_STOCKS = ["IDEA", "YESBANK", "SAGILITY"]


def write_choppy_list(stocks: list[str]) -> dict:
    """Normalizes (uppercase, dedup, sorted) and writes choppy_stocks.json
    atomically (temp file + Path.replace - POSIX guarantees this can't
    leave a half-written file for a concurrent reader)."""
    CHOPPY_DIR.mkdir(parents=True, exist_ok=True)
    data = {"stocks": sorted({s.strip().upper() for s in stocks if s.strip()})}
    tmp_path = CHOPPY_FILE.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    tmp_path.replace(CHOPPY_FILE)
    return data


def ensure_choppy_list_exists() -> None:
    """One-time bootstrap - writes DEFAULT_CHOPPY_STOCKS to disk ONLY if
    choppy_stocks.json doesn't exist yet (first-ever deploy of this
    feature, or a fresh server). Never touches an existing file - once
    it's there, only a human hand-editing it changes its contents (delete
    it to reset back to the default). Called once from Options' lifespan
    at startup."""
    if CHOPPY_FILE.exists():
        return
    logger.info("No choppy-stocks list on disk yet - seeding it with the default: %s", DEFAULT_CHOPPY_STOCKS)
    write_choppy_list(DEFAULT_CHOPPY_STOCKS)


def read_choppy_list() -> Optional[dict]:
    """Raw contents of choppy/choppy_stocks.json, or None if it doesn't
    exist or can't be parsed."""
    try:
        with open(CHOPPY_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Could not read/parse %s - treating as absent", CHOPPY_FILE)
        return None


def is_choppy(symbol: str) -> bool:
    """Reads choppy/choppy_stocks.json fresh on every call - deliberately
    NOT cached in memory, so a manual edit to the file takes effect on the
    very next webhook alert with no restart needed. This is safe to do
    per-call because it's only checked per webhook ALERT (a handful of
    times a minute at most, never per price tick), against a file that
    normally holds a handful of symbols - the OS page cache makes a
    repeat read of a file this small and this rarely-changing effectively
    free, nowhere near the same class of I/O concern as the per-tick
    monitor loop elsewhere in this codebase (which does stay fully
    cached/async - see dhan_client.py's ltp cache).

    FAILS OPEN: any problem reading the file (missing, corrupt) returns
    False for every symbol - see module docstring."""
    data = read_choppy_list()
    if data is None:
        return False
    try:
        return symbol.strip().upper() in {s.upper() for s in data["stocks"]}
    except Exception:  # noqa: BLE001
        logger.exception("Malformed choppy-stocks list - treating as empty (nothing excluded) for this check")
        return False
