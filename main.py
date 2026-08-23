"""
Shared entry point across all trading strategies. Each strategy owns its
own package (its own lifespan, its own FastAPI router, its own state) -
this file just composes them onto one app so they can run side by side
in the same process. Three are mounted today:
  - Options/option_main.py - the live options-buying strategy (real
    orders, real money).
  - IndexScalping/index_main.py - a NIFTY/BankNifty scalping strategy,
    PAPER TRADING ONLY (see IndexScalping/paper_engine.py's safety
    invariant) - runs its own signal/exit logic and logs what it would
    have done, places no real orders.
  - CopperOptions/copper_main.py - an MCX Copper options-buying strategy
    (gap + daily-RSI momentum, dual-Supertrend confirmed), also PAPER
    TRADING ONLY (see CopperOptions/paper_engine.py's safety invariant),
    with its own on/off flag (config.STRATEGY_ENABLED) independent of
    the paper-trading invariant.
A fourth strategy would be added the same way - its own package,
exporting `router` + `lifespan`, mounted below - without touching any
existing one.

Run with:
    uv run uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from Options import option_main
from IndexScalping import index_main
from CopperOptions import copper_main

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Combines every mounted strategy's own lifespan. Add a new
    strategy's context manager to this stack the same way to bring its
    startup/shutdown along without touching the others. Options' lifespan
    runs first since IndexScalping reuses its already-authenticated Dhan
    connection (see IndexScalping/paper_engine.py's docstring) - keep it
    first in this nesting if more strategies are added later that also
    depend on it."""
    async with option_main.lifespan(app):
        async with index_main.lifespan(app):
            async with copper_main.lifespan(app):
                yield


app = FastAPI(title="Chartink -> Dhan Algo Bot", lifespan=lifespan)
app.include_router(option_main.router)
app.include_router(index_main.router)
app.include_router(copper_main.router)


# --------------------------------------------------------------------------- #
# Endpoints common to every strategy (not specific to options)
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/incidents")
async def get_incidents(limit: int = 20):
    """Records from watchdog.py - a separate process/systemd unit that
    polls /health independently of this app (so it can see this app being
    down) and logs any outage past its threshold to incidents.log,
    including the actual dhanboy.service journal output for that window.
    Exists because journald's own retention is limited and a restart that
    lands in a transient failure (e.g. a Dhan auth blip) can otherwise
    self-heal via systemd's Restart=always and leave no lasting trace.
    Returns the most recent `limit` incidents, newest first."""
    path = "/root/apps/traderBoy/incidents.log"
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        return {"incidents": [], "note": "no incidents recorded yet"}
    blocks = [b.strip() for b in content.split("=== INCIDENT ") if b.strip()]
    incidents = [("=== INCIDENT " + b) for b in blocks]
    return {"incidents": list(reversed(incidents))[:limit], "total_recorded": len(incidents)}
