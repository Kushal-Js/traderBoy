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

# Same-day RSI-gated loss re-entry block (added 11 Sep 2026, REPLACING
# the old time-based LOSS_COOLDOWN_ENABLED/LOSS_COOLDOWN_MINUTES pair
# that used to live here - user request: "Remove this cooldown period
# logic from everywhere and all strategies, instead create another
# global common function which checks if RSI of 5 min candle is greater
# than number 88 or if RSI of current candle is lesser than previous 5
# min candle (means RSI is falling), then don't take a trade for that
# stock in same day if MAX_LOSS_HIT is already hit earlier for that
# day." The trigger for a fixed-minutes wait was found to be too blunt
# in practice - a real INDUSTOWER alert on 11 Sep 2026 was skipped at the
# 19-minute mark of a 20-minute cooldown, one minute short, regardless of
# whether the stock had actually recovered. This checks the ACTUAL
# market state instead of a clock: RSI(RSI_LOSS_REENTRY_PERIOD) on
# RSI_LOSS_REENTRY_INTERVAL_MINUTES-min candles, evaluated fresh at every
# new entry attempt (not latched once and held for the rest of the day) -
# if the stock already stopped THIS strategy out today via MAX_LOSS_HIT
# (trade_history.loss_exit_count_today, same durable real_trades log as
# before) AND its RSI is either overbought (> RSI_LOSS_REENTRY_OVERBOUGHT)
# or still falling (current 5-min RSI < the previous 5-min RSI), the
# stock hasn't shown genuine recovery yet, so the re-entry is skipped;
# once RSI is neither overbought nor falling, it's allowed through again
# the same day. See Options/dhan_client.py's refresh_rsi_signal/
# get_cached_rsi for the shared computation (one implementation, used by
# Options/Futures/Luxury alike, same as Supertrend/EMA-cross above) and
# Options/trading_engine.py's _process_one_entry for where the two
# pieces (loss-today + RSI condition) are combined.
RSI_LOSS_REENTRY_PERIOD = int(os.getenv("RSI_LOSS_REENTRY_PERIOD", "14"))
RSI_LOSS_REENTRY_INTERVAL_MINUTES = int(os.getenv("RSI_LOSS_REENTRY_INTERVAL_MINUTES", "5"))
RSI_LOSS_REENTRY_OVERBOUGHT = float(os.getenv("RSI_LOSS_REENTRY_OVERBOUGHT", "88"))
RSI_LOSS_REENTRY_REFRESH_SECONDS = int(os.getenv("RSI_LOSS_REENTRY_REFRESH_SECONDS", "15"))

# Per-package on/off switch (Options' own copy - unprefixed, matching this
# package's other flags). Futures/Luxury have their own FUTURES_/LUXURY_
# prefixed copies in their own config.py, all defaulting to "true".
ENABLE_RSI_LOSS_REENTRY_BLOCK = os.getenv("ENABLE_RSI_LOSS_REENTRY_BLOCK", "true").lower() == "true"

# Repeat-loss same-day block - ported from Luxury/config.py's own LOSS_
# REPEAT_BLOCK_ENABLED (added there 8 Sep 2026, ported here 10 Sep 2026,
# same "similar rule set and guard rails" request). Distinct from both
# guards above: MAX_DAILY_ENTRIES_PER_SYMBOL is a flat COUNT cap
# regardless of outcome (wins count too); ENABLE_RSI_LOSS_REENTRY_BLOCK
# is a CONDITION gate re-checked on every attempt (RSI recovering re-
# opens the symbol the same day). This one is a same-day OUTCOME-COUNTING
# block: once a symbol has closed on a genuine loss-
# designated exit (MAX_LOSS_HIT/STOP_LOSS_HIT specifically, not every
# trade that happened to close a little negative for some other reason
# like SUPERTREND_EXIT/TRAILING_SL_HIT) LOSS_REPEAT_BLOCK_COUNT times
# today, it's done for the SYMBOL for the rest of the day, no more
# re-entries regardless of how much time has passed. Backed by trade_
# history.loss_exit_count_today() - the same durable, restart-surviving
# real_trades log every other daily count/cooldown here reads.
LOSS_REPEAT_BLOCK_ENABLED = os.getenv("LOSS_REPEAT_BLOCK_ENABLED", "true").lower() == "true"
LOSS_REPEAT_BLOCK_COUNT = int(os.getenv("LOSS_REPEAT_BLOCK_COUNT", "2"))
LOSS_REPEAT_BLOCK_EXIT_REASONS = ("MAX_LOSS_HIT", "STOP_LOSS_HIT")

