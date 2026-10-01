"""
Scalper's own state, kept apart from every other strategy's (user rule: a new strategy gets its own book
and logs):
  - position_store: REAL positions/orders, trade-history tag "Scalper" (history/*_real_trades.log,
    *_position_opened.log - restart reconciliation reads that tag).
  - paper_book: PAPER positions (data/scalper_paper_positions.json) and closed paper trades
    (history/<date>_scalper_paper_trades.log).
  - memory: each REAL position's best price and entry candle (data/scalper_position_memory.json), so a
    restart keeps profit protection and the Supertrend-exit guard.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from Bollinger.paper_book import PaperBook
from Bollinger.position_store import BollingerPositionStore

logger = logging.getLogger("scalper_state")

STRATEGY = "Scalper"
EVENTS_LOG = "scalper_events"
PAPER_TRADES_LOG = "scalper_paper_trades"

position_store = BollingerPositionStore(STRATEGY, entry_retry_cooldown_seconds=60)
paper_book = PaperBook("data/scalper_paper_positions.json", PAPER_TRADES_LOG, {
    "strategy": STRATEGY, "entry_mode": "1-min bar close", "sides": "both",
    "exit_mode": "Swing ladder: max loss / target / profit protection / hard stop / Supertrend, 15:25"})

MEMORY_FILE = Path("data/scalper_position_memory.json")


def memory_load() -> dict:
    try:
        return json.loads(MEMORY_FILE.read_text()) if MEMORY_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        logger.exception("could not read %s", MEMORY_FILE)
        return {}


def memory_save(rows: dict) -> None:
    try:
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MEMORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=1, default=str))
        os.replace(tmp, MEMORY_FILE)
    except Exception:  # noqa: BLE001
        logger.exception("could not write %s", MEMORY_FILE)
