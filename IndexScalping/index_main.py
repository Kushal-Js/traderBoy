"""
Index scalping strategy - PAPER TRADING ONLY (see paper_engine.py's
safety invariant docstring; config.PAPER_TRADING_ONLY is asserted at
startup). Signal: opening-range breakout + short EMA momentum on
NIFTY/BANKNIFTY's own 1-min index candles - buy ATM CE/PE, exit on a
tight target/stop or a hard time-box, whichever comes first. See
NOTES.md's index-scalping entry for the backtest that motivated this
(a 3-day mechanism sanity-check, not a validated edge - real validation
needs the sample size only paper-trading over real weeks can provide,
since index options' weekly/near-term expiry means Dhan's instrument
master doesn't retain enough historical contracts to backtest further).

Deliberately REST-polling (config.POLL_INTERVAL_SECONDS, default 15s),
not tick-driven off the WebSocket feed like the options bot's exits.
Building a real-time 1-min-bar aggregator from raw index ticks would be
a meaningfully bigger, riskier engineering lift for a paper-only feature
whose main open question right now is whether the *signal logic* holds
up over more data, not execution speed - 15s is fast enough to test that
without hammering Dhan's rate limits. Worth revisiting if paper results
ever look promising enough to consider real capital, at which point
execution latency would start to matter for real.

Exports `router` + `lifespan`, mounted onto the shared app by main.py
alongside Options/option_main.py's own router + lifespan.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI

from . import config
from .paper_engine import poll_loop, snapshot

logger = logging.getLogger("index_scalping")

router = APIRouter()

_poll_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts the paper-trading poll loop. Does not authenticate with
    Dhan itself - reuses the connection Options/option_main.py's own
    lifespan already established (see paper_engine.py's docstring for
    why sharing that connection is the right call here). main.py's
    combined lifespan runs Options' lifespan first, so by the time this
    starts, dhan_wrapper is already authenticated."""
    global _poll_task
    assert config.PAPER_TRADING_ONLY, "Refusing to start index scalping: PAPER_TRADING_ONLY must stay True."
    _poll_task = asyncio.create_task(poll_loop())
    logger.info("Index scalping (PAPER ONLY) startup complete.")
    yield
    if _poll_task:
        _poll_task.cancel()


@router.get("/scalping/paper-trades")
async def get_paper_trades():
    """Completed + currently-open paper trades, gross vs. net P&L
    (net = after the estimated round-trip cost + slippage haircut - see
    config.ROUND_TRIP_COST_RS / SLIPPAGE_PCT), most recent first. No real
    orders are ever placed by this strategy - see paper_engine.py."""
    return snapshot()
