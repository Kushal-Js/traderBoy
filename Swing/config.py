"""
Central configuration for the Swing strategy.

New package (user request 31 Aug 2026): buys 1 lot of a stock's FUTURES
contract, hedged with 1 lot of its ATM PE option, as a single "basket" -
placed as two separate real orders with an application-level all-or-
nothing guarantee (Dhan has no native basket-order API - see the separate
trading-skills repo's `basket-order-feasibility.md` for the full
investigation this design is based on).

STRATEGY_MODE (added 1 Sep 2026, user request) - Swing now has THREE
entirely different trading-mechanics implementations living side by
side, switched by this ONE flag rather than one replacing another ("we
may need basket strategy again in coming days so a flag would be a
better approach"):
  - "basket" (the original design above): futures + PE bought TOGETHER,
    all-or-nothing, exited together, then back to plain watching.
  - "sequential": "2 different orders running sequentially" - buy ONLY
    the futures contract on entry; when the exit condition fires, SELL
    the futures and BUY the ATM PE instead (as a hedge/hold while
    deciding); exit that PE either on its own rupee loss cap or once the
    entry condition fires again (which also immediately re-buys futures) -
    looping between the two instruments indefinitely for as long as the
    underlying keeps producing signals. See trading_engine.py's own
    module docstring for the full state-machine diagram and the two
    ambiguous points confirmed with the user before building this
    (AskUserQuestion, 1 Sep 2026): a PE loss-cap exit returns to plain
    watching rather than blindly re-buying futures, and paper trading
    mirrors whichever mode is active here rather than staying pinned to
    basket mode.
  - "basket_hedge" (added 1 Sep 2026, now the DEFAULT/active mode, user
    request: "enabling basket buy strategy but with a caveat") - ENTRY is
    the SAME as "basket" (futures + PE bought together, all-or-nothing).
    But once the exit condition fires, instead of just going flat, sells
    the basket and buys ONE standalone ATM PE hedge instead - held until
    ANY of THREE conditions (user's own numbered list): (1) loss exceeds
    PE_MAX_LOSS_RS, (2) profit exceeds PE_PROFIT_LOCK_RS ("lock profit"),
    or (3) the underlying's 5-min close crosses back ABOVE its own
    Supertrend again - checked as a BARE Supertrend reversal, deliberately
    NOT the full entry signal (user's own words: "even if buy signal is
    not yet triggered" - the price-confirmation gate and 1-min confirm
    timeframe are NOT required for this specific exit). Once the PE hedge
    exits (any of the 3), returns to plain watching for a fresh basket
    entry - same "does not blindly chain into a new position" choice
    already made for sequential mode's own loss-cap exit, since none of
    the three PE-hedge exit conditions carry a confirmed fresh BUY signal
    the way sequential mode's own entry-refire path does.
Every mode's code, config, position store, and (for "basket"/"sequential")
paper-trading engine all coexist unconditionally - flipping this flag
(and restarting) is the only thing needed to switch, no code changes
required in any direction. Paper trading does NOT yet have a
"basket_hedge" implementation (see paper_engine.py's own docstring) -
harmless today since PAPER_TRADING_ENABLED is False, but flag this if
paper trading and basket_hedge mode are ever both wanted at once.

Entry/exit CONDITION logic (added 31 Aug 2026, user request; ENTRY's
price gate RELAXED from a strict gap-up to a broader "at/above
yesterday's close" check on 1 Sep 2026) - a price-confirmation check plus
a dual-timeframe Supertrend signal on the underlying's own STOCK price
(not the futures/option premium - same convention Options/Futures/
Luxury's own SUPERTREND_EXIT already uses):
  - ENTRY: today's price is confirmed at or above yesterday's close -
    true as soon as EITHER today's open >= yesterday's close (checked
    once, at market open) OR the current price has, at any point since,
    reached or crossed above yesterday's close (see trading_engine.py's
    own _is_price_confirmed_above_prev_close for the exact one-way-latch
    caching behavior - an explicit gap-up is no longer required, user's
    own wording: "an explicit gap up is not mandatory... the entry
    condition becomes active when current price cross above yesterday
    close price"), AND the 5-min close crosses ABOVE the 5-min
    Supertrend, AND the 1-min close is above (or has itself just crossed
    above) the 1-min Supertrend - the two Supertrend conditions each read
    on their own most recently fully-closed candle.
  - EXIT: the 5-min close crosses BELOW the 5-min Supertrend (unchanged
    since first defined).
  - Both legs of a basket are always entered/exited together (unchanged -
    the all-or-nothing entry/exit design predates and is untouched by
    this price-gate change).
See trading_engine.py's own module docstring for the full implementation
(a self-contained, dual-timeframe crossover detector - deliberately NOT
built on top of Options/dhan_client.py's own single-timeframe Supertrend
cache, to avoid any risk to that already-live exit-protection mechanism
for the three real-money strategies already relying on it).

LIVE as of 1 Sep 2026 (STRATEGY_ENABLED=true, user confirmed explicitly
after a stated-risk confirmation covering the untested-live sequential
mode, then asked for MAX_LIVE_BASKETS lowered to 1 "we will change it
later" as a deliberately cautious first real run) - real money, and a
genuinely new capability for this codebase (the first package to trade
an actual futures contract rather than buying an ATM option as a stand-in
for one - see Options/dhan_client.py's new get_futures_contract()).
Deployed DISABLED from this package's own creation (31 Aug 2026) until
this date, paper-traded in the meantime (PAPER_TRADING_ENABLED, now
turned back off below now that real trading is live - the two were
always meant to be mutually exclusive, see that flag's own comment).
Mode switched again the same day, "sequential" -> "basket_hedge" (see
STRATEGY_MODE's own docstring) - the one real position already open at
that point (APLAPOLLO futures, entered under sequential mode) was
explicitly grandfathered in as a "basket_hedge" BASKET-state position by
user request ("consider the open trade as a basket order for this time
as it is already live") rather than left behind in the now-inactive
sequential mode's own store - see reconcile_basket_hedge_positions()'s
own docstring for how a lone futures leg (no paired PE - true for this
one, since it was never bought as part of an all-or-nothing entry) is
handled as a degraded/incomplete BASKET rather than treated as an
anomaly the way pure "basket" mode's own reconciliation would.

Reuses the Options package's single authenticated Dhan connection (see
this package's own dhan_client.py) - no DHAN_CLIENT_ID/DHAN_PIN/etc. auth
vars here, those only matter to Options/dhan_client.py's authenticate().
"""
import os

