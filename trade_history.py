"""
Persistent, cross-strategy trade/log history, stored in a dedicated
`history/` folder, date-prefixed per file (user request, 31 Aug 2026 -
"create a folder named history... store all trades and logs related files
named with date prefix... keep on adding to it").

This module has two layers:
  1. Generic helpers (`append_jsonl`/`read_all_jsonl`) - a new day writes a
     new dated file (`history/<YYYY-MM-DD>_<name>.log`); reading always
     globs and merges every dated file for that name, so "history" means
     the full multi-day record, not just today. Used by this module's own
     real-trade functions below AND by K01/CopperOptions/IndexScalping's
     PaperTradeStore (K01/paper_engine.py, CopperOptions/paper_engine.py,
     IndexScalping/paper_engine.py) - this file is the single shared place
     that owns the `history/` naming convention, so every store rotates
     files the exact same way rather than each package inventing its own.
  2. Real-trade-specific functions (`record_closed_trade`/`read_all_trades`)
     - the original purpose of this module (30 Aug 2026): a persistent
     record of every REAL (non-paper) closed trade, tagged by which
     package placed it. Options/position_store.py and
     Futures/position_store.py's own in-memory `closed_positions_today`
     resets daily and doesn't survive a restart - this is the durable
     record. `record_closed_trade` is called AFTER a position is already
     closed (the real exit order has already been placed and confirmed),
     so it can never affect whether/when/at-what-price a real order is
     placed. Every function here is wrapped so a logging failure can never
     raise and break a caller's actual trading-critical flow.

Existing pre-migration files (`real_trade_history.log`,
`copper_paper_trades.log`, `paper_trades.log`) were one-time migrated into
this dated scheme - see NOTES.md's entry for the exact migration and where
the original files were preserved (not deleted).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("trade_history")

HISTORY_DIR = Path(__file__).resolve().parent / "history"

# Fire-and-forget task tracking - a real asyncio gotcha, found and fixed
# 31 Aug 2026 during a concurrency audit: `asyncio.create_task(coro)`
# without keeping a reference to the returned Task is documented (Python's
# own asyncio docs) to risk the task being garbage-collected mid-execution
# - "The event loop only keeps weak references to tasks." Every fire-and-
# forget logging call in this codebase (record_closed_trade,
# record_webhook_alert) goes through fire_and_forget() below instead of a
# bare asyncio.create_task(...), so a strong reference is held in
# _background_tasks until the task actually finishes.
_background_tasks: set[asyncio.Task] = set()


def fire_and_forget(coro) -> asyncio.Task:
    """Schedule `coro` as a background task that cannot be garbage-
    collected before it completes, without the caller having to await it
    or hold its own reference. Use this instead of a bare
    asyncio.create_task(...) for any logging-only call that must never
    delay or be able to affect its caller (see this module's own
    record_closed_trade/record_webhook_alert for the pattern this
    supports)."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def dated_path(name: str, d: Optional[date] = None) -> Path:
    """history/<YYYY-MM-DD>_<name>.log - creates the history/ dir on first
    use if it doesn't exist yet."""
    d = d or date.today()
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return HISTORY_DIR / f"{d.isoformat()}_{name}.log"


def append_jsonl(name: str, record: dict, d: Optional[date] = None) -> None:
    """Appends one JSON line to today's (or `d`'s) dated file for `name`.
    Swallows and logs any failure - never raises, since every caller of
    this is logging-only and must never break the caller's real flow."""
    try:
        path = dated_path(name, d)
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:  # noqa: BLE001
        logger.exception("Could not append to history/%s - the underlying "
                          "action itself is unaffected, this is logging-only.", name)


def read_all_jsonl(name: str) -> list[dict]:
    """Reads every history/<date>_<name>.log file that exists, oldest
    first (glob + sort - ISO-format dates in the filename sort correctly
    as plain strings, no date parsing needed for ordering)."""
    records: list[dict] = []
    for path in sorted(HISTORY_DIR.glob(f"*_{name}.log")):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            continue
    return records


