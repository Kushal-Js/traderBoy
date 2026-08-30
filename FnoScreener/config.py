"""
Tunables for the daily F&O screener strategy - PAPER TRADING ONLY, see
paper_engine.py's safety invariant.

Full design/rationale lives in trading-skills/designs/fno-daily-screener.md
(github.com/Kushal-Js/trading-skills) - this file implements the MVP scope
shipped 30 Aug 2026 for the first live test: Stage 0 (Minervini Trend
Template, hard gate) + Stage 1 (liquidity/anti-thin-option floor, hard
gate) + Stage 3 (intraday momentum: RSI band + Supertrend regime/crossover
+ ROC sign, all three must align for an entry). Stage 2 (OI-buildup
gating, via Dhan's Option Chain API) and VCP detection (Stage 0's scored
bonus) are DELIBERATELY NOT implemented yet - both need their own careful
build+verify pass before being load-bearing on a live test. See
trading-skills for the full phase-2 plan.

MVP simplification vs. the design doc: no OI-buildup gate means entries
here are momentum-only (Stage 0+1 filter the universe once daily, Stage 3
decides intraday timing) - the design's "gate requires OI + momentum
agreement" becomes just "momentum agrees with itself" (RSI band + Supertrend
regime + Supertrend crossover + ROC sign, all four already required to
agree in this file's Stage 3 implementation) until Stage 2 is added.
"""
import os

PAPER_TRADING_ONLY = True  # see FnoScreener/paper_engine.py - hard safety invariant, not just a label

# --------------------------------------------------------------------- #
# Stage 0 - Minervini Trend Template (hard gate, daily timeframe)
# See trading-skills/learnings/technical-patterns/minervini-trend-template.md
# --------------------------------------------------------------------- #
TREND_TEMPLATE_LOOKBACK_DAYS = int(os.getenv("FNO_TREND_LOOKBACK_DAYS", "300"))  # >252 trading days incl. weekends/holidays padding
MA_SHORT = 50
MA_MID = 150
MA_LONG = 200
PCT_ABOVE_52W_LOW_MIN = float(os.getenv("FNO_PCT_ABOVE_52W_LOW_MIN", "30.0"))   # close >= 30% above 52w low
PCT_WITHIN_52W_HIGH_MAX = float(os.getenv("FNO_PCT_WITHIN_52W_HIGH_MAX", "25.0"))  # close within 25% of 52w high
MA_LONG_RISING_LOOKBACK_DAYS = int(os.getenv("FNO_MA_RISING_LOOKBACK_DAYS", "21"))  # ~1 trading month

# --------------------------------------------------------------------- #
# Stage 1 - liquidity/volatility floor (hard gate)
# See trading-skills/learnings/exit-mechanics.md's SAGILITY finding
# --------------------------------------------------------------------- #
ATR_PERIOD = 14
ATR_PCT_MIN = float(os.getenv("FNO_ATR_PCT_MIN", "1.0"))            # ATR(14)/price >= 1.0%
AVG_TURNOVER_LOOKBACK_DAYS = 20
MIN_AVG_TURNOVER_CR = float(os.getenv("FNO_MIN_AVG_TURNOVER_CR", "50.0"))  # 20-session avg turnover >= Rs.50cr
ANTI_SAGILITY_MAX_PREMIUM_RS = float(os.getenv("FNO_ANTI_SAGILITY_MAX_PREMIUM_RS", "5.0"))
ANTI_SAGILITY_MIN_LOT_SIZE = int(os.getenv("FNO_ANTI_SAGILITY_MIN_LOT_SIZE", "5000"))
# Reject only the COMBINATION: ATM premium < ANTI_SAGILITY_MAX_PREMIUM_RS
# AND lot_size >= ANTI_SAGILITY_MIN_LOT_SIZE - either alone is fine.

