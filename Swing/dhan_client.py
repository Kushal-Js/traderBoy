"""
Swing reuses the Options package's single authenticated Dhan connection
(dhan_wrapper) rather than opening a second session - the same reuse
pattern Futures/Luxury/CopperOptions/IndexScalping already use for the
same reason (one Dhan account, no benefit to a second login, and a real
risk of tripping Dhan's own authentication rate limiter with one - see
NOTES.md).

Also re-exports get_futures_contract/FuturesContract - genuinely new
capability added 31 Aug 2026 specifically to support this package (the
first strategy in this codebase to trade a real futures contract instead
of buying an ATM option as a placeholder for one). Lives in the shared
Options/dhan_client.py since that's the file that owns the one Dhan
connection and instrument master, same reasoning as everything else
re-exported here.

Also re-exports _compute_supertrend (the pure, module-level Supertrend
implementation - same one CopperOptions/Futures already import) and
_retry - trading_engine.py's own dual-timeframe Supertrend signal (added
31 Aug 2026 for the entry/exit rule) is built directly on these rather
than on Options/dhan_client.py's own single-timeframe Supertrend
cache/refresh mechanism, to avoid any risk to that already-live
exit-protection path for the three real-money strategies relying on it -
see trading_engine.py's own module docstring for why.

`dhan_wrapper` (already re-exported above) also gained
get_today_open_and_prev_close() the same day, for the "today's open >
yesterday's close" entry gate - no separate re-export line needed here
since it's just a new method on the same already-imported singleton.

Same goes for get_margin_required()/get_fund_limits(), added 1 Sep 2026
for paper_engine.py's own margin/funds logging (user request - "logging
real margin and funds required during paper trading so that we can do
analysis also").
"""
from Options.dhan_client import (  # noqa: F401
    AtmOption,
    FuturesContract,
    OrderStatus,
    _compute_supertrend,
    _retry,
    dhan_wrapper,
)
