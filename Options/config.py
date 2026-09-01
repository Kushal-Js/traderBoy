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
# Raised 3->4 (user request 27 Aug 2026), alongside MAX_LIVE_POSITIONS_CE
# also raised 2->4 - so a 4-stock alert can now fill all 4 CE slots in one
# shot instead of only the first 3 (previously the 4th-ranked candidate in
# a same-size alert was never even considered). See NOTES.md's design-
# decision entry for the DELHIVERY capacity-cap investigation that
# prompted this.
TOP_N_STOCKS = int(os.getenv("TOP_N_STOCKS", "4"))

# When true (default, set by user request 26 Aug 2026), rank_and_pick_top_
# stocks() selects the BOTTOM TOP_N_STOCKS of the ranked list instead of the
# top - for CE (ranked strongest %change first) this means the weakest
# gainers among the alerted list (possibly even flat/negative names), and
# for PE (ranked biggest decliners first) the weakest decliners (possibly
# even flat/positive names) - a contrarian/laggard bet that the weakest
# confirmers of the alert's own direction have more room to catch up,
# rather than chasing the names that already moved the most (which showed
# a pattern of sharp reversals right after entry earlier the same day -
# see NOTES.md's design-decision entry). Set false to restore the original
# top-N/strongest-mover selection. Only changes anything when an alert
# ranks MORE than TOP_N_STOCKS candidates - with 3 or fewer, top-N and
# bottom-N are the same slice.
SELECT_BOTTOM_N_STOCKS = os.getenv("SELECT_BOTTOM_N_STOCKS", "true").lower() == "true"
# Separate caps per option type - CE (from /chartink/webhook) and PE (from
# /chartink/webhook-sell) each get their own budget rather than sharing one
# pool, so a run of bearish alerts can't crowd out capacity for bullish
# ones or vice versa. A symbol already open/in-flight as either type still
# blocks a new entry of the *other* type for that same symbol - see
# PositionStore.reserve_symbol().
# CE raised 2->4->3, PE lowered 2->0 (user request 27 Aug 2026, CE lowered
# 4->3 30 Aug 2026 alongside the matching Futures change) - PE (bearish
# scan, /chartink/webhook-sell) was fully OFF from 27 Aug until 31 Aug 2026:
# reserve_symbol()'s capacity check (current >= _cap_for("PE")) was 0 >= 0
# on the very first attempt, so every PE alert was rejected immediately -
# option_main.py's early "no capacity left" bail-out (before ranking even
# runs) also caught this via remaining_capacity()'s max(0, cap - current).
# PE re-enabled at 2 (user request 31 Aug 2026), CE lowered 3->2 in the same
# change to keep combined CE+PE exposure in line with the prior CE=3/PE=0
# total. No code changes needed for either direction - both already handle
# any cap value correctly, this is a pure config change. See NOTES.md's
# design-decision entry.
MAX_LIVE_POSITIONS_CE = int(os.getenv("MAX_LIVE_POSITIONS_CE", "2"))
MAX_LIVE_POSITIONS_PE = int(os.getenv("MAX_LIVE_POSITIONS_PE", "2"))

# Daily re-entry cap, added 1 Sep 2026 (user request: "only allow entry
# into same trade max 3 times a day for Luxury, Options and Future
# package") - independent of MAX_LIVE_POSITIONS_CE/_PE above (that caps
# how many can be LIVE at once; this caps how many times the SAME
# underlying can be entered across the whole day, even after each prior
# entry has already been exited). Counted per underlying_symbol
# regardless of CE/PE ("same trade" = same underlying), via
# trade_history.count_opened_today() - see its own docstring for why
# that's backed by the durable on-disk opened-position log rather than an
# in-memory counter (survives a mid-day restart, unlike every other
# per-day counter in position_store.py).
MAX_DAILY_ENTRIES_PER_SYMBOL = int(os.getenv("MAX_DAILY_ENTRIES_PER_SYMBOL", "3"))

# /chartink/webhook-papertrade (paper_webhook.py) - a second, independent
# position pool for evaluating a new Chartink scan before trusting it with
# real money. Deliberately separate from TOP_N_STOCKS/MAX_LIVE_POSITIONS_CE
# so a burst of alerts on this scan can't starve the real strategy's
# capacity, or vice versa.
PAPERTRADE_TOP_N_STOCKS = int(os.getenv("PAPERTRADE_TOP_N_STOCKS", "3"))
PAPERTRADE_MAX_POSITIONS = int(os.getenv("PAPERTRADE_MAX_POSITIONS", "2"))

TARGET_PCT = float(os.getenv("TARGET_PCT", "0.10"))          # +10% target
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.03"))    # -3% hard stop loss

