"""
Option prices for thinly traded contracts (30 Sep 2026).

Two live problems the same afternoon, both from relying on the LAST TRADED
price alone:
  - PNBHOUSING (12:45) and LAURUSLABS (12:46): the premium gate's price call
    returned nothing three times and two real entries were skipped as
    "ENTRY_SKIPPED_LOW_PREMIUM" with premium = null. A missing price is not a
    low premium.
  - APLAPOLLO 2240 CE: the hedge trigger was only seen at 15:03:56 although
    the stock's low was at 14:55 - a last-traded price only moves when the
    option trades, and 282 price-call attempts failed in that half hour.
The order book does not have either problem: Dhan's quote API returns the live
best bid and ask even when nothing has traded.

  price_for_entry()  last traded price, else the quote (ask, the side we buy);
  live_price()       the normal price read for an open position, falling back
                     to the quote's mid when the price call fails (real
                     positions only - paper keeps its own cheap path);
  mark_for_loss()    for judging an open loss close to a trigger: the lower of
                     the last traded price and the live bid/ask mid.
Quote calls are rate limited by Dhan (~1/s, enforced in dhan_client), so
mark_for_loss re-reads a contract at most every MARK_MAX_AGE_SECONDS.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from Bollinger import trading_engine as engine
from Bollinger.position_store import Position
from Options.dhan_client import dhan_wrapper

logger = logging.getLogger("super_bollinger_pricing")

MAX_SPREAD_PCT = 15.0          # a wider book than this says nothing useful about fair value
MARK_MAX_AGE_SECONDS = 10.0
_quotes: dict[str, tuple[float, Optional[dict]]] = {}


async def quote(trading_symbol: str, max_age: float = 0.0) -> Optional[dict]:
    """{"ltp","bid","ask",...} or None. max_age > 0 reuses a recent read."""
    now = time.monotonic()
    cached = _quotes.get(trading_symbol)
    if cached and max_age > 0 and now - cached[0] <= max_age:
        return cached[1]
    try:
        q = await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.get_option_quote, trading_symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("no quote for %s (%r)", trading_symbol, exc)
        q = None
    _quotes[trading_symbol] = (now, q)
    return q


def mid_of(q: Optional[dict]) -> Optional[float]:
    """Bid/ask midpoint when both sides exist and the spread is sane."""
    if not q or not q.get("bid") or not q.get("ask"):
        return None
    mid = (q["bid"] + q["ask"]) / 2
    if mid <= 0 or (q["ask"] - q["bid"]) / mid * 100 > MAX_SPREAD_PCT:
        return None
    return mid


async def price_for_entry(trading_symbol: str) -> tuple[Optional[float], str]:
    """(price, source) for the premium gate. source: "ltp" | "quote_ask" | "quote_mid" | "quote_ltp" | "none"."""
    try:
        price = await dhan_wrapper.get_option_ltp_async(trading_symbol)
        if price:
            return price, "ltp"
    except Exception:  # noqa: BLE001
        logger.warning("price call failed for %s - trying the order book", trading_symbol)
    q = await quote(trading_symbol)
    if q:
        if q.get("ask"):
            return q["ask"], "quote_ask"
        if mid_of(q):
            return mid_of(q), "quote_mid"
        if q.get("ltp"):
            return q["ltp"], "quote_ltp"
    return None, "none"


async def live_price(position: Position) -> float:
    """engine._get_ltp with an order-book fallback for REAL positions. Raises
    like _get_ltp when there is no price at all."""
    try:
        return await engine._get_ltp(position)
    except Exception:
        if engine.is_paper_position(position):
            raise
        q = await quote(position.trading_symbol, max_age=2.0)
        price = mid_of(q) or (q or {}).get("ltp")
        if not price:
            raise
        logger.info("%s: price from the order book (%.2f) after the price call failed", position.trading_symbol, price)
        return price


async def mark_for_loss(position: Position, ltp: float) -> tuple[float, str]:
    """The price to judge an open LOSS by when it is close to a trigger: the
    lower of the last traded price and the live bid/ask mid. -> (price, "ltp" | "mid")."""
    mid = mid_of(await quote(position.trading_symbol, max_age=MARK_MAX_AGE_SECONDS))
    if mid is not None and mid < ltp:
        return mid, "mid"
    return ltp, "ltp"
