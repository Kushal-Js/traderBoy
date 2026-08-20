"""
FastAPI service that:
  1. Accepts Chartink scanner webhook alerts on POST /chartink/webhook
  2. Picks the top-N stocks (by today's %change) from the alert
  3. Buys the ATM option (default: CE) for each, at market price
  4. Runs a background monitor loop that exits a leg on:
       - +10% target
       - -3% hard stop loss
       - 1% trailing stop-loss (trails the peak price in the trade's favor)
       - 3:15 PM hard square-off of everything still open
  5. Never re-enters a symbol already traded today, and caps concurrent
     live positions at 3.

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, field_validator

import config
from groww_client import groww_wrapper
from position_store import position_store
from trading_engine import enter_positions_for_stocks, monitor_loop, rank_and_pick_top_stocks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

_monitor_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _monitor_task
    groww_wrapper.authenticate()
    groww_wrapper.refresh_instruments()
    _monitor_task = asyncio.create_task(monitor_loop())
    logger.info("Startup complete: authenticated + monitor loop running.")
    yield
    if _monitor_task:
        _monitor_task.cancel()


app = FastAPI(title="Chartink -> Groww Algo Bot", lifespan=lifespan)


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
    # if config.WEBHOOK_SHARED_SECRET and x_webhook_secret != config.WEBHOOK_SHARED_SECRET:
    #     raise HTTPException(status_code=401, detail="Invalid webhook secret")

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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/square-off-now")
async def manual_square_off():
    """Manual kill-switch: closes every live position immediately."""
    from trading_engine import _square_off_all  # local import to avoid cycles at module load
    await _square_off_all("MANUAL_SQUARE_OFF")
    return await position_store.snapshot()
