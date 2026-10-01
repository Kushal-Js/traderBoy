"""
Unified Momentum's own state, kept apart from every other strategy's (user rule: a new strategy gets its own
book and logs, never mixed with another's results). Three books, each with its own trade-history tag so a
restart attributes every broker position to the right one:
  - position_store / paper_book: engine A, PULLBACK CALLS (Super Bollinger's rules) - tag "UnifiedMomentum",
    capacity key "UnifiedMomentum" (data/unified_momentum_paper_positions.json, history/<date>_unified_momentum_
    paper_trades.log).
  - hedge_store / hedge_paper_book: the supervisor's PUT hedges on engine A's calls - tag "UnifiedMomentumHedge".
  - put_store / put_paper_book: engine B, MOMENTUM PUTS (SwingMomentum's signal) - tag "UnifiedMomentumPut".
Kept in its own tiny module so other strategies can import it lazily (their one-real-position-per-stock and
broker-stop sweep guards) without an import cycle.
"""
from __future__ import annotations

from Bollinger import config as bcfg
from Bollinger.paper_book import PaperBook
from Bollinger.position_store import BollingerPositionStore

STRATEGY = "UnifiedMomentum"
EVENTS_LOG = "unified_momentum_events"
PAPER_TRADES_LOG = "unified_momentum_paper_trades"

position_store = BollingerPositionStore(STRATEGY, entry_retry_cooldown_seconds=bcfg.ENTRY_RETRY_COOLDOWN_SECONDS)
paper_book = PaperBook("data/unified_momentum_paper_positions.json", PAPER_TRADES_LOG, {
    "strategy": STRATEGY, "engine": "A pullback calls", "entry_mode": "resting", "sides": "long",
    "exit_mode": "max_loss + breakeven stop + daily square-off"})

# ---- Supervisor hedges on engine A's calls ----
HEDGE_STRATEGY = "UnifiedMomentumHedge"
SUPERVISOR_LOG = "unified_momentum_supervisor"
hedge_store = BollingerPositionStore(HEDGE_STRATEGY, entry_retry_cooldown_seconds=0)
hedge_paper_book = PaperBook("data/unified_momentum_hedge_paper_positions.json", "unified_momentum_hedge_paper_trades", {
    "strategy": HEDGE_STRATEGY, "entry_mode": "hedge", "sides": "PE", "exit_mode": "trail/stop/square-off"})

# ---- Engine B: momentum PUTs ----
PUT_STRATEGY = "UnifiedMomentumPut"


def _b_slots() -> int:
    """Engine B's slot limit is its own setting (b_max_concurrent_trades), not a capacity_control entry."""
    from . import settings
    return settings.get("b_max_concurrent_trades")


put_store = BollingerPositionStore(PUT_STRATEGY, entry_retry_cooldown_seconds=bcfg.ENTRY_RETRY_COOLDOWN_SECONDS,
                                   max_trades=_b_slots)
put_paper_book = PaperBook("data/unified_momentum_put_paper_positions.json", "unified_momentum_put_paper_trades", {
    "strategy": PUT_STRATEGY, "engine": "B momentum puts", "entry_mode": "supertrend_cross_momentum", "sides": "PE",
    "exit_mode": "max_loss / profit protection / target / hard stop / supertrend reversal / daily square-off"})

# Disaster brake: set by the supervisor for the rest of the day (date it was hit).
halted = {"day": None, "reason": None}
