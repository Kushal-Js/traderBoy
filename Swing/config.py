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

# Global paper-mode kill switch (added 23 Sep 2026, user request - the
# Swing counterpart to Options/Futures/Luxury's own BREAKOUT_PAPER_MODE_
# ENABLED, see breakout_paper_engine.py's module docstring for the full
# design this mirrors). REPLACES real trading entirely while true - every
# real entry Swing's own monitor_tick would otherwise place instead goes
# to swing_paper_engine.process_paper_entry, using the exact same real
# entry gates/exit logic (_evaluate_entry_signal, enter_position_for_
# stock's own gate sequence, _exit_reason_for, _evaluate_exit_signal),
# just never placing a real order. Defaults FALSE - flipping this on is a
# deliberate, explicit action, never a silent side effect of deploying
# this feature. See swing_paper_engine.py's own module docstring for
# exactly what is and isn't replicated (broker-side stop-loss placement
# can't be, by construction - it's a real order).
PAPER_MODE_ENABLED = os.getenv("SWING_PAPER_MODE_ENABLED", "false").lower() == "true"

# INDEX_SYMBOLS-only paper-mode kill switch (added 24 Sep 2026, user
# request - "add a flag to turn off real trading and start paper trading"
# specifically for the newly-added NIFTY/BANKNIFTY v3 Day Range logic +
# its brand-new WS index feed, both deployed the same day with no live
# track record yet). Independent of PAPER_MODE_ENABLED above (that one
# still replaces ALL of Swing when true) - this one ONLY reroutes NIFTY/
# BANKNIFTY candidates to swing_paper_engine.process_paper_entry;
# ASHOKLEY/COALINDIA/COPPER/NATURALGAS keep trading real, unaffected, the
# same instant this is flipped. See Swing/trading_engine.py's
# _monitor_tick for where this is checked (ORed with PAPER_MODE_ENABLED,
# so either flag alone is enough to paper-trade an index candidate).
# Turning this OFF later does not retroactively affect any already-open
# paper position - swing_paper_engine.py's own paper_engine_monitor_loop
# keeps managing it through to its own exit regardless.
INDEX_PAPER_MODE_ENABLED = os.getenv("SWING_INDEX_PAPER_MODE_ENABLED", "false").lower() == "true"

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

# OPTIONS-specific override (user request 15 Sep 2026, straight off
# switching BASKET_TYPE to options): the values above were tuned against
# FUTURES-notional P&L (see the backtest history right above) - an
# option's own premium swings represent a much smaller absolute rupee
# move for the same underlying move, so the same flat 5000/2% threshold
# arms far later relative to a typical options trade's real profit
# potential. Falls back to the shared PROFIT_PROTECTION_RS/_GIVEBACK_PCT
# above when unset, so FUTURES/EQUITY baskets are completely unaffected -
# same "new variant, old value stays the default" pattern already used
# for Options/Futures/Luxury's own CE/PE-specific splits. Read by
# basket_type at each exit check (see current_profit_protection_rs/
# current_profit_protection_giveback_pct in trading_engine.py), not
# baked into the Position at entry - so a live tune here applies to an
# already-open OPTIONS position's very next check too.
PROFIT_PROTECTION_RS_OPTIONS = float(os.getenv("SWING_PROFIT_PROTECTION_RS_OPTIONS", str(PROFIT_PROTECTION_RS)))
PROFIT_PROTECTION_GIVEBACK_PCT_OPTIONS = float(
    os.getenv("SWING_PROFIT_PROTECTION_GIVEBACK_PCT_OPTIONS", str(PROFIT_PROTECTION_GIVEBACK_PCT))
)
# MCX-only OPTIONS override (user request 22 Sep 2026) - scoped to MCX
# (COPPER/NATURALGAS) specifically, NOT every OPTIONS position: ASHOKLEY
# (NSE OPTIONS) keeps reading PROFIT_PROTECTION_RS_OPTIONS above,
# unchanged. Falls back to the general OPTIONS value if unset, same
# fallback-chain convention as that value falling back to the plain
# PROFIT_PROTECTION_RS. See Swing/trading_engine.py's current_profit_
# protection_rs/_giveback_pct for where is_mcx gets threaded in.
PROFIT_PROTECTION_RS_MCX = float(os.getenv("SWING_PROFIT_PROTECTION_RS_MCX", str(PROFIT_PROTECTION_RS_OPTIONS)))
PROFIT_PROTECTION_GIVEBACK_PCT_MCX = float(
    os.getenv("SWING_PROFIT_PROTECTION_GIVEBACK_PCT_MCX", str(PROFIT_PROTECTION_GIVEBACK_PCT_OPTIONS))
)
TARGET_PCT = float(os.getenv("SWING_TARGET_PCT", "0.20"))
# Non-COPPER override (user request 24 Sep 2026) - every OTHER watchlist
# symbol (NATURALGAS, ASHOKLEY, SONACOMS, NIFTY, BANKNIFTY, ...) now
# targets 35%, not the shared 20% above. COPPER keeps reading TARGET_PCT
# unchanged - this is deliberately scoped to "not COPPER" specifically,
# not "not MCX" (NATURALGAS, also MCX, gets the new 35% same as every
# NSE symbol). See Swing/trading_engine.py's current_target_pct for where
# the symbol check happens - same is_copper-branch convention as
# current_profit_protection_rs/_giveback_pct's own is_mcx branch above.
TARGET_PCT_NON_COPPER = float(os.getenv("SWING_TARGET_PCT_NON_COPPER", "0.35"))
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

