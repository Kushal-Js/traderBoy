"""
Futures strategy: Chartink -> Dhan ATM CE buying, exiting on
target/stop-loss/dynamic-SL/Supertrend - PLACEHOLDER logic (buys ATM CE
*options*, identical mechanics to Options/option_main.py) standing in
until real futures-contract buying replaces it, by explicit request. See
NOTES.md's design-decision entry for the full rationale, including why
this package does NOT run broker-position reconciliation at startup
(risk of double-tracking Options' own real positions - see
trading_engine.py's module docstring).

`lifespan` and `router` are composed into the shared app by the top-level
main.py, the same way every other strategy package is - see main.py's own
docstring. Reuses the Options package's single authenticated Dhan
connection (dhan_client.py here just re-exports it) - main.py must mount
this package's lifespan *inside* Options' own nesting (after it), the
same way IndexScalping/CopperOptions already do, so authenticate() and
start_feed() have already run by the time this package's lifespan starts.

Accepts one Chartink scanner webhook alert endpoint:
   - POST /chartink/webhook-futures  (bullish scan -> buys ATM CE)
Only one leg/direction was requested for this package (unlike Options'
CE+PE pair) - no bearish endpoint exists here.
  1. Picks the top-N stocks by today's %change from the alert (highest first)
  2. Buys the ATM option for each, at market price (AMO if placed outside
     market hours)
  3. Runs a background monitor loop that exits a leg on target/stop-loss/
     dynamic-SL/Supertrend/SQUARE_OFF_TIME - identical rules to Options',
     this package's own config values (config.py)
  4. Won't re-enter a symbol already open/in-flight, capped at
     config.MAX_LIVE_POSITIONS_CE - entirely separate pool/capacity from
     Options', so alerts on either side can't crowd out the other's.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, field_validator

from . import config
from .dhan_client import dhan_wrapper
from .position_store import position_store
from .trading_engine import (
    enter_positions_for_stocks,
    is_past_allowed_trading_time,
    is_past_square_off_time,
    monitor_loop,
    on_price_tick,
    rank_and_pick_top_stocks,
)

logger = logging.getLogger("futures_main")

router = APIRouter()

_monitor_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Futures strategy's own startup/shutdown. Does NOT authenticate or
    start the Dhan feed - it reuses Options' already-authenticated
    connection, so main.py must nest this lifespan inside Options' own
    (after it), the same pattern IndexScalping/CopperOptions use. Does NOT
    reconcile broker positions at startup either - see this package's
    trading_engine.py module docstring for why."""
    global _monitor_task
    loop = asyncio.get_running_loop()

    def _on_price_tick(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(on_price_tick(trading_symbol, ltp), loop)

    dhan_wrapper.add_price_tick_subscriber(_on_price_tick)

    _monitor_task = asyncio.create_task(monitor_loop())
    logger.info("Futures strategy startup complete: monitor loop running (reusing Options' Dhan connection).")
    yield
    if _monitor_task:
        _monitor_task.cancel()


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


# --------------------------------------------------------------------------- #
# Webhook endpoint
# --------------------------------------------------------------------------- #
@router.post("/chartink/webhook-futures")
async def chartink_webhook_futures(payload: ChartinkWebhookPayload):
    """Bullish scan - buys ATM CE (placeholder for a real futures contract
    buy) on the alerted stocks with the highest %change."""
    await position_store.maybe_reset_for_new_day()

    if is_past_allowed_trading_time():
        logger.info(
            "Ignoring alert - past today's allowed trading cutoff (%s), not opening new positions.",
            config.ALLOWED_TRADING_TIME,
        )
        return {
            "status": "ignored",
            "reason": "past_allowed_trading_time",
            "allowed_trading_time": config.ALLOWED_TRADING_TIME,
        }

    if is_past_square_off_time():
        logger.info(
            "Ignoring alert - past today's %s square-off time, not opening new positions.",
            config.SQUARE_OFF_TIME,
        )
        return {
            "status": "ignored",
            "reason": "past_square_off_time",
            "square_off_time": config.SQUARE_OFF_TIME,
        }

    stocks = payload.stock_list()
    logger.info(
        "Futures webhook received: scan=%s alert=%s stocks=%s",
        payload.scan_name, payload.alert_name, stocks,
    )

    remaining = await position_store.remaining_capacity(config.OPTION_TYPE)
    if remaining == 0:
        logger.info("No capacity left (%s live/in-flight already) - ignoring alert.", config.MAX_LIVE_POSITIONS_CE)
        return {
            "status": "ignored",
            "reason": "max_live_positions_reached",
            "max_live_positions": config.MAX_LIVE_POSITIONS_CE,
        }

    loop = asyncio.get_running_loop()
    ranked = await loop.run_in_executor(
        None, rank_and_pick_top_stocks, stocks, config.TOP_N_STOCKS, True
    )

    if not ranked:
        return {"status": "no_action", "reason": "could_not_rank_any_stock"}

    results = await enter_positions_for_stocks(ranked, config.OPTION_TYPE)

    return {
        "status": "processed",
        "ranked_by_day_change_pct": ranked,
        "entries": results,
    }


# --------------------------------------------------------------------------- #
# Observability endpoints
# --------------------------------------------------------------------------- #
@router.get("/futures/positions")
async def get_positions():
    return await position_store.snapshot()


@router.get("/futures/orders")
async def get_orders():
    snapshot = await position_store.snapshot()
    return {"orders": snapshot["orders_today"]}


@router.post("/futures/square-off-now")
async def manual_square_off():
    """Manual kill-switch: closes every live Futures position immediately."""
    from .trading_engine import _square_off_all  # local import to avoid cycles at module load
    await _square_off_all("MANUAL_SQUARE_OFF")
    return await position_store.snapshot()
