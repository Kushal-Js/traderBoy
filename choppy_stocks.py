"""
Weekly "choppy stocks" exclusion list for the Options strategy - stocks
whose single F&O options lot exceeds LOT_SIZE_THRESHOLD units. A large
lot size means a large notional/margin commitment per lot and coarse
position sizing (can't scale down to manage risk the way a smaller-lot
stock allows), which the user chose to avoid entirely for Options entries
rather than tune around per-trade - user request 31 Aug 2026.

Stored on disk at choppy/choppy_stocks.json - a gitignored, server-only
runtime data folder, same convention as history/ (see trade_history.py's
own module docstring: runtime data lives outside git, only source does).
Refreshed automatically every Monday at 12:00 PM IST by
choppy_list_refresh_loop(), started from Options/option_main.py's
lifespan (the sole current consumer - Futures/K01/etc. could read the
same file later without needing their own copy of this module).

Two read paths, deliberately different:
  - is_choppy(symbol) - the HOT path, called once per candidate stock on
    every webhook alert (Options/trading_engine.py's _process_one_entry
    and option_main.py's own pre-ranking filter). Pure in-memory set
    membership, zero I/O, safe to call directly from an async function
    without an executor.
  - read_choppy_list() / GET /choppy-stocks - the COLD path, for humans
    checking what's currently excluded and why. Reads the JSON file fresh
    each time; fine to be slower since it's not on any trading-latency
    path.
The in-memory cache is populated at process startup (best-effort, from
whatever's already on disk) and kept current by every refresh_choppy_list()
call afterward - see _load_into_cache().

FAILS OPEN, not closed: if the file is missing, unreadable, or corrupt,
the in-memory cache ends up empty (nothing excluded) with a warning
logged, rather than risk a bug in this module silently blocking every
real Options entry. The refresh loop is equally defensive - any exception
during a refresh just keeps whatever was cached before and tries again at
the next scheduled slot, never crashes Options' own startup.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("choppy_stocks")

IST = ZoneInfo("Asia/Kolkata")

CHOPPY_DIR = Path("choppy")
CHOPPY_FILE = CHOPPY_DIR / "choppy_stocks.json"

# "stock quantity more than 6000 per their single Options lot" - user's
# own phrasing, 31 Aug 2026.
LOT_SIZE_THRESHOLD = 6000

REFRESH_WEEKDAY = 0  # Monday (datetime.weekday(): Monday == 0)
REFRESH_HOUR_IST = 12  # 12:00 PM IST


# --------------------------------------------------------------------------- #
# Compute + persist
# --------------------------------------------------------------------------- #
def compute_choppy_stocks(dhan_wrapper) -> dict[str, int]:
    """Every NSE stock-option (OPTSTK) underlying whose current lot size
    exceeds LOT_SIZE_THRESHOLD, read from Tradehull's own cached scrip-
    master (same instrument-master source/filter K01's own
    _fetch_fno_universe uses for the full F&O universe - see
    K01/paper_engine.py). A given underlying has many OPTSTK rows
    (different strikes/expiries); all share the same lot size at any
    point in time (NSE revises it periodically for everyone at once, not
    per-strike), so the first row seen per underlying is enough."""
    df = dhan_wrapper.instruments()
    optstk = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")]

    lot_size_by_underlying: dict[str, int] = {}
    for trading_symbol, lot_units in zip(optstk["SEM_TRADING_SYMBOL"], optstk["SEM_LOT_UNITS"]):
        try:
            underlying = dhan_wrapper._underlying_from_trading_symbol(str(trading_symbol))
            lot_size = int(float(lot_units))
        except Exception:  # noqa: BLE001
            continue
        lot_size_by_underlying.setdefault(underlying, lot_size)

    return {
        symbol: lot_size
        for symbol, lot_size in lot_size_by_underlying.items()
        if lot_size > LOT_SIZE_THRESHOLD
    }


def write_choppy_list(stocks: dict[str, int]) -> dict:
    """Writes choppy/choppy_stocks.json atomically (write to a temp file,
    then os.replace via Path.replace - POSIX guarantees this can't leave
    a half-written file for a concurrent reader, e.g. a GET /choppy-stocks
    request landing mid-write)."""
    CHOPPY_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": datetime.now(IST).isoformat(),
        "threshold": LOT_SIZE_THRESHOLD,
        "count": len(stocks),
        "stocks": [
            {"symbol": symbol, "lot_size": lot_size}
            for symbol, lot_size in sorted(stocks.items(), key=lambda kv: -kv[1])
        ],
    }
    tmp_path = CHOPPY_FILE.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    tmp_path.replace(CHOPPY_FILE)
    return data


def refresh_choppy_list(dhan_wrapper) -> dict:
    """Synchronous - computes + writes + updates the in-memory cache in
    one call. Runs in a worker thread from choppy_list_refresh_loop
    (never call directly from the event loop - instruments() over the
    full scrip master, though brief, is real blocking work)."""
    stocks = compute_choppy_stocks(dhan_wrapper)
    data = write_choppy_list(stocks)
    _load_into_cache(data)
    logger.info(
        "Choppy-stocks list refreshed: %d stock(s) with lot size > %d: %s",
        data["count"], LOT_SIZE_THRESHOLD, sorted(stocks.keys()),
    )
    return data


# --------------------------------------------------------------------------- #
# Read paths
# --------------------------------------------------------------------------- #
def read_choppy_list() -> Optional[dict]:
    """Raw contents of choppy/choppy_stocks.json, or None if it doesn't
    exist yet (e.g. before the first bootstrap/refresh has ever run) or
    can't be parsed. Cold path - GET /choppy-stocks and startup only."""
    try:
        with open(CHOPPY_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Could not read/parse %s - treating as absent", CHOPPY_FILE)
        return None


_cached_choppy_symbols: set[str] = set()


def _load_into_cache(data: Optional[dict]) -> None:
    global _cached_choppy_symbols
    if data is None:
        _cached_choppy_symbols = set()
        return
    try:
        _cached_choppy_symbols = {s["symbol"] for s in data["stocks"]}
    except Exception:  # noqa: BLE001
        logger.exception("Malformed choppy-stocks data - treating as empty (nothing excluded)")
        _cached_choppy_symbols = set()


def load_choppy_cache_at_startup() -> None:
    """Best-effort initial load from whatever's already on disk (e.g. from
    a previous process's run, surviving this restart). Called once from
    Options' lifespan before choppy_list_refresh_loop's own bootstrap-if-
    missing logic (which calls refresh_choppy_list -> _load_into_cache
    itself if no file exists on disk at all yet)."""
    _load_into_cache(read_choppy_list())


def is_choppy(symbol: str) -> bool:
    """HOT path - pure in-memory set membership, zero I/O. Safe to call
    directly (no executor needed) from anywhere in the entry path. See
    module docstring for why this is deliberately NOT a disk read."""
    return symbol in _cached_choppy_symbols


# --------------------------------------------------------------------------- #
# Weekly refresh loop
# --------------------------------------------------------------------------- #
def _next_monday_noon_ist(now: datetime) -> datetime:
    """The next instant that is REFRESH_WEEKDAY (Monday) at
    REFRESH_HOUR_IST (12:00 PM), strictly after `now`. `now` must be
    IST-aware (this file only ever calls it with datetime.now(IST))."""
    days_ahead = (REFRESH_WEEKDAY - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=REFRESH_HOUR_IST, minute=0, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


async def choppy_list_refresh_loop(dhan_wrapper) -> None:
    """Runs forever. Bootstraps the list immediately if nothing exists on
    disk yet (so a freshly-provisioned server isn't excluding nothing all
    week just because it happened to start mid-week), then refreshes
    every Monday at 12:00 PM IST after that. Started from
    Options/option_main.py's lifespan (asyncio.create_task, same pattern
    as monitor_loop/paper_webhook.poll_loop); cancelled on shutdown the
    same way."""
    loop = asyncio.get_running_loop()

    if read_choppy_list() is None:
        logger.info("No choppy-stocks list on disk yet - generating an initial one now.")
        try:
            await loop.run_in_executor(None, refresh_choppy_list, dhan_wrapper)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Initial choppy-stocks list generation failed - nothing excluded "
                "until the next scheduled refresh succeeds."
            )

    while True:
        now = datetime.now(IST)
        target = _next_monday_noon_ist(now)
        sleep_seconds = (target - now).total_seconds()
        logger.info(
            "Choppy-stocks list: next refresh at %s IST (in %.1f hours).",
            target.isoformat(), sleep_seconds / 3600,
        )
        await asyncio.sleep(sleep_seconds)
        try:
            await loop.run_in_executor(None, refresh_choppy_list, dhan_wrapper)
        except Exception:  # noqa: BLE001
            logger.exception("Weekly choppy-stocks list refresh failed - keeping the previous list.")
