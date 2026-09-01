"""
Central configuration for the Luxury strategy.

New package (user request 31 Aug 2026): "Groww"/"Luxury" - a same-account
duplicate of the Options strategy (same ranking/ATM-buying/exit logic,
CE+PE webhooks like Options, not a real separate broker integration - the
user clarified this after being asked). Reuses the Options package's
single authenticated Dhan connection (see this package's own
dhan_client.py) - no DHAN_CLIENT_ID/DHAN_PIN/etc. auth vars here, those
only matter to Options/dhan_client.py's authenticate().

Built as a near-verbatim copy of Futures/config.py's own structure (itself
already proven as "Options standing on its own, separate pool/config"),
extended with the second (PE) webhook/leg Futures doesn't have - see
Luxury/trading_engine.py's and Luxury/luxury_main.py's own docstrings.

Only lists tunables this package's own trading_engine.py/position_store.py
actually read via their own `from . import config`. A few Supertrend
internals (period, multiplier) are deliberately NOT here even though
Options/config.py has them - those are consumed inside the *shared*
dhan_client.py (bound to Options/config.py, since that's the package that
owns the one Dhan connection), so redefining them here would be a config
surface that looks tunable but silently isn't. ENABLE_SUPERTREND_EXIT IS
read directly by this package's own trading_engine.py, so it's genuinely
independent per strategy.

Does NOT include choppy_stocks.py filtering - that feature was scoped to
Options only per the user's own explicit wording when it was requested
("avoid taking trades in these stocks Options from bot"), same as
Futures doesn't have it either. Ask if you want it extended here too.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Strategy parameters - entirely separate position pool/capacity from
# Options'/Futures', so a burst of alerts on any side can't crowd out the
# others' capacity. LUXURY_-prefixed env vars keep all three independently
# tunable in the same .env.
# ---------------------------------------------------------------------------
TOP_N_STOCKS = int(os.getenv("LUXURY_TOP_N_STOCKS", "4"))

# See Options/config.py's identical flag - this package's own independently-
# tunable bottom-N/top-N selection toggle.
SELECT_BOTTOM_N_STOCKS = os.getenv("LUXURY_SELECT_BOTTOM_N_STOCKS", "true").lower() == "true"

# Both CE and PE webhooks are real here (unlike Futures, which only exposes
# a bullish/CE endpoint) - matching Options' own MAX_LIVE_POSITIONS_CE/_PE
# split, defaulted to the same values Options currently runs with.
MAX_LIVE_POSITIONS_CE = int(os.getenv("LUXURY_MAX_LIVE_POSITIONS_CE", "2"))
MAX_LIVE_POSITIONS_PE = int(os.getenv("LUXURY_MAX_LIVE_POSITIONS_PE", "2"))

# See Options/config.py's identical MAX_DAILY_ENTRIES_PER_SYMBOL - this
# package's own independently-tunable daily re-entry cap (user request
# 1 Sep 2026), same "same underlying, across the whole day" semantics.
MAX_DAILY_ENTRIES_PER_SYMBOL = int(os.getenv("LUXURY_MAX_DAILY_ENTRIES_PER_SYMBOL", "3"))

# Code default kept at its ORIGINAL value, same convention as Options'
# own TARGET_PCT/STOP_LOSS_PCT (whose code default is STILL "0.10"/"0.03"
# even though the actual deployed value moved to 0.25/0.16 purely via
# .env override, never a code-default edit) - see .env's own
# LUXURY_TARGET_PCT/LUXURY_STOP_LOSS_PCT for the value this package
# actually runs with as of 1 Sep 2026 (matched to Options' own deployed
# value, user request: "Match luxury Entry and Exit conditions to
# Options package conditions"). This file's own comment used to claim
# this default was "the same values Options currently runs with" - true
# only at the moment Luxury was created, since Options' own .env value
# moved afterward and this code default was never updated to follow it.
TARGET_PCT = float(os.getenv("LUXURY_TARGET_PCT", "0.10"))
STOP_LOSS_PCT = float(os.getenv("LUXURY_STOP_LOSS_PCT", "0.03"))

# See Options/config.py's MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF/_AFTER_CUTOFF -
# identical rationale, this package's own independently-tunable pair,
# defaulted to the same values Options currently runs with.
MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF = float(os.getenv("LUXURY_MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF", "1200"))
MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF = float(os.getenv("LUXURY_MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF", "1000"))

# See Options/config.py's PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF/
# _AFTER_CUTOFF - identical rationale, this package's own independently-
# tunable pair.
PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF = float(os.getenv("LUXURY_PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF", "1500"))
PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF = float(os.getenv("LUXURY_PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF", "1000"))

# See Options/config.py's RISK_THRESHOLD_CUTOFF_TIME - this package's own
# independently-tunable cutoff (defaults to the same "11:30").
RISK_THRESHOLD_CUTOFF_TIME = os.getenv("LUXURY_RISK_THRESHOLD_CUTOFF_TIME", "11:30")

ENABLE_TRAILING_SL = os.getenv("LUXURY_ENABLE_TRAILING_SL", "false").lower() == "true"
TRAILING_SL_PCT = float(os.getenv("LUXURY_TRAILING_SL_PCT", "0.015"))

ENABLE_DYNAMIC_SL = os.getenv("LUXURY_ENABLE_DYNAMIC_SL", "true").lower() == "true"
DYNAMIC_SL_STEP_PCT_CE = float(os.getenv("LUXURY_DYNAMIC_SL_STEP_PCT_CE", "0.07"))
# Code default kept at its original 0.07 (see TARGET_PCT's own comment
# above for why) - .env's LUXURY_DYNAMIC_SL_STEP_PCT_PE carries the
# actual deployed value (0.09, matched to Options' own, 1 Sep 2026).
DYNAMIC_SL_STEP_PCT_PE = float(os.getenv("LUXURY_DYNAMIC_SL_STEP_PCT_PE", "0.07"))
DYNAMIC_SL_INCREASE_PCT = float(os.getenv("LUXURY_DYNAMIC_SL_INCREASE_PCT", "0.01"))

ENABLE_SUPERTREND_EXIT = os.getenv("LUXURY_ENABLE_SUPERTREND_EXIT", "true").lower() == "true"
# SUPERTREND_ENTRY_GRACE_MINUTES deliberately doesn't exist - see
# Options/config.py's identical removal note (user request 27 Aug 2026).
# The only remaining delay is trading_engine._supertrend_signal_for()
# never acting on the exact same candle a position was entered on.

# Fallback default only (used where Dhan's own reported option_type comes
# back missing/None, e.g. reconciliation/AMO-sync) - both real webhooks
# below pass their own explicit option_type, same as Options' identical
# constant.
OPTION_TYPE = os.getenv("LUXURY_OPTION_TYPE", "CE").upper()

QUANTITY_LOTS = int(os.getenv("LUXURY_QUANTITY_LOTS", "1"))

# See Options/config.py's identical OPTIONS_PRODUCT for the full rationale -
# "MARGIN" is Tradehull's code for NRML/carry-forward, not "NRML" itself.
OPTIONS_PRODUCT = os.getenv("LUXURY_OPTIONS_PRODUCT", "MARGIN")

MARKET_TZ = "Asia/Kolkata"
SQUARE_OFF_TIME = os.getenv("LUXURY_SQUARE_OFF_TIME", "15:15")

# See Options/config.py's identical flag - this package's own independently-
# tunable master switch for the automatic EOD square-off. Code default
# kept at its original "true" (see TARGET_PCT's own comment above for
# why) - .env's LUXURY_ENABLE_SQUARE_OFF carries the actual deployed
# value ("false", matched to Options'/Futures' own NRML-carry-forward
# behavior, 1 Sep 2026 - Luxury's default had drifted to force-closing
# everything at SQUARE_OFF_TIME daily, which Options/Futures don't do).
ENABLE_SQUARE_OFF = os.getenv("LUXURY_ENABLE_SQUARE_OFF", "true").lower() == "true"

# See Options/config.py's identical flags - this package's own independently-
# tunable Friday carve-out (applies regardless of ENABLE_SQUARE_OFF above).
ENABLE_FRIDAY_SQUARE_OFF = os.getenv("LUXURY_ENABLE_FRIDAY_SQUARE_OFF", "true").lower() == "true"
FRIDAY_SQUARE_OFF_TIME = os.getenv("LUXURY_FRIDAY_SQUARE_OFF_TIME", "15:20")

# See Options/config.py's identical flag - this package's own independently-
# tunable cutoff for NEW entries only.
ENABLE_TRADING_TIME_LIMIT = os.getenv("LUXURY_ENABLE_TRADING_TIME_LIMIT", "false").lower() == "true"
ALLOWED_TRADING_TIME = os.getenv("LUXURY_ALLOWED_TRADING_TIME", "11:30")

# See Options/config.py's MONITOR_INTERVAL_SECONDS - LTP_STALE_AFTER_SECONDS
# lives only in Options/config.py since it governs the one shared
# dhan_client LTP cache all packages read from - no separate Luxury copy
# needed.
MONITOR_INTERVAL_SECONDS = int(os.getenv("LUXURY_MONITOR_INTERVAL_SECONDS", "2"))

LOT_SIZE_FALLBACK = int(os.getenv("LUXURY_LOT_SIZE_FALLBACK", "1"))
ORDER_TAG_PREFIX = os.getenv("LUXURY_ORDER_TAG_PREFIX", "Lux")
