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

# Market-data WebSocket reconnect backoff (added 21 Sep 2026, real incident -
# see trading-skills' incidents/2026-09-21-market-feed-thread-death-on-429.md).
# dhanhq's own MarketFeed.run() only catches KeyboardInterrupt, so a single
# HTTP 429 on its very FIRST connection attempt silently killed the whole
# background thread forever, zero further retries - confirmed live, twice,
# on 21 Sep 2026. dhan_client._run_market_feed_forever() now owns the entire
# retry loop itself (never trusting the SDK's own internal one to survive
# its own first attempt), backing off exponentially instead of hammering
# Dhan's rate limiter at a fixed cadence the way the SDK's own internal
# mid-session reconnect loop does once past that first connect.
MARKET_FEED_BACKOFF_BASE_SECONDS = float(os.getenv("MARKET_FEED_BACKOFF_BASE_SECONDS", "2"))
MARKET_FEED_BACKOFF_MAX_SECONDS = float(os.getenv("MARKET_FEED_BACKOFF_MAX_SECONDS", "60"))
# A connection that stayed up at least this long before dying is treated as
# "the problem had actually cleared" - the NEXT reconnect starts back at the
# base delay instead of continuing to escalate from wherever a much older,
# unrelated bad patch had left it.
MARKET_FEED_BACKOFF_RESET_AFTER_SECONDS = float(os.getenv("MARKET_FEED_BACKOFF_RESET_AFTER_SECONDS", "120"))

# Watchdog for the OTHER failure mode seen the same incident: once dhanhq's
# MarketFeed gets PAST its first connect, its own internal loop reconnects
# on disconnect/error just fine - but at a fixed ~1s cadence with NO backoff
# of its own, so a rate-limited endpoint just gets hammered continuously
# (confirmed live: feed_errors climbed ~1/sec for 25+ minutes straight,
# feed_connects frozen, zero ticks the entire time) instead of ever getting
# the quiet gap it needs to actually clear. That inner loop lives inside the
# vendored SDK and isn't something dhan_client.py's own outer retry wrapper
# ever sees return control - so this watchdog polls the error counter from
# OUTSIDE instead, and force-closes+recycles the feed if it looks stuck,
# handing control back to the outer backoff loop above.
MARKET_FEED_WATCHDOG_INTERVAL_SECONDS = float(os.getenv("MARKET_FEED_WATCHDOG_INTERVAL_SECONDS", "30"))
MARKET_FEED_WATCHDOG_ERROR_THRESHOLD = int(os.getenv("MARKET_FEED_WATCHDOG_ERROR_THRESHOLD", "5"))

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

# When true, a CE/bullish alert (prefer_highest=True) ranks candidates by
# reversal_filters.rank_by_ribbon_expansion (MA-ribbon-expansion chart-
# structure score) instead of rank_and_pick_top_stocks' day-change%-only
# ranking - see ribbon_score.py's own module docstring for what the score
# measures. Backtested with a real improvement over day-change% ranking
# on 2026-09-17's real alerts (+Rs 3,085 vs -Rs 611.75 baseline, CE-side
# only). A PE/bearish alert (prefer_highest=False) always keeps using
# rank_and_pick_top_stocks regardless of this flag - there is no
# validated bearish/PUT-side ribbon score yet.
#
# Went live 18 Sep 2026: turning this on changes the code path for EVERY
# CE alert including single-stock ones. It briefly defaulted false after
# a first attempt broke 14 tests across 6 files that mock rank_and_pick_
# top_stocks directly (allowed-trading-time cutoffs, cross-strategy races,
# fund allocation, Luxury package races, alert-candidate shadow wiring),
# none of which anticipated a second ranking path - all 14 were updated
# (each forces RIBBON_RANKING_ENABLED=False for its own scope, since none
# of them are actually about ranking) and the full suite re-confirmed
# clean before this flipped true.
RIBBON_RANKING_ENABLED = os.getenv("RIBBON_RANKING_ENABLED", "true").lower() == "true"