# v2 entry rewrite, 17 Sep 2026 (user request, direct follow-up to the
# same-night COPPER trade investigation - see trading-skills' own
# incident write-up: that trade fired on a hairline, ALREADY-SHRINKING
# regime gap that happened to sit on the bearish side by sign alone, with
# the far-more-reliable 15-min Supertrend still reading bullish). Two
# real, DISTINCT changes to what "regime" means for v2's own entry rule:
#   1. RegimeState.crossed_above/crossed_below (an EDGE, the fast EMA200
#      crossing the slow one - mirrors Supertrend's own crossed_above/
#      crossed_below) is now v2's own "Regime Bullish/Bearish" leg,
#      replacing the old plain LEVEL check (is_bullish) that leg used to
#      read. is_bullish itself is UNCHANGED and still used by v1's own
#      simpler rule and GET /swing/signals - only v2's combined formula
#      switched what "Regime Bullish" means for its own purposes.
#   2. A NEW "Trend-aware Filter" leg: the level check (is_bullish/not)
#      AND the gap must have WIDENED (not narrowed) over this many 5-min
#      candles before counting as "genuinely" bullish/bearish - a
#      strengthening-trend confirmation on top of the plain level, since
#      that COPPER trade's own regime gap was NARROWING every candle
#      leading up to entry (converging toward a flip), the opposite of a
#      confirmed trend.
REGIME_GAP_WIDENING_LOOKBACK_CANDLES = int(os.getenv("SWING_REGIME_GAP_WIDENING_LOOKBACK_CANDLES", "8"))

