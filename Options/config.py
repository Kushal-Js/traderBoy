"""
Central configuration for the Chartink -> Dhan algo trading bot.
All values can be overridden via environment variables.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Dhan authentication - see Dhan-Tradehull docs
#   https://pypi.org/project/Dhan-Tradehull/
# Two modes:
#   "access_token" (default) - DHAN_ACCESS_TOKEN, manually generated from
#     web.dhan.co, expires every 24h (a SEBI/exchange-mandated cap since
#     1 Oct 2025, not a Dhan choice - no token lasts longer, regardless of
#     how it's generated). Needs manual refresh - see NOTES.md bug #17 for
#     the incident this caused once (a stale droplet-side token, silently
#     out of sync with a locally-refreshed one).
#   "pin_totp" - DHAN_PIN + DHAN_TOTP_SECRET, both static/long-lived
#     credentials (TOTP secret doesn't rotate - it's the RFC 6238 seed,
#     not the 6-digit code; Tradehull computes the current code from it
#     internally via pyotp on every login). Fully automated - no manual
#     step, no expiry to track, since Tradehull re-authenticates from
#     scratch each time using these two values. Verified working
#     end-to-end before switching over. DHAN_PIN is the account's trading
#     PIN - meaningfully more sensitive than an access token since it
#     doesn't expire/rotate on its own; treat this .env with that in mind.
# ---------------------------------------------------------------------------
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")
DHAN_AUTH_MODE = os.getenv("DHAN_AUTH_MODE", "access_token").lower()
DHAN_PIN = os.getenv("DHAN_PIN", "")
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET", "")

# Shared secret the webhook caller must send back to us, since Chartink
# webhooks are unauthenticated by default. Optional but recommended.
WEBHOOK_SHARED_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "")

# Live order-update / market-data WebSocket feed. Off by default failure
# mode is REST polling (see dhan_client.py), so this can be safely disabled
# if the socket connection is unavailable/misbehaving.
ENABLE_WS_FEED = os.getenv("ENABLE_WS_FEED", "true").lower() == "true"

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------
TOP_N_STOCKS = int(os.getenv("TOP_N_STOCKS", "3"))
# Separate caps per option type - CE (from /chartink/webhook) and PE (from
# /chartink/webhook-sell) each get their own budget rather than sharing one
# pool, so a run of bearish alerts can't crowd out capacity for bullish
# ones or vice versa. A symbol already open/in-flight as either type still
# blocks a new entry of the *other* type for that same symbol - see
# PositionStore.reserve_symbol().
MAX_LIVE_POSITIONS_CE = int(os.getenv("MAX_LIVE_POSITIONS_CE", "2"))
MAX_LIVE_POSITIONS_PE = int(os.getenv("MAX_LIVE_POSITIONS_PE", "2"))

TARGET_PCT = float(os.getenv("TARGET_PCT", "0.10"))          # +10% target
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.03"))    # -3% hard stop loss

# Trailing stop-loss: raises the exit floor as price rises above entry,
# instead of only exiting at the fixed hard stop loss. When disabled, a
# position only exits on TARGET_PCT or the fixed STOP_LOSS_PCT - see
# Position.current_trailing_sl in position_store.py.
ENABLE_TRAILING_SL = os.getenv("ENABLE_TRAILING_SL", "true").lower() == "true"
TRAILING_SL_PCT = float(os.getenv("TRAILING_SL_PCT", "0.01"))  # 1% trailing stop

# Stepped/"ratchet" stop-loss, independent of ENABLE_TRAILING_SL above and
# can run alongside it (the effective floor is whichever mechanism is more
# protective). For every step % the option's own premium has climbed from
# entry (peak seen, not live price - so a pullback after a step doesn't
# undo protection already earned), the stop-loss floor moves up
# DYNAMIC_SL_INCREASE_PCT of entry price. TARGET_PCT is untouched - this
# only tightens how much room a trade has to give back before target, it
# never changes where target itself sits. The mechanism itself is
# symmetric for CE and PE (both are always a BUY of the option itself, so
# "premium rising" means the same thing either way - see
# Position.current_trailing_sl), but the step width is configured
# separately per option type since backtesting found they don't
# necessarily need the same value: 7% backtested net-positive for CE
# (NOTES.md bug #12, BACKTEST_RESULTS.md round 4) but net-negative for PE
# on a later dataset - one severe single-trade whipsaw (VOLTAS, 17 Aug)
# outweighed the genuine catches - see BACKTEST_RESULTS.md's PE section.
# Both default to 7% for now (strategy unchanged pending more data); split
# out so either can be tuned independently once there's enough history.
ENABLE_DYNAMIC_SL = os.getenv("ENABLE_DYNAMIC_SL", "true").lower() == "true"
DYNAMIC_SL_STEP_PCT_CE = float(os.getenv("DYNAMIC_SL_STEP_PCT_CE", "0.07"))
DYNAMIC_SL_STEP_PCT_PE = float(os.getenv("DYNAMIC_SL_STEP_PCT_PE", "0.07"))
DYNAMIC_SL_INCREASE_PCT = float(os.getenv("DYNAMIC_SL_INCREASE_PCT", "0.01"))  # raises the floor 1% per step

# Exits a position when the underlying's 5-min candle closes below its 5-min
# Supertrend (trend-reversal exit), in addition to target/stop-loss - see
# dhan_client.refresh_supertrend_signal(). Computed on the underlying stock,
# not the option's own premium (too noisy/decay-affected for a clean trend
# read). A runtime toggle for the same reason as ENABLE_TRAILING_SL above.
ENABLE_SUPERTREND_EXIT = os.getenv("ENABLE_SUPERTREND_EXIT", "true").lower() == "true"
SUPERTREND_PERIOD = int(os.getenv("SUPERTREND_PERIOD", "10"))
SUPERTREND_MULTIPLIER = float(os.getenv("SUPERTREND_MULTIPLIER", "3.0"))
SUPERTREND_INTERVAL_MINUTES = int(os.getenv("SUPERTREND_INTERVAL_MINUTES", "5"))
# How long a cached Supertrend signal is reused before re-fetching candles -
# doesn't need to track candle closes exactly (the poll loop refreshes it
# every tick anyway, this just caps REST call frequency).
SUPERTREND_REFRESH_SECONDS = int(os.getenv("SUPERTREND_REFRESH_SECONDS", "60"))
# Extra minutes past a position's own entry candle before a Supertrend exit
# is honored, on top of always skipping the entry candle itself - see
# trading_engine._supertrend_signal_for(). Backtested across 7 real trading
# days: skipping only the entry candle (0) still let the very next candle
# exit a position riding the same breakout's aftershock; one extra 5-min
# candle of grace was the best-performing setting tested.
SUPERTREND_ENTRY_GRACE_MINUTES = int(os.getenv("SUPERTREND_ENTRY_GRACE_MINUTES", "5"))
# Minimum 5-min candles since market open before ANY Supertrend signal is
# trusted, independent of SUPERTREND_PERIOD and independent of any single
# position's own entry+grace gating above - see
# dhan_client.refresh_supertrend_signal(). SUPERTREND_PERIOD=10 means the
# indicator's very first computable value lands at exactly 10:10 for every
# underlying (10th 5-min candle from a 09:15 open) - but that first value
# has no prior trend/band to seed from, so it defaults via a naive
# band-width comparison that reads "bearish" on ~every underlying
# regardless of actual trend (see NOTES.md bug #10). Backtested on a
# 14-day, 104-trade CE dataset: this cluster went from "roughly neutral"
# (the original 7-day test) to -36,948 vs. target/SL alone - the risk bug
# #10 flagged as worth revisiting if a future backtest showed it turning
# net-harmful. Swept 15/20/25 candles: 20 was the clear best (+22,027.50
# vs. the unfixed behavior on that dataset, non-monotonic - 25 was worse
# than 20) - see NOTES.md bug #16 and BACKTEST_RESULTS.md's 14-day
# validation section. Doesn't fix the seed's bias, just delays when the
# (still-biased) first signal can fire, so fewer freshly-entered positions
# are still open and exposed by the time it does - positions still exit
# normally via target/stop-loss/dynamic-SL during the warmup window, they
# just can't exit via Supertrend until it completes.
SUPERTREND_MIN_WARMUP_CANDLES = int(os.getenv("SUPERTREND_MIN_WARMUP_CANDLES", "20"))

# Default ATM leg for /chartink/webhook (the bullish scan) and the
# fallback used when reconciling a broker position of unknown origin.
# /chartink/webhook-sell (bearish scan) always buys PE regardless of this -
# see main.py's two webhook handlers.
OPTION_TYPE = os.getenv("OPTION_TYPE", "CE").upper()

QUANTITY_LOTS = int(os.getenv("QUANTITY_LOTS", "1"))  # number of lots per leg

# Order product for options: MIS = intraday (auto square-off by broker as a
# safety net; we still explicitly square off ourselves at SQUARE_OFF_TIME).
OPTIONS_PRODUCT = "MIS"

DEFAULT_EXCHANGE = "NFO"  # Dhan-Tradehull's exchange code for NSE F&O

# ---------------------------------------------------------------------------
# Timing (all times are IST / Asia-Kolkata)
# ---------------------------------------------------------------------------
MARKET_TZ = "Asia/Kolkata"
MARKET_OPEN_TIME = "09:15"
SQUARE_OFF_TIME = os.getenv("SQUARE_OFF_TIME", "15:15")
MARKET_CLOSE_TIME = "15:30"

MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "5"))

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
LOT_SIZE_FALLBACK = int(os.getenv("LOT_SIZE_FALLBACK", "1"))
ORDER_TAG_PREFIX = os.getenv("ORDER_TAG_PREFIX", "Cti")  # correlation id prefix
