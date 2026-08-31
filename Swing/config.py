"""
Central configuration for the Swing strategy.

New package (user request 31 Aug 2026): buys 1 lot of a stock's FUTURES
contract, hedged with 1 lot of its ATM PE option, as a single "basket" -
placed as two separate real orders with an application-level all-or-
nothing guarantee (Dhan has no native basket-order API - see the separate
trading-skills repo's `basket-order-feasibility.md` for the full
investigation this design is based on).

Entry/exit CONDITION logic (added 31 Aug 2026, user request, same day as
this package's own creation): a dual-timeframe Supertrend signal on the
underlying's own STOCK price (not the futures/option premium - same
convention Options/Futures/Luxury's own SUPERTREND_EXIT already uses):
  - ENTRY: the 5-min close crosses ABOVE the 5-min Supertrend, AND the
    1-min close is above (or has itself just crossed above) the 1-min
    Supertrend - both conditions read on their own most recently fully-
    closed candle.
  - EXIT: the 5-min close crosses BELOW the 5-min Supertrend.
See trading_engine.py's own module docstring for the full implementation
(a self-contained, dual-timeframe crossover detector - deliberately NOT
built on top of Options/dhan_client.py's own single-timeframe Supertrend
cache, to avoid any risk to that already-live exit-protection mechanism
for the three real-money strategies already relying on it).

DEPLOYED DISABLED by default (STRATEGY_ENABLED=false) - real money, and a
genuinely new capability for this codebase (the first package to trade an
actual futures contract rather than buying an ATM option as a stand-in
for one - see Options/dhan_client.py's new get_futures_contract()). Turn
on only once this logic has been reviewed/tested to the user's
satisfaction.

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
# are no-ops. Deployed False - see this file's own module docstring.
STRATEGY_ENABLED = os.getenv("SWING_STRATEGY_ENABLED", "false").lower() == "true"

# How many baskets (futures leg + PE leg pair) can be live at once -
# entirely separate from every other package's own capacity, since this
# trades a different instrument combination on its own schedule.
MAX_LIVE_BASKETS = int(os.getenv("SWING_MAX_LIVE_BASKETS", "2"))

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
