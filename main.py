"""
FastAPI service that:
  1. Accepts Chartink scanner webhook alerts on POST /chartink/webhook
  2. Picks the top-N stocks (by today's %change) from the alert
  3. Buys the ATM option (default: CE) for each, at market price (AMO if
     placed outside market hours)
  4. Runs a background monitor loop that exits a leg on:
       - +10% target
       - -3% hard stop loss
       - trailing stop-loss (trails the peak price in the trade's favor) -
         optional, see config.ENABLE_TRAILING_SL
       - 3:15 PM hard square-off of everything still open
  5. Won't re-enter a symbol that already has an open or in-flight
     position, and caps concurrent live positions at 3 - once that
     position closes, the symbol is free to be entered again on a later
     alert the same day.

Run with:
    uv run uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, field_validator

import config
from dhan_client import dhan_wrapper
from position_store import position_store
from trading_engine import (
    enter_positions_for_stocks,
    monitor_loop,
    on_price_tick,
    rank_and_pick_top_stocks,
    reconcile_broker_positions,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

_monitor_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _monitor_task
    loop = asyncio.get_running_loop()

    # Bridges the market-feed's WebSocket thread back onto the event loop -
    # on_price_tick is a plain sync function called directly from that
    # thread (see dhan_client._on_market_tick), so it can't just `await`.
    # Wired up before start_feed() so no tick can arrive before this exists.
    def _on_price_tick(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(on_price_tick(trading_symbol, ltp), loop)

    dhan_wrapper.on_price_tick = _on_price_tick

    # authenticate() and start_feed() are blocking SDK calls; push them to a
    # worker thread rather than calling them directly on the lifespan
    # coroutine (a socket connection spinning up its own event loop
    # internally can't happen on a thread that already has uvicorn's event
    # loop running).
    await loop.run_in_executor(None, dhan_wrapper.authenticate)

    try:
        # A bad/misscoped token could otherwise hang startup on the socket
        # retrying; fail fast and run in REST-only (polling) mode instead.
        # All feed-reading call sites already fall back to REST when the
        # feed has no cached data.
        # This must happen before reconcile_broker_positions() below, since
        # that also touches the feed (to subscribe reconciled positions'
        # prices).
        await asyncio.wait_for(loop.run_in_executor(None, dhan_wrapper.start_feed), timeout=15)
    except Exception:  # noqa: BLE001
        logger.exception("Could not start Dhan WebSocket feed - continuing in REST-only mode.")

    try:
        reconciled = await reconcile_broker_positions()
        if reconciled:
            await position_store.reconcile_from_broker(reconciled)
            logger.info(
                "Reconciled %d existing broker position(s) at startup: %s",
                len(reconciled), [p.underlying_symbol for p in reconciled],
            )
    except Exception:  # noqa: BLE001
        logger.exception("Could not reconcile broker positions at startup - continuing without them.")

    _monitor_task = asyncio.create_task(monitor_loop())
    logger.info("Startup complete: authenticated + monitor loop running.")
    yield
    if _monitor_task:
        _monitor_task.cancel()


app = FastAPI(title="Chartink -> Dhan Algo Bot", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Webhook payload schema (matches the sample Chartink payload exactly)
# --------------------------------------------------------------------------- #
class ChartinkWebhookPayload(BaseModel):
    stocks: str
    trigger_prices: str
    triggered_at: str
    scan_name: str
    scan_url: str
    alert_name: str
    webhook_url: Optional[str] = None

    @field_validator("stocks")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("stocks must not be empty")
        return v

    def stock_list(self) -> list[str]:
        return [s.strip().upper() for s in self.stocks.split(",") if s.strip()]

    def trigger_price_list(self) -> list[float]:
        out = []
        for p in self.trigger_prices.split(","):
            p = p.strip()
            if p:
                try:
                    out.append(float(p))
                except ValueError:
                    out.append(0.0)
        return out


# --------------------------------------------------------------------------- #
# Webhook endpoint
# --------------------------------------------------------------------------- #
@app.post("/chartink/webhook")
async def chartink_webhook(
    payload: ChartinkWebhookPayload,
):
    await position_store.maybe_reset_for_new_day()

    stocks = payload.stock_list()
    logger.info(
        "Webhook received: scan=%s alert=%s stocks=%s",
        payload.scan_name, payload.alert_name, stocks,
    )

    remaining = await position_store.remaining_capacity()
    if remaining == 0:
        logger.info("No capacity left (%s live positions already open) - ignoring alert.",
                     config.MAX_LIVE_POSITIONS)
        return {
            "status": "ignored",
            "reason": "max_live_positions_reached",
            "max_live_positions": config.MAX_LIVE_POSITIONS,
        }

    loop = asyncio.get_running_loop()
    ranked = await loop.run_in_executor(
        None, rank_and_pick_top_stocks, stocks, config.TOP_N_STOCKS
    )

    if not ranked:
        return {"status": "no_action", "reason": "could_not_rank_any_stock"}

    results = await enter_positions_for_stocks(ranked)

    return {
        "status": "processed",
        "ranked_by_day_change_pct": ranked,
        "entries": results,
    }


# --------------------------------------------------------------------------- #
# Observability endpoints
# --------------------------------------------------------------------------- #
@app.get("/positions")
async def get_positions():
    return await position_store.snapshot()


@app.get("/orders")
async def get_orders():
    """Every order placed today (entry BUY + exit SELL legs), with Dhan's
    own order_status (e.g. REJECTED, TRADED, CANCELLED - see
    dhan_client.OrderStatus for the full documented enum)."""
    snapshot = await position_store.snapshot()
    return {"orders": snapshot["orders_today"]}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/feed-stats")
async def feed_stats():
    """Proves (or disproves) whether the WebSocket caches are actually
    being used instead of REST fallbacks - see dhan_client.DhanWrapper.stats."""
    return dhan_wrapper.stats


@app.post("/square-off-now")
async def manual_square_off():
    """Manual kill-switch: closes every live position immediately."""
    from trading_engine import _square_off_all  # local import to avoid cycles at module load
    await _square_off_all("MANUAL_SQUARE_OFF")
    return await position_store.snapshot()
