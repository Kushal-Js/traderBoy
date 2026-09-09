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

# Same-day loss cooldown (added 2 Sep 2026, user's own corrective-action
# request after investigating 2-3 Sep real trades: MAHABANK/PHOENIXLTD/
# GVT&D each got re-entered multiple times the same day right after
# stopping out, and the re-entries mostly lost again too - MAHABANK went
# 0-for-3 that way, -Rs.2,600 total). Skips a fresh entry into an
# underlying that stopped THIS strategy out (a real pnl<=0 close) within
# the last LOSS_COOLDOWN_MINUTES, backed by trade_history.
# minutes_since_last_loss_today() (the SAME durable real_trades log
# record_closed_trade() already writes, same "survives a restart"
# reasoning MAX_DAILY_ENTRIES_PER_SYMBOL's own cap already relies on).
# Independent of and stacks with the daily re-entry cap above - this one
# is about TIMING (don't immediately chase a loss), that one is about
# COUNT (at most N times all day, no matter how spaced out).
LOSS_COOLDOWN_ENABLED = os.getenv("LUXURY_LOSS_COOLDOWN_ENABLED", "true").lower() == "true"
# 20 minutes - CORRECTED after backtesting a sweep of cooldown lengths
# against all 37 real Luxury trades from 2-3 Sep 2026. The original
# guess (30 min) actually backtests NET NEGATIVE (-Rs.367): it blocks
# BOTH of MAHABANK's/RVNL's genuine repeat-losses AND a GVT&D re-entry
# that went on to hit PROFIT_PROTECTION_HIT for +Rs.1,512 - a real
# false positive. At 20 minutes, only the two genuine back-to-back
# losses get blocked (MAHABANK's second attempt launched barely a
# minute after its first loss closed; RVNL similarly close) while the
# GVT&D win survives untouched (its own gap before re-entering was
# just past 20 minutes) - net backtested effect: +Rs.1,146, the best of
# every length tested (5/10/15/20/25/30/45/60/90/120 minutes all
# swept). Full sweep table in NOTES.md's corrective-action entry for
# this feature - worth re-validating as more real trade data
# accumulates, since this is tuned against only 2 days.
LOSS_COOLDOWN_MINUTES = float(os.getenv("LUXURY_LOSS_COOLDOWN_MINUTES", "20"))

# Repeat-loss same-day block (added 8 Sep 2026, user request straight
# off a real Chartink-alert backtest of this scan: "I can see that re
# occurring losses hit at OIL and at many places... after a loss is hit
# 2 times on a same stock trade on that day, same stock trading should
# not be allowed for loss based condition only"). Distinct from BOTH
# existing per-symbol guards above: MAX_DAILY_ENTRIES_PER_SYMBOL is a
# flat COUNT cap regardless of outcome (wins count too); LOSS_COOLDOWN_
# MINUTES is a short TIMING gap after the most recent loss (a win in
# between resets nothing, but doesn't block anything either once the
# window passes). This one is neither - it's a same-day OUTCOME-COUNTING
# block: once a symbol has closed on a genuine loss-designated exit
# (MAX_LOSS_HIT/STOP_LOSS_HIT specifically - "loss based condition[s]",
# not every trade that happened to close a little negative for some
# other reason like SUPERTREND_EXIT/TRAILING_SL_HIT) LOSS_REPEAT_BLOCK_
# COUNT times today, it's done for the SYMBOL for the rest of the day,
# no more re-entries regardless of how much time has passed. Backed by
# trade_history.loss_exit_count_today() - the same durable, restart-
# surviving real_trades log every other daily count/cooldown here reads.
LOSS_REPEAT_BLOCK_ENABLED = os.getenv("LUXURY_LOSS_REPEAT_BLOCK_ENABLED", "true").lower() == "true"
LOSS_REPEAT_BLOCK_COUNT = int(os.getenv("LUXURY_LOSS_REPEAT_BLOCK_COUNT", "2"))
LOSS_REPEAT_BLOCK_EXIT_REASONS = ("MAX_LOSS_HIT", "STOP_LOSS_HIT")

