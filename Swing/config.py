"""
Central configuration for the Swing strategy.

New package (user request 31 Aug 2026): buys 1 lot of a stock's FUTURES
contract, hedged with 1 lot of its ATM PE option, as a single "basket" -
placed as two separate real orders with an application-level all-or-
nothing guarantee (Dhan has no native basket-order API - see the separate
trading-skills repo's `basket-order-feasibility.md` for the full
investigation this design is based on).

Entry and exit CONDITION logic (when to actually trigger a basket, when
to close one) is deliberately NOT built yet - the user will define this
later. What's built now is the MECHANICS: the watchlist, the two
webhooks, the all-or-nothing basket placement/rollback, capacity, and
reconciliation - all the plumbing a future signal can plug into without
anything else here needing to change. See trading_engine.py's own
docstring for the two clearly-marked extension points
(`_evaluate_watchlist_entry_signal`/`_evaluate_basket_exit_signal`).

DEPLOYED DISABLED by default (STRATEGY_ENABLED=false) - real money, and a
genuinely new capability for this codebase (the first package to trade an
actual futures contract rather than buying an ATM option as a stand-in
for one - see Options/dhan_client.py's new get_futures_contract()). Turn
on only once entry/exit logic is actually defined and tested.

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

# Deliberately NO TARGET_PCT/STOP_LOSS_PCT/etc. here either - exit
# condition logic is explicitly deferred to the user, see this file's own
# module docstring. Adding tunables for logic that doesn't exist yet
# would be a config surface that looks real but silently isn't - see
# Futures/config.py's own docstring for why this codebase avoids that
# trap deliberately.