# Separate flag for PE/bearish alerts (added 18 Sep 2026, user request) -
# uses reversal_filters.rank_by_ribbon_breakdown (ribbon_score.score_
# ribbon_breakdown's own bearish/breakdown mirror of the CE logic above).
# Kept as its OWN flag, independent of RIBBON_RANKING_ENABLED, because the
# PE/bearish direction has NOT been backtested against real data the way
# the CE side was (2026-09-17's backtest was explicitly CE-only, since
# shadow_evaluator.py's simulated fills are always CE-ATM regardless of
# alert direction) - it's built on the reasonable but unconfirmed
# assumption that the ribbon pattern is direction-symmetric. Being able to
# turn this off independently of the CE side (which DOES have backtest
# evidence behind it) is the whole point of a separate flag.
RIBBON_RANKING_PE_ENABLED = os.getenv("RIBBON_RANKING_PE_ENABLED", "true").lower() == "true"
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

# Opening-burst extra CE capacity (added 19 Sep 2026, backtested against 6
# days of real alerts/shadow data - see trading-skills' designs/opening-
# burst-slot-and-sl-target-sensitivity.md). The highest-quality window for
# alerts we'd otherwise drop for lack of capacity is right after open, and
# a single extra slot held open a bit longer (09:15-10:00, not just the
# first 25 min) captures more of that than a second permanent slot would,
# since it cycles through multiple positions as they resolve. CE only -
# the backtest never modeled PE (shadow_evaluator itself is CE-only).
BURST_CAPACITY_ENABLED = os.getenv("BURST_CAPACITY_ENABLED", "true").lower() == "true"
BURST_WINDOW_START = os.getenv("BURST_WINDOW_START", "09:15")
BURST_WINDOW_END = os.getenv("BURST_WINDOW_END", "10:00")
BURST_EXTRA_SLOTS_CE = int(os.getenv("BURST_EXTRA_SLOTS_CE", "1"))

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
# block: once a symbol has closed at a genuine monetary loss
# LOSS_REPEAT_BLOCK_COUNT times today, it's done for the SYMBOL for the
# rest of the day, no more re-entries regardless of how much time has
# passed. Backed by trade_history.loss_count_today() - the same durable,
# restart-surviving real_trades log every other daily count/cooldown here
# reads.
#
# BROADENED 18 Sep 2026 (real incident, user request): originally only
# counted MAX_LOSS_HIT/STOP_LOSS_HIT exits (LOSS_REPEAT_BLOCK_EXIT_REASONS,
# kept below only because Paper01/trading_engine.py still reads it for its
# own, separate paper-only reason-scoped check - the live gate here no
# longer uses it). That narrow scoping meant a real ATHERENERG 29 SEP 1540
# PUT (17 Sep 2026) lost Rs 1,537.50 via SUPERTREND_EXIT and Rs 2,325.00
# via EMA_CROSS_EXIT - neither reason counted - so this block never
# engaged despite two real same-day losses, and a 3rd entry followed. Now
# counts ANY exit that closed at pnl < 0, regardless of reason.
LOSS_REPEAT_BLOCK_ENABLED = os.getenv("LOSS_REPEAT_BLOCK_ENABLED", "true").lower() == "true"
# Tightened 2->1 (18 Sep 2026, user request): block re-entry into a symbol
# for the rest of the day after its VERY FIRST loss-exit today, not the
# second. loss_count >= LOSS_REPEAT_BLOCK_COUNT is the comparison
# (trading_engine._process_one_entry) - with COUNT=1 that's true the
# moment loss_count reaches 1.
LOSS_REPEAT_BLOCK_COUNT = int(os.getenv("LOSS_REPEAT_BLOCK_COUNT", "1"))
LOSS_REPEAT_BLOCK_EXIT_REASONS = ("MAX_LOSS_HIT", "STOP_LOSS_HIT")

# Loss-re-entry trend-strength check (added 18 Sep 2026, same incident as
# above) - once a symbol has closed at a loss >= 1 time today (but hasn't
# yet hit LOSS_REPEAT_BLOCK_COUNT outright), a re-entry attempt must ALSO
# pass reversal_filters.check_trend_strength (ADX or Efficiency Ratio
# confirming a genuine trend, not chop) before being allowed through -
# see that function's own docstring for the exact ATHERENERG numbers
# (ADX=13.24, well below ADX_MIN) that motivated this. A clean symbol
# (zero losses today) never pays this extra REST call.
LOSS_REENTRY_TREND_CHECK_ENABLED = os.getenv("LOSS_REENTRY_TREND_CHECK_ENABLED", "true").lower() == "true"

