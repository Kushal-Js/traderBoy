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
"""
from Options.dhan_client import (  # noqa: F401
    AtmOption,
    FuturesContract,
    OrderStatus,
    dhan_wrapper,
)