# v3 - originally INDEX_SYMBOLS (NIFTY/BANKNIFTY) ONLY (24 Sep 2026),
# following the exact formula backtested in backtest_nifty_options_
# swing_v2_1min.py (see trading-skills' designs/nifty-options-swing-v2-
# 1min-v3.md for the full backtest writeup: 5-min fast layer + 0.6x
# volume-floor gate, +Rs14,752/59.4% WR over a 30-day NIFTY window).
# PROMOTED TO THE DEFAULT FOR EVERY SWING SYMBOL (24 Sep 2026, later same
# day - user request: "Make v3 as default for SWING so that all entries
# apart from COPPER follow it ... Nifty and BankNifty already using v3
# only"), after a 30-day SONACOMS backtest comparing the old edge-only
# regime leg against this LEVEL+Day-Range formula showed materially more
# trades and higher net P&L (backtest_swing_sonacoms_v2_vs_v3_30day.py).
# COPPER is unaffected only because its structure-break signal (see
# COPPER_STRUCTURE_BREAK_ENABLED below) bypasses this entire v2/v3 code
# path before either of the two changes below is ever reached - there is
# no separate COPPER carve-out to write here. Both changes now apply to
# every symbol that reaches Swing/trading_engine.py's v2 entry branch
# (NSE equities and MCX alike):
#   1. The "Regime Bullish/Bearish" leg reads regime.is_bullish (a LEVEL,
#      persisting until the next flip) instead of regime.crossed_above/
#      crossed_below (an EDGE). This is a DELIBERATE, now-universal
#      departure from the 17 Sep 2026 COPPER-incident fix above (a
#      hairline, narrowing-gap regime reading firing this leg on sign
#      alone) - that fix's OWN leg (st15.is_above / the Trend-aware
#      Filter) is untouched; only this third OR-leg changed from edge to
#      level, same as the NIFTY backtest that validated it.
#   2. A second OR-branch: "Day Range Bull/Bear" (today's open vs
#      yesterday's close, 5-min close vs 5-min Supertrend LEVEL, RSI(14)
#      crossing 60/40 - see Swing/signals.py's DayRangeState) ANDed with
#      the existing Trend-aware Filter - see Swing/trading_engine.py's
#      _evaluate_entry_signal for the exact combined formula. Now fetched
#      for every symbol, including an MCX underlying like NATURALGAS -
#      DayRangeState's fetch already goes through the same MCX-aware
#      _underlying_reference path regime/Supertrend use.
DAY_RANGE_RSI_PERIOD = int(os.getenv("SWING_DAY_RANGE_RSI_PERIOD", "14"))
DAY_RANGE_RSI_BULL_LEVEL = float(os.getenv("SWING_DAY_RANGE_RSI_BULL_LEVEL", "60"))
DAY_RANGE_RSI_BEAR_LEVEL = float(os.getenv("SWING_DAY_RANGE_RSI_BEAR_LEVEL", "40"))

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

# COPPER-only alternate entry/exit signal (user request 22 Sep 2026):
# structure_break.py's 5m+15m+1h "Smart Money Flow Cloud" band-cross
# agreement (see .claude/skills/structure-break/SKILL.md), backtested the
# same day via backtest_swing_structure_break_mtf.py against 10 days of
# real COPPER data before being wired in here - +Rs 2,51,000/12 trades/
# 75% win rate vs the same window's 2-timeframe (no 1h) variant's
# +Rs 1,82,750/19 trades/47% - see trading-skills' designs/structure-
# break-indicator.md for the full comparison, including the important
# caveat that PnL was modeled on COPPER's FUTURES price move, never a
# real option premium (no theta/IV). User explicitly chose to enable this
# live immediately despite that gap and the thin (single-symbol,
# 10-day) sample - NOT validated on ASHOKLEY/NATURALGAS (both showed a
# much smaller, statistically-insignificant edge either way for the same
# rule, see the same design doc).
#
# BULLISH (all 3 timeframes agree bullish) -> close any open PE, buy ATM
# CE. BEARISH -> close any open CE, buy ATM PE. If the agreement breaks
# WITHOUT a clean opposite signal, squares off to flat and waits for the
# next fresh agreement (either direction) - user-confirmed semantics,
# exactly what the backtest above measured.
#
# When false (the default - existing Swing v2 Supertrend/regime logic
# below is completely UNCHANGED for every symbol, including COPPER, which
# still uses it until this is explicitly turned on). Scoped to COPPER
# ONLY, hardcoded in Swing/trading_engine.py's _evaluate_entry_signal/
# _evaluate_exit_signal - not a general multi-symbol switch, since
# nothing beyond COPPER has been backtested against this rule.
COPPER_STRUCTURE_BREAK_ENABLED = os.getenv("SWING_COPPER_STRUCTURE_BREAK_ENABLED", "false").lower() == "true"
# 3 REST calls (5m/15m/1h) per refresh, unlike Supertrend's single call -
# kept well above SUPERTREND_REFRESH_SECONDS(15) to limit the extra load
# on the shared account-wide Dhan rate limit (see [[f915d98]]'s own
# exponential-backoff fix for why REST pressure here is a real, already-
# incident-producing concern, not a hypothetical one). A 5-min-bar signal
# has no real information gain from refreshing faster than this anyway.
STRUCTURE_BREAK_REFRESH_SECONDS = int(os.getenv("SWING_STRUCTURE_BREAK_REFRESH_SECONDS", "60"))