from dotenv import load_dotenv

load_dotenv()

# Master on/off switch - see CopperOptions/config.py's identical flag for
# the established pattern this follows: the monitor loop and both
# webhooks stay running/reachable (no restart needed to flip this later)
# but do nothing at all while False - both webhooks return
# status=ignored/reason=strategy_disabled, and monitor_loop's own ticks
# are no-ops. Flipped to DEPLOYED TRUE 1 Sep 2026 (user request, explicit
# confirmation given) - see this file's own module docstring for the
# full context of what went live and what was tightened first.
STRATEGY_ENABLED = os.getenv("SWING_STRATEGY_ENABLED", "false").lower() == "true"

# Which trading-mechanics implementation is active - see this file's own
# module docstring for the full "basket"/"sequential"/"basket_hedge"
# explanation. Deployed "basket_hedge" as of 1 Sep 2026 ("enabling basket
# buy strategy but with a caveat") - the other two modes are fully
# preserved, not deleted, switch back any time by setting this to
# "basket" or "sequential" and restarting.
STRATEGY_MODE = os.getenv("SWING_STRATEGY_MODE", "basket_hedge").lower()

# How many baskets/positions can be live at once - entirely separate
# from every other package's own capacity, since this trades a different
# instrument combination on its own schedule. Shared across all THREE
# modes (a symbol under active management in ANY mode occupies one slot,
# same concept regardless of mode) - safe since only one mode's store is
# ever actually written to at a time.
#
# Lowered 2->1 (user request 1 Sep 2026), deliberately, for the first
# real live run of the new sequential mode ("make capacity smaller to 1,
# we will change it later") - only one symbol can be under active real
# management at a time until the user chooses to raise it again. Still 1
# after the same-day switch to basket_hedge mode.
MAX_LIVE_BASKETS = int(os.getenv("SWING_MAX_LIVE_BASKETS", "2"))