# Volume-floor entry gate (promoted from shadow-mode logging to a real
# live gate, 16 Sep 2026) - the single strongest filter across two
# backtest rounds against real trades (+Rs.7,131.50 on 37 trades/2 days,
# +Rs.17,123.00 on 143 trades/15 days - see reversal_filters.py's own
# module docstring for the full evidence). Blocks a new entry if the
# underlying's own 5-min entry candle traded on less than
# VOLUME_FLOOR_RATIO_MIN times its 20-bar average volume - a thin,
# below-average-conviction candle is exactly the pattern behind most of
# the LIQUIDITY_GUARD/SUPERTREND_EXIT losses this filter was built to
# catch. Deliberately a flag (default enabled per explicit user request,
# 16 Sep 2026: "keep this turned on before deployment to live") so it can
# be switched off instantly via .env without touching any strategy logic
# if it ever needs to be paused. Fails OPEN (never blocks) on a fetch
# failure or insufficient data - see reversal_filters.check_volume_floor's
# own docstring for why a diagnostic check's own failure must never
# itself cause a missed entry.
VOLUME_FLOOR_GATE_ENABLED = os.getenv("VOLUME_FLOOR_GATE_ENABLED", "true").lower() == "true"
VOLUME_FLOOR_RATIO_MIN = float(os.getenv("VOLUME_FLOOR_RATIO_MIN", "1.2"))

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
# so at 1.0, the SL-L order's own fillable price band (trigger down to
# limit) would be exactly as wide, in rupees, as the MAX_LOSS_HIT cap
# itself - meaning a worst-case fill at the very bottom of that band
# could run to roughly 2x the cap. Lowered 1.0->0.05 same day (user
# feedback: "I don't want loss to trail much farther from MAX_LOSS
# LIMIT... contained within max 200/300 rupees") - since the rupee gap
# is cap * this multiple regardless of quantity, 0.05 caps the extra
# tolerance at Rs 225 (before 11:30, cap 4500) / Rs 105 (after, cap
# 2100), comfortably under that 200-300 ceiling. Code default kept at
# its ORIGINAL 1.0 (see TARGET_PCT's own comment elsewhere in this file
# for why) - .env's own BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE carries the
# real deployed 0.05. Set lower still for an even tighter worst-case
# (at the cost of a higher chance of no fill on a violent gap) or higher
# for a wider, more-likely-to-fill band.
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

# Real incident, ANGELONE 17 Sep 2026 (Swing) + ICICIPRULI 10 Sep 2026
# (Options, see trading-skills' incidents/2026-09-10-icicipruli-
# unmonitorable-position.md - that write-up's own "fix direction, not yet
# built"): Dhan's live-quote endpoint can go opaquely dark for a specific
# contract for many minutes to hours (confirmed via a direct raw quote
# call returning status=failure with null error details) while a fresh,
# separate re-authenticated session gets a normal quote back immediately -
# meaning it's Dhan-side flakiness on that one contract's live feed, not a
# code bug, and self-heals eventually, but with no bound on how long. A
# position the monitor loop can't price is a position with ZERO active
# exit-ladder protection (target/trailing-SL/MAX_LOSS/regime-reversal all
# need a real LTP) for however long the outage lasts - only a resting
# broker-side stop-loss order (if BROKER_STOP_LOSS_ENABLED) still protects
# it independently. Once _get_ltp has failed continuously for this many
# minutes on an open position, _check_one_position forces a market exit
# (using get_last_historical_close as a rough logging mark, since the
# historical 1-min feed kept working through the ICICIPRULI outage even
# though live quotes didn't) rather than continuing to hold something
# un-monitorable - _exit_position's own existing stale-pending-order check
# also cancels any resting broker-side SL before placing the fresh exit,
# so this closes both the position AND its own protective order together.
LTP_STALE_FORCE_EXIT_MINUTES = float(os.getenv("LTP_STALE_FORCE_EXIT_MINUTES", "5"))

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