# WS-based local candle reconstruction for the regime/Supertrend signals
# above (added 23 Sep 2026, user request, direct follow-up to Options/
# Luxury/Futures' own breakout_signal.py rearchitecture the same day -
# see Swing/candle_feed.py's module docstring for the full design). Swing
# is the heaviest single source of DH-904 rate-limit hits of any package
# (confirmed live: continuous ASHOKLEY/NATURALGAS regime+Supertrend REST
# failures every refresh cycle right after a restart, see trading-skills'
# incidents/) because it's the only package running TWO REST fetches
# (5-min AND 15-min) per symbol per refresh, on top of Supertrend's own
# second-timeframe fetch and (when enabled) COPPER's 3-timeframe
# structure-break fetch - same account-wide rate limit as everyone else,
# more calls per symbol than any other package.
#
# Same flag-gated, fail-open discipline as OPTIONS/LUXURY/FUTURES_
# BREAKOUT_USE_WS_CANDLES: default false, zero behavior change until
# explicitly turned on, and even when on, a fetch always falls back to
# the existing REST path whenever the local WS-reconstructed series isn't
# fresh enough or doesn't yet have enough history for that specific call
# (200-period EMA needs materially more history than breakout_signal.py's
# own 10-candle lookback ever did - see candle_feed.py's own MAX_BARS_
# KEPT/lookback sizing for why "flag on" does not mean "instant, silent
# switch-over" the way it effectively does for the other three).
USE_WS_CANDLES = os.getenv("SWING_USE_WS_CANDLES", "false").lower() == "true"
WS_STALE_AFTER_SECONDS = float(os.getenv("SWING_WS_STALE_AFTER_SECONDS", "90"))

# Entry-signal strategy version (user request 14 Sep 2026: "mark current
# deployed EMA-regime strategy with a version and then add another
# version to this Combined (15min ST OR EMA) strategy"). Two entry
# filters share the SAME 5-min Supertrend crossover trigger and the SAME
# exit ladder (nothing below this point changes between versions - see
# Swing/trading_engine.py's _evaluate_exit_signal/_exit_reason_for,
# neither reads this setting):
#
#   "v1" (default - the original 12 Sep 2026 design, UNCHANGED): regime
#     is bullish (5-min EMA200 > 15-min EMA200) AND 5-min close crosses
#     above the 5-min Supertrend -> BULLISH; the mirror for BEARISH.
#
#   "v2" (added 14 Sep 2026, backtested same-day: ADANIPORTS +Rs 17,765/
#     35.0% WR, COALINDIA +Rs 12,487/40.9% WR, Copper +Rs 52,950/45.3% WR
#     over the same 20-trading-day window that produced v1's own
#     comparison numbers - see trading-skills for the full writeup):
#     (15-min Supertrend showing green, i.e. close > its own Supertrend
#     line - a LEVEL, not a crossover) OR (regime bullish) -> filter
#     passes bullish; 5-min close crosses above the 5-min Supertrend ->
#     BULLISH. Mirror for BEARISH. This is a strict OR against v1's
#     single EMA-regime filter, so v2 never rejects a signal v1 would
#     have taken - it only ever admits MORE entries.
#
# Default stays "v1" - a fresh deploy with no explicit override must
# reproduce today's live behavior byte-for-byte, never silently switch
# strategies. Switching to "v2" is a deliberate, explicit .env change.
ENTRY_STRATEGY_VERSION = os.getenv("SWING_ENTRY_STRATEGY_VERSION", "v1").lower()
if ENTRY_STRATEGY_VERSION not in ("v1", "v2"):
    import logging
    logging.getLogger(__name__).error(
        "SWING_ENTRY_STRATEGY_VERSION=%r is not one of v1/v2 - falling back to v1.",
        ENTRY_STRATEGY_VERSION,
    )
    ENTRY_STRATEGY_VERSION = "v1"