# The standalone PE hedge leg's own hard rupee loss cap (sequential mode,
# and basket_hedge mode's own PE-hedge phase): "Exit this PE option
# contract once loss become more than 2k" (user's own wording, reused
# verbatim for basket_hedge mode's own condition 1). Checked continuously
# (unrealized loss, mark-to-market against current LTP) the same way
# every other rupee-cap in this codebase works (e.g. Options/config.py's
# MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF) - NOT the trigger that re-buys
# futures/re-enters a basket (that's a separate, signal-driven check in
# both modes) - a loss-capped PE exit returns the symbol to plain
# watching instead (user confirmed via AskUserQuestion 1 Sep 2026 for
# sequential mode; explicitly requested outright for basket_hedge mode).
PE_MAX_LOSS_RS = float(os.getenv("SWING_PE_MAX_LOSS_RS", "2000"))

# basket_hedge mode only (added 1 Sep 2026) - condition 2 of the PE
# hedge's own 3-way exit: "Lock profit when it becomes more than 2k"
# (user's own wording) - the mirror image of PE_MAX_LOSS_RS above, on the
# upside. Unrealized profit, mark-to-market against current LTP, checked
# every tick alongside the loss cap.
PE_PROFIT_LOCK_RS = float(os.getenv("SWING_PE_PROFIT_LOCK_RS", "2000"))

QUANTITY_LOTS = int(os.getenv("SWING_QUANTITY_LOTS", "1"))

# "MARGIN" is Tradehull's code for NRML/carry-forward (see Options/config.py's
# identical OPTIONS_PRODUCT for the full rationale) - used for BOTH legs,
# not MIS, since Swing baskets are explicitly meant to be held across
# multiple days, not squared off same-day like every other package here.
FUTURES_PRODUCT = os.getenv("SWING_FUTURES_PRODUCT", "MARGIN")
OPTIONS_PRODUCT = os.getenv("SWING_OPTIONS_PRODUCT", "MARGIN")

MARKET_TZ = "Asia/Kolkata"

MONITOR_INTERVAL_SECONDS = int(os.getenv("SWING_MONITOR_INTERVAL_SECONDS", "5"))

ORDER_TAG_PREFIX = os.getenv("SWING_ORDER_TAG_PREFIX", "Swg")

# Deliberately NO SQUARE_OFF_TIME/ENABLE_SQUARE_OFF here - unlike every
# other package in this codebase, Swing baskets are meant to carry
# overnight/across multiple days by design ("swing" trading), not be
# force-closed at end of day. A manual kill-switch still exists
# (POST /swing/square-off-now) for closing everything on demand.

# --------------------------------------------------------------------------- #
# Supertrend entry/exit signal (user request 31 Aug 2026) - own
# independently-tunable Supertrend parameters, deliberately NOT read from
# Options/config.py's own SUPERTREND_PERIOD/SUPERTREND_MULTIPLIER even
# though they default to the same values - Swing computes its own
# dual-timeframe signal directly (see trading_engine.py), it doesn't go
# through Options/dhan_client.py's single-timeframe cache at all, so
# there's no shared computation to couple these to.
SUPERTREND_PERIOD = int(os.getenv("SWING_SUPERTREND_PERIOD", "10"))
SUPERTREND_MULTIPLIER = float(os.getenv("SWING_SUPERTREND_MULTIPLIER", "3.0"))

# The two timeframes the entry rule reads - "5 min close cross above
# supertrend WITH 1 min close greater than or crossed above 1 min
# supertrend" (user's own wording). Exit only ever reads the entry
# timeframe (5-min). Configurable rather than hardcoded 5/1 in the code,
# consistent with every other tunable in this codebase, even though
# changing them changes what the user's own stated rule actually means.
SUPERTREND_ENTRY_TIMEFRAME_MINUTES = int(os.getenv("SWING_SUPERTREND_ENTRY_TIMEFRAME_MINUTES", "5"))
SUPERTREND_CONFIRM_TIMEFRAME_MINUTES = int(os.getenv("SWING_SUPERTREND_CONFIRM_TIMEFRAME_MINUTES", "1"))

