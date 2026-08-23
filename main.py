"""
Shared entry point across all trading strategies. Each strategy owns its
own package (its own lifespan, its own FastAPI router, its own state) -
this file just composes them onto one app so they can run side by side
in the same process. Today that's just the options strategy
(Options/option_main.py); adding a second, non-options strategy later
means adding its own package the same way and mounting it here, without
touching the options code at all.

Run with:
    uv run uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from Options import option_main

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Combines every mounted strategy's own lifespan. Add a new
    strategy's context manager to this stack the same way to bring its
    startup/shutdown along without touching the others."""
    async with option_main.lifespan(app):
        yield


app = FastAPI(title="Chartink -> Dhan Algo Bot", lifespan=lifespan)
app.include_router(option_main.router)


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