# ---------------------------------------------------------------------------
# Capacity (user request: "Keep Max Concurrent Trade capacity as 2 as of
# now but make it configurable also"). ONE shared counter across the
# whole strategy - not split by direction/basket-type, since a stock's
# regime is mutually exclusive at any given time (it's never
# simultaneously both a long and a short candidate). Code default raised
# 2->5 (25 Sep 2026, user request, same value as the .env override below)
# - previously raised 2->3 in .env alone on 24 Sep after live evidence
# showed 2 slots being pinned (COPPER+BANKNIFTY) starved the entire
# entry-scan loop (NIFTY saw zero signal activity until that raise).
# ---------------------------------------------------------------------------
MAX_CONCURRENT_TRADES = int(os.getenv("SWING_MAX_CONCURRENT_TRADES", "5"))

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
# future trading also via SWING strategy" - corrected twice the same day:
# first to "disable Copper Future trading as of now, only Options trading
# for Copper", then again to "whatever is the BASKET_TYPE, it should not
# impact COPPER as it only has to trade in options" - i.e. Copper doesn't
# just skip when BASKET_TYPE happens to be "futures", it always trades
# options regardless of what BASKET_TYPE is set to, for the ENTIRE rest of
# the watchlist. A watchlist symbol in MCX_SYMBOLS routes through Options/
# dhan_client.py's MCX-aware resolvers (get_mcx_futures_contract for the
# regime/Supertrend signal reference - there's no continuous "spot" for an
# MCX commodity, only its futures contract - and the now-MCX-capable
# get_atm_option for the tradeable leg) instead of the NSE-equity path
# every other watchlist symbol uses.
# ---------------------------------------------------------------------------
MCX_SYMBOLS = {s.strip().upper() for s in os.getenv("SWING_MCX_SYMBOLS", "COPPER").split(",") if s.strip()}

# The subset of MCX_SYMBOLS that ALWAYS trades OPTIONS, overriding the
# otherwise-global BASKET_TYPE entirely for just that symbol - deliberately
# a SEPARATE set from MCX_SYMBOLS, not "every MCX symbol behaves this way"
# (explicit user correction, 12 Sep 2026: "this doesn't apply to all
# instruments under MCX but only for COPPER to trade in options"). Any
# future MCX symbol added to MCX_SYMBOLS but NOT to this set would simply
# follow the global BASKET_TYPE like every NSE symbol does today - this
# set exists so that decision is explicit per-symbol, never a blanket rule.
# See Swing/trading_engine.py's enter_position_for_stock for where this is
# actually applied (computes an `effective_basket_type` that's forced to
# "OPTIONS" for a symbol in this set, used instead of the raw config.
# BASKET_TYPE for every entry decision - side, instrument resolution, and
# what gets stored on the resulting Position).
MCX_OPTIONS_ONLY_SYMBOLS = {
    s.strip().upper() for s in os.getenv("SWING_MCX_OPTIONS_ONLY_SYMBOLS", "COPPER").split(",") if s.strip()
}

# Index underlyings (added 24 Sep 2026, fixing a real gap: adding NIFTY/
# BANKNIFTY to the watchlist raised "No NSE equity instrument found" on
# every regime/Supertrend fetch - Dhan's instrument master has no
# SEM_INSTRUMENT_NAME=="EQUITY" row for an index, so the ordinary NSE-
# equity path in _underlying_reference (dhan_wrapper._equity_security_id)
# can never resolve one; needs the IDX_I/INDEX segment via dhan_wrapper.
# index_security_id instead - see Swing/signals.py's own updated
# _underlying_reference. Only NIFTY/BANKNIFTY are backed by a real,
# confirmed security_id today (dhan_wrapper.INDEX_SECURITY_ID, the same
# values IndexScalping/config.py's own INDEX_SECURITY_ID and
# should_delay_ce_entry's NIFTY_SECURITY_ID already use in production) -
# adding a symbol here that isn't in that dict raises clearly rather than
# silently resolving nothing. Deliberately does NOT gain WS-based candle
# reconstruction (Swing/candle_feed.py) - an index tick's volume
# semantics aren't confirmed the way MCX's were before that got WS
# support, so an index watchlist symbol stays on the REST-only fetch
# path (already proven correct - see should_delay_ce_entry, which has
# used this exact segment/instrument_type in production since before
# this file's own MCX support existed).
INDEX_SYMBOLS = {s.strip().upper() for s in os.getenv("SWING_INDEX_SYMBOLS", "NIFTY,BANKNIFTY").split(",") if s.strip()}

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

