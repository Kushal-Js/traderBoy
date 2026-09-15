"""
Paper01: a real-time, paper-only twin of the Options strategy (user
request 15 Sep 2026 - "exact same rules, entry, exit conditions like
Options package for both PE and CE web hook urls which never uses real
money"). Every entry/exit RULE (ranking, entry cutoffs, exit ladder, RSI
re-entry block, loss-repeat block, gap-down CE delay, quantity sizing) is
read directly from Options/config.py at call time throughout
Paper01/trading_engine.py - never copied here - so "exact same rules"
holds by construction and stays true automatically as Options' own
thresholds get tuned later, with zero drift risk.

The ONLY things this file defines are what must be independently
controlled because they're a *capacity pool*, not a *rule* - Paper01's
own position capacity must never compete with or be affected by Options'
real capacity, and vice versa.
"""
import os

# Hard safety invariant, not just a label - see Paper01/trading_engine.py
# and Paper01/paper01_main.py's own docstrings and the assertion at
# startup. Matches IndexScalping/config.py's and K01/config.py's exact
# wording/pattern for the same invariant.
PAPER_TRADING_ONLY = True

# Independent capacity pool - default 2 CE + 2 PE concurrent paper
# positions at once, per explicit user instruction (not derived from
# whatever Options' own MAX_LIVE_POSITIONS_CE/_PE happen to be live at
# the time - deliberately its own fixed, independently-tunable value).
MAX_LIVE_POSITIONS_CE = int(os.getenv("PAPER01_MAX_LIVE_POSITIONS_CE", "2"))
MAX_LIVE_POSITIONS_PE = int(os.getenv("PAPER01_MAX_LIVE_POSITIONS_PE", "2"))

# Paper01's own trade-history log name (trade_history.py's dated-file
# convention) - deliberately NOT Options' REAL_TRADES_NAME/
# OPENED_POSITIONS_NAME, so paper trades can never be mixed into the real
# trade ledger (see trade_history.py's own explicit design note on this -
# the same reason K01/IndexScalping never call record_closed_trade).
PAPER_TRADES_LOG_NAME = "paper01_trades"
PAPER_OPENED_LOG_NAME = "paper01_opened"
OPEN_STATE_PATH = "paper01_open.json"