# --------------------------------------------------------------------- #
# Real (non-paper) trade history - Options + Futures only. K01/
# CopperOptions/IndexScalping are paper-only and use their own separate
# names via append_jsonl/read_all_jsonl directly (see each package's
# paper_engine.py) - mixing real and paper trades under one name would be
# actively misleading for later analysis.
# --------------------------------------------------------------------- #
REAL_TRADES_NAME = "real_trades"


async def record_closed_trade(strategy: str, pos) -> None:
    """strategy: "Options" or "Futures". pos: a closed
    Options.position_store.Position or Futures.position_store.Position -
    both have an identical field set for everything read here (confirmed
    by reading both dataclasses directly).

    IMPORTANT (found + fixed 31 Aug 2026, user asked to audit for lag):
    this used to be a plain sync function called directly inside
    PositionStore.close_position()'s `async with self._lock:` block - a
    blocking disk write on the event loop thread, while holding the lock
    every other position operation (a concurrent exit, a fresh entry
    reserving the same symbol, a price-tick's update_highest_price) has to
    wait on. Now async for the same reason record_webhook_alert already
    is - call it via `asyncio.create_task(record_closed_trade(...))` and
    do NOT await it (see both position_store.py call sites). The actual
    write still runs in run_in_executor's thread pool, never the event
    loop, and still can't raise into the caller."""
    try:
        pnl = None
        if pos.exit_price is not None and pos.entry_price is not None:
            pnl = (pos.exit_price - pos.entry_price) * pos.quantity
        record = {
            "strategy": strategy,
            "underlying_symbol": pos.underlying_symbol,
            "option_trading_symbol": pos.option_trading_symbol,
            "option_type": pos.option_type,
            "quantity": pos.quantity,
            "product_type": pos.product_type,
            "entry_price": pos.entry_price,
            "exit_price": pos.exit_price,
            "exit_reason": pos.exit_reason,
            "pnl": pnl,
            "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
            "closed_at": pos.closed_at.isoformat() if pos.closed_at else None,
            "reconciled": getattr(pos, "reconciled", False),
            "order_id": pos.order_id,
            "logged_at": datetime.now().isoformat(),
        }
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, append_jsonl, REAL_TRADES_NAME, record)
    except Exception:  # noqa: BLE001
        logger.exception("Could not build/append real trade record for %s %s - "
                          "trade itself is unaffected, this is logging-only.",
                          strategy, getattr(pos, "underlying_symbol", "?"))


def read_all_trades(strategy: Optional[str] = None) -> list[dict]:
    """Read-only. strategy=None returns everything; "Options"/"Futures"
    filters. Used by the /trade-history endpoint - never called from any
    order-placement code path."""
    trades = read_all_jsonl(REAL_TRADES_NAME)
    if strategy is not None:
        trades = [t for t in trades if t.get("strategy") == strategy]
    return trades


# --------------------------------------------------------------------- #
# Opened-position log + broker-position attribution - user request
# 31 Aug 2026 ("make trades under Futures also reconcile"). The blocker
# this solves, confirmed via Dhan's own API docs before writing any code:
# Dhan's /positions endpoint has NO per-strategy tag at all -
# correlationId (the order tag Options/Futures already set on every entry
# order, see trading_engine.py's _gen_tag) exists ONLY on order-level
# responses, never on the aggregated position record itself. Since Options
# and Futures now both place REAL orders for the identical instrument
# type (ATM options), the broker's own data genuinely cannot distinguish
# "this open position is Options' vs Futures'" - this is why Futures never
# got reconciliation originally (see NOTES.md's design-decision entry) and
# why Options' own existing reconciliation was ALSO already latently
# vulnerable to importing a Futures-opened position (never observed live,
# but real given both place the same kind of order).
#
# The fix: our own persistent, durable, per-strategy record of every
# position OPENED (this section) - not Dhan's data - is what reconciliation
# now checks. record_opened_position mirrors record_closed_trade exactly
# (same fire-and-forget discipline, called from PositionStore.add_position
# right after a position is actually opened - can't affect the entry
# order itself). attribute_open_broker_position is the read side, called
# once per candidate position during startup reconciliation (a brief
# blocking read is fine there - it is NOT a hot path).
# --------------------------------------------------------------------- #
OPENED_POSITIONS_NAME = "position_opened"