# Volume-floor entry gate for MCX symbols ONLY (promoted from shadow-mode
# analysis, 16 Sep 2026, user request: "enable this to SWING strategy but
# for MCX only") - NSE-equity watchlist symbols (ADANIPORTS/ANGELONE/
# COALINDIA) are never gated by this. Blocks a new MCX entry if the
# underlying futures contract's own 5-min entry candle traded on less
# than MCX_VOLUME_FLOOR_RATIO_MIN times its 20-bar average volume - same
# threshold and rationale as Options' own VOLUME_FLOOR_GATE_ENABLED (see
# that flag's own docstring in Options/config.py for the full backtest
# evidence). A real, if smaller, live confirmation exists too: COPPER's
# own real entry on 16 Sep 2026 had a volume ratio of 0.688 (would have
# been blocked) and was later stopped out for a real loss. Deliberately a
# flag, default ENABLED per explicit user request ("make it enabled as of
# now") - can be switched off instantly via .env without touching any
# strategy logic.
MCX_VOLUME_FLOOR_GATE_ENABLED = os.getenv("SWING_MCX_VOLUME_FLOOR_GATE_ENABLED", "true").lower() == "true"
MCX_VOLUME_FLOOR_RATIO_MIN = float(os.getenv("SWING_MCX_VOLUME_FLOOR_RATIO_MIN", "1.2"))

# Same volume-floor gate, extended to every NON-MCX watchlist symbol (18 Sep
# 2026, user request after investigating the ANGELONE 29 SEP 295 PUT real
# loss of Rs 4,125 on 17 Sep 2026) - independently controllable from the MCX
# gate above, not a shared flag, so either can be tuned/disabled without
# touching the other. That ANGELONE entry had a shadow reversal-filter
# VolRatio of 0.01 at entry time (recommended_combo_blocks=True, logged but
# not enforced outside MCX) - almost zero entry-candle volume, the same
# thin-liquidity pattern this gate already blocks for MCX. The 1.2x
# threshold reuses the SAME value MCX_VOLUME_FLOOR_RATIO_MIN defaults to,
# which is itself no coincidence: that number was originally derived from a
# 15-day shadow-mode backtest across Options/Futures/Luxury REAL trades
# (see MCX_VOLUME_FLOOR_RATIO_MIN's own docstring) - i.e. non-MCX data -
# before ever being applied to MCX, so there is already real empirical
# grounding for using it here too, not a blind copy. Default ENABLED,
# consistent with the MCX gate's own "make it enabled as of now" precedent.
NSE_VOLUME_FLOOR_GATE_ENABLED = os.getenv("SWING_NSE_VOLUME_FLOOR_GATE_ENABLED", "true").lower() == "true"
NSE_VOLUME_FLOOR_RATIO_MIN = float(os.getenv("SWING_NSE_VOLUME_FLOOR_RATIO_MIN", "1.2"))

