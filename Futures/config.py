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

# See Options/config.py's identical MAX_DAILY_ENTRIES_PER_SYMBOL - this
# package's own independently-tunable daily re-entry cap (user request
# 1 Sep 2026), same "same underlying, across the whole day" semantics.
MAX_DAILY_ENTRIES_PER_SYMBOL = int(os.getenv("FUTURES_MAX_DAILY_ENTRIES_PER_SYMBOL", "3"))

# ---------------------------------------------------------------------------
# Guard rails - ported verbatim from Options/config.py (10 Sep 2026, user
# request: "update Futures strategy same as Options"). See Options' own
# comments for the full rationale behind each; these are this package's own
# independently-tunable copies (FUTURES_-prefixed env).
# ---------------------------------------------------------------------------
# Same-day RSI-gated loss re-entry block - see Options/config.py's own
# "Same-day RSI-gated loss re-entry block" comment (11 Sep 2026) for the
# full rationale; this package's own independently-tunable on/off switch.
# The RSI computation itself (period/interval/overbought threshold) is
# shared - always reads Options.config, same as SUPERTREND_PERIOD does.
ENABLE_RSI_LOSS_REENTRY_BLOCK = os.getenv("FUTURES_ENABLE_RSI_LOSS_REENTRY_BLOCK", "true").lower() == "true"

# Repeat-loss same-day block: once a symbol has closed on MAX_LOSS_HIT/
# STOP_LOSS_HIT this many times today, it's blocked for the rest of the day.
LOSS_REPEAT_BLOCK_ENABLED = os.getenv("FUTURES_LOSS_REPEAT_BLOCK_ENABLED", "true").lower() == "true"
LOSS_REPEAT_BLOCK_COUNT = int(os.getenv("FUTURES_LOSS_REPEAT_BLOCK_COUNT", "2"))
LOSS_REPEAT_BLOCK_EXIT_REASONS = ("MAX_LOSS_HIT", "STOP_LOSS_HIT")

# Real broker-side SELL STOP-LOSS LIMIT (SL-L) order placed after every
# entry - an additional faster backstop on top of the poll/tick MAX_LOSS
# check, never a replacement. Kept OFF by default (same rollout discipline
# as Options: earn trust via a controlled live test before it runs against
# a real production entry).
BROKER_STOP_LOSS_ENABLED = os.getenv("FUTURES_BROKER_STOP_LOSS_ENABLED", "false").lower() == "true"

# RETIRED 12 Sep 2026 - see Options/config.py's identical flag for the
# full story (replaced by BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE below,
# user feedback: "this has to be in sync with MAX_LOSS_HIT"). Left
# defined (unused) rather than deleted.
BROKER_STOP_LOSS_LIMIT_BUFFER_PCT = float(os.getenv("FUTURES_BROKER_STOP_LOSS_LIMIT_BUFFER_PCT", "0.03"))

# Real broker-side SL-L limit gap, IN RUPEES, computed per-trade from the
# same current_max_loss_per_trade_rs() used for trigger_price - see
# Options/config.py's identical flag for the full mechanics/rationale.
# Deployed at 0.05 (lowered from 1.0 same day, user feedback: "contained
# within max 200/300 rupees") - Rs 225/105 extra tolerance before/after
# 11:30 at the current 4500/2100 caps, independent of quantity.
BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE = float(os.getenv("FUTURES_BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE", "1.0"))

# Liquidity guard: on/off gate for whether this package's own
# _exit_reason_for acts on the shared dhan_client illiquidity signal
# (the numeric params LIQUIDITY_GUARD_ZERO_VOLUME_BARS/_REFRESH_SECONDS
# are shared and live in Options/config.py).
LIQUIDITY_GUARD_ENABLED = os.getenv("FUTURES_LIQUIDITY_GUARD_ENABLED", "true").lower() == "true"

# Profit-protection give-back buffer (default 0.0 = bit-identical to the
# original zero-tolerance behaviour; net-negative in backtest, left off).
PROFIT_PROTECTION_GIVEBACK_PCT = float(os.getenv("FUTURES_PROFIT_PROTECTION_GIVEBACK_PCT", "0.0"))

# Only in a docstring in the shared engine copy, but define it so a
# config.LTP_STALE_AFTER_SECONDS reference can never AttributeError.
LTP_STALE_AFTER_SECONDS = float(os.getenv("FUTURES_LTP_STALE_AFTER_SECONDS", "5"))

TARGET_PCT = float(os.getenv("FUTURES_TARGET_PCT", "0.25"))
STOP_LOSS_PCT = float(os.getenv("FUTURES_STOP_LOSS_PCT", "0.16"))