# Absolute per-trade rupee-loss cap, independent of STOP_LOSS_PCT above - a
# large-quantity position can still lose more than this in rupee terms
# before its percentage stop-loss fires (e.g. a low-premium, high-lot-size
# contract). Checked first in _exit_reason_for(), ahead of every other exit
# condition - a hard risk ceiling on any single trade, applies identically
# to CE and PE since both are long-premium positions (loss = (entry_price -
# ltp) * quantity either way).
#
# FLAT value, same for every trade regardless of re-entry count - a 1.75x-
# per-re-entry escalation (capped at 3x) was tried and deployed 27 Aug 2026
# (prompted by ADANIPOWER/LICHSGFIN re-entering repeatedly the same
# morning), then explicitly REVERTED the same day by user request in favor
# of this simple fixed value - the escalation logic (Position.
# max_loss_override_rs, PositionStore.get_max_loss_cap_for, the
# consecutive-MAX_LOSS_HIT counter) no longer exists in the codebase at
# all. See NOTES.md's design-decision entries for both the original
# feature and the revert.
#
# Lowered 1500->1200 (user request 27 Aug 2026) after a backtest against
# 02 Kaashvi.csv comparing 1500 vs 1000: 1000 cut losers faster/cheaper but
# with a materially lower win rate (56.5%->52.2%) and ~50% more MAX_LOSS_HIT
# stop-outs (49->72) for only a small P&L edge (+3,296 over 4 days). 1200 is
# the user's chosen middle ground. See NOTES.md's design-decision entry.
#
# Split into a before/after-cutoff pair (user request 31 Aug 2026) - looser
# (1200) for the first part of the session, tighter (1000) after
# RISK_THRESHOLD_CUTOFF_TIME - the idea being a trade that's been open into
# the afternoon gets less rope before the hard rupee-loss cap kicks in.
# Independent of ALLOWED_TRADING_TIME (which only gates NEW entries) even
# though both default to the same 11:30 - this governs the EXIT check on
# positions already open, regardless of when they were entered. See
# trading_engine.current_max_loss_per_trade_rs().
MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF = float(os.getenv("MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF", "1200"))
MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF = float(os.getenv("MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF", "1000"))

# Absolute per-trade rupee profit-protection threshold, added 26 Aug 2026 by
# user request - the mirror image of MAX_LOSS_PER_TRADE_RS above, but on the
# upside. Once a trade's PEAK unrealized profit ((highest_price -
# entry_price) * quantity - highest_price is already tracked for the
# trailing-SL mechanism below, reused here rather than a new field) exceeds
# this, "protection" is armed: deliberately the SIMPLE version requested -
# no drawdown tolerance once armed, exit the moment price is off that peak
# at all (ltp < highest_price), rather than waiting for a percentage-based
# floor to be breached. Checked in _exit_reason_for() after TARGET_HIT (a
# full target hit is a strictly better outcome and takes priority) but
# before the percentage-based trailing/hard stop-loss. Applies identically
# to CE and PE for the same reason MAX_LOSS_PER_TRADE_RS does.
#
# Split into a before/after-cutoff pair the same way and for the same
# reason as MAX_LOSS_PER_TRADE_RS above (user request 31 Aug 2026) - locks
# in profit sooner (1000 instead of 1500) once RISK_THRESHOLD_CUTOFF_TIME
# has passed. See trading_engine.current_profit_protection_threshold_rs().
PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF = float(os.getenv("PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF", "1500"))
PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF = float(os.getenv("PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF", "1000"))

# The time-of-day boundary the two before/after-cutoff pairs above switch
# on - user request 31 Aug 2026 ("before 11:30 AM" / "after 11:30").
# Deliberately its own separate setting from ALLOWED_TRADING_TIME even
# though both default to "11:30" - see the comments above for why these
# are independent concepts that could diverge later.
RISK_THRESHOLD_CUTOFF_TIME = os.getenv("RISK_THRESHOLD_CUTOFF_TIME", "11:30")

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
# Moved to 1-min by user request 26 Aug 2026 (was 5), then moved BACK to
# 5-min by user request 27 Aug 2026 - the same day both values were first
# changed. No re-validation data was gathered at 1-min before reverting;
# this restores the original, actually-backtested 5-min/5-min pairing
# (see SUPERTREND_ENTRY_GRACE_MINUTES's docstring below).
SUPERTREND_INTERVAL_MINUTES = int(os.getenv("SUPERTREND_INTERVAL_MINUTES", "5"))
# How long a cached Supertrend signal is reused before re-fetching candles -
# doesn't need to track candle closes exactly (the poll loop refreshes it
# every tick anyway, this just caps REST call frequency).
#
# Lowered 60->15 (user request 27 Aug 2026) for responsiveness - a 5-min
# candle can close and the bot wouldn't notice for up to a full 60s
# afterwards under the old value, in tension with the same day's "no
# waiting, immediate action" Supertrend-exit request. 15s caps that worst-
# case detection lag at 15s instead. Cheap to lower: this REST call is on
# the UNDERLYING stock (not the option), independently rate-limited per
# underlying by this same value, so even at MAX_LIVE_POSITIONS_CE=4 worst
# case is 4 calls/15s (~0.27/s) - far under Dhan's undocumented rate limit
# (see NOTES.md bug #5).
SUPERTREND_REFRESH_SECONDS = int(os.getenv("SUPERTREND_REFRESH_SECONDS", "15"))