# Real broker-side SELL STOP-LOSS LIMIT (SL-L) order - ported from
# Luxury/config.py's own BROKER_STOP_LOSS_ENABLED (see that file's own
# docstring for the full SL-M->SL-L story: NSE bans SL-M for index/stock
# options exchange-wide since Sep 2021, SL-L is the only broker-side
# conditional stop still permitted, confirmed working via a controlled
# live test 9 Sep 2026 - see NOTES.md entries #99/#100). Placed
# immediately after every entry via the shared Options/dhan_client.py:
# place_stop_loss_limit_order (used by Luxury too - proven in
# production there), triggered at the rupee-equivalent price of
# current_max_loss_per_trade_rs(). Additional, faster backstop on top
# of the existing poll/tick-driven MAX_LOSS_HIT check - never a
# replacement, and a placement failure never blocks the entry itself.
#
# Kept OFF by default here (unlike LOSS_COOLDOWN/LOSS_REPEAT_BLOCK/
# LIQUIDITY_GUARD above, which introduce no new order-placement risk) -
# the underlying SL-L mechanism is already proven correct via Luxury's
# own live tests, but Options' own entry flow calling it for the first
# time is new code that hasn't itself been exercised against a real
# order yet. Same rollout discipline as Luxury's own original launch:
# earn trust via an actual controlled live test before running against
# a real production entry, not by assumption.
BROKER_STOP_LOSS_ENABLED = os.getenv("BROKER_STOP_LOSS_ENABLED", "false").lower() == "true"

# RETIRED 12 Sep 2026 - trading_engine.py no longer reads this. Was a flat
# % of trigger_price (limit_price = trigger_price * (1 - this)), untied to
# MAX_LOSS_HIT's own rupee size - user feedback: "this has to be in sync
# with MAX_LOSS_HIT". Replaced by BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE
# below. Left defined (unused) rather than deleted, matching this file's
# own convention for retired flags - see RSI_LOSS_REENTRY_* above for the
# same pattern when LOSS_COOLDOWN_MINUTES was replaced.
BROKER_STOP_LOSS_LIMIT_BUFFER_PCT = float(os.getenv("BROKER_STOP_LOSS_LIMIT_BUFFER_PCT", "0.03"))

# Real broker-side SL-L limit gap, IN RUPEES, computed per-trade from the
# same current_max_loss_per_trade_rs() used for trigger_price itself -
# added 12 Sep 2026 (user request: "the gap value actually comes around
# MAX_LOSS_HIT value... that's what I mean by in sync"). At entry:
#   trigger_price = fill_price - (current_max_loss_per_trade_rs() / qty)
#   limit_price   = trigger_price - (current_max_loss_per_trade_rs()
#                                     * BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE / qty)
# so with the default 1.0, the SL-L order's own fillable price band
# (trigger down to limit) is exactly as wide, in rupees, as the MAX_LOSS_HIT
# cap itself - if it fills anywhere in that band, the worst realistic
# outcome is roughly 2x the cap, never an unbounded/unrelated-to-cap
# amount the way a fixed 3%-of-price band could be (a few paise on a
# cheap option, or a huge rupee swing on an expensive one, regardless of
# what the cap actually is). Set below 1.0 for a tighter band (higher
# chance of no fill on a violent gap, but a better floor if it does fill)
# or above 1.0 for a wider one (more likely to fill, worse worst-case).
BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE = float(os.getenv("BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE", "1.0"))

# Liquidity guard master switch - ported from Luxury/config.py's own
# LIQUIDITY_GUARD_ENABLED (added there 2 Sep 2026 after the real
# CHOLAFIN MAX_LOSS_HIT overshoot investigation - see that file's own
# docstring for the full incident/tuning history). The shared numeric
# parameters (LIQUIDITY_GUARD_ZERO_VOLUME_BARS/_REFRESH_SECONDS) already
# live below in this same file since dhan_client.refresh_liquidity_
# signal()/get_cached_illiquid() are shared, global functions - this is
# just the on/off gate for whether OPTIONS' OWN _exit_reason_for acts on
# that shared signal, ported 10 Sep 2026 as part of the same "similar
# guard rails as Luxury" request.
LIQUIDITY_GUARD_ENABLED = os.getenv("LIQUIDITY_GUARD_ENABLED", "true").lower() == "true"

