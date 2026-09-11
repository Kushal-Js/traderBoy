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

# Same-day RSI-gated loss re-entry block - REPLACED the old time-based
# LOSS_COOLDOWN_ENABLED/LOSS_COOLDOWN_MINUTES pair that used to live here
# (originally added 2 Sep 2026 after MAHABANK/PHOENIXLTD/GVT&D re-entry
# incidents, tuned to 20 minutes via a backtest sweep - both retired 11
# Sep 2026). See Options/config.py's own "Same-day RSI-gated loss
# re-entry block" comment for the full replacement rationale (a real
# INDUSTOWER alert got skipped 1 minute short of the old 20-minute
# window, regardless of whether the stock had actually recovered). The
# RSI computation itself (period/interval/overbought threshold) is
# shared - always reads Options.config, same as SUPERTREND_PERIOD does;
# this is just this package's own independently-tunable on/off switch.
ENABLE_RSI_LOSS_REENTRY_BLOCK = os.getenv("LUXURY_ENABLE_RSI_LOSS_REENTRY_BLOCK", "true").lower() == "true"

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
# found MAX_LOSS_HIT overshooting its own cap). A real broker-side
# conditional order is placed at Dhan immediately after every entry,
# triggered at the rupee-equivalent price of current_max_loss_per_
# trade_rs(), so it can fire at the EXCHANGE's own matching engine the
# instant price trades through the trigger - independent of our own
# process being slow, disconnected, or between ticks.
#
# ORIGINAL DESIGN (8 Sep 2026) used a real SELL STOP-LOSS MARKET (SL-M)
# order - CONFIRMED BROKEN for OPTIONS on 9 Sep 2026, and not fixable
# from our side: NSE discontinued SL-M orders for index/stock OPTIONS
# exchange-wide back in Sep 2021 (a freak-trade protection measure,
# applies to every NSE-registered broker, not just Dhan - see NOTES.md
# entry #99 / trading-skills' incidents/2026-09-09-luxury-sl-m-orders-
# fill-as-limit.md for the full live-money investigation, including a
# controlled live test proving it wasn't about our own price/trigger
# values). Every real "STOP_LOSS_MARKET" SELL order placed for an
# NSE_FNO option came back from Dhan's own get_order_by_id as
# orderType="LIMIT" and filled INSTANTLY at market - exactly what caused
# Luxury's real COALINDIA/GVT&D/PAYTM entries that morning to be sold
# within seconds of fill, regardless of actual price movement.
#
# CURRENT DESIGN (9 Sep 2026): a real SELL STOP-LOSS LIMIT (SL-L) order
# instead - the ONLY broker-side conditional stop NSE still permits for
# options. TWO prices: `trigger_price` (same rupee-cap calculation as
# before) and a `limit_price` = trigger_price * (1 -
# BROKER_STOP_LOSS_LIMIT_BUFFER_PCT) below it (see place_stop_loss_
# limit_order's own docstring in Options/dhan_client.py for the exact
# mechanics). Real tradeoff, inherent to SL-L and not a bug: if price
# gaps straight through limit_price before filling, the order can sit
# UNFILLED while price keeps falling - the freak-trade protection
# working as intended, but it means a violent gap can still leave a
# position open past its cap. The existing poll/tick-driven MAX_LOSS_HIT
# check (below) keeps running in parallel regardless, so this can only
# ever ADD protection on top of it, same principle as the original SL-M
# design - never a replacement.
#
# A genuinely new risk this introduces that SL-M's failure mode never
# did: SL-L can PARTIALLY fill (some qty at the limit price, remainder
# still resting) if price only briefly touches the limit band. If a
# DIFFERENT exit condition (e.g. TARGET_HIT) then fires via the normal
# reactive path while that remainder is still resting,
# trading_engine._exit_position's own stale-pending-order cancel (pre-
# existing, built for the unrelated BHARATFORG incident 26 Aug 2026)
# now ALSO re-derives the real broker net quantity via get_broker_net_
# quantity right after cancelling it, and sells exactly that instead of
# blindly trusting the stored Position.quantity - without this, a
# partial fill could make the bot try to sell MORE than it actually
# still holds, risking an accidental naked short (exactly the mistake
# made, caught, and fixed in the controlled live test above). See that
# function's own comments for the exact handling, including the fully-
# filled-during-the-race case (broker shows 0 qty left - reconciled as
# closed directly, no fresh SELL placed at all).
#
# Kept OFF by default (both this flag and BROKER_STOP_LOSS_LIMIT_BUFFER_
# PCT below) until live-tested and confirmed working end-to-end - a
# second broker-side stop mechanism failing silently is exactly the
# failure mode this whole investigation started from, so this one earns
# trust via an actual controlled live test before ever running against
# a real production entry, not by assumption. The existing _exit_
# reason_for MAX_LOSS_HIT check (poll/tick-driven, ~2s worst case via
# monitor_loop + synchronous on_price_tick on every real market tick)
# plus LIQUIDITY_GUARD_ENABLED (exits early on a thinly-traded option
# going quiet - the actual precursor pattern behind the CHOLAFIN-style
# overshoot-via-gap case this feature exists to backstop) and LOSS_
# REPEAT_BLOCK_ENABLED remain the baseline protection stack either way.
# NOTE: this is a different F&O instrument (options) than Swing's own
# uncommitted broker-stop-loss work, which targets FUTURES legs via the
# (still valid there) SL-M primitive - that has NOT been shown broken by
# any of this and should be independently verified before trusting it,
# not assumed safe or assumed broken either way.
BROKER_STOP_LOSS_ENABLED = os.getenv("LUXURY_BROKER_STOP_LOSS_ENABLED", "false").lower() == "true"