# Split into CE/PE-specific overrides (user request 14 Sep 2026: tighter
# caps for PE only - Rs 3500/1600 vs whatever CE stays at) - same
# established pattern as MAX_LIVE_POSITIONS_CE/_PE and DYNAMIC_SL_STEP_
# PCT_CE/_PE above. Each falls back to the existing shared value above if
# its own CE/PE-specific env var isn't set, so CE's behavior (and PE's,
# until explicitly overridden) is unchanged from before this split -
# nothing here silently changes what was already deployed. See
# trading_engine.current_max_loss_per_trade_rs(option_type) for where
# this is actually applied.
MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_CE = float(os.getenv(
    "MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_CE", str(MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF)))
MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF_CE = float(os.getenv(
    "MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF_CE", str(MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF)))
MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_PE = float(os.getenv(
    "MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_PE", str(MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF)))
MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF_PE = float(os.getenv(
    "MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF_PE", str(MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF)))

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

# Minimum-underlying-move confirmation gate (added 18 Sep 2026) - requires
# the underlying to have moved against a position by at least
# reversal_filters.MIN_UNDERLYING_MOVE_CONFIRMATION_PCT (0.10%, backtest-
# derived) before trusting SUPERTREND_EXIT/EMA_CROSS_EXIT as a genuine
# reversal rather than option-premium noise. Scoped to those two exits
# ONLY - MAX_LOSS_HIT/TARGET_HIT/PROFIT_PROTECTION_HIT/TRAILING_SL_HIT/
# STOP_LOSS_HIT and LIQUIDITY_GUARD_ZERO_VOLUME are untouched hard
# backstops, never delayed by this gate. Backtest: all 9 real
# SUPERTREND_EXIT/EMA_CROSS_EXIT trades on 18 Sep 2026 were losses where the
# underlying never genuinely moved (7/9 recovered after the forced exit) -
# see trading-skills repo for the full writeup. Default true per explicit
# user instruction to enable for Options/Futures/Luxury together.
UNDERLYING_MOVE_CONFIRMATION_ENABLED = os.getenv("UNDERLYING_MOVE_CONFIRMATION_ENABLED", "true").lower() == "true"

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

# Option-liquidity ENTRY gate (added 18 Sep 2026, real incident - see
# reversal_filters.check_option_liquidity's own docstring for the full
# SOLARINDS rationale). Reuses the same zero-volume-streak check and the
# same LIQUIDITY_GUARD_ZERO_VOLUME_BARS/_REFRESH_SECONDS thresholds as
# the EXIT-side guard above, but checked once BEFORE placing a real
# order instead of only after a position is already open - the option's
# OWN illiquidity, not the underlying's, which none of the other entry
# gates (volume floor, trend-strength) can see.
LIQUIDITY_ENTRY_GATE_ENABLED = os.getenv("LIQUIDITY_ENTRY_GATE_ENABLED", "true").lower() == "true"

# Global liquid-contract resolution (added 18 Sep 2026, real incident -
# see dhan_client.get_liquid_atm_option's own docstring for the full
# ATHERENERG 29 SEP 1540 PUT rationale: a broker-side stop-loss REJECTED
# with "EXCH:17181: Contract not traded" because the natural ATM strike
# had never printed a single trade before the position was opened). This
# is the single shared gate all 4 live-trading packages (Options/Futures/
# Luxury/Swing) route their contract resolution through - one set of
# thresholds here, consumed by the shared dhan_client.py, same pattern as
# LIQUIDITY_GUARD_ZERO_VOLUME_BARS/_REFRESH_SECONDS above.
#
# LIQUID_CONTRACT_MAX_STRIKE_SEARCH: how many strikes outward (each
# direction) to try if the ATM strike itself fails either check below,
# before giving up and skipping the entry entirely.
#
# LIQUID_CONTRACT_LOOKBACK_DAYS/_MIN_PRIOR_SESSION_VOLUME: a candidate
# must have traded at least this much (summed across the last N calendar
# days, via a real daily-historical-data fetch - see get_daily_volume_sum)
# to count as "actively traded", not just "not currently silent". 500 is
# a starting, disclosed judgment call (not backtested against real
# volume distributions) rather than a value tuned from data - adjust via
# env if it turns out too strict/loose in practice.
LIQUID_CONTRACT_GATE_ENABLED = os.getenv("LIQUID_CONTRACT_GATE_ENABLED", "true").lower() == "true"
LIQUID_CONTRACT_MAX_STRIKE_SEARCH = int(os.getenv("LIQUID_CONTRACT_MAX_STRIKE_SEARCH", "5"))
LIQUID_CONTRACT_LOOKBACK_DAYS = int(os.getenv("LIQUID_CONTRACT_LOOKBACK_DAYS", "7"))
LIQUID_CONTRACT_MIN_PRIOR_SESSION_VOLUME = float(os.getenv("LIQUID_CONTRACT_MIN_PRIOR_SESSION_VOLUME", "500"))

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

