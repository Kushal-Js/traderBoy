"""
Central configuration for the Bollinger strategy (added 26 Sep 2026, user
request: "create another trading strategy as 'bollinger'... deploy in our
bot"). Mirrors Swing/config.py's shape/conventions - see this repo's
established pattern of one config module per strategy package.

STRATEGY, PRECISELY: a Bollinger-ribbon (5 Bollinger Bands, all period=20,
deviations 0.1/0.2/0.3/0.4/0.5) trend filter combined with a Vortex
Indicator (period=14) confirmation, entering via a software-simulated
"pending stop order" once a genuine multi-candle pullback forms against a
confirmed trend, with a swing-distance-derived dynamic stop-loss and a
1/3-of-stop/1/5-of-that trailing stop. NO PROFIT TARGET - pure trend-
following trailing-stop exit. Full derivation, every interpretation call,
and the 30-day backtest results (the "5min / lookback=2 (original)"
variant this deploys) are in trading-skills' learnings/bollinger-vortex-
strategy-30day-backtest.md and this repo's own
backtest_bollinger_vortex_9symbols_30day.py, which this package's
Bollinger/signals.py ports its indicator math from verbatim.

SCOPE, v1: NSE EQUITY symbols only. No MCX/index handling - the backtest
never covered those, and this is a brand-new strategy, not a place to add
untested scope. data/bollinger_watchlist starts empty; the user adds
symbols explicitly post-deploy.

PAPER_MODE_ENABLED defaults to FALSE, deliberately, unlike every other
strategy in this codebase's history (Options/Futures/Luxury/Swing all
launched with paper mode ON first). User was shown this exact tradeoff -
zero live/paper track record for this strategy, and the backtest itself
showed real parameter sensitivity across timeframe/lookback variants - and
explicitly reconfirmed real trades twice. paper_mode_control.py's runtime
POST /paper-mode override still works for Bollinger (see that module's
STRATEGIES tuple) as a redeploy-free kill switch if this needs to change
fast.

FUND_BUCKET = "primary" (NOT a new bucket) - shares Swing's own bucket per
explicit user decision: "use primary only... both SWING and BOLLINGER use
primary, no sharing between these 2" - see .env's FUND_PRIMARY_BUCKET_PCT
(raised 85 -> 100 alongside this deploy) and fund_allocation.py's own
docstring for why buckets are independent % ceilings against total
balance, not a partitioned/contended pool - Swing and Bollinger each still
enforce their OWN separate MAX_CONCURRENT_TRADES independently.
"""
import os