# /chartink/webhook-papertrade (paper_webhook.py) - a second, independent
# position pool for evaluating a new Chartink scan before trusting it with
# real money. Deliberately separate from TOP_N_STOCKS/MAX_LIVE_POSITIONS_CE
# so a burst of alerts on this scan can't starve the real strategy's
# capacity, or vice versa.
PAPERTRADE_TOP_N_STOCKS = int(os.getenv("PAPERTRADE_TOP_N_STOCKS", "3"))
PAPERTRADE_MAX_POSITIONS = int(os.getenv("PAPERTRADE_MAX_POSITIONS", "2"))

TARGET_PCT = float(os.getenv("TARGET_PCT", "0.10"))          # +10% target
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.03"))    # -3% hard stop loss

# Master switch for the fixed +TARGET_PCT profit exit in _exit_reason_for().
# Default on = original behaviour. When off, a winning position is no longer
# closed the instant it reaches entry * (1 + TARGET_PCT); it rides until
# PROFIT_PROTECTION_HIT (peak-profit give-back), the trailing/dynamic/hard
# SL, SUPERTREND_EXIT, the liquidity guard, or the EOD square-off take it
# instead. target_price is still computed and stored (reconciliation/display
# use it) - this only stops it being an exit trigger. Introduced 10 Sep 2026
# for Futures (FUTURES_ENABLE_TARGET_EXIT=false) to let futures winners run;
# Options and Luxury keep it on.
ENABLE_TARGET_EXIT = os.getenv("ENABLE_TARGET_EXIT", "true").lower() == "true"

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

# Master on/off switch for the MAX_LOSS_HIT exit specifically BEFORE
# RISK_THRESHOLD_CUTOFF_TIME (added 11 Sep 2026, user request: "disable
# MAX_LOSS_HIT for before 11:30 and add exit conditions as below: EMA 9
# of 5 min close crossed below EMA 12 of 5 min close or 5 min close
# crossed below 5 min supertrend" - i.e. rely on trend-reversal exits
# (Supertrend/EMA-cross below) rather than a hard rupee cap during the
# more volatile first part of the session, and only start enforcing
# MAX_LOSS_HIT from the cutoff onward). Default False = the requested
# behavior (no MAX_LOSS_HIT before cutoff at all, at any loss amount);
# set True to restore the original always-on behavior. Only this ONE
# exit is affected - TARGET_HIT/PROFIT_PROTECTION_HIT/the percentage
# TRAILING_SL_HIT-STOP_LOSS_HIT/SUPERTREND_EXIT/EMA_CROSS_EXIT/
# LIQUIDITY_GUARD all still apply before cutoff exactly as before. Note
# this genuinely widens the morning downside on a single trade: with
# this off, the effective floor before cutoff becomes whatever the
# percentage stop-loss (STOP_LOSS_PCT, dynamic-SL-adjusted) or a trend-
# reversal signal catches first - which, on a large-quantity/low-premium
# position, can be a materially bigger rupee loss than the
# MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF cap this replaces. See
# trading_engine._exit_reason_for()'s own comment for exactly where this
# is checked.
ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF = os.getenv("ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF", "false").lower() == "true"

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

# Give-back buffer for the profit-protection exit (added 10 Sep 2026, user
# request after OIL: PROFIT_PROTECTION_HIT fired on a 10-paise dip from the
# peak - zero drawdown tolerance clips a trade that's still trending). Once
# peak profit has crossed the threshold above, the exit now only fires when
# price has retraced at least this FRACTION OF THE PEAK PRICE, i.e.
# ltp < highest_price * (1 - PROFIT_PROTECTION_GIVEBACK_PCT), instead of the
# old ltp < highest_price. 0.0 (the default) is bit-identical to the old
# behaviour (highest_price * 1.0 == highest_price); 0.05 = "let it wiggle
# 5% off the peak before locking in". Independent of the trailing/dynamic
# SL, which still runs after this and catches a bigger reversal. Tune via
# backtest against real PROFIT_PROTECTION_HIT trades before raising it.
PROFIT_PROTECTION_GIVEBACK_PCT = float(os.getenv("PROFIT_PROTECTION_GIVEBACK_PCT", "0.0"))

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

