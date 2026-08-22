"""
FastAPI service that:
  1. Accepts Chartink scanner webhook alerts on two endpoints:
       - POST /chartink/webhook       (bullish scan -> buys ATM CE)
       - POST /chartink/webhook-sell  (bearish scan -> buys ATM PE)
     Same entry/exit/dedup/capacity machinery either way, sharing one
     position pool - only the ATM leg and which end of the %change
     ranking counts as "strongest" differ.
  2. Picks the top-N stocks by today's %change from the alert (highest
     first for the bullish webhook, lowest/most negative first for the
     bearish one)
  3. Buys the ATM option for each, at market price (AMO if placed outside
     market hours)
  4. Runs a background monitor loop that exits a leg on:
       - target / hard stop-loss (config.TARGET_PCT / STOP_LOSS_PCT)
       - continuous trailing stop-loss (trails the peak price in the
         trade's favor) - optional, see config.ENABLE_TRAILING_SL
       - stepped/"ratchet" stop-loss (every step % the option's own
         premium climbs from entry, the floor moves up
         DYNAMIC_SL_INCREASE_PCT; step width is per-leg -
         config.DYNAMIC_SL_STEP_PCT_CE / _PE) - optional, stacks with the
         continuous trailing stop above, see config.ENABLE_DYNAMIC_SL
       - the underlying's 5-min Supertrend turning against the position's
         direction - optional, see config.ENABLE_SUPERTREND_EXIT
       - config.SQUARE_OFF_TIME hard square-off of everything still open
  5. Won't re-enter a symbol that already has an open or in-flight
     position of either type, and caps concurrent live positions
     separately per option type - config.MAX_LIVE_POSITIONS_CE for
     /chartink/webhook, config.MAX_LIVE_POSITIONS_PE for
     /chartink/webhook-sell - so a run of alerts on one side can't crowd
     out capacity for the other. Once a position closes, its symbol is
     free to be entered again on a later alert the same day.

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
# Webhook endpoints
# --------------------------------------------------------------------------- #
async def _handle_chartink_webhook(
    payload: ChartinkWebhookPayload, option_type: str, prefer_highest: bool,
) -> dict:
    """Shared by both webhooks below - only the ATM leg (CE/PE) and which
    end of the %change ranking counts as "strongest" differ."""
    await position_store.maybe_reset_for_new_day()

    stocks = payload.stock_list()
    logger.info(
        "Webhook received (%s): scan=%s alert=%s stocks=%s",
        option_type, payload.scan_name, payload.alert_name, stocks,
    )

    cap = config.MAX_LIVE_POSITIONS_CE if option_type == "CE" else config.MAX_LIVE_POSITIONS_PE
    remaining = await position_store.remaining_capacity(option_type)
    if remaining == 0:
        logger.info("No %s capacity left (%s live/in-flight already) - ignoring alert.", option_type, cap)
        return {
            "status": "ignored",
            "reason": "max_live_positions_reached",
            "option_type": option_type,
            "max_live_positions": cap,
        }

    loop = asyncio.get_running_loop()
    ranked = await loop.run_in_executor(
        None, rank_and_pick_top_stocks, stocks, config.TOP_N_STOCKS, prefer_highest
    )

    if not ranked:
        return {"status": "no_action", "reason": "could_not_rank_any_stock"}

    results = await enter_positions_for_stocks(ranked, option_type)

    return {
        "status": "processed",
        "ranked_by_day_change_pct": ranked,
        "entries": results,
    }


@app.post("/chartink/webhook")
async def chartink_webhook(payload: ChartinkWebhookPayload):
    """Bullish scan - buys ATM CE (call) on the alerted stocks with the
    highest %change."""
    return await _handle_chartink_webhook(payload, option_type="CE", prefer_highest=True)


@app.post("/chartink/webhook-sell")
async def chartink_webhook_sell(payload: ChartinkWebhookPayload):
    """Bearish scan - buys ATM PE (put) on the alerted stocks with the
    lowest %change (biggest decliners). Same entry/exit/dedup/capacity
    machinery as /chartink/webhook, sharing the same position pool - a
    symbol already open from either webhook blocks the other from also
    entering it."""
    return await _handle_chartink_webhook(payload, option_type="PE", prefer_highest=False)


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
