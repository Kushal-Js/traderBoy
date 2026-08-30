"""
Daily F&O screener strategy - PAPER TRADING ONLY (see paper_engine.py's
safety invariant docstring; config.PAPER_TRADING_ONLY is asserted at
startup). Full design in trading-skills/designs/fno-daily-screener.md
(github.com/Kushal-Js/trading-skills) - this ships the MVP scope (Stage 0
Trend Template + Stage 1 liquidity floor, run once daily, feeding Stage 3
intraday momentum entries) for the first live paper-trading test, 30 Aug
2026. Stage 2 (OI-buildup) and VCP detection are explicitly deferred -
see config.py's module docstring.

Exports `router` + `lifespan`, mounted onto the shared app by main.py
alongside the other strategies' own routers + lifespans.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI

from . import config
from .paper_engine import poll_loop, snapshot

logger = logging.getLogger("fno_screener")

router = APIRouter()

_poll_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts the paper-trading poll loop. Does not authenticate with Dhan
    itself - reuses the connection Options/option_main.py's own lifespan
    already established (main.py's combined lifespan runs Options first)."""
    global _poll_task
    assert config.PAPER_TRADING_ONLY, "Refusing to start F&O screener: PAPER_TRADING_ONLY must stay True."
    _poll_task = asyncio.create_task(poll_loop())
    logger.info("F&O daily screener (PAPER ONLY) startup complete.")
    yield
    if _poll_task:
        _poll_task.cancel()


@router.get("/fno-screener/status")
async def get_status():
    """Today's watchlist (Stage 0+1 survivors, with each one's most
    recent Stage 3 momentum read), currently open paper positions,
    completed paper trades, and overall P&L. No real orders are ever
    placed by this strategy - see paper_engine.py."""
    return snapshot()
