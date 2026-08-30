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


def record_closed_trade(strategy: str, pos) -> None:
    """strategy: "Options" or "Futures". pos: a closed
    Options.position_store.Position or Futures.position_store.Position -
    both have an identical field set for everything read here (confirmed
    by reading both dataclasses directly)."""
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
        append_jsonl(REAL_TRADES_NAME, record)
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
