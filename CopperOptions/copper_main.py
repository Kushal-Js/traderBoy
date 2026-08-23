"""
Copper (MCX) options-buying strategy - PAPER TRADING ONLY (see
paper_engine.py's safety invariant; config.PAPER_TRADING_ONLY is
asserted at startup). Gap + daily-RSI momentum gate, confirmed by two
5-min Supertrends agreeing, buys ATM+/-20-point CE/PE on the Copper
futures options chain. See NOTES.md's copper-options entry for the full
rules and the assumptions made where they were underspecified.

config.STRATEGY_ENABLED is the on/off switch requested for after paper
results are in - when False, the poll loop stays running (so the
process doesn't need a restart to flip it) but does nothing at all: no
data fetches, no signal checks, no side effects.

Same REST-polling design as IndexScalping/index_main.py, for the same
reason - see that module's docstring.

Exports `router` + `lifespan`, mounted onto the shared app by main.py
alongside Options/option_main.py and IndexScalping/index_main.py.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI

from . import config
from .paper_engine import poll_loop, snapshot

logger = logging.getLogger("copper_options")

router = APIRouter()

_poll_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts the paper-trading poll loop. Reuses Options/dhan_client's
    already-authenticated connection, same reasoning as
    IndexScalping/index_main.py - main.py's combined lifespan runs
    Options' lifespan first."""
    global _poll_task
    assert config.PAPER_TRADING_ONLY, "Refusing to start Copper options: PAPER_TRADING_ONLY must stay True."
    _poll_task = asyncio.create_task(poll_loop())
    logger.info("Copper options (PAPER ONLY) startup complete. strategy_enabled=%s", config.STRATEGY_ENABLED)
    yield
    if _poll_task:
        _poll_task.cancel()


@router.get("/copper/paper-trades")
async def get_copper_paper_trades():
    """Completed + currently-open Copper paper trades, the on/off flag's
    current state, today's daily gate (bullish/bearish, or both None
    before enough daily history/today's bar exists), and which option
    expiry cycle is currently in use. No real orders are ever placed by
    this strategy - see paper_engine.py."""
    return snapshot()