# MCX (commodity) segment trades a materially longer session than NSE F&O -
# Copper and other non-agri commodities run into the evening (23:30 most of
# the year, 23:55 during the Nov-Mar window when Dhan/MCX extends it for US
# daylight saving - override MCX_MARKET_CLOSE_TIME via env during that
# window if needed). Added 15 Sep 2026 after a real incident: dhan_client.
# is_market_open() used to check ONLY these NSE hours regardless of segment,
# so every Swing MCX order placed after 15:30 IST was wrongly tagged AMO
# even though MCX was still genuinely open - see is_market_open()'s own
# docstring for the full incident and Swing/trading_engine.py's per-symbol
# entry-retry-cooldown addition for the other half of that fix.
MCX_MARKET_OPEN_TIME = os.getenv("MCX_MARKET_OPEN_TIME", "09:00")
MCX_MARKET_CLOSE_TIME = os.getenv("MCX_MARKET_CLOSE_TIME", "23:30")

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

# Stale entry-order timeout (user request 15 Sep 2026, real incident:
# ICICIPRULI's BUY market order sat PENDING at the broker for 10+ minutes
# straight during live market hours with no fill and no rejection -
# _sync_pending_orders already re-checks every non-AMO-queued pending BUY
# order on every monitor tick, but previously had no notion of "this has
# been stuck too long, do something" - it would just keep re-logging the
# same PENDING status forever. Once a BUY order (placed during market
# hours, NOT a genuinely-queued AMO - those are SUPPOSED to sit non-
# terminal until the next session) has been non-terminal for this many
# seconds, _sync_pending_orders cancels it and makes exactly ONE retry
# attempt (a fresh market order for the identical contract/quantity - a
# market order always fills at whatever the CURRENT price is, so simply
# re-submitting IS the "adjust to current price" retry; there's no
# separate limit price to change on a market order). If that retry ALSO
# times out, the entry is abandoned (reservation released) rather than
# retried indefinitely.
STALE_ENTRY_ORDER_TIMEOUT_SECONDS = int(os.getenv("STALE_ENTRY_ORDER_TIMEOUT_SECONDS", "300"))

# --------------------------------------------------------------------------
# Breakout-signal live entry trigger, CE+PE, SOLE real entry path for
# Options (added 21 Sep 2026, user request) - reuses breakout_signal.py
# unchanged (already generic per-strategy/per-direction, same module
# Luxury/Futures already use). Originally backtested PE-only (designs/
# options-pe-breakout-signal-gated-live-full-real-gates.md, +Rs41,172.87
# delta/17 trades), then widened to CE+PE and promoted to the SOLE entry
# path the same day on explicit user direction: "the breakout-signal
# scanner has to be main entry path... webhook path will feed the signals
# to breakout-signal scanner and it will decide which trades to be
# placed" - applied identically to Luxury/Futures too (see each package's
# own _handle_chartink_webhook, which no longer calls enter_positions_
# for_stocks at all). Thresholds default to the SAME values already
# deployed live on Luxury (clearance=0.3%, body=0.5%, relvol=1.2x - the
# 27-way sweep's #1 combo, see trading-skills' designs/luxury-breakout-
# detection-parameter-sweep.md).
#
# Made a genuine TWO-WAY switch the same day (follow-up user request:
# "once this flag is false the signals directly reach for being placed
# as trade and not being parsed via breakout scanner") - option_main.py's
# own _handle_chartink_webhook branches on this:
#   True (default)  - a raw alert only records into the watchlist;
#                      _breakout_entry_fn (via the scanner loop) decides
#                      whether/when to actually enter.
#   False            - falls through to _enter_directly_from_webhook, the
#                      restored pre-21-Sep-2026 direct ranked-entry path -
#                      bypasses the breakout scanner's filtering entirely.
# DEFAULTS TRUE per explicit user instruction - "unless I explicitly ask
# the breakout scanner is only used for filtering out the signals" i.e.
# this should default to filtering ON, not off.
BREAKOUT_SIGNAL_ENABLED = os.getenv("OPTIONS_BREAKOUT_SIGNAL_ENABLED", "true").lower() == "true"

