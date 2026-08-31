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
"""
from Options.dhan_client import (  # noqa: F401
    AtmOption,
    FuturesContract,
    OrderStatus,
    _compute_supertrend,
    _retry,
    dhan_wrapper,
)