async def record_opened_position(strategy: str, pos) -> None:
    """strategy: "Options" or "Futures". pos: a just-opened
    Options.position_store.Position or Futures.position_store.Position.
    Call via fire_and_forget from PositionStore.add_position - never
    awaited, same reasoning as record_closed_trade."""
    try:
        record = {
            "strategy": strategy,
            "underlying_symbol": pos.underlying_symbol,
            "option_trading_symbol": pos.option_trading_symbol,
            "option_type": pos.option_type,
            "quantity": pos.quantity,
            "entry_price": pos.entry_price,
            "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
            "order_id": pos.order_id,
            "logged_at": datetime.now().isoformat(),
        }
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, append_jsonl, OPENED_POSITIONS_NAME, record)
    except Exception:  # noqa: BLE001
        logger.exception("Could not append opened-position record for %s %s - "
                          "the position itself is unaffected, this is logging-only "
                          "(but WILL make this position unattributable if the "
                          "process restarts before it closes - see "
                          "attribute_open_broker_position's docstring).",
                          strategy, getattr(pos, "underlying_symbol", "?"))


def attribute_open_broker_position(option_trading_symbol: str) -> Optional[str]:
    """Read-only. Returns "Options"/"Futures" if - and ONLY if - exactly
    one strategy's own history shows this exact option_trading_symbol
    opened with no later matching close (i.e. still open per OUR records,
    not the broker's). Returns None if there is no record at all (predates
    this logging, or the position was opened manually outside the bot) OR
    if it's ambiguous (should never legitimately happen, but this never
    guesses if it does).

    Callers MUST treat None as "cannot safely attribute this position -
    log a clear warning and skip reconciling it" rather than defaulting
    to either strategy. Silently guessing wrong here means two strategies
    could both try to independently manage/exit the same real broker
    position - exactly the failure mode this whole mechanism exists to
    prevent."""
    opened = [r for r in read_all_jsonl(OPENED_POSITIONS_NAME)
              if r.get("option_trading_symbol") == option_trading_symbol]
    closed = [r for r in read_all_jsonl(REAL_TRADES_NAME)
              if r.get("option_trading_symbol") == option_trading_symbol]

    candidates = {r.get("strategy") for r in opened if r.get("strategy")}
    still_open_for = []
    for strategy in candidates:
        strategy_opens = [r for r in opened if r.get("strategy") == strategy]
        strategy_closes = [r for r in closed if r.get("strategy") == strategy]
        last_open_at = max((r.get("opened_at") or "" for r in strategy_opens), default="")
        last_closed_at = max((r.get("closed_at") or "" for r in strategy_closes), default="")
        if last_open_at and last_open_at > last_closed_at:
            still_open_for.append(strategy)

    if len(still_open_for) == 1:
        return still_open_for[0]
    return None


def count_opened_today(strategy: str, underlying_symbol: str) -> int:
    """Read-only. Counts how many times `strategy` has genuinely OPENED a
    position for `underlying_symbol` TODAY - added 1 Sep 2026 for
    Options/Futures/Luxury's daily re-entry cap (user request: "only
    allow entry into same trade max 3 times a day"). Backs that cap with
    this SAME on-disk log record_opened_position already writes to
    (OPENED_POSITIONS_NAME), rather than a fresh in-memory counter -
    deliberately, so the cap survives a mid-day restart. An in-memory
    counter (matching every other per-day count in position_store.py)
    would silently reset to 0 on any restart, which is exactly the wrong
    failure mode for a cap whose whole point is limiting how many times a
    volatile/whipsawing stock gets re-entered - a restart on such a day
    is a real possibility (a deploy, a crash-restart via
    Restart=always), not just a hypothetical.

    Reads only TODAY's own dated file (dated_path, not read_all_jsonl's
    full-history glob) - cheap enough to call on every entry attempt, a
    handful of JSON lines on a normal day. Counts genuine entries only:
    reconcile_from_broker() never calls record_opened_position() (see
    that function's own call site in position_store.py), so a position
    recovered at startup - already counted whenever it was FIRST opened,
    earlier today or on a prior day - is never double-counted here.

    Fails OPEN (returns 0) on a read error, same philosophy as every
    other logging-only path in this file (append_jsonl's own docstring:
    "never raises, since ... must never break the caller's real flow") -
    the position-count CAPS (MAX_LIVE_POSITIONS_CE/_PE) remain the
    primary real-money risk control regardless of this secondary limit;
    treating a rare disk hiccup as "silently lock this stock out for the
    rest of the day" would be a worse failure mode than the reverse."""
    path = dated_path(OPENED_POSITIONS_NAME)
    if not path.exists():
        return 0
    count = 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("strategy") == strategy and record.get("underlying_symbol") == underlying_symbol:
                    count += 1
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not read today's %s log for %s %s - treating as 0 entries so far "
            "(fail open, same as every other logging-only read in this file).",
            OPENED_POSITIONS_NAME, strategy, underlying_symbol,
        )
        return 0
    return count