# Same volume-floor gate again, this time for INDEX_SYMBOLS only (NIFTY/
# BANKNIFTY) - added 24 Sep 2026, user request, explicitly NOT sharing
# MCX's or NSE-equity's own flag/threshold (an index's own candle volume
# is real, confirmed non-zero data - see Swing/signals.py's _underlying_
# reference's own INDEX branch and its real-data verification - but its
# scale/character is genuinely different from a single stock's, so a
# separate, independently-tunable threshold is the right default rather
# than forcing it to share NSE_VOLUME_FLOOR_RATIO_MIN's 1.2x). User-set
# default of 0.6x is deliberately LOWER than the 1.2x stock/MCX floor -
# not yet backtested against real index trades the way 1.2x was for MCX/
# NSE (see NSE_VOLUME_FLOOR_RATIO_MIN's own docstring for that lineage) -
# revisit once there's real NIFTY/BANKNIFTY trade history to check it
# against.
INDEX_VOLUME_FLOOR_GATE_ENABLED = os.getenv("SWING_INDEX_VOLUME_FLOOR_GATE_ENABLED", "true").lower() == "true"
INDEX_VOLUME_FLOOR_RATIO_MIN = float(os.getenv("SWING_INDEX_VOLUME_FLOOR_RATIO_MIN", "0.6"))

# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------
MARKET_TZ = "Asia/Kolkata"
MONITOR_INTERVAL_SECONDS = int(os.getenv("SWING_MONITOR_INTERVAL_SECONDS", "5"))

# Real incident, ANGELONE 29 SEP 295 PUT, 17 Sep 2026: Dhan's live-quote
# endpoint went opaquely dark for this one contract for 20+ minutes
# (confirmed via a direct raw quote call returning status=failure with
# null error details, while a fresh re-authenticated session got a normal
# quote back immediately - Dhan-side flakiness on that one contract's live
# feed, not a code bug, but with no bound on how long it can last). See
# also trading-skills' incidents/2026-09-10-icicipruli-unmonitorable-
# position.md (Options) for the same failure mode, and its own "fix
# direction, not yet built" that this setting finally builds. A position
# _check_one_position can't price has ZERO active exit-ladder protection
# (target/trailing-SL/MAX_LOSS/regime-reversal all need a real LTP) for as
# long as the outage lasts - only a resting broker-side stop-loss order
# (BROKER_STOP_LOSS_ENABLED) still protects it independently in the
# meantime. Once _get_ltp has failed continuously for this many minutes on
# an open position, force a market exit (using get_last_historical_close
# as a rough logging mark - the option's own historical 1-min feed kept
# working through the ICICIPRULI outage even though live quotes didn't)
# rather than continuing to hold something un-monitorable. _exit_position's
# own existing stale-pending-order check also cancels any resting
# broker-side SL before placing the fresh exit, so this closes both the
# position AND its own protective order together.
LTP_STALE_FORCE_EXIT_MINUTES = float(os.getenv("SWING_LTP_STALE_FORCE_EXIT_MINUTES", "5"))

# Real incident 18 Sep 2026: without a market-hours guard, the LTP-
# staleness timer above accumulates through the ordinary pre-market
# silence too (every symbol genuinely has zero live quotes before its own
# exchange opens), so by the time the market opens the threshold has often
# ALREADY been crossed - forcing a false-positive exit within the first
# tick of the trading day. Confirmed live: this exact gap closed OIL
# (Options) at a real -Rs 280 loss the same morning, mislabeled as
# MAX_LOSS_HIT in the trade log. Defaults to MCX's own open (09:00, not
# NSE's 09:15) since Swing trades both (MCX_SYMBOLS) - the earlier of the
# two is the safe choice: a pure-NSE symbol's timer just starts up to 15
# minutes earlier than strictly necessary, which is harmless, whereas
# using 09:15 would leave the same false-positive window open for MCX
# positions between 09:00-09:15.
MARKET_OPEN_TIME = os.getenv("SWING_MARKET_OPEN_TIME", "09:00")

