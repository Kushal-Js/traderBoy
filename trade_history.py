"""
Persistent, cross-strategy trade history - a real gap this closes: both
Options/position_store.py and Futures/position_store.py only ever kept
closed_positions_today/orders_today in memory, reset at the start of every
new trading day (PositionStore.maybe_reset_for_new_day) and wiped entirely
by a service restart. There was no way to tell "which package placed this
trade" or look at trade history from a prior day at all, once either of
those happened - confirmed by reading both stores' source directly (30-31
Aug 2026), not assumed.

This module is a pure logging addition - it is called AFTER a position is
already closed (from PositionStore.close_position, after the real exit
order has already been placed and confirmed), so it can never affect
whether/when/at-what-price a real order is placed. If the append itself
fails for any reason, it logs the error and returns - it must never raise
and break the caller's actual position-closing flow.

File format: one JSON object per line (JSONL), same pattern as K01's own
PAPER_LOG_PATH (K01/paper_engine.py's PaperTradeStore) - append-only, so a
day's or a restart's worth of trades never overwrites prior history.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("trade_history")

TRADE_HISTORY_PATH = Path(__file__).resolve().parent / "real_trade_history.log"


def record_closed_trade(strategy: str, pos) -> None:
    """strategy: "Options" or "Futures" (the only two real, order-placing
    packages as of 31 Aug 2026 - K01/CopperOptions/IndexScalping are paper-
    only and use their own separate paper-trade logs, not this one, since
    mixing real and paper trades in one file would be actively misleading
    for later analysis). pos: a closed Options.position_store.Position or
    Futures.position_store.Position - both have an identical field set for
    everything read here (confirmed by reading both dataclasses directly)."""
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
        with open(TRADE_HISTORY_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001
        logger.exception("Could not append to real_trade_history.log for %s %s - "
                          "trade itself is unaffected, this is logging-only.",
                          strategy, getattr(pos, "underlying_symbol", "?"))


def read_all_trades(strategy: Optional[str] = None) -> list[dict]:
    """Read-only. strategy=None returns everything; "Options"/"Futures"
    filters. Used by the /trade-history endpoint - never called from any
    order-placement code path."""
    trades: list[dict] = []
    try:
        with open(TRADE_HISTORY_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if strategy is None or rec.get("strategy") == strategy:
                    trades.append(rec)
    except FileNotFoundError:
        pass
    return trades