# How often a (symbol, timeframe) Supertrend read is allowed to re-fetch
# from Dhan - see Options/config.py's identical SUPERTREND_REFRESH_SECONDS
# for the same rate-limit-avoidance rationale. Own independent value/cache
# (trading_engine.py's own _supertrend_cache), not shared with Options'.
SUPERTREND_REFRESH_SECONDS = int(os.getenv("SWING_SUPERTREND_REFRESH_SECONDS", "15"))

# --------------------------------------------------------------------------- #
# Daily watchlist prune (user request 1 Sep 2026): "removing any stock
# when its daily close crossed below daily super trend / daily 12 EMA...
# has to run daily when market starts at 9:15 AM." Runs once per trading
# day, gated by a DATE comparison rather than an exact clock-time match
# (see trading_engine._daily_watchlist_prune_tick's own docstring) -
# unlike the entry/exit signal above (which reads intraday 5-min/1-min
# candles), this reads DAILY candles via Dhan's own historical_daily_data
# endpoint, using whichever daily candle is the LAST FULLY CLOSED one
# (yesterday's at market open - today's daily candle can't exist yet).
# Deliberately its own on/off flag, independent of STRATEGY_ENABLED -
# this only ever mutates the watchlist (no order-placement risk), same
# "watchlist hygiene is independent of the trading-enabled flag"
# convention watchlist_store.sync_from_file() already follows.
WATCHLIST_DAILY_PRUNE_ENABLED = os.getenv("SWING_WATCHLIST_DAILY_PRUNE_ENABLED", "true").lower() == "true"

# Reuses the SAME Supertrend period/multiplier as the intraday signal
# above (SUPERTREND_PERIOD/SUPERTREND_MULTIPLIER) - just fed daily
# candles instead of intraday ones; the indicator's own settings aren't
# meant to differ by timeframe, only the candles it's computed over do.
# DAILY_EMA_PERIOD is the "12" in "DAILY 12 EMA" - the user's own number,
# made configurable rather than hardcoded like every other tunable here.
DAILY_EMA_PERIOD = int(os.getenv("SWING_DAILY_EMA_PERIOD", "12"))

# Calendar days of daily-candle history to fetch per symbol for the
# prune check - comfortably more than SUPERTREND_PERIOD/DAILY_EMA_PERIOD
# need to seed (accounting for weekends/holidays eating into calendar
# days - roughly 5 trading days per 7 calendar days), so both indicators
# have settled past their own warm-up window rather than just barely
# meeting the bare minimum candle count.
DAILY_TREND_LOOKBACK_DAYS = int(os.getenv("SWING_DAILY_TREND_LOOKBACK_DAYS", "90"))

# --------------------------------------------------------------------------- #
# Paper trading (added 1 Sep 2026 - "enable paper trading for tomorrow...
# keep track of trades, history and profit loss also like real trades in
# files", turned back OFF the same day once real trading went live -
# "Enable live real market trading for Swing package and disable paper
# trading"). Entirely INDEPENDENT of STRATEGY_ENABLED above - the two are
# meant to be mutually exclusive in practice (paper trading exists to
# evaluate the signal BEFORE trusting it with a real order; once
# STRATEGY_ENABLED is genuinely live, paper trading's own job is done)
# though nothing in the code actually enforces that exclusivity - both
# could be flipped on together if ever useful again. See
# paper_engine.py's own module docstring for the full design (simulated
# fills at current LTP, its own on-disk log entirely separate from real
# trade history, no capacity cap since nothing here risks real capital).
# Mirrors STRATEGY_MODE for "basket"/"sequential" (user confirmed via
# AskUserQuestion) - paper trading simulates whichever of THOSE two modes
# is active, so paper results actually reflect what real trading would do
# if turned on right now. Does NOT yet have a "basket_hedge" simulation
# (added 1 Sep 2026, after paper trading was already turned off for
# going live) - harmless while this stays False, but if paper trading and
# basket_hedge mode are ever both wanted at once, paper_engine.py's own
# poll loop needs a third tick implementation first (see its own
# docstring).
PAPER_TRADING_ENABLED = os.getenv("SWING_PAPER_TRADING_ENABLED", "false").lower() == "true"
