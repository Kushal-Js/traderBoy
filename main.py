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
  - Futures/futures_main.py - PLACEHOLDER strategy (buys ATM CE options
    via the identical mechanics as Options/, standing in until real
    futures-contract buying replaces it, by explicit request), REAL
    orders, own separate position pool/capacity - see
    Futures/trading_engine.py's module docstring for why it skips broker
    reconciliation at startup.
  - K01/screener_main.py - "K01", the daily F&O stock screener (Minervini
    Trend Template + liquidity floor, run once/day, feeding intraday
    Supertrend/RSI/ROC momentum entries), PAPER TRADING ONLY (see
    K01/paper_engine.py's safety invariant). Named/documented 30 Aug 2026
    (was FnoScreener/ until this rename - no trade history existed yet to
    migrate). MVP scope shipped the same day for first live test - full
    design in the separate trading-skills repo (designs/k01.md); OI-buildup
    gating and VCP detection are
    explicit phase-2 items, not yet built.
A sixth strategy would be added the same way - its own package,
exporting `router` + `lifespan`, mounted below - without touching any
existing one.

Run with:
    uv run uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from trade_history import HISTORY_DIR, read_all_trades, read_all_webhook_alerts
import choppy_stocks
from Options import option_main
from IndexScalping import index_main
from CopperOptions import copper_main
from Futures import futures_main
from K01 import screener_main

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
    runs first since IndexScalping/Futures reuse its already-authenticated
    Dhan connection (see IndexScalping/paper_engine.py's and
    Futures/futures_main.py's docstrings) - keep it first in this nesting
    if more strategies are added later that also depend on it."""
    async with option_main.lifespan(app):
        async with index_main.lifespan(app):
            async with copper_main.lifespan(app):
                async with futures_main.lifespan(app):
                    async with screener_main.lifespan(app):
                        yield


app = FastAPI(title="Chartink -> Dhan Algo Bot", lifespan=lifespan)
app.include_router(option_main.router)
app.include_router(index_main.router)
app.include_router(copper_main.router)
app.include_router(futures_main.router)
app.include_router(screener_main.router)


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
    down) and logs any outage past its threshold, including the actual
    dhanboy.service journal output for that window. Exists because
    journald's own retention is limited and a restart that lands in a
    transient failure (e.g. a Dhan auth blip) can otherwise self-heal via
    systemd's Restart=always and leave no lasting trace.

    Reads every history/<date>_incidents.log file (dated-history
    convention, 31 Aug 2026 - see trade_history.py/watchdog.py), not one
    fixed path, so incidents from any prior day are still visible here.
    Returns the most recent `limit` incidents, newest first."""
    incidents: list[str] = []
    for path in sorted(HISTORY_DIR.glob("*_incidents.log")):
        try:
            with open(path) as f:
                content = f.read()
        except FileNotFoundError:
            continue
        blocks = [b.strip() for b in content.split("=== INCIDENT ") if b.strip()]
        incidents.extend("=== INCIDENT " + b for b in blocks)
    if not incidents:
        return {"incidents": [], "note": "no incidents recorded yet"}
    return {"incidents": list(reversed(incidents))[:limit], "total_recorded": len(incidents)}


@app.get("/trade-history")
async def trade_history(strategy: str | None = None):
    """Persistent, cross-restart record of every REAL (non-paper) closed
    trade, tagged by which package placed it - see trade_history.py.
    Options/position_store.py and Futures/position_store.py's own
    closed_positions_today reset daily and don't survive a restart; this
    is the durable record for later analysis. strategy=None returns both;
    pass strategy=Options or strategy=Futures to filter to one."""
    if strategy is not None and strategy not in ("Options", "Futures"):
        return {"error": "strategy must be 'Options' or 'Futures' (or omitted for both)"}
    trades = read_all_trades(strategy)
    return {"count": len(trades), "trades": trades}


@app.get("/webhook-alerts")
async def webhook_alerts(strategy: str | None = None):
    """Every incoming Chartink alert (processed AND ignored, with why),
    tagged by which endpoint received it - see trade_history.py's
    record_webhook_alert. Each handler already logged receipt via the
    standard logger, but that only reaches journald (limited retention,
    not queryable) - this is the durable, structured record. strategy=None
    returns all; pass strategy=Options/Futures/Options-PaperTrade to
    filter to one."""
    if strategy is not None and strategy not in ("Options", "Futures", "Options-PaperTrade"):
        return {"error": "strategy must be 'Options', 'Futures', or 'Options-PaperTrade' (or omitted for all)"}
    alerts = read_all_webhook_alerts(strategy)
    return {"count": len(alerts), "alerts": alerts}


@app.get("/choppy-stocks")
async def choppy_stocks_list():
    """Stocks the Options strategy currently won't enter new positions in -
    a manually-maintained list (user request 31 Aug 2026), edited directly
    on the server at choppy/choppy_stocks.json, not auto-computed or
    auto-refreshed. See choppy_stocks.py's own docstring for how to edit
    it and when an edit takes effect."""
    data = choppy_stocks.read_choppy_list()
    if data is None:
        return {"stocks": [],
                "note": "No choppy-stocks list on disk yet - nothing is being excluded in the meantime (fails open)."}
    return data