# REMOVED (user request 27 Aug 2026): there used to be two extra "waiting"
# knobs here - SUPERTREND_ENTRY_GRACE_MINUTES (extra minutes past the
# entry candle before honoring a reversal) and SUPERTREND_MIN_WARMUP_CANDLES
# (a minimum-candles-since-open gate before ANY signal was trusted at all,
# the bug #10/#16 fix). Both are gone entirely, not just set to 0 - the
# user wants immediate action the instant a reversal signal is read, no
# tuned delay of any kind. The ONE thing deliberately kept (explicit user
# confirmation): trading_engine._supertrend_signal_for() still never acts
# on the exact same candle a position was entered on - see that function's
# own docstring for the real live bug ("cutting winning trades flat at
# breakeven the instant they were entered") this specific check prevents.
# Everything past that one candle now triggers immediately - see NOTES.md's
# design-decision entry for the full history of what this setting used to
# be tuned to, in case a future regression suggests reintroducing some
# form of it.

# Default ATM leg for /chartink/webhook (the bullish scan) and the
# fallback used when reconciling a broker position of unknown origin.
# /chartink/webhook-sell (bearish scan) always buys PE regardless of this -
# see main.py's two webhook handlers.
OPTION_TYPE = os.getenv("OPTION_TYPE", "CE").upper()

QUANTITY_LOTS = int(os.getenv("QUANTITY_LOTS", "1"))  # number of lots per leg

# Proactive funds check (added 1 Sep 2026, user request: "create 2 funds
# buckets, primary - 85% of total fund, secondary - 15% of total fund...
# Secondary bucket to be used for Options, Futures or Luxury trades
# only"). Before placing any real order, checks the required margin
# (Dhan's own /margincalculator) against this package's own SECONDARY
# bucket share of the account's real available balance (see
# fund_allocation.py's own module docstring for the full 2-bucket
# design) - never the whole account's own residual balance, so an
# Options entry can't eat into capital the "primary" bucket reserves
# for Swing. The broker's own reactive RMS rejection remains the final
# safety net regardless of this flag - it controls only this proactive
# check.
FUNDS_CHECK_ENABLED = os.getenv("FUNDS_CHECK_ENABLED", "true").lower() == "true"

# Order product for options: "MIS" = intraday (auto square-off by broker as
# a safety net; we still explicitly square off ourselves at SQUARE_OFF_TIME
# when ENABLE_SQUARE_OFF is on). "MARGIN" is Dhan-Tradehull's code for what's
# commonly called NRML/carry-forward - no broker-side auto square-off, and a
# position can survive past market close into the next session. Changed to
# "MARGIN" by user request 25 Aug 2026 alongside ENABLE_SQUARE_OFF=false
# below - see NOTES.md's design-decision entry on NRML/overnight carry for
# the real risk this introduces (no exit protection while the market is
# shut - a position is exposed to the full overnight gap with zero
# automated response). "NRML" itself is NOT a value Tradehull accepts here -
# its order_placement() only recognizes MIS/MARGIN/MTF/CO/BO/CNC.
OPTIONS_PRODUCT = os.getenv("OPTIONS_PRODUCT", "MIS")

DEFAULT_EXCHANGE = "NFO"  # Dhan-Tradehull's exchange code for NSE F&O

# ---------------------------------------------------------------------------
# Timing (all times are IST / Asia-Kolkata)
# ---------------------------------------------------------------------------
MARKET_TZ = "Asia/Kolkata"
MARKET_OPEN_TIME = "09:15"
SQUARE_OFF_TIME = os.getenv("SQUARE_OFF_TIME", "15:15")