# Weekly square-off (user request 22/23 Sep 2026): Swing positions are
# meant to carry across DAYS by design (see this module's own docstring),
# but never across a WEEKEND - a Friday-evening position in particular
# would otherwise sit unmonitored (no live ticks, no exit-signal refresh -
# see _symbol_market_open's own docstring) through two full non-trading
# days of gap risk before Monday. Every open NON-MCX Swing position (NSE
# equities/futures/options) gets force-closed at this time every Friday,
# and no new entry is taken for the rest of the week (see trading_engine.
# py's _monitor_tick) - 15:25, 5 minutes before NSE's own 15:30 close.
# MCX_SYMBOLS positions are excluded here - see MCX_FRIDAY_SQUARE_OFF_
# TIME below (added 25 Sep 2026, user request: MCX should square off 5
# minutes before ITS OWN Friday-night close, not NSE's much earlier one).
FRIDAY_SQUARE_OFF_ENABLED = os.getenv("SWING_FRIDAY_SQUARE_OFF_ENABLED", "true").lower() == "true"
FRIDAY_SQUARE_OFF_TIME = os.getenv("SWING_FRIDAY_SQUARE_OFF_TIME", "15:25")

# MCX's own Friday square-off time (user request 25 Sep 2026: "For MCX
# trades square off time should be 5 mins before market closes on Friday
# night"). MCX_SYMBOLS (COPPER/CRUDEOIL/NATURALGAS) trade a much longer
# Friday session than NSE (close ~23:30 IST vs NSE's 15:30), so they get
# their own, later square-off instead of being swept up in
# FRIDAY_SQUARE_OFF_TIME above - same 5-minutes-before-close idea, just
# timed off MCX's own close. Gated by the SAME FRIDAY_SQUARE_OFF_ENABLED
# flag as the NSE one (no separate on/off switch - this is a timing split
# of one feature, not a second feature). See trading_engine.py's
# _monitor_tick for where this is checked and enforced.
MCX_FRIDAY_SQUARE_OFF_TIME = os.getenv("SWING_MCX_FRIDAY_SQUARE_OFF_TIME", "23:25")

# Daily (not just weekly) square-off, INDEX_SYMBOLS (NIFTY/BANKNIFTY) ONLY -
# user request 24 Sep 2026 ("all open positions for NIFTY and BANKNIFTY to
# be strictly squared off at 3:25 daily with no carry forward over the
# night as they are very volatile instruments"). Every OTHER Swing symbol
# (NSE equities, COPPER/CRUDEOIL/NATURALGAS) keeps carrying across days
# exactly as today - see FRIDAY_SQUARE_OFF_TIME's own docstring above for
# why THAT one stays weekly, not daily, for everyone else. Same 15:25
# default (5 minutes before NSE close) as the Friday one, but this fires
# EVERY trading day, not just Friday, and only ever touches NIFTY/
# BANKNIFTY positions - see Swing/trading_engine.py's _monitor_tick for
# where this is checked and enforced (both the forced square-off itself
# and blocking a fresh same-day index re-entry after this time).
INDEX_DAILY_SQUARE_OFF_ENABLED = os.getenv("SWING_INDEX_DAILY_SQUARE_OFF_ENABLED", "true").lower() == "true"
INDEX_DAILY_SQUARE_OFF_TIME = os.getenv("SWING_INDEX_DAILY_SQUARE_OFF_TIME", "15:25")

# Cooldown before re-attempting entry for the same symbol after a failed
# (non-TRADED, error, or skipped) entry attempt - added 15 Sep 2026 after a
# real incident: with no cooldown, a persistent failure (e.g. a genuinely
# stuck AMO tag from the MCX-hours bug, or a real insufficient-funds
# rejection) got retried on the very next MONITOR_INTERVAL_SECONDS tick,
# placing 4 duplicate real broker orders for COPPER in under a minute
# before the retries were finally blocked. A transient one-off failure
# (a single rate-limited LTP call) just waits out this cooldown and tries
# again - this only prevents the HOT retry loop, not retrying at all.
ENTRY_RETRY_COOLDOWN_SECONDS = int(os.getenv("SWING_ENTRY_RETRY_COOLDOWN_SECONDS", "180"))
# Inter-symbol delay while scanning the watchlist for entry signals -
# respects Dhan's market-data rate limits on back-to-back calls, same
# rationale as every other per-symbol pacing sleep in this codebase.
SYMBOL_PACING_SECONDS = float(os.getenv("SWING_SYMBOL_PACING_SECONDS", "0.35"))
ORDER_TAG_PREFIX = os.getenv("SWING_ORDER_TAG_PREFIX", "Sw2")
