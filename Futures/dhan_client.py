"""
Futures reuses the Options package's single authenticated Dhan connection
(dhan_wrapper) rather than opening a second session - the same reuse
pattern IndexScalping/CopperOptions already use for the same reason (one
Dhan account, no benefit to a second login). Options' lifespan runs first
in main.py's nesting specifically so this connection is already
authenticated and feed-started by the time this package's own lifespan
runs. See NOTES.md's design-decision entry for the Futures package.

_compute_supertrend is the pure, module-level Supertrend implementation -
re-exported here rather than duplicated, same as CopperOptions imports it.
"""
from Options.dhan_client import OrderStatus, _compute_supertrend, dhan_wrapper  # noqa: F401