# See Options/config.py's ENABLE_TARGET_EXIT - master switch for the fixed
# +TARGET_PCT profit exit in _exit_reason_for(). This package's own
# independently-tunable copy. Code default kept "true" for parity; the
# deployed FUTURES_ENABLE_TARGET_EXIT=false turns it OFF (user request
# 10 Sep 2026: "disable TARGET_HIT for Futures, rest to remain same") so a
# winning futures position rides on to PROFIT_PROTECTION_HIT / trailing SL /
# SUPERTREND / EOD instead of being capped at entry * 1.25.
ENABLE_TARGET_EXIT = os.getenv("FUTURES_ENABLE_TARGET_EXIT", "true").lower() == "true"

# See Options/config.py's MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF/_AFTER_CUTOFF -
# identical rationale, this package's own independently-tunable pair. Split
# from a single flat value into before/after-cutoff the same way and same
# day (user request 31 Aug 2026).
MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF = float(os.getenv("FUTURES_MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF", "1200"))
MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF = float(os.getenv("FUTURES_MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF", "1000"))

# See Options/config.py's ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF comment (11
# Sep 2026) for the full rationale - this package's own independently-
# tunable switch. Default False = MAX_LOSS_HIT never fires before
# RISK_THRESHOLD_CUTOFF_TIME; every other exit is unaffected.
ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF = os.getenv("FUTURES_ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF", "false").lower() == "true"

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

# EMA-cross exit: close a position when the fast EMA of the underlying's
# 5-min close crosses BELOW the slow EMA (for a CE; the reverse for a PE),
# on a candle later than the entry candle. See Options/config.py's
# ENABLE_EMA_CROSS_EXIT and dhan_client.refresh_ema_cross_signal(). The
# periods/interval (EMA_CROSS_FAST_PERIOD 9 / EMA_CROSS_SLOW_PERIOD 12 /
# 5-min) are shared, global settings in Options/config.py - only this
# toggle is per-package. Code default kept "false" for parity; the deployed
# FUTURES_ENABLE_EMA_CROSS_EXIT=true turns it on (user request 10 Sep 2026).
ENABLE_EMA_CROSS_EXIT = os.getenv("FUTURES_ENABLE_EMA_CROSS_EXIT", "false").lower() == "true"
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

# Proactive funds check (added 1 Sep 2026) - see Options/config.py's
# identical FUNDS_CHECK_ENABLED for the full rationale (the 2-bucket
# fund allocation system, fund_allocation.py) - this package's own
# entries check against the shared SECONDARY bucket (Options/Futures/
# Luxury all draw from it together, never the whole account's residual
# balance).
FUNDS_CHECK_ENABLED = os.getenv("FUTURES_FUNDS_CHECK_ENABLED", "true").lower() == "true"

# See Options/config.py's identical OPTIONS_PRODUCT for the full rationale -
# "MARGIN" is Tradehull's code for NRML/carry-forward, not "NRML" itself.
OPTIONS_PRODUCT = os.getenv("FUTURES_OPTIONS_PRODUCT", "MARGIN")  # MARGIN = NRML; code default hardened from "MIS" 10 Sep 2026 so a dropped .env line can't re-enable leveraged intraday. rename to reflect futures once real contracts replace the placeholder

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

# See Options/config.py's ENABLE_TRADING_WINDOWS / TRADING_WINDOWS - the
# multi-window entry schedule (added 10 Sep 2026). Supersedes the single-
# cutoff pair above when on. Only gates NEW entries; SQUARE_OFF_TIME still
# force-closes at 15:15 regardless.
ENABLE_TRADING_WINDOWS = os.getenv("FUTURES_ENABLE_TRADING_WINDOWS", "false").lower() == "true"
TRADING_WINDOWS = os.getenv("FUTURES_TRADING_WINDOWS", "09:15-11:00,14:00-15:28")

# See Options/config.py's "Nifty50 open gap-down / sharp-fall CE cool-off"
# block - the actual gap/fall computation is shared (always reads
# GAP_DOWN_THRESHOLD_POINTS/GAP_DOWN_SHARP_FALL_PCT/GAP_DOWN_CE_DELAY_MINUTES
# from Options.config, one market-wide fact). This is just this package's
# own independently-tunable on/off switch for honoring it.
ENABLE_GAP_DOWN_CE_DELAY = os.getenv("FUTURES_ENABLE_GAP_DOWN_CE_DELAY", "true").lower() == "true"

# Lowered 5->2 alongside Options' own value (user request 27 Aug 2026) - see
# Options/config.py's comment for the full rationale. LTP_STALE_AFTER_SECONDS
# lives only in Options/config.py since it governs the one shared dhan_client
# LTP cache both packages read from - no separate Futures copy needed.
MONITOR_INTERVAL_SECONDS = int(os.getenv("FUTURES_MONITOR_INTERVAL_SECONDS", "2"))

LOT_SIZE_FALLBACK = int(os.getenv("FUTURES_LOT_SIZE_FALLBACK", "1"))
ORDER_TAG_PREFIX = os.getenv("FUTURES_ORDER_TAG_PREFIX", "Fut")
