"""
Tunables for the index scalping strategy - deliberately separate from
Options/config.py since this is a different strategy with a different
risk profile (seconds-to-minutes holds vs. the options bot's
minutes-to-hours), not because it needs different broker credentials
(it reuses Options.dhan_client's already-authenticated connection - see
IndexScalping/index_main.py's own docstring for why).

Defaults mirror the backtest run against 19-21 Aug 2026 real NIFTY/
BANKNIFTY data (see NOTES.md's index-scalping entry and
BACKTEST_RESULTS.md) - that backtest was only a 3-day mechanism
sanity-check (index options' weekly/near-term expiry means older
contracts are delisted from Dhan's instrument master entirely, so
further history isn't resolvable), not a validated edge. This strategy
runs in PAPER mode only until real evidence says otherwise - see
PAPER_TRADING_ONLY below.
"""
import os

PAPER_TRADING_ONLY = True  # see IndexScalping/paper_engine.py - hard safety invariant, not just a label

INDEX_SECURITY_ID = {"NIFTY": "13", "BANKNIFTY": "25"}  # NSE index (spot) segment IDX_I

TOP_N_OPTIONS = 1  # always ATM only for now, no ranking needed (single underlying per signal)

OPENING_RANGE_MINUTES = int(os.getenv("SCALP_OPENING_RANGE_MINUTES", "15"))
EMA_FAST_PERIOD = int(os.getenv("SCALP_EMA_FAST_PERIOD", "3"))
EMA_SLOW_PERIOD = int(os.getenv("SCALP_EMA_SLOW_PERIOD", "8"))
TARGET_PCT = float(os.getenv("SCALP_TARGET_PCT", "0.10"))
STOP_LOSS_PCT = float(os.getenv("SCALP_STOP_LOSS_PCT", "0.06"))
MAX_HOLD_MINUTES = int(os.getenv("SCALP_MAX_HOLD_MINUTES", "3"))
MAX_TRADES_PER_DAY = int(os.getenv("SCALP_MAX_TRADES_PER_DAY", "4"))

# Cost modeling - the whole point of tracking gross vs. net separately in
# paper mode, since transaction costs dominate scalping economics far
# more than the options bot's minutes-to-hours holds. Flat estimate for
# brokerage + statutory charges (STT, exchange fees, GST, stamp duty) per
# completed round-trip trade, plus a slippage haircut applied to both
# entry and exit fills as a stand-in for bid-ask spread (no L2/order-book
# data available via this API).
ROUND_TRIP_COST_RS = float(os.getenv("SCALP_ROUND_TRIP_COST_RS", "40.0"))
SLIPPAGE_PCT = float(os.getenv("SCALP_SLIPPAGE_PCT", "0.005"))

QUANTITY_LOTS = int(os.getenv("SCALP_QUANTITY_LOTS", "1"))

MARKET_OPEN = "09:15"
SQUARE_OFF_TIME = os.getenv("SCALP_SQUARE_OFF_TIME", "15:15")

# How often the paper-trading poll loop re-checks index candles / open
# paper-position LTPs. Not tick-driven (see index_main.py's docstring for
# why) - 15s is fast enough to meaningfully test the strategy's signal
# logic without hammering Dhan's rate limits (bug #5 in NOTES.md).
POLL_INTERVAL_SECONDS = int(os.getenv("SCALP_POLL_INTERVAL_SECONDS", "15"))

PAPER_LOG_PATH = os.getenv("SCALP_PAPER_LOG_PATH", "paper_trades.log")