# The gap between the SL-L order's trigger_price and its limit_price, as
# a fraction of trigger_price (limit_price = trigger_price * (1 -
# this)). This is the real dial controlling the SL-L tradeoff described
# above: too TIGHT (small) and a fast/illiquid move can blow straight
# through the limit band and leave the order unfilled, exactly the
# scenario Dhan's own docs warn about; too WIDE (large) and a fill,
# if it happens, could land meaningfully worse than the intended
# max-loss cap, weakening the whole point of the order. 0.03 (3%) is a
# starting value in the same scale as this file's own STOP_LOSS_PCT
# (0.03) and DYNAMIC_SL_STEP_PCT_CE/_PE (0.07) - not yet tuned against
# real fill data, since this feature hasn't run live yet (see
# BROKER_STOP_LOSS_ENABLED's own docstring).
BROKER_STOP_LOSS_LIMIT_BUFFER_PCT = float(os.getenv("LUXURY_BROKER_STOP_LOSS_LIMIT_BUFFER_PCT", "0.03"))

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

# See Options/config.py's ENABLE_TARGET_EXIT - master switch for the fixed
# +TARGET_PCT profit exit. This package's own independently-tunable copy;
# left ON (Luxury keeps the fixed target, only Futures turns it off).
ENABLE_TARGET_EXIT = os.getenv("LUXURY_ENABLE_TARGET_EXIT", "true").lower() == "true"

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

# See Options/config.py's PROFIT_PROTECTION_GIVEBACK_PCT - the profit-
# protection give-back buffer (added 10 Sep 2026 after OIL exited on a
# 10-paise dip from the peak). Once peak profit crosses the threshold
# above, the exit only fires when ltp < highest_price * (1 - this pct).
# 0.0 default = bit-identical to the old zero-tolerance behaviour.
PROFIT_PROTECTION_GIVEBACK_PCT = float(os.getenv("LUXURY_PROFIT_PROTECTION_GIVEBACK_PCT", "0.0"))

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

# See Options/config.py's ENABLE_EMA_CROSS_EXIT - per-package toggle for the
# EMA-cross exit. Kept here so the trading_engine.py copies stay identical;
# left OFF for Luxury (only Futures runs it).
ENABLE_EMA_CROSS_EXIT = os.getenv("LUXURY_ENABLE_EMA_CROSS_EXIT", "false").lower() == "true"
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

# See Options/config.py's ENABLE_TRADING_WINDOWS / TRADING_WINDOWS - the
# multi-window entry schedule (added 10 Sep 2026). Supersedes the single-
# cutoff pair above when on. Only gates NEW entries; SQUARE_OFF_TIME still
# force-closes at 15:15 regardless.
ENABLE_TRADING_WINDOWS = os.getenv("LUXURY_ENABLE_TRADING_WINDOWS", "false").lower() == "true"
TRADING_WINDOWS = os.getenv("LUXURY_TRADING_WINDOWS", "09:15-11:00,14:00-15:28")

# See Options/config.py's "Nifty50 open gap-down / sharp-fall CE cool-off"
# block - the actual gap/fall computation is shared (always reads
# GAP_DOWN_THRESHOLD_POINTS/GAP_DOWN_SHARP_FALL_PCT/GAP_DOWN_CE_DELAY_MINUTES
# from Options.config, one market-wide fact). This is just this package's
# own independently-tunable on/off switch for honoring it.
ENABLE_GAP_DOWN_CE_DELAY = os.getenv("LUXURY_ENABLE_GAP_DOWN_CE_DELAY", "true").lower() == "true"

# See Options/config.py's MONITOR_INTERVAL_SECONDS - LTP_STALE_AFTER_SECONDS
# lives only in Options/config.py since it governs the one shared
# dhan_client LTP cache all packages read from - no separate Luxury copy
# needed.
MONITOR_INTERVAL_SECONDS = int(os.getenv("LUXURY_MONITOR_INTERVAL_SECONDS", "2"))

LOT_SIZE_FALLBACK = int(os.getenv("LUXURY_LOT_SIZE_FALLBACK", "1"))
ORDER_TAG_PREFIX = os.getenv("LUXURY_ORDER_TAG_PREFIX", "Lux")