# Consolidation/breakout shape - same values the backtest validated.
BREAKOUT_LOOKBACK_CANDLES = int(os.getenv("OPTIONS_BREAKOUT_LOOKBACK_CANDLES", "10"))
BREAKOUT_MAX_CONSOLIDATION_RANGE_PCT = float(os.getenv("OPTIONS_BREAKOUT_MAX_CONSOLIDATION_RANGE_PCT", "12"))
BREAKOUT_CLEARANCE_PCT = float(os.getenv("OPTIONS_BREAKOUT_CLEARANCE_PCT", "0.3"))
BREAKOUT_MIN_BODY_PCT = float(os.getenv("OPTIONS_BREAKOUT_MIN_BODY_PCT", "0.5"))
BREAKOUT_MIN_RELATIVE_VOLUME = float(os.getenv("OPTIONS_BREAKOUT_MIN_RELATIVE_VOLUME", "1.2"))
BREAKOUT_MIN_AVG_DAILY_VOLUME = float(os.getenv("OPTIONS_BREAKOUT_MIN_AVG_DAILY_VOLUME", "500000"))
BREAKOUT_MAX_PCT_FROM_HIGH_LOW = float(os.getenv("OPTIONS_BREAKOUT_MAX_PCT_FROM_HIGH_LOW", "10"))

# Data-fetch windows - continuous multi-day, per the standing continuous-
# candles rule. Same defaults as Luxury/Futures' own blocks - see those
# files' own comments for the sizing rationale (50-day SMA/high/low needs
# >=50 real trading days; 120 calendar days comfortably covers that).
BREAKOUT_CANDLE_LOOKBACK_DAYS = int(os.getenv("OPTIONS_BREAKOUT_CANDLE_LOOKBACK_DAYS", "15"))
BREAKOUT_DAILY_LOOKBACK_DAYS = int(os.getenv("OPTIONS_BREAKOUT_DAILY_LOOKBACK_DAYS", "120"))

# Scan cadence.
BREAKOUT_SCAN_INTERVAL_SECONDS = float(os.getenv("OPTIONS_BREAKOUT_SCAN_INTERVAL_SECONDS", "60"))
BREAKOUT_SCAN_MAX_PER_CYCLE = int(os.getenv("OPTIONS_BREAKOUT_SCAN_MAX_PER_CYCLE", "10"))
BREAKOUT_SCAN_PACE_SECONDS = float(os.getenv("OPTIONS_BREAKOUT_SCAN_PACE_SECONDS", "1.6"))

# Daily watchlist refresh - see breakout_signal.py's own module docstring
# for the full mechanism (before-market-open reset is automatic via date-
# keyed persistence; this is the explicit after-market-close truncation).
# Matches breakout_signal.py's own _market_hours_now upper bound by default.
BREAKOUT_MARKET_END_TIME = os.getenv("OPTIONS_BREAKOUT_MARKET_END_TIME", "15:35")