# ---------------------------------------------------------------------------
# Strategy on/off
# ---------------------------------------------------------------------------
STRATEGY_ENABLED = os.getenv("BOLLINGER_STRATEGY_ENABLED", "true").lower() == "true"
ENTRY_ENABLED = os.getenv("BOLLINGER_ENTRY_ENABLED", "true").lower() == "true"
# See module docstring - deliberately FALSE by default, unlike every other
# strategy's own launch history in this repo.
PAPER_MODE_ENABLED = os.getenv("BOLLINGER_PAPER_MODE_ENABLED", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Capacity - independent of Swing's own MAX_CONCURRENT_TRADES
# ---------------------------------------------------------------------------
MAX_CONCURRENT_TRADES = int(os.getenv("BOLLINGER_MAX_CONCURRENT_TRADES", "5"))

# ---------------------------------------------------------------------------
# Order/position basics
# ---------------------------------------------------------------------------
BASKET_TYPE = "OPTIONS"  # only mode this package implements - always a LONG CE/PE, never futures/equity/short
QUANTITY_LOTS = int(os.getenv("BOLLINGER_QUANTITY_LOTS", "1"))
OPTIONS_PRODUCT = os.getenv("BOLLINGER_OPTIONS_PRODUCT", "MARGIN")
ORDER_TAG_PREFIX = os.getenv("BOLLINGER_ORDER_TAG_PREFIX", "Bol")
FUND_BUCKET = "primary"  # see module docstring - shares Swing's bucket, not a new one
FUNDS_CHECK_ENABLED = os.getenv("BOLLINGER_FUNDS_CHECK_ENABLED", "true").lower() == "true"
FUNDS_CHECK_BUFFER_RS = float(os.getenv("BOLLINGER_FUNDS_CHECK_BUFFER_RS", "0"))

# ---------------------------------------------------------------------------
# Risk - MAX_LOSS_PROTECTION_RS is an absolute rupee circuit-breaker, same
# role as Swing's own (not present in the source video at all - this
# repo's own real-money discipline, not the strategy's design).
# BROKER_STOP_LOSS_ENABLED defaults TRUE here (unlike Swing's own more
# cautious rollout history) since paper mode is off from day one - a
# brand-new real-money strategy should have the resting broker-side
# protection live from its very first trade, not phased in later.
# ---------------------------------------------------------------------------
MAX_LOSS_PROTECTION_RS = float(os.getenv("BOLLINGER_MAX_LOSS_PROTECTION_RS", "4500"))
BROKER_STOP_LOSS_ENABLED = os.getenv("BOLLINGER_BROKER_STOP_LOSS_ENABLED", "true").lower() == "true"
BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE = float(os.getenv("BOLLINGER_BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE", "0.05"))
ENTRY_RETRY_COOLDOWN_SECONDS = int(os.getenv("BOLLINGER_ENTRY_RETRY_COOLDOWN_SECONDS", "180"))
LTP_STALE_FORCE_EXIT_MINUTES = float(os.getenv("BOLLINGER_LTP_STALE_FORCE_EXIT_MINUTES", "5"))
MARKET_OPEN_TIME = os.getenv("BOLLINGER_MARKET_OPEN_TIME", "09:00")

# ---------------------------------------------------------------------------
# Monitor loop cadence - own values, independent of Swing's own tick loop
# (no shared tick, only the candle_feed data underneath is shared).
# ---------------------------------------------------------------------------
MONITOR_INTERVAL_SECONDS = int(os.getenv("BOLLINGER_MONITOR_INTERVAL_SECONDS", "5"))
SYMBOL_PACING_SECONDS = float(os.getenv("BOLLINGER_SYMBOL_PACING_SECONDS", "0.35"))

# ---------------------------------------------------------------------------
# Strategy parameters - the exact "5min / lookback=2 (original)" backtested
# variant. See backtest_bollinger_vortex_9symbols_30day.py's own
# INTERPRETATION-CALLS docstring for what each of these means precisely.
# ---------------------------------------------------------------------------
SIGNAL_INTERVAL_MINUTES = int(os.getenv("BOLLINGER_SIGNAL_INTERVAL_MINUTES", "5"))
BB_PERIOD = int(os.getenv("BOLLINGER_BB_PERIOD", "20"))
BB_DEVIATIONS = (0.1, 0.2, 0.3, 0.4, 0.5)  # video's stated 5 bands, widest = 0.5
VORTEX_PERIOD = int(os.getenv("BOLLINGER_VORTEX_PERIOD", "14"))  # not stated in the video - standard default
SWING_FRACTAL_LOOKBACK = int(os.getenv("BOLLINGER_SWING_FRACTAL_LOOKBACK", "2"))
MIN_PULLBACK_CANDLES = int(os.getenv("BOLLINGER_MIN_PULLBACK_CANDLES", "2"))
MIN_STOP_PCT = float(os.getenv("BOLLINGER_MIN_STOP_PCT", "0.01"))
TRAILING_STOP_FRACTION = 1.0 / 3.0   # video's stated ratio
TRAILING_STEP_FRACTION = 1.0 / 5.0   # video's stated ratio (of the trailing-stop distance)

# ---------------------------------------------------------------------------
# WS candle feed - Bollinger's OWN flag (independently configurable), but
# defaults to matching Swing's own current live value (SWING_USE_WS_
# CANDLES=true in .env) since Bollinger calls into Swing.candle_feed
# directly (shared module, shared subscriptions - see Bollinger/signals.py).
# ---------------------------------------------------------------------------
USE_WS_CANDLES = os.getenv("BOLLINGER_USE_WS_CANDLES", "true").lower() == "true"
WS_STALE_AFTER_SECONDS = float(os.getenv("BOLLINGER_WS_STALE_AFTER_SECONDS", "90"))
# Throttle on recomputing the full pullback-state-machine replay (see
# Bollinger/signals.py's own module docstring for why this must replay the
# FULL retained series every time it runs, not a short window) - the
# underlying 5-min candle it depends on can't have changed more often than
# this anyway, so re-running the O(n) replay on every single 5s monitor
# tick would be pure waste. Close to Swing's own SUPERTREND_REFRESH_
# SECONDS (15) cadence.
SIGNAL_REFRESH_SECONDS = float(os.getenv("BOLLINGER_SIGNAL_REFRESH_SECONDS", "15"))
# REST fallback's own lookback window (days) when the WS candle feed isn't
# fresh/long enough yet - generous on purpose so a REST-served signal is
# never computed over materially LESS history than the WS path would give
# (candle_feed.py keeps up to DISK_RESTORE_LOOKBACK_DAYS=55 days/
# MAX_BARS_KEPT=2600 bars), avoiding the two paths silently disagreeing.
REST_LOOKBACK_DAYS = int(os.getenv("BOLLINGER_REST_LOOKBACK_DAYS", "60"))