# --------------------------------------------------------------------- #
# Stage 3 - intraday momentum (all of RSI band + Supertrend regime +
# Supertrend 1-min crossover + ROC sign must agree for an entry signal)
# --------------------------------------------------------------------- #
RSI_PERIOD = int(os.getenv("FNO_RSI_PERIOD", "14"))
RSI_BULLISH_MIN = float(os.getenv("FNO_RSI_BULLISH_MIN", "40.0"))
RSI_BULLISH_MAX = float(os.getenv("FNO_RSI_BULLISH_MAX", "75.0"))
RSI_BEARISH_MIN = float(os.getenv("FNO_RSI_BEARISH_MIN", "25.0"))
RSI_BEARISH_MAX = float(os.getenv("FNO_RSI_BEARISH_MAX", "60.0"))

# Deliberately the BOT'S OWN exit-side Supertrend parameters
# (Options/config.py's SUPERTREND_PERIOD/MULTIPLIER), not Krishvi's
# period-7 - see trading-skills/learnings/screener-analysis/krishvi.md.
SUPERTREND_5MIN_PERIOD = int(os.getenv("FNO_SUPERTREND_5MIN_PERIOD", "10"))
SUPERTREND_5MIN_MULTIPLIER = float(os.getenv("FNO_SUPERTREND_5MIN_MULTIPLIER", "3.0"))
SUPERTREND_1MIN_PERIOD = int(os.getenv("FNO_SUPERTREND_1MIN_PERIOD", "10"))
SUPERTREND_1MIN_MULTIPLIER = float(os.getenv("FNO_SUPERTREND_1MIN_MULTIPLIER", "3.0"))

ROC_PERIOD = int(os.getenv("FNO_ROC_PERIOD", "9"))  # matches Krishvi's own ROC period (coincidental validation, see krishvi.md)

# --------------------------------------------------------------------- #
# Paper-trade sizing / exits - deliberately matching the live Options
# strategy's own values for consistency (Options/config.py), not
# independently invented.
# --------------------------------------------------------------------- #
TARGET_PCT = float(os.getenv("FNO_TARGET_PCT", "0.25"))
STOP_LOSS_PCT = float(os.getenv("FNO_STOP_LOSS_PCT", "0.16"))
MAX_LOSS_PER_TRADE_RS = float(os.getenv("FNO_MAX_LOSS_PER_TRADE_RS", "1200.0"))
QUANTITY_LOTS = int(os.getenv("FNO_QUANTITY_LOTS", "1"))

# Shortlist/capacity - deliberately smaller than the design doc's "top 10
# each" for the paper-engine's ACTUAL concurrent execution (the full daily
# shortlist is still logged/exposed in full via the status endpoint; this
# caps how many of them the paper engine will simultaneously hold a
# position in, same spirit as the live bot's MAX_LIVE_POSITIONS_CE/PE).
MAX_CONCURRENT_CE = int(os.getenv("FNO_MAX_CONCURRENT_CE", "4"))
MAX_CONCURRENT_PE = int(os.getenv("FNO_MAX_CONCURRENT_PE", "4"))
DAILY_SHORTLIST_SIZE = int(os.getenv("FNO_DAILY_SHORTLIST_SIZE", "10"))  # per direction

# --------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------- #
MARKET_OPEN = "09:15"
DAILY_SCREEN_TIME = os.getenv("FNO_DAILY_SCREEN_TIME", "10:15")  # Stage 0+1 run once, frozen for the day, at/after this time
SQUARE_OFF_TIME = os.getenv("FNO_SQUARE_OFF_TIME", "15:15")
POLL_INTERVAL_SECONDS = int(os.getenv("FNO_POLL_INTERVAL_SECONDS", "15"))  # intraday momentum re-check cadence for shortlisted stocks

# --------------------------------------------------------------------- #
# Rate-limit pacing (NOTES.md bug #5 in this repo - Dhan's undocumented
# market-data REST rate limit) - a small fixed delay between per-stock
# REST calls during the ~208-stock daily universe scan, not a naive
# tight loop.
# --------------------------------------------------------------------- #
UNIVERSE_SCAN_DELAY_SECONDS = float(os.getenv("FNO_UNIVERSE_SCAN_DELAY_SECONDS", "0.3"))

PAPER_LOG_PATH = os.getenv("FNO_PAPER_LOG_PATH", "fno_screener_paper_trades.log")