# EMA-cross exit signal (added 10 Sep 2026 for Futures - see each package's
# own ENABLE_EMA_CROSS_EXIT flag). Shared, global computation parameters for
# dhan_client.refresh_ema_cross_signal(), living here (not per-package) for
# the same reason SUPERTREND_PERIOD/MULTIPLIER/INTERVAL do - the fetch/compute
# runs inside the one shared dhan_wrapper, which binds to this config. Only
# the per-package ENABLE_EMA_CROSS_EXIT toggle is independent. Exit fires
# when the fast EMA of the 5-min close crosses BELOW the slow EMA (for a CE;
# the reverse for a PE), on a candle later than the position's entry candle.
EMA_CROSS_FAST_PERIOD = int(os.getenv("EMA_CROSS_FAST_PERIOD", "9"))
EMA_CROSS_SLOW_PERIOD = int(os.getenv("EMA_CROSS_SLOW_PERIOD", "12"))
EMA_CROSS_INTERVAL_MINUTES = int(os.getenv("EMA_CROSS_INTERVAL_MINUTES", "5"))
EMA_CROSS_REFRESH_SECONDS = int(os.getenv("EMA_CROSS_REFRESH_SECONDS", "15"))

# ---------------------------------------------------------------------------
# Continuous intraday history (added 10 Sep 2026, user request: "no lag
# across ALL strategies - calculations run continuously across sessions,
# not with a fresh day start like a charting platform"). Every intraday
# indicator fetch (Supertrend, EMA cross, the liquidity guard, Swing's
# intraday Supertrend, the paper engines) pulls this many CALENDAR days of
# history THROUGH today rather than today-only, so recursive indicators
# (Supertrend/EMA/RSI/ATR) are fully seeded from the first bar of the
# session instead of needing ~an hour of fresh candles to warm up. Read by
# dhan_wrapper.fetch_continuous_intraday(), which every fetch site now
# routes through. 7 calendar days always covers >=4 trading sessions even
# across a long weekend - hundreds of 5-min / thousands of 1-min bars,
# far more than any period-10..14 indicator needs to be stable.
INTRADAY_CONTINUOUS_LOOKBACK_DAYS = int(os.getenv("INTRADAY_CONTINUOUS_LOOKBACK_DAYS", "7"))

# Per-package on/off for the EMA-cross exit above. Kept in all three option
# packages so the Options/Futures trading_engine.py copies stay byte-
# identical; default off, only FUTURES_ENABLE_EMA_CROSS_EXIT is turned on
# (user request 10 Sep 2026: "add EMA 9 crossed below EMA 12 of 5-min close
# as an exit for Futures").
ENABLE_EMA_CROSS_EXIT = os.getenv("ENABLE_EMA_CROSS_EXIT", "false").lower() == "true"

# Liquidity guard (added 2 Sep 2026, user's own corrective-action request
# after investigating a real CHOLAFIN MAX_LOSS_HIT overshoot 3 Sep 2026 -
# see Luxury/config.py's own LIQUIDITY_GUARD_ENABLED docstring for the
# full incident/rationale). Shared, global computation parameters for
# dhan_client.refresh_liquidity_signal()/get_cached_illiquid() - lives
# here rather than per-package for the identical reason SUPERTREND_
# PERIOD/MULTIPLIER/REFRESH_SECONDS above do (the function itself lives
# in this shared dhan_client.py, keyed by option_trading_symbol - the
# SAME contract quotes the same way regardless of which package happens
# to be holding it). Currently wired into Luxury ONLY (LUXURY_LIQUIDITY_
# GUARD_ENABLED, that package's own on/off gate) - the evidenced package
# from the 2-3 Sep investigation; Options/Futures don't check this yet,
# not because it wouldn't apply to them too, but so this first version
# ships scoped to where it was actually validated against real trades.
#
# LIQUIDITY_GUARD_ZERO_VOLUME_BARS=4 means 4 CONSECUTIVE completed
# 1-min bars of exactly zero traded volume. Originally guessed at 3
# (matching the CHOLAFIN replay's own 4 quiet minutes, minus one for an
# earlier margin) - CORRECTED after backtesting against all 37 real
# Luxury trades from 2-3 Sep 2026 (not just replaying CHOLAFIN in
# isolation): at 3 bars, the guard also fired on a BLUESTARCO position
# that was actually fine and went on to hit PROFIT_PROTECTION_HIT for
# +1,056.25 - the guard would have turned that real win into a
# -666.25 loss (a false positive, net -1,722.50 on that one trade). At
# 4 bars, that false positive disappears entirely while EVERY genuine
# catch is preserved (CHOLAFIN, NBCC, PIIND - each only 1 minute later
# than at 3 bars, no meaningful loss of protection) - net backtested
# effect across the 2 days: +Rs.3,201 at 4 bars vs +Rs.1,479 at 3 bars.
# 5+ bars becomes too conservative (misses NBCC and CHOLAFIN too, only
# +Rs.280 net). Full sweep in trading-skills' own incident/backtest
# writeup - see NOTES.md's corrective-action entry for this feature.
# LIQUIDITY_GUARD_REFRESH_SECONDS=30 (vs Supertrend's own 15s) - this
# REST call fetches the OPTION's own candles (not the underlying's), one
# extra call per HELD position on top of Supertrend's own per-underlying
# call; a slightly longer cache window keeps total REST volume
# reasonable without meaningfully widening the detection window, since
# the underlying condition (multiple minutes of zero volume) is itself
# already a multi-minute-scale signal, not one where a few extra seconds
# of staleness materially changes the outcome.
LIQUIDITY_GUARD_ZERO_VOLUME_BARS = int(os.getenv("LIQUIDITY_GUARD_ZERO_VOLUME_BARS", "4"))
LIQUIDITY_GUARD_REFRESH_SECONDS = int(os.getenv("LIQUIDITY_GUARD_REFRESH_SECONDS", "30"))

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
# Code default is "MARGIN" (= NRML): the deployed .env has always set
# MARGIN, and a config slip that dropped that line must NOT silently fall
# back to leveraged intraday MIS (user request 10 Sep 2026: "NRML across
# all strategies"). Set OPTIONS_PRODUCT=MIS explicitly to go back to
# intraday.
OPTIONS_PRODUCT = os.getenv("OPTIONS_PRODUCT", "MARGIN")

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

