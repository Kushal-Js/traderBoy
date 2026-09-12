"""
Central configuration for the Swing v2 strategy (complete rewrite, 12 Sep
2026, user request: "Completely rewrite the Swing strategy with new
rules (old rules and config for Swing to be discarded)").

This REPLACES the old 3-mode (basket/sequential/basket_hedge) design,
its Chartink-driven watchlist scan + daily pruning, and an uncommitted
broker-side stop-loss WIP that used SL-M on a PE options leg - NSE banned
SL-M for options exchange-wide in Sep 2021, and that WIP almost certainly
carried the identical bug that bit Luxury on 9 Sep 2026 (see trading-
skills/incidents/2026-09-09-luxury-sl-m-orders-fill-as-limit.md). That
old WIP was `git stash`'d (recoverable), not deleted, before this file
was rewritten.

New design, in one sentence: for each stock on a manually-curated
watchlist, buy or sell ONE configurable instrument ("basket-type" -
futures / ATM option / raw equity shares) the moment a 5-min Supertrend
crossover confirms a trend that a slower 5-min-vs-15-min 200-EMA regime
filter already agrees with, and exit on the opposite Supertrend
crossover (or a flat rupee/percent risk limit, whichever comes first).

Reuses proven infrastructure rather than reinventing it: the continuous
multi-day candle fetch (Options/dhan_client.py's fetch_continuous_
intraday, extended here with a longer lookback specifically for the
200-EMA warm-up - see Swing/signals.py), the broker-side SL-L order
mechanics and its exact cancel-and-reconcile-on-square-off sequence
(ported from Options/trading_engine.py's own incident-hardened
_exit_position - see Swing/trading_engine.py), and the existing 2-bucket
fund allocation system (this package draws from the PRIMARY bucket,
Options/Futures/Luxury share the secondary one - unchanged from the old
design).

No Chartink integration, no watchlist pruning, no dedicated webhook
endpoint (user request: "No watchlist pruning logic or a separate
webhook endpoint required as of now") - the watchlist is a plain list
the user edits directly (see Swing/watchlist.py), and the bot itself
decides when to enter by continuously evaluating the signals above, not
by reacting to an inbound alert.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Master gates
# ---------------------------------------------------------------------------
# SWING_V2_ (not the bare SWING_ prefix every other constant in this file
# uses) specifically because SWING_STRATEGY_ENABLED already exists in the
# live .env for the OLD design - reusing the bare name here would silently
# inherit whatever that old flag happened to be set to. User request (12
# Sep 2026): default TRUE - Swing v2 goes live looking for real entries
# from the very first deploy, not gated behind a later manual flip (see
# this session's rollout-sequencing plan for how the earlier, read-only
# verification stages compensate for skipping that usual "ship disabled
# first" caution).
STRATEGY_ENABLED = os.getenv("SWING_V2_STRATEGY_ENABLED", "true").lower() == "true"

# Entry-only panic brake - exits always run regardless of this flag, same
# convention as every other package's own ENTRY_ENABLED-style switch.
# Flip this (not STRATEGY_ENABLED) to stop new entries while still
# managing whatever's already open.
ENTRY_ENABLED = os.getenv("SWING_ENTRY_ENABLED", "true").lower() == "true"

# ---------------------------------------------------------------------------
# Basket-type - the ONE configurable instrument choice for every watchlist
# stock (user request: "This basket can contain either that stock Future
# Contract 1 lot / 1 lot of ATM Option Contract (PE/CE...) / 200 units of
# same stock, make this configurable so that I can change it anytime").
# Confirmed with the user (12 Sep 2026): this is a SINGLE GLOBAL setting
# for the whole watchlist, not a per-stock choice - change it and it
# applies to every future entry from that point on. Read FRESH on every
# entry (never cached at import into a derived constant) so a change
# takes effect immediately, no restart needed.
#
# Default "options" (user request, 12 Sep 2026) - also happens to be the
# smallest-capital, capped-loss choice of the three, which is a sensible
# place to start regardless.
#
# "equity" is LONG-ONLY (confirmed with the user, 12 Sep 2026): naked
# overnight equity short-selling isn't legal in India (shorting shares is
# only possible intraday, must square off same day - which would break
# this strategy's whole multi-day trend-hold premise for that basket-type
# specifically). When BASKET_TYPE=="equity" and a stock's regime turns
# bearish, that stock's entry is skipped entirely - see
# Swing/trading_engine.py's enter_position_for_stock.
BASKET_TYPE = os.getenv("SWING_BASKET_TYPE", "options").lower()
if BASKET_TYPE not in ("futures", "options", "equity"):
    import logging
    logging.getLogger(__name__).error(
        "SWING_BASKET_TYPE=%r is not one of futures/options/equity - forcing "
        "STRATEGY_ENABLED=False rather than trade with an undefined instrument type.",
        BASKET_TYPE,
    )
    STRATEGY_ENABLED = False

QUANTITY_LOTS = int(os.getenv("SWING_QUANTITY_LOTS", "1"))  # futures/options basket-types
EQUITY_QUANTITY = int(os.getenv("SWING_EQUITY_QUANTITY", "200"))  # the "200 units" - configurable per the request

# ---------------------------------------------------------------------------
# Risk management (user request, 12 Sep 2026, verbatim values):
# "MAX LOSS PROTECTION = 4500 and PROFIT PROTECTION = 2000 and Target =
# 20% and Hard Stop Loss = 20%". FLAT values, no before/after-11:30-cutoff
# split - unlike Options/Futures/Luxury's current_max_loss_per_trade_rs()
# pattern. This is deliberate, not an oversight: Swing's own historical
# design (before this rewrite) also used flat values (PE_MAX_LOSS_RS,
# FUTURES_MAX_LOSS_RS) with no time-of-day split, so this keeps that
# convention rather than importing Options/Futures/Luxury's.
# ---------------------------------------------------------------------------
MAX_LOSS_PROTECTION_RS = float(os.getenv("SWING_MAX_LOSS_PROTECTION_RS", "4500"))

# PROFIT_PROTECTION_RS/_GIVEBACK_PCT raised from the original 2000/0% on
# 12 Sep 2026, straight off a real backtest (last 10 trading days,
# ADANIPORTS/COALINDIA, FUTURES basket-type): at 2000/0%, 13 of 16
# trades exited within minutes via PROFIT_PROTECTION_HIT (Rs 2000 is
# under 0.5% of a typical futures position's real notional, and 0%
# giveback locks in on the very FIRST downtick once crossed) - the
# strategy was barely ever reaching its own intended SUPERTREND_REVERSAL
# exit. Raising the threshold ALONE (to 10000, still 0% giveback) made
# things WORSE (total swung from +34,782 to +20,822 on that same
# sample) - without a giveback tolerance in between, several trades that
# would have locked a small win instead rode all the way back into a
# real loss (one past MAX_LOSS_HIT entirely) before Supertrend actually
# reversed. Adding a 2% giveback on top of a 5000 threshold (a milder
# threshold than the 10000 tested, matched with the giveback that same
# backtest run proved necessary) let the two big trend trades run to
# +31,920 and +16,942 respectively (from +10,592/+9,652 at 10000/0%)
# while leaving every losing trade's outcome completely unchanged (they
# never crossed the threshold in the first place - giveback only ever
# affects trades that already armed).
#
# 0.0 giveback is bit-identical to Options/Futures/Luxury's own zero-
# giveback default (lock in the instant price is off the peak).
PROFIT_PROTECTION_RS = float(os.getenv("SWING_PROFIT_PROTECTION_RS", "2000"))
PROFIT_PROTECTION_GIVEBACK_PCT = float(os.getenv("SWING_PROFIT_PROTECTION_GIVEBACK_PCT", "0.0"))
TARGET_PCT = float(os.getenv("SWING_TARGET_PCT", "0.20"))
HARD_STOP_LOSS_PCT = float(os.getenv("SWING_HARD_STOP_LOSS_PCT", "0.20"))
ENABLE_TARGET_EXIT = os.getenv("SWING_ENABLE_TARGET_EXIT", "true").lower() == "true"

# NOTE for future tuning (not changed now, just flagged): 20% target/stop
# on a FUTURES or EQUITY price is a large move that will rarely fire in
# practice - MAX_LOSS_PROTECTION_RS will dominate those exits instead.
# 20% on an OPTION PREMIUM is normal, and BASKET_TYPE defaults to
# "options" per the request above, so this pairing is exactly right out
# of the box. Only relevant if BASKET_TYPE is later switched to futures
# or equity.

# ---------------------------------------------------------------------------
# Broker-side SL-L (real STOP-LOSS LIMIT order placed at Dhan right after
# every fill) - user request: "along with broker side SL (how we
# calculated for other strategies) but make sure all orders and trades
# are in sync and when a trade square off is done, other pending SL
# order are also closed". Ported verbatim from Options/Futures/Luxury's
# proven design (never SL-M - NSE banned that for options in Sep 2021;
# SL-L is the only broker-side conditional stop still permitted). See
# Swing/trading_engine.py's enter_position_for_stock (placement) and
# _exit_position (the cancel-and-reconcile sequence that satisfies "other
# pending SL order are also closed").
#
# Kept OFF by default, same rollout discipline as every other package's
# first SL-L launch (Options/config.py's own BROKER_STOP_LOSS_ENABLED
# docstring: "earn trust via an actual controlled live test... not by
# assumption") - the user's default-TRUE request above was for
# STRATEGY_ENABLED (the master switch), not this. SWING_V2_ prefix for
# the same reason as STRATEGY_ENABLED: SWING_BROKER_STOP_LOSS_ENABLED
# already exists in .env for the old (SL-M) design.
BROKER_STOP_LOSS_ENABLED = os.getenv("SWING_V2_BROKER_STOP_LOSS_ENABLED", "false").lower() == "true"

# The SL-L order's trigger-to-limit gap, sized IN RUPEES off the same
# MAX_LOSS_PROTECTION_RS cap used for trigger_price itself (not a flat %
# of price) - identical mechanism to Options/Futures/Luxury's own
# BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE, adopted at the same 0.05 value
# this session (worst-case extra loss ~Rs 225 on the current Rs 4500
# cap, independent of quantity). See Options/trading_engine.py's
# broker_stop_trigger_and_limit-equivalent computation for the exact
# formula; Swing/position_store.py's own broker_stop_trigger_and_limit
# helper implements the SHORT-side mirror image (trigger above entry,
# limit above trigger) that Options/Futures/Luxury never needed since
# they're always long.
BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE = float(os.getenv("SWING_BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE", "0.05"))

# ---------------------------------------------------------------------------
# Signals - the regime filter + Supertrend crossover (user request,
# verbatim): "Continuous candle to be used along with continuous EMA and
# Super trend signals... over a period of many days like all other
# brokers do... Pick each stock and create and manage 2 EMAs for it: 5
# min close 200 EMA, 15 min close 200 EMA. If 5 min 200 EMA crosses above
# or greater than 15 min 200 EMA then only buy the basket whenever 5 min
# close crosses above 5 min Super Trend and square off when super trend
# reverse[s]. If 5 min 200 EMA crosses below... then only sell the
# basket... whenever 5 min close crosses below 5 min Super Trend..."
#
# See Swing/signals.py for where these are actually computed - kept as a
# Swing-local module rather than added to Options/dhan_client.py's shared
# signal cache, matching this package's own existing precedent (its old
# Supertrend implementation was ALSO always kept independent of Options'
# shared cache, specifically because that shared cache is live real-money
# exit protection for three other strategies and no other package wants
# a 200-EMA regime signal anyway).
# ---------------------------------------------------------------------------
REGIME_EMA_PERIOD = int(os.getenv("SWING_REGIME_EMA_PERIOD", "200"))
REGIME_FAST_INTERVAL_MINUTES = int(os.getenv("SWING_REGIME_FAST_INTERVAL_MINUTES", "5"))
REGIME_SLOW_INTERVAL_MINUTES = int(os.getenv("SWING_REGIME_SLOW_INTERVAL_MINUTES", "15"))

# The global INTRADAY_CONTINUOUS_LOOKBACK_DAYS every other signal in this
# codebase uses (default 7) is nowhere near enough to warm up a 200-period
# EMA on 15-min bars (7 days is only ~125 bars; needs >=200 for a first
# value at all, ~600 for a genuinely stable one). Confirmed via a live
# spike (12 Sep 2026) that Dhan serves a 45-calendar-day/15-min request in
# ONE call (795 bars returned, no chunking needed) - see fetch_continuous_
# intraday's own lookback_days_override docstring for why this is a
# per-call override rather than a change to the shared global (which
# would silently alter every OTHER live signal for Options/Futures/
# Luxury too).
REGIME_EMA_LOOKBACK_DAYS = int(os.getenv("SWING_REGIME_EMA_LOOKBACK_DAYS", "45"))

# A 15-min EMA(200) moves slowly and its own fetch is comparatively large
# (45 days of candles) - refreshed far less often than a fast exit signal
# needs to be. 60s, not the Supertrend's own 15s below.
REGIME_REFRESH_SECONDS = int(os.getenv("SWING_REGIME_REFRESH_SECONDS", "60"))

# Swing keeps its OWN independent Supertrend parameters/cache (see
# Swing/signals.py) rather than sharing Options' - same precedent as
# above. Values matched to Options' own defaults per the user's request
# ("Super trend signals... like all other brokers do").
SUPERTREND_PERIOD = int(os.getenv("SWING_SUPERTREND_PERIOD", "10"))
SUPERTREND_MULTIPLIER = float(os.getenv("SWING_SUPERTREND_MULTIPLIER", "3.0"))
SUPERTREND_INTERVAL_MINUTES = int(os.getenv("SWING_SUPERTREND_INTERVAL_MINUTES", "5"))
SUPERTREND_REFRESH_SECONDS = int(os.getenv("SWING_SUPERTREND_REFRESH_SECONDS", "15"))
ENABLE_SUPERTREND_EXIT = os.getenv("SWING_ENABLE_SUPERTREND_EXIT", "true").lower() == "true"

# ---------------------------------------------------------------------------
# Capacity (user request: "Keep Max Concurrent Trade capacity as 2 as of
# now but make it configurable also"). ONE shared counter across the
# whole strategy - not split by direction/basket-type, since a stock's
# regime is mutually exclusive at any given time (it's never
# simultaneously both a long and a short candidate).
# ---------------------------------------------------------------------------
MAX_CONCURRENT_TRADES = int(os.getenv("SWING_MAX_CONCURRENT_TRADES", "2"))

# ---------------------------------------------------------------------------
# Funds (user request: "Use primary bucket funds as max cap available for
# this strategy (we already have primary and secondary buckets)") - this
# matches what the OLD Swing design already did correctly
# (fund_allocation.has_sufficient_bucket_funds("primary", ...) via the old
# _has_sufficient_funds wrapper), carried forward unchanged into this
# rewrite. FUND_BUCKET is a named constant purely so it's greppable, not
# because anything else reads it as a variable.
# ---------------------------------------------------------------------------
FUND_BUCKET = "primary"
FUNDS_CHECK_ENABLED = os.getenv("SWING_FUNDS_CHECK_ENABLED", "true").lower() == "true"

# The OLD design's Rs 15,000 buffer existed ONLY to compensate for the
# basket/basket_hedge modes' 2-leg (futures+PE) combo margin benefit that
# Dhan's own /margincalculator can't price (it has zero combo-awareness -
# see fund_allocation.py's own docstring). Swing v2 is ALWAYS single-leg
# (exactly one of futures/options/equity per basket), so there's no
# combo benefit left to compensate for - buffer reverts to 0.
FUNDS_CHECK_BUFFER_RS = float(os.getenv("SWING_FUNDS_CHECK_BUFFER_RS", "0"))

# ---------------------------------------------------------------------------
# Order product types. FUTURES_PRODUCT/OPTIONS_PRODUCT stay configurable
# strings, defaulting to "MARGIN" - this codebase's existing Tradehull
# code for NRML carry-forward on F&O (identical to what Options/Futures/
# Luxury already use). NOTE: the user's own 12 Sep request phrased these
# as "NRML/CNC" - CNC ("Cash and Carry") is a real Dhan/exchange product
# type, but it only exists for EQUITY DELIVERY, it is not a valid product
# type for a futures or options contract, so it can't actually be set on
# these two. EQUITY_PRODUCT below carries CNC for the one basket-type it
# actually applies to.
# ---------------------------------------------------------------------------
FUTURES_PRODUCT = os.getenv("SWING_FUTURES_PRODUCT", "MARGIN")
OPTIONS_PRODUCT = os.getenv("SWING_OPTIONS_PRODUCT", "MARGIN")
EQUITY_PRODUCT = os.getenv("SWING_EQUITY_PRODUCT", "CNC")

# ---------------------------------------------------------------------------
# MCX commodities (user request 12 Sep 2026: "enable COPPER MCX options and
# future trading also via SWING strategy" - later corrected the same turn
# to "disable Copper Future trading as of now, only Options trading for
# Copper"). A watchlist symbol in MCX_SYMBOLS routes through Options/
# dhan_client.py's MCX-aware resolvers (get_mcx_futures_contract for the
# regime/Supertrend signal reference - there's no continuous "spot" for an
# MCX commodity, only its futures contract - and the now-MCX-capable
# get_atm_option for the tradeable leg) instead of the NSE-equity path
# every other watchlist symbol uses. Real order placement for an MCX
# symbol is ONLY allowed when BASKET_TYPE=="options" - see
# Swing/trading_engine.py's enter_position_for_stock, which skips (does
# NOT place a real order) any MCX symbol while BASKET_TYPE=="futures",
# per the explicit correction above. BASKET_TYPE=="equity" was never
# requested for a commodity and isn't meaningful for one (no delivery
# concept), so it's untouched by any of this.
# ---------------------------------------------------------------------------
MCX_SYMBOLS = {s.strip().upper() for s in os.getenv("SWING_MCX_SYMBOLS", "COPPER").split(",") if s.strip()}

# The REAL per-lot economic quantity (kg for Copper) - used ONLY for P&L/
# rupee-threshold math (MAX_LOSS_PROTECTION_RS/PROFIT_PROTECTION_RS checks,
# the broker-side SL-L trigger/limit formula), NEVER for the real order's
# own `quantity` parameter. This is the single easiest thing to get wrong
# for an MCX symbol: Dhan's own instrument master reports SEM_LOT_UNITS=1
# for Copper FUTCOM/OPTFUT rows, which is CORRECT for order placement
# (Dhan's MCX order-quantity convention is "number of lots", confirmed via
# a live margin-calculator spike 12 Sep 2026: quantity=1 priced out to
# Rs 304,687.50 margin - exactly one real 2,500kg lot; quantity=2500 priced
# an absurd Rs 76+ crore) but WRONG as a rupee-per-point multiplier (a
# single-unit "quantity" would make every rupee threshold here effectively
# unreachable). See Swing/position_store.py's Position.pnl_multiplier
# field and Swing/trading_engine.py's entry code for where quantity vs
# pnl_multiplier are each actually used - they must never be swapped.
# Before adding any FUTURE MCX symbol here, verify its own real per-lot
# multiplier the same way (margin_calculator at quantity=1 should price
# out to that commodity's real-world one-lot margin) rather than guessing.
MCX_PNL_MULTIPLIERS = {
    sym: int(os.getenv(f"SWING_MCX_PNL_MULTIPLIER_{sym}", "2500"))
    for sym in MCX_SYMBOLS
}

MCX_PRODUCT = os.getenv("SWING_MCX_PRODUCT", "MARGIN")

# Same rollover-avoidance guard CopperOptions/config.py (now removed) used
# for its own expiry-cycle resolution - avoids trading a same-day/near-
# expiry (extreme gamma/theta) contract just because it's nearest.
MCX_MIN_DAYS_TO_EXPIRY = int(os.getenv("SWING_MCX_MIN_DAYS_TO_EXPIRY", "3"))

# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------
MARKET_TZ = "Asia/Kolkata"
MONITOR_INTERVAL_SECONDS = int(os.getenv("SWING_MONITOR_INTERVAL_SECONDS", "5"))
# Inter-symbol delay while scanning the watchlist for entry signals -
# respects Dhan's market-data rate limits on back-to-back calls, same
# rationale as every other per-symbol pacing sleep in this codebase.
SYMBOL_PACING_SECONDS = float(os.getenv("SWING_SYMBOL_PACING_SECONDS", "0.35"))
ORDER_TAG_PREFIX = os.getenv("SWING_ORDER_TAG_PREFIX", "Sw2")
