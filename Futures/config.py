"""
Central configuration for the Futures strategy.

PLACEHOLDER, by explicit request: this currently buys ATM CE *options*
(identical mechanics to Options/) rather than actual futures contracts -
standing in until real futures-contract buying replaces it. See
NOTES.md's design-decision entry for why it exists this way and what's
still a TODO before it genuinely trades futures.

Reuses the Options package's single authenticated Dhan connection (see
this package's own dhan_client.py) - no DHAN_CLIENT_ID/DHAN_PIN/etc. auth
vars here, those only matter to Options/dhan_client.py's authenticate().

Only lists tunables this package's own trading_engine.py/position_store.py
actually read via their own `from . import config`. A few Supertrend
internals (period, multiplier, warmup-candle count) are deliberately
NOT here even though Options/config.py has them - those are consumed
inside the *shared* dhan_client.py (bound to Options/config.py, since
that's the package that owns the one Dhan connection), so redefining
them here would be a config surface that looks tunable but silently
isn't. ENABLE_SUPERTREND_EXIT and SUPERTREND_ENTRY_GRACE_MINUTES ARE
read directly by this package's own trading_engine.py, so they genuinely
are independent per strategy.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Strategy parameters - entirely separate position pool/capacity from
# Options', so a burst of alerts on either side can't crowd out the
# other's capacity. FUTURES_-prefixed env vars keep both independently
# tunable in the same .env.
# ---------------------------------------------------------------------------
TOP_N_STOCKS = int(os.getenv("FUTURES_TOP_N_STOCKS", "3"))

# See Options/config.py's identical flag - this package's own independently-
# tunable bottom-N/top-N selection toggle.
SELECT_BOTTOM_N_STOCKS = os.getenv("FUTURES_SELECT_BOTTOM_N_STOCKS", "true").lower() == "true"
# PE cap exists because position_store.py's capacity gate is generic per
# option_type - unused today since futures_main.py only exposes a bullish
# (CE) webhook, kept for parity if a bearish endpoint is added later.
MAX_LIVE_POSITIONS_CE = int(os.getenv("FUTURES_MAX_LIVE_POSITIONS_CE", "2"))
MAX_LIVE_POSITIONS_PE = int(os.getenv("FUTURES_MAX_LIVE_POSITIONS_PE", "2"))

TARGET_PCT = float(os.getenv("FUTURES_TARGET_PCT", "0.25"))
STOP_LOSS_PCT = float(os.getenv("FUTURES_STOP_LOSS_PCT", "0.16"))

# See Options/config.py's MAX_LOSS_PER_TRADE_RS - identical rationale, this
# package's own independently-tunable cap.
MAX_LOSS_PER_TRADE_RS = float(os.getenv("FUTURES_MAX_LOSS_PER_TRADE_RS", "1000"))

# See Options/config.py's MAX_LOSS_REENTRY_MULTIPLIER/CEILING_MULTIPLIER -
# identical rationale, this package's own independently-tunable values.
MAX_LOSS_REENTRY_MULTIPLIER = float(os.getenv("FUTURES_MAX_LOSS_REENTRY_MULTIPLIER", "1.75"))
MAX_LOSS_REENTRY_CEILING_MULTIPLIER = float(os.getenv("FUTURES_MAX_LOSS_REENTRY_CEILING_MULTIPLIER", "3.0"))

# See Options/config.py's PROFIT_PROTECTION_THRESHOLD_RS - identical
# rationale, this package's own independently-tunable threshold.
PROFIT_PROTECTION_THRESHOLD_RS = float(os.getenv("FUTURES_PROFIT_PROTECTION_THRESHOLD_RS", "1500"))

ENABLE_TRAILING_SL = os.getenv("FUTURES_ENABLE_TRAILING_SL", "false").lower() == "true"
TRAILING_SL_PCT = float(os.getenv("FUTURES_TRAILING_SL_PCT", "0.015"))

ENABLE_DYNAMIC_SL = os.getenv("FUTURES_ENABLE_DYNAMIC_SL", "true").lower() == "true"
DYNAMIC_SL_STEP_PCT_CE = float(os.getenv("FUTURES_DYNAMIC_SL_STEP_PCT_CE", "0.07"))
DYNAMIC_SL_STEP_PCT_PE = float(os.getenv("FUTURES_DYNAMIC_SL_STEP_PCT_PE", "0.09"))
DYNAMIC_SL_INCREASE_PCT = float(os.getenv("FUTURES_DYNAMIC_SL_INCREASE_PCT", "0.01"))

ENABLE_SUPERTREND_EXIT = os.getenv("FUTURES_ENABLE_SUPERTREND_EXIT", "true").lower() == "true"
# Moved from 5 to 1 minute by user request 26 Aug 2026, alongside
# Options/config.py's SUPERTREND_INTERVAL_MINUTES also moving to 1 (that
# value is shared/not duplicated here - see this file's own module
# docstring - so it applies to this package's Supertrend reads too).
SUPERTREND_ENTRY_GRACE_MINUTES = int(os.getenv("FUTURES_SUPERTREND_ENTRY_GRACE_MINUTES", "1"))

# Default ATM leg for /chartink/webhook-futures - bullish/CE only for now,
# matching Options' /chartink/webhook convention (no bearish endpoint was
# requested for this package).
OPTION_TYPE = "CE"

QUANTITY_LOTS = int(os.getenv("FUTURES_QUANTITY_LOTS", "1"))

# See Options/config.py's identical OPTIONS_PRODUCT for the full rationale -
# "MARGIN" is Tradehull's code for NRML/carry-forward, not "NRML" itself.
OPTIONS_PRODUCT = os.getenv("FUTURES_OPTIONS_PRODUCT", "MIS")  # rename to reflect futures once real contracts replace the placeholder

MARKET_TZ = "Asia/Kolkata"
SQUARE_OFF_TIME = os.getenv("FUTURES_SQUARE_OFF_TIME", "15:15")

# See Options/config.py's identical flag - this package's own independently-
# tunable master switch for the automatic EOD square-off.
ENABLE_SQUARE_OFF = os.getenv("FUTURES_ENABLE_SQUARE_OFF", "true").lower() == "true"

# See Options/config.py's identical flags - this package's own independently-
# tunable Friday carve-out (applies regardless of ENABLE_SQUARE_OFF above).
ENABLE_FRIDAY_SQUARE_OFF = os.getenv("FUTURES_ENABLE_FRIDAY_SQUARE_OFF", "true").lower() == "true"
FRIDAY_SQUARE_OFF_TIME = os.getenv("FUTURES_FRIDAY_SQUARE_OFF_TIME", "15:20")

# See Options/config.py's identical flag - this package's own independently-
# tunable cutoff for NEW entries only.
ENABLE_TRADING_TIME_LIMIT = os.getenv("FUTURES_ENABLE_TRADING_TIME_LIMIT", "false").lower() == "true"
ALLOWED_TRADING_TIME = os.getenv("FUTURES_ALLOWED_TRADING_TIME", "11:30")

MONITOR_INTERVAL_SECONDS = int(os.getenv("FUTURES_MONITOR_INTERVAL_SECONDS", "5"))

LOT_SIZE_FALLBACK = int(os.getenv("FUTURES_LOT_SIZE_FALLBACK", "1"))
ORDER_TAG_PREFIX = os.getenv("FUTURES_ORDER_TAG_PREFIX", "Fut")
