"""
Tunables for the index scalping strategy - paper trading only, see
paper_engine.py's safety invariant.

Rules implemented (user request, 26 Aug 2026 - replaces the original
opening-range-breakout + EMA-momentum signal entirely; see NOTES.md's
index-scalping design-decision entry for the full history):

  CE entry: today's daily open > yesterday's daily close AND today's
            daily RSI(RSI_PERIOD) > yesterday's daily RSI [both on the
            index spot, NIFTY/BANKNIFTY] AND the index's 5-min close is
            above its own 5-min Supertrend(SUPERTREND_5MIN_PERIOD,
            SUPERTREND_5MIN_MULTIPLIER) AND the index's 1-min close just
            crossed ABOVE its own 1-min
            Supertrend(SUPERTREND_1MIN_PERIOD, SUPERTREND_1MIN_MULTIPLIER)
            - a genuine edge-detected crossover against the prior
            confirmed 1-min bar, not a plain state check (see
            ASSUMPTIONS below for why this one condition gets
            edge-detection and the 5-min one doesn't).
  PE entry: the exact mirror (open <, RSI <, 5-min close below its own
            Supertrend, 1-min close crossed BELOW its own Supertrend).
  Exit (either side): the index's 1-min close crosses back the other way
            through its own 1-min Supertrend, or the paper position's
            unrealized loss exceeds MAX_LOSS_RS - whichever comes first.
            Also force-closed at SQUARE_OFF_TIME regardless.

ASSUMPTIONS MADE EXPLICIT (same practice as CopperOptions/config.py's
docstring, for the same reason - flagged here for correction if wrong):
  - "Today's"/"yesterday's" open, close, RSI are DAILY-timeframe values
    on the index SPOT itself (NIFTY/BANKNIFTY, security IDs 13/25,
    segment IDX_I - the same segment the original index-scalping signal
    already fetched 1-min candles from successfully). RSI is computed on
    daily closes including today's still-forming daily close (i.e.
    today's index price-so-far) - same interpretation already used for
    CopperOptions's identical rule wording. The daily gate is computed
    once per day and frozen (recomputed only the first successful poll
    each day, same as CopperOptions) - since this strategy starts
    evaluating right at MARKET_OPEN, that first poll's "price-so-far" is
    very close to the actual day's open, so this doesn't distort the
    intended "today's open"/"today's RSI at the open" reading much.
  - The 5-min condition ("5 min close is greater/lesser than 5 min
    supertrend") is read as a plain current-state check, matching
    CopperOptions's precedent for its analogous rule. The 1-min
    condition is explicitly worded "crossed above/below" - different
    wording from the 5-min rule - so unlike Copper's blanket
    state-check interpretation, this one gets genuine edge-detection:
    it only fires on the bar where the close was on the wrong side of
    the 1-min Supertrend the PRIOR confirmed bar and is on the right
    side on THIS confirmed bar. The daily gate and the 5-min check are
    the slower-moving "regime" filters; the 1-min crossover is the
    precise entry/exit timing trigger.
  - Both Supertrends and the crossover check only ever look at
    COMPLETED candles - the current still-forming bar (this poll cycle
    landed before that bar's own close time) is dropped before computing
    anything, same reasoning as Options/dhan_client.py's
    refresh_supertrend_signal - otherwise a crossover could flicker
    true/false multiple times within one still-forming minute as new
    ticks arrive.
"""
import os

PAPER_TRADING_ONLY = True  # see IndexScalping/paper_engine.py - hard safety invariant, not just a label

INDEX_SECURITY_ID = {"NIFTY": "13", "BANKNIFTY": "25"}  # NSE index (spot) segment IDX_I

RSI_PERIOD = int(os.getenv("SCALP_RSI_PERIOD", "14"))

SUPERTREND_5MIN_PERIOD = int(os.getenv("SCALP_SUPERTREND_5MIN_PERIOD", "10"))
SUPERTREND_5MIN_MULTIPLIER = float(os.getenv("SCALP_SUPERTREND_5MIN_MULTIPLIER", "3.0"))
SUPERTREND_1MIN_PERIOD = int(os.getenv("SCALP_SUPERTREND_1MIN_PERIOD", "10"))
SUPERTREND_1MIN_MULTIPLIER = float(os.getenv("SCALP_SUPERTREND_1MIN_MULTIPLIER", "3.0"))

MAX_LOSS_RS = float(os.getenv("SCALP_MAX_LOSS_RS", "1000.0"))

# Cost modeling carried over from the original strategy - transaction
# costs dominate scalping economics far more than the options bot's
# minutes-to-hours holds, so gross vs. net P&L is tracked separately.
# Flat estimate for brokerage + statutory charges (STT, exchange fees,
# GST, stamp duty) per completed round-trip trade, plus a slippage
# haircut applied to both entry and exit fills as a stand-in for
# bid-ask spread (no L2/order-book data available via this API).
ROUND_TRIP_COST_RS = float(os.getenv("SCALP_ROUND_TRIP_COST_RS", "40.0"))
SLIPPAGE_PCT = float(os.getenv("SCALP_SLIPPAGE_PCT", "0.005"))

QUANTITY_LOTS = int(os.getenv("SCALP_QUANTITY_LOTS", "1"))

MARKET_OPEN = "09:15"
SQUARE_OFF_TIME = os.getenv("SCALP_SQUARE_OFF_TIME", "15:15")

# How often the paper-trading poll loop re-checks index candles / open
# paper-position LTPs. Not tick-driven (see index_main.py's docstring for
# why) - 15s is fast enough to meaningfully test the strategy's signal
# logic without hammering Dhan's rate limits (bug #5 in NOTES.md).
POLL_INTERVAL_SECONDS = int(os.getenv("SCALP_POLL_INTERVAL_SECONDS", "15"))

# PAPER_LOG_PATH removed 31 Aug 2026 - see K01/config.py's identical note.
# Paper trades now go through trade_history.py's shared dated history/
# convention (PaperTradeStore's PAPER_LOG_NAME="index_scalping_paper_trades" -
# renamed from the old generic "paper_trades.log" for clarity now that
# history/ holds multiple strategies' files side by side).
