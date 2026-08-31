"""
Luxury reuses the Options package's single authenticated Dhan connection
(dhan_wrapper) rather than opening a second session - the same reuse
pattern IndexScalping/CopperOptions/Futures already use for the same
reason (one Dhan account, no benefit to a second login - and a second
fresh login risks tripping Dhan's own authentication rate limiter, see
NOTES.md). Options' lifespan runs first in main.py's nesting specifically
so this connection is already authenticated and feed-started by the time
this package's own lifespan runs.

_compute_supertrend is the pure, module-level Supertrend implementation -
re-exported here rather than duplicated, same as CopperOptions/Futures
import it.
"""
from Options.dhan_client import OrderStatus, _compute_supertrend, dhan_wrapper  # noqa: F401
