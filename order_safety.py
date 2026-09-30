"""
Never leave an unfilled entry order resting at the broker (30 Sep 2026).

Live incident (Super Bollinger, SONACOMS 27 OCT 830 CALL x1225): the market BUY
came back PENDING after the ~6s wait - Dhan had turned it into a LIMIT at 28.05
under the ask (market-price protection) - and the "TRADED-only" fill rule
treated the entry as failed and simply LEFT the order at Dhan. It blocked
~Rs 34k of funds and, had it filled later, would have been a real position
nobody managed (no stop, no square-off). Every entry path had the same shape.

cancel_unfilled() is the shared fix: if the order is still open (TRANSIT /
PENDING / PART_TRADED) when the wait ends, cancel it and read its final status
once more. A fill that raced the cancel comes back TRADED - the caller then
continues as a normal fill. Used by Super Bollinger (entries + hedges),
Bollinger and Swing.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from Options.dhan_client import OrderResult, OrderStatus, dhan_wrapper

logger = logging.getLogger("order_safety")


async def cancel_unfilled(order_id: str, result: OrderResult, is_amo: bool = False, polls: int = 4
                          ) -> tuple[OrderResult, Optional[str]]:
    """-> (final result, cancel error or None). A terminal `result` is returned untouched."""
    if result.status not in OrderStatus.OPEN_STATUSES:
        return result, None
    loop = asyncio.get_running_loop()
    cancel_error = None
    try:
        await loop.run_in_executor(None, dhan_wrapper.cancel_order, order_id)
    except Exception as exc:  # noqa: BLE001
        cancel_error = repr(exc)
        logger.warning("cancel of unfilled order %s failed (%s) - re-checking its status", order_id, cancel_error)
    final = result
    try:
        final = await asyncio.wait_for(
            loop.run_in_executor(None, dhan_wrapper.wait_for_order_result, order_id, is_amo, polls, 1.0), timeout=polls + 16)
    except Exception:  # noqa: BLE001
        logger.exception("could not re-check order %s after the cancel", order_id)
    if final.status in OrderStatus.OPEN_STATUSES:
        logger.error("order %s is STILL OPEN at the broker (status=%s) after a cancel attempt - cancel it by hand",
                     order_id, final.status)
    if final.status != OrderStatus.TRADED and final.filled_quantity:
        logger.error("order %s ended %s with %s qty FILLED - that quantity is NOT managed by the bot", order_id,
                     final.status, final.filled_quantity)
    return final, cancel_error


def outcome_event(final: OrderResult) -> str:
    if final.status == OrderStatus.TRADED:
        return "ORDER_FILLED_DURING_CANCEL"
    if final.status in OrderStatus.OPEN_STATUSES:
        return "ORDER_STILL_RESTING_AT_BROKER"
    return "ORDER_UNFILLED_CANCELLED"