# Multi-window trading schedule (added 10 Sep 2026, user request: "trading
# only allowed within 09:15-11:00 and 14:00-15:28"). When ENABLE_TRADING_
# WINDOWS is on, a new entry is refused unless the current IST time falls
# inside one of the TRADING_WINDOWS ranges. This SUPERSEDES the single-
# cutoff ENABLE_TRADING_TIME_LIMIT / ALLOWED_TRADING_TIME above - when
# windows are on, is_past_allowed_trading_time() short-circuits to False so
# the two mechanisms can't fight. Only gates NEW entries; open positions
# keep full exit monitoring, and SQUARE_OFF_TIME (15:15) still force-closes
# regardless - so the practical upper bound is 15:15 even if a window ends
# later. Format: comma-separated "HH:MM-HH:MM" ranges, start inclusive /
# end exclusive. Parsed by trading_engine._parse_trading_windows().
ENABLE_TRADING_WINDOWS = os.getenv("ENABLE_TRADING_WINDOWS", "false").lower() == "true"
TRADING_WINDOWS = os.getenv("TRADING_WINDOWS", "09:15-11:00,14:00-15:28")
MARKET_CLOSE_TIME = "15:30"

# ---------------------------------------------------------------------------
# Nifty50 open gap-down / sharp-fall CE cool-off (added 11 Sep 2026, user
# request: "evaluate if Nifty50 has a Gap Down opening of more than 100
# points or is sharp falling when market open, then wait for 10 mins
# before placing any CE orders"; SCALED + recovery-gated 11 Sep 2026,
# after a real -207.5 point gap-down day showed a flat 10-minute wait is
# nowhere near enough on a big gap - Nifty stayed red (below today's own
# open) until 10:03 IST, 48 minutes in, and 6 of the 7 real CE entries
# taken before it turned green that day lost: "look at scaling the delay
# to the gap size or until Nifty50 daily candle starts showing
# recovering (turning green from red)..."). This is a single market-wide
# fact (is the index falling right now), not a per-strategy one, so the
# actual computation - dhan_wrapper.evaluate_nifty_open_condition() /
# is_nifty_recovering() / should_delay_ce_entry() in dhan_client.py - is
# shared: it always reads these params from Options.config regardless of
# which package (Options/Futures/Luxury) calls it, exactly like
# SUPERTREND_PERIOD above already does for the shared Supertrend signal.
# Each package still gets its own ENABLE_GAP_DOWN_CE_DELAY on/off switch
# in its own config.py so it can be disabled independently if it turns
# out to cost more good entries than it saves.
#
# The open condition itself is evaluated ONCE per day, at whichever
# webhook alert is the first to ask (a one-shot judgment "at the open"),
# and cached for the rest of the day:
#   gap_points  = today's first 1-min bar's open - yesterday's last close
#   gap_down    = gap_points <= -GAP_DOWN_THRESHOLD_POINTS
#   fall_pct    = (today's open - latest close so far) / today's open
#   sharp_fall  = fall_pct >= GAP_DOWN_SHARP_FALL_PCT
# If either fires, CE entries are held back for AT LEAST a MINIMUM delay
# that SCALES with how big the gap actually is (a -110 point gap and a
# -400 point gap don't deserve the same wait):
#   scaled_minutes = min(GAP_DOWN_MAX_DELAY_MINUTES, GAP_DOWN_CE_DELAY_
#                         MINUTES + GAP_DOWN_EXTRA_DELAY_MINUTES_PER_
#                         100_POINTS * max(0, |gap_points| -
#                         GAP_DOWN_THRESHOLD_POINTS) / 100)
# (a sharp-fall-only trigger, with no comparable "gap size" to scale on,
# always uses the plain GAP_DOWN_CE_DELAY_MINUTES base). PAST that
# minimum, if ENABLE_NIFTY_RECOVERY_GATE is on, the hold EXTENDS until
# Nifty's own still-forming daily candle has turned green - its latest
# close back at or above TODAY's OPEN (is_nifty_recovering()) - the
# "wait for the bleeding to actually stop" half of the user's request,
# not just a clock. GAP_DOWN_MAX_DELAY_MINUTES is a hard safety ceiling
# on the WHOLE mechanism (scaled minimum + recovery wait combined) - past
# it, CE always resumes regardless of Nifty's own color, so a market that
# genuinely never recovers intraday can't silently disable CE all day.
# PE is never affected by any of this - a falling Nifty is exactly when
# a PE-buying alert should be allowed to act; every other gate (capacity,
# trading windows, ...) is untouched either way.
GAP_DOWN_THRESHOLD_POINTS = float(os.getenv("GAP_DOWN_THRESHOLD_POINTS", "100"))
GAP_DOWN_SHARP_FALL_PCT = float(os.getenv("GAP_DOWN_SHARP_FALL_PCT", "0.003"))  # 0.3%
GAP_DOWN_CE_DELAY_MINUTES = int(os.getenv("GAP_DOWN_CE_DELAY_MINUTES", "10"))
# Extra minutes added per additional 100 Nifty points the gap runs past
# GAP_DOWN_THRESHOLD_POINTS - e.g. with the defaults, today's real -207.5
# point gap (107.5 points past the 100-point threshold) would scale to
# 10 + 5*(107.5/100) = 15.4 minutes, before the recovery gate below even
# gets a say.
GAP_DOWN_EXTRA_DELAY_MINUTES_PER_100_POINTS = float(
    os.getenv("GAP_DOWN_EXTRA_DELAY_MINUTES_PER_100_POINTS", "5")
)
# Hard ceiling on the combined scaled-minimum + recovery-wait delay - past
# this, CE always resumes for the day regardless of Nifty's own color.
# 120 (2 hours) is deliberately generous: on the one real gap-down day
# this was built from, actual recovery (10:03 IST, 48 minutes after
# open) landed well inside it - this cap exists as a safety backstop
# against a persistently-red session, not as the expected binding case.
GAP_DOWN_MAX_DELAY_MINUTES = int(os.getenv("GAP_DOWN_MAX_DELAY_MINUTES", "120"))
# How often is_nifty_recovering() is allowed to re-fetch Nifty's latest
# price once the scaled minimum has elapsed - avoids a REST call on
# every single webhook alert while the recovery check is pending.
NIFTY_RECOVERY_REFRESH_SECONDS = int(os.getenv("NIFTY_RECOVERY_REFRESH_SECONDS", "30"))

# Per-package on/off switch (Options' own copy - unprefixed, matching
# this package's other flags). Futures/Luxury have their own FUTURES_/
# LUXURY_ prefixed copies in their own config.py, all defaulting to
# "true" - this is the one each package independently decides whether to
# honor at all.
ENABLE_GAP_DOWN_CE_DELAY = os.getenv("ENABLE_GAP_DOWN_CE_DELAY", "true").lower() == "true"

# NOT per-package - this tunes the shared mechanism's own internal
# behavior (like GAP_DOWN_THRESHOLD_POINTS above), read directly by
# dhan_client.py's should_delay_ce_entry() regardless of which package
# called it. Off falls back to just the scaled minimum delay above (no
# recovery-wait extension), if that turns out to hold CE back for too
# long in practice.
ENABLE_NIFTY_RECOVERY_GATE = os.getenv("ENABLE_NIFTY_RECOVERY_GATE", "true").lower() == "true"

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