# Master on/off switch for the automatic end-of-day square-off, separate
# from the SQUARE_OFF_TIME value itself. When true (default), monitor_loop
# force-closes every live position at SQUARE_OFF_TIME and is_past_square_off_
# time() blocks new entries past that point - the behavior that has always
# existed. When false, NEITHER of those happens - a position rides past
# market close and keeps being evaluated (target/stop-loss/Supertrend/
# MAX_LOSS_HIT) once the next session's ticks resume, letting a trade
# genuinely continue into the next trading day (paired with
# OPTIONS_PRODUCT=MARGIN above, since MIS carries an implicit same-day-only
# assumption). Set false by user request 25 Aug 2026 - see NOTES.md's
# design-decision entry for the overnight gap-risk this introduces (no exit
# protection while the market is shut) and PositionStore.maybe_reset_for_
# new_day's matching change (a day-boundary reset must NOT clear live
# positions in this mode, or a real overnight position would be silently
# orphaned from all future monitoring).
ENABLE_SQUARE_OFF = os.getenv("ENABLE_SQUARE_OFF", "true").lower() == "true"

# Friday-specific carve-out, applies REGARDLESS of ENABLE_SQUARE_OFF above -
# even when weekday carry-forward is on (ENABLE_SQUARE_OFF=false), a
# position still must not be carried into the WEEKEND, a much longer and
# riskier gap than a single weeknight (confirmed live in a 25 Aug 2026
# backtest: a position carried Thu->Mon with no data in between took a
# materially worse exit than it would have with same-day protection - see
# NOTES.md's design-decision entry). When true (default) and today is
# Friday, both is_past_square_off_time() (blocks new entries) and
# monitor_loop's force-close trigger switch to FRIDAY_SQUARE_OFF_TIME
# instead of the normal SQUARE_OFF_TIME/ENABLE_SQUARE_OFF logic. Has no
# effect Monday-Thursday, and no effect at all if ENABLE_SQUARE_OFF is
# already true (that already covers every day, Friday included).
ENABLE_FRIDAY_SQUARE_OFF = os.getenv("ENABLE_FRIDAY_SQUARE_OFF", "true").lower() == "true"
FRIDAY_SQUARE_OFF_TIME = os.getenv("FRIDAY_SQUARE_OFF_TIME", "15:20")

# Restricts NEW entries to before a cutoff time - independent of
# SQUARE_OFF_TIME above, which governs closing EXISTING positions, not
# opening new ones. When false (default), new entries are allowed all day
# up to market hours/SQUARE_OFF_TIME, same as before this flag existed.
# When true, no new entry is opened once ALLOWED_TRADING_TIME has passed -
# already-open positions are unaffected either way and keep full
# target/SL/Supertrend/square-off monitoring regardless of this flag; it
# only gates new entries (see option_main.py's webhook handler).
ENABLE_TRADING_TIME_LIMIT = os.getenv("ENABLE_TRADING_TIME_LIMIT", "false").lower() == "true"
ALLOWED_TRADING_TIME = os.getenv("ALLOWED_TRADING_TIME", "11:30")
MARKET_CLOSE_TIME = "15:30"

# Lowered 5->2 (user request 27 Aug 2026) for a tighter fallback-heartbeat
# check on live positions. Doesn't scale REST call volume on its own -
# refresh_supertrend_signal has its own independent SUPERTREND_REFRESH_SECONDS
# cache floor, and _get_ltp() prefers the WebSocket-cached LTP (no I/O) for
# any position ticking normally. See NOTES.md's design-decision entry for
# why this alone doesn't fix stale-LTP overshoot - LTP_STALE_AFTER_SECONDS
# below is what actually addresses that.
MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "2"))

# How old a WebSocket-cached option LTP is allowed to get before
# get_cached_option_ltp() treats it as a miss and forces dhan_client._get_ltp
# to REST-refetch instead of trusting a possibly-ancient tick indefinitely.
# Added 27 Aug 2026 after a real overshoot: SAGILITY's MAX_LOSS_HIT exit on
# 28 Aug 2026 realized -Rs.1,800 against a Rs.1,200 cap because the cached
# tick was stale for ~2 minutes (SAGILITY is thin - real trades printing on
# that option contract are sparse) and nothing forced a fresh check in the
# meantime. See Options/dhan_client.py's get_cached_option_ltp/note_rest_ltp
# docstrings for the full mechanism, including how a REST refetch re-primes
# the cache so a persistently-quiet option isn't hammered with a REST call
# on every single poll (rate-limit safety - NOTES.md bug #5).
LTP_STALE_AFTER_SECONDS = float(os.getenv("LTP_STALE_AFTER_SECONDS", "5"))

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
LOT_SIZE_FALLBACK = int(os.getenv("LOT_SIZE_FALLBACK", "1"))
ORDER_TAG_PREFIX = os.getenv("ORDER_TAG_PREFIX", "Cti")  # correlation id prefix