# --------------------------------------------------------------------- #
# Webhook alert log - every incoming Chartink alert, tagged by which
# endpoint/strategy received it and what happened to it (processed vs
# ignored + reason). User request (31 Aug 2026), prompted by "are we
# logging the webhook alerts received?" - the answer was "only to
# journald, which has limited retention and isn't queryable" - this closes
# that the same way trade_history.py already closed the equivalent gap for
# closed trades.
#
# HARD REQUIREMENT (explicit user instruction): this must never add
# latency to, or be able to break, real order placement or position
# monitoring. Two things make that true:
#   1. The actual disk write runs in the default thread-pool executor
#      (loop.run_in_executor), never on the event loop thread - so even a
#      slow/stalled disk can't stall the event loop other requests run on.
#   2. Callers MUST fire this via `asyncio.create_task(record_webhook_alert(...))`
#      and NOT await it - see each call site (Options/option_main.py,
#      Futures/futures_main.py, Options/paper_webhook.py). A fire-and-
#      forget task means the webhook handler's own coroutine returns
#      (and, for a "processed" alert, the real entry-order placement it
#      already triggered) without ever waiting on this write to finish.
#      append_jsonl itself also already can't raise (wrapped in try/
#      except), so even a failed write can't surface as an unhandled task
#      exception.
# --------------------------------------------------------------------- #
WEBHOOK_ALERTS_NAME = "webhook_alerts"


async def record_webhook_alert(
    strategy: str, scan_name: Optional[str], alert_name: Optional[str],
    stocks: list, status: str, reason: Optional[str] = None,
) -> None:
    """strategy: "Options"/"Futures"/"Options-PaperTrade" (matches the
    endpoint that received the alert, not necessarily "did this place a
    real order" - PaperTrade never does). status: "processed"/"ignored"/
    "no_action" (mirrors the same status values each handler already
    returns to Chartink). reason: the same reason string already being
    returned to the caller where applicable (e.g.
    "past_allowed_trading_time", "max_live_positions_reached") - not a
    new classification, just persisting what the handler already knows.

    Call this via `asyncio.create_task(record_webhook_alert(...))` and do
    NOT await the task - see this module's docstring above for why."""
    try:
        loop = asyncio.get_running_loop()
        record = {
            "strategy": strategy, "scan_name": scan_name, "alert_name": alert_name,
            "stocks": stocks, "status": status, "reason": reason,
            "logged_at": datetime.now().isoformat(),
        }
        await loop.run_in_executor(None, append_jsonl, WEBHOOK_ALERTS_NAME, record)
    except Exception:  # noqa: BLE001
        logger.exception("Could not record webhook alert for %s (scan=%s) - "
                          "the alert itself was already handled independently, "
                          "this is logging-only.", strategy, scan_name)


def read_all_webhook_alerts(strategy: Optional[str] = None) -> list[dict]:
    """Read-only, same filtering convention as read_all_trades."""
    alerts = read_all_jsonl(WEBHOOK_ALERTS_NAME)
    if strategy is not None:
        alerts = [a for a in alerts if a.get("strategy") == strategy]
    return alerts
