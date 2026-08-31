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
internals (period, multiplier) are deliberately NOT here even though
Options/config.py has them - those are consumed inside the *shared*
dhan_client.py (bound to Options/config.py, since that's the package that
owns the one Dhan connection), so redefining them here would be a config
surface that looks tunable but silently isn't. ENABLE_SUPERTREND_EXIT IS
read directly by this package's own trading_engine.py, so it's genuinely
independent per strategy. SUPERTREND_ENTRY_GRACE_MINUTES (this package's
own former copy) and SUPERTREND_MIN_WARMUP_CANDLES (Options' own copy,
consumed inside the shared dhan_client.py) were BOTH removed entirely -
user request 27 Aug 2026, immediate action on a reversal signal with no
tuned delay - see this file's own SUPERTREND_ENTRY_GRACE_MINUTES removal
note below and Options/config.py's identical one.
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
# CE raised 2->3 (user request 30 Aug 2026), matching Options'
# MAX_LIVE_POSITIONS_CE change made the same day. Lowered 3->2 (user
# request 31 Aug 2026), matching Options' CE 3->2 change made the same day.
# PE left at its prior default (2) - not part of that request, and unused
# today per the note above anyway.
MAX_LIVE_POSITIONS_CE = int(os.getenv("FUTURES_MAX_LIVE_POSITIONS_CE", "2"))
MAX_LIVE_POSITIONS_PE = int(os.getenv("FUTURES_MAX_LIVE_POSITIONS_PE", "2"))

TARGET_PCT = float(os.getenv("FUTURES_TARGET_PCT", "0.25"))
STOP_LOSS_PCT = float(os.getenv("FUTURES_STOP_LOSS_PCT", "0.16"))

# See Options/config.py's MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF/_AFTER_CUTOFF -
# identical rationale, this package's own independently-tunable pair. Split
# from a single flat value into before/after-cutoff the same way and same
# day (user request 31 Aug 2026).
MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF = float(os.getenv("FUTURES_MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF", "1200"))
MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF = float(os.getenv("FUTURES_MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF", "1000"))

# See Options/config.py's PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF/
# _AFTER_CUTOFF - identical rationale, this package's own independently-
# tunable pair.
PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF = float(os.getenv("FUTURES_PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF", "1500"))
PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF = float(os.getenv("FUTURES_PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF", "1000"))

# See Options/config.py's RISK_THRESHOLD_CUTOFF_TIME - this package's own
# independently-tunable cutoff (defaults to the same "11:30").
RISK_THRESHOLD_CUTOFF_TIME = os.getenv("FUTURES_RISK_THRESHOLD_CUTOFF_TIME", "11:30")

ENABLE_TRAILING_SL = os.getenv("FUTURES_ENABLE_TRAILING_SL", "false").lower() == "true"
TRAILING_SL_PCT = float(os.getenv("FUTURES_TRAILING_SL_PCT", "0.015"))

ENABLE_DYNAMIC_SL = os.getenv("FUTURES_ENABLE_DYNAMIC_SL", "true").lower() == "true"
DYNAMIC_SL_STEP_PCT_CE = float(os.getenv("FUTURES_DYNAMIC_SL_STEP_PCT_CE", "0.07"))
DYNAMIC_SL_STEP_PCT_PE = float(os.getenv("FUTURES_DYNAMIC_SL_STEP_PCT_PE", "0.09"))
DYNAMIC_SL_INCREASE_PCT = float(os.getenv("FUTURES_DYNAMIC_SL_INCREASE_PCT", "0.01"))

ENABLE_SUPERTREND_EXIT = os.getenv("FUTURES_ENABLE_SUPERTREND_EXIT", "true").lower() == "true"
# SUPERTREND_ENTRY_GRACE_MINUTES REMOVED entirely (user request 27 Aug
# 2026) - see Options/config.py's identical removal note. The only
# remaining delay is trading_engine._supertrend_signal_for() never acting
# on the exact same candle a position was entered on - everything past
# that candle triggers immediately, no extra grace window.

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

# Lowered 5->2 alongside Options' own value (user request 27 Aug 2026) - see
# Options/config.py's comment for the full rationale. LTP_STALE_AFTER_SECONDS
# lives only in Options/config.py since it governs the one shared dhan_client
# LTP cache both packages read from - no separate Futures copy needed.
MONITOR_INTERVAL_SECONDS = int(os.getenv("FUTURES_MONITOR_INTERVAL_SECONDS", "2"))

LOT_SIZE_FALLBACK = int(os.getenv("FUTURES_LOT_SIZE_FALLBACK", "1"))
ORDER_TAG_PREFIX = os.getenv("FUTURES_ORDER_TAG_PREFIX", "Fut")
