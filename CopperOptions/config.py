"""
Tunables for the Copper (MCX) options-buying strategy - paper trading
only, see paper_engine.py's safety invariant.

ASSUMPTIONS MADE EXPLICIT (the rules as given left these underspecified -
flagged here, and in the deploy message, for correction if wrong):
  - "ATM + 20 points" is read as "20 points more OTM than ATM" for BOTH
    legs, matching the stated goal ("so that option contract is little
    cheaper") for both - i.e. CE uses ATM_strike + STRIKE_OFFSET_POINTS,
    PE uses ATM_strike - STRIKE_OFFSET_POINTS. A literal "+20" on both
    would make the CE cheaper (more OTM) but the PE *more expensive*
    (more ITM), contradicting the stated goal for PE.
  - "Today's RSI" / "yesterday's RSI" and "today's open" / "yesterday's
    close" are read as DAILY-timeframe values on the underlying Copper
    FUTURES contract (there's no continuously-quoted "spot" for MCX
    commodities via this API, only futures) - RSI(14), Wilder smoothing,
    computed on daily closes including today's still-forming daily bar.
  - "5 min close crossed above/below" is read as a plain state check
    (close > / < the Supertrend line right now) rather than requiring
    edge-detection against the previous bar - algebraically equivalent
    for a poll loop that re-evaluates every cycle.
  - MCX metals-segment close time isn't available via this API directly;
    23:30 IST is used (matches the 23:30:00 time component seen on every
    Copper contract's own SEM_EXPIRY_DATE in Dhan's instrument master -
    a reasonable proxy, but worth confirming against Dhan's own published
    session times if this ever needs to be precise).
"""
import os

STRATEGY_ENABLED = os.getenv("COPPER_STRATEGY_ENABLED", "true").lower() == "true"
PAPER_TRADING_ONLY = True  # see CopperOptions/paper_engine.py - hard safety invariant, not just a label

UNDERLYING = "COPPER"

STRIKE_OFFSET_POINTS = float(os.getenv("COPPER_STRIKE_OFFSET_POINTS", "20"))
RSI_PERIOD = int(os.getenv("COPPER_RSI_PERIOD", "14"))

# Two Supertrends must BOTH agree for entry; only ST1 (12,3) is checked
# for the exit, per the rules as given.
SUPERTREND_1_PERIOD = int(os.getenv("COPPER_ST1_PERIOD", "12"))
SUPERTREND_1_MULTIPLIER = float(os.getenv("COPPER_ST1_MULTIPLIER", "3"))
SUPERTREND_2_PERIOD = int(os.getenv("COPPER_ST2_PERIOD", "11"))
SUPERTREND_2_MULTIPLIER = float(os.getenv("COPPER_ST2_MULTIPLIER", "2"))
SUPERTREND_INTERVAL_MINUTES = 5

MAX_LOSS_RS = float(os.getenv("COPPER_MAX_LOSS_RS", "5000.0"))

STRATEGY_START_TIME = os.getenv("COPPER_STRATEGY_START_TIME", "15:31")
MARKET_CLOSE_TIME = os.getenv("COPPER_MARKET_CLOSE_TIME", "23:30")

# Roll to the next monthly option cycle if the nearest one has fewer than
# this many calendar days left - avoids trading a same-day/near-expiry
# (extreme gamma/theta) contract just because it happens to be "nearest."
MIN_DAYS_TO_EXPIRY = int(os.getenv("COPPER_MIN_DAYS_TO_EXPIRY", "3"))

QUANTITY_LOTS = int(os.getenv("COPPER_QUANTITY_LOTS", "1"))

POLL_INTERVAL_SECONDS = int(os.getenv("COPPER_POLL_INTERVAL_SECONDS", "15"))

# PAPER_LOG_PATH removed 31 Aug 2026 - see K01/config.py's identical note.
# Paper trades now go through trade_history.py's shared dated history/
# convention (PaperTradeStore's PAPER_LOG_NAME="copper_paper_trades").
