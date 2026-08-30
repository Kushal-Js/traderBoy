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
