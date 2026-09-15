"""
Paper01 shares the one already-authenticated Dhan connection every other
package reuses - see Options/dhan_client.py's own module docstring for
the singleton. Same re-export pattern as Futures/dhan_client.py.

SAFETY: Paper01 must NEVER import place_market_order,
place_stop_loss_limit_order, place_stop_loss_market_order, cancel_order,
or dhan_wrapper.client.order_placement - only this one read-only
singleton import exists anywhere in this package (verified by
tests/test_paper01_safety_invariant.py's source scan).
"""
from Options.dhan_client import dhan_wrapper  # noqa: F401
