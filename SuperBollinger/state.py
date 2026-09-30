"""
Super Bollinger's own state, kept apart from the deployed Bollinger
strategy's (user rule: a new strategy gets its own book and logs, never
mixed with another's results):
  - position_store: REAL positions/orders, capacity key "SuperBollinger",
    trade-history tag "SuperBollinger" (history/*_real_trades.log,
    *_position_opened.log - restart reconciliation reads that tag).
  - paper_book: PAPER positions (data/super_bollinger_paper_positions.json)
    and closed paper trades (history/<date>_super_bollinger_paper_trades.log).
Kept in its own tiny module so Bollinger/trading_engine.py can import it
lazily (for its one-real-position-per-stock guard) without a cycle.
"""
from __future__ import annotations

from Bollinger import config as bcfg
from Bollinger.paper_book import PaperBook
from Bollinger.position_store import BollingerPositionStore

STRATEGY = "SuperBollinger"
EVENTS_LOG = "super_bollinger_events"
PAPER_TRADES_LOG = "super_bollinger_paper_trades"

position_store = BollingerPositionStore(STRATEGY, entry_retry_cooldown_seconds=bcfg.ENTRY_RETRY_COOLDOWN_SECONDS)
paper_book = PaperBook("data/super_bollinger_paper_positions.json", PAPER_TRADES_LOG, {
    "strategy": STRATEGY, "entry_mode": "resting", "sides": "long",
    "exit_mode": "max_loss + breakeven stop + daily square-off"})

# ---- Supervisor (SuperBollinger/supervisor.py, 30 Sep 2026) ----
# Hedges are kept apart from the CE book: their own store (trade-history tag
# "SuperBollingerHedge", so restart reconciliation attributes real PE hedges
# here and never to the CE book) and their own paper book / log.
HEDGE_STRATEGY = "SuperBollingerHedge"
SUPERVISOR_LOG = "super_bollinger_supervisor"
hedge_store = BollingerPositionStore(HEDGE_STRATEGY, entry_retry_cooldown_seconds=0)
hedge_paper_book = PaperBook("data/super_bollinger_hedge_paper_positions.json", "super_bollinger_hedge_paper_trades", {
    "strategy": HEDGE_STRATEGY, "entry_mode": "hedge", "sides": "PE", "exit_mode": "trail/stop/square-off"})
# Disaster brake: set by the supervisor for the rest of the day (date it was hit).
halted = {"day": None, "reason": None}