# Broker-side stop-loss order (added 8 Sep 2026, user request: "broker-
# side stop order that fires instantly regardless of polling interval
# would be a better approach" - a follow-up to the same backtest that
# found MAX_LOSS_HIT overshooting its own cap). Intended design: a real
# SELL STOP-LOSS MARKET (SL-M) order placed at Dhan immediately after
# every entry, triggered at the rupee-equivalent price of
# current_max_loss_per_trade_rs(), firing at the EXCHANGE's own matching
# engine the instant price trades through the trigger - independent of
# our own process being slow, disconnected, or between ticks.
#
# CONFIRMED BROKEN on this account/segment, 9 Sep 2026 - live-money
# evidence, not a guess. Every real "STOP_LOSS_MARKET" SELL order placed
# for an NSE_FNO option (productType=MARGIN) came back from Dhan's own
# get_order_by_id as orderType="LIMIT", triggerPrice=0.0, and filled
# INSTANTLY at the prevailing market price - not at the intended
# trigger. This is exactly what caused Luxury's real COALINDIA/GVT&D/
# PAYTM entries that morning to be sold within seconds of fill,
# regardless of actual price movement. Traced the full client call chain
# (Tradehull.order_placement -> dhanhq.place_order -> DhanHTTP.post) and
# confirmed the outgoing payload genuinely carried orderType=
# "STOP_LOSS_MARKET" with the correct trigger - no client-side bug, no
# mistranslation. Also confirmed the conversion is NOT about price=0 vs
# price=trigger_price: a controlled live test (buy 1 lot COALINDIA PE,
# then two real SL-M SELL attempts with trigger deliberately far below
# LTP, one with price=0 and one with price=trigger_price) had BOTH
# variants come back orderType="LIMIT" and BOTH fill immediately at
# ~market price. So this is a genuine Dhan-side behavior for SL-M SELL
# orders on F&O options with this product type, not something fixable
# from our side by changing what we send. See NOTES.md and trading-
# skills' incidents/ for the full writeup.
#
# Decision (user's own call, 9 Sep 2026): abandon broker-side SL-M for
# OPTIONS. Code default flipped to "false" (not just .env) so this
# known-broken/dangerous path can't silently re-enable itself from a
# fresh .env or test environment. The code/tests for it are kept, not
# deleted, per this repo's own "never delete, keep it off instead"
# convention - see tests/test_luxury_broker_stop_loss.py, which already
# pins BROKER_STOP_LOSS_ENABLED explicitly per-test rather than relying
# on this default, so flipping it here doesn't affect them.
#
# The existing _exit_reason_for MAX_LOSS_HIT check (poll/tick-driven,
# ~2s worst case via monitor_loop + synchronous on_price_tick on every
# real market tick) plus LIQUIDITY_GUARD_ENABLED (exits early on a
# thinly-traded option going quiet, the actual precursor pattern behind
# the CHOLAFIN-style overshoot-via-gap case this feature was originally
# meant to backstop) and LOSS_REPEAT_BLOCK_ENABLED remain the real
# protection stack going forward. NOTE: this is a different F&O
# instrument (options) than Swing's own uncommitted broker-stop-loss
# work, which targets FUTURES legs - that has NOT been shown broken by
# this finding and should be independently verified before trusting it,
# not assumed safe or assumed broken either way.
BROKER_STOP_LOSS_ENABLED = os.getenv("LUXURY_BROKER_STOP_LOSS_ENABLED", "false").lower() == "true"

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

# Liquidity guard (added 2 Sep 2026, user's own corrective-action request
# after investigating a CHOLAFIN MAX_LOSS_HIT overshoot 3 Sep 2026 - real
# 1-min option-candle replay showed price sitting flat on ZERO traded
# volume for 4 straight minutes, then gapping ~7% straight past the
# stop-loss threshold in one untracked candle, -Rs.2,594 against a
# Rs.1,000 cap). A thinly-traded option going quiet for several minutes
# is exactly the precursor pattern behind that kind of un-catchable gap -
# this exits a held position EARLY (before any price threshold is even
# close to firing) the moment its OWN option contract has printed ZERO
# volume for several consecutive completed 1-min bars, rather than
# waiting for MAX_LOSS_HIT to (over)fire after the gap has already
# happened. This package's own on/off gate, same pattern as
# ENABLE_SUPERTREND_EXIT above - the actual bar-count/refresh-cadence
# parameters are shared, global computation settings that live in
# Options/config.py (LIQUIDITY_GUARD_ZERO_VOLUME_BARS/_REFRESH_SECONDS),
# since refresh_liquidity_signal()/get_cached_illiquid() live in the
# shared dhan_client.py - identical reasoning to why SUPERTREND_PERIOD/
# MULTIPLIER/REFRESH_SECONDS are ALSO only ever set in Options/config.py
# even though every package's own ENABLE_SUPERTREND_EXIT is independent.
LIQUIDITY_GUARD_ENABLED = os.getenv("LUXURY_LIQUIDITY_GUARD_ENABLED", "true").lower() == "true"

# Fallback default only (used where Dhan's own reported option_type comes
# back missing/None, e.g. reconciliation/AMO-sync) - both real webhooks
# below pass their own explicit option_type, same as Options' identical
# constant.
OPTION_TYPE = os.getenv("LUXURY_OPTION_TYPE", "CE").upper()

QUANTITY_LOTS = int(os.getenv("LUXURY_QUANTITY_LOTS", "1"))

# Proactive funds check (added 1 Sep 2026) - see Options/config.py's
# identical FUNDS_CHECK_ENABLED for the full rationale (the 2-bucket
# fund allocation system, fund_allocation.py) - this package's own
# entries check against the shared SECONDARY bucket (Options/Futures/
# Luxury all draw from it together, never the whole account's residual
# balance).
FUNDS_CHECK_ENABLED = os.getenv("LUXURY_FUNDS_CHECK_ENABLED", "true").lower() == "true"

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