# Curated-universe watchlist seeding + WebSocket-based candle reconstruction
# (added 21 Sep 2026, user request - see underlying_candle_feed.py's own
# module docstring and trading-skills' designs/all-fno-universe-breakout-
# signal-15day-backtest.md for why REST-polling a wide watchlist doesn't
# scale). ALL FOUR default to inert/off - nothing changes for Options
# unless these are explicitly set, and per the user's own scoping this
# feature is being built for Luxury/Futures first, not Options.
#   BREAKOUT_SEED_UNIVERSE_ENABLED - if true, breakout_signal.py's scanner
#     loop seeds BOTH the CE and PE watchlists with BREAKOUT_UNIVERSE_
#     SYMBOLS once per trading day (in addition to, not instead of, real
#     Chartink alerts still being recorded normally).
#   BREAKOUT_UNIVERSE_SYMBOLS - comma-separated NSE trading symbols, e.g.
#     "RELIANCE,TCS,INFY". Empty by default - inert until populated.
#   BREAKOUT_USE_WS_CANDLES - if true, a symbol's 5-min intraday candles
#     are read from underlying_candle_feed's locally-reconstructed,
#     WebSocket-fed bars when fresh, falling back to the existing REST
#     fetch otherwise (never the sole source of truth - REST polling stays
#     the correctness fallback, per the user's own framing).
#   BREAKOUT_WS_STALE_AFTER_SECONDS - a symbol's WS-fed bars are trusted
#     only if a tick arrived within this many seconds; otherwise REST.
BREAKOUT_SEED_UNIVERSE_ENABLED = os.getenv("OPTIONS_BREAKOUT_SEED_UNIVERSE_ENABLED", "false").lower() == "true"
BREAKOUT_UNIVERSE_SYMBOLS = [s.strip().upper() for s in os.getenv("OPTIONS_BREAKOUT_UNIVERSE_SYMBOLS", "").split(",") if s.strip()]
BREAKOUT_USE_WS_CANDLES = os.getenv("OPTIONS_BREAKOUT_USE_WS_CANDLES", "false").lower() == "true"
BREAKOUT_WS_STALE_AFTER_SECONDS = float(os.getenv("OPTIONS_BREAKOUT_WS_STALE_AFTER_SECONDS", "90"))

# Where BREAKOUT_SEED_UNIVERSE_ENABLED's symbol list comes from (added 21
# Sep 2026, user request - see universe_bucket.py's own module docstring):
#   "static"          (default) - the fixed BREAKOUT_UNIVERSE_SYMBOLS list
#                       above, re-seeded once per day, unchanged behavior.
#   "universe_bucket" - universe_bucket.active_symbols("CE"/"PE")'s own
#                       rolling 3-trading-day window, re-synced EVERY scan
#                       cycle (not just once/day) so a fresh webhook alert
#                       reaches the watchlist promptly rather than waiting
#                       for the next day's seed - see breakout_signal.py's
#                       own _maybe_seed_universe for the two code paths.
BREAKOUT_UNIVERSE_SOURCE = os.getenv("OPTIONS_BREAKOUT_UNIVERSE_SOURCE", "static").lower()

# Cross-package universe_bucket signal dispatcher (added 21 Sep 2026, user
# request - see breakout_signal.py's own "dispatcher" section, right
# after _maybe_seed_universe, for the full design). Shared/global, not
# per-package, since it spans whichever packages main.py's own lifespan
# lists as targets (Luxury+Futures as of 21 Sep 2026, per explicit user
# scoping) - lives here in the shared config module for that reason, the
# same place LIQUID_CONTRACT_*/GAP_DOWN_* already live for an identical
# reason. Default false - nothing changes until this is explicitly
# enabled AND main.py's lifespan is told which packages to target.
UNIVERSE_DISPATCHER_ENABLED = os.getenv("UNIVERSE_DISPATCHER_ENABLED", "false").lower() == "true"

# Capacity backlog (added 21 Sep 2026, user request: "once any slot from
# LUXURY or FUTURES gets free... should be attempted") - how long (from
# when EVERY dispatcher target first rejected a signal specifically as
# duplicate_or_capacity_full) a still-in-momentum signal keeps getting
# retried against freed-up capacity before being dropped as stale. See
# breakout_signal.py's own "capacity backlog" section for the full
# mechanism. In-memory only (resets on restart) - same accepted tradeoff
# as cross_strategy_registry/PositionStore's own reserved_symbols.
BREAKOUT_CAPACITY_BACKLOG_MAX_AGE_MINUTES = float(os.getenv("BREAKOUT_CAPACITY_BACKLOG_MAX_AGE_MINUTES", "60"))
