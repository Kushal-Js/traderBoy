"""Detect a position that was closed OUTSIDE the bot (e.g. a manual exit in
the Dhan app) before the bot sends its own exit SELL.

Real incident, 28 Sep 2026: a real Swing NATURALGAS 23 OCT 300 CALL was
closed by hand (the broker-side SL-L was cancelled first, then a manual
market sell). The bot noticed only that its SL-L "ended as CANCELLED" and
kept tracking the position - its next exit signal would have sent a real
SELL for a contract no longer held, i.e. opened a naked short.

Used by every package's _check_broker_stop_already_filled (SL-L ended
without firing) and _exit_position (before the first exit order).

A single zero read is NOT trusted: Dhan's positions API can lag a fill by a
moment, and some positions exit seconds after entry. Flat is confirmed only
by two zero reads RECHECK_DELAY_SECONDS apart. Any error -> None ("unknown"),
and callers carry on exactly as before (fail open - never block a real exit).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from Options.dhan_client import dhan_wrapper

logger = logging.getLogger("broker_flat_check")

RECHECK_DELAY_SECONDS = 2.0
_READ_TIMEOUT_SECONDS = 10.0


async def confirmed_flat(read_qty: Callable[[], int]) -> Optional[bool]:
    """True only if the broker shows zero net quantity twice, 2s apart.
    False if it shows a position. None if the broker couldn't be read."""
    loop = asyncio.get_running_loop()
    try:
        qty = await asyncio.wait_for(loop.run_in_executor(None, read_qty), timeout=_READ_TIMEOUT_SECONDS)
        if qty != 0:
            return False
        await asyncio.sleep(RECHECK_DELAY_SECONDS)
        qty = await asyncio.wait_for(loop.run_in_executor(None, read_qty), timeout=_READ_TIMEOUT_SECONDS)
        return qty == 0
    except Exception:  # noqa: BLE001
        logger.exception("could not read the broker's net quantity - treating it as unknown")
        return None


async def last_price(trading_symbol: str, fallback: float) -> float:
    """Best-effort mark price for recording a position closed outside the
    bot (its real fill price isn't known here): the cached live LTP, else
    `fallback`."""
    try:
        ltp = dhan_wrapper.get_cached_option_ltp(trading_symbol)  # in-memory read (memoized lookup) - no worker thread (1 Oct 2026)
    except Exception:  # noqa: BLE001
        ltp = None
    return ltp or fallback
