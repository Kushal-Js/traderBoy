from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from config import Settings
from groww_client import GrowwClient
from instruments import InstrumentCache
from models import ChartinkStock
from order_manager import OrderManager
from state_store import StateStore
from strategy import parse_chartink_payload, rank_stocks
from tracker import PositionTracker


settings = Settings.from_env()
groww = GrowwClient(settings)
state = StateStore(settings.state_file)
instruments = InstrumentCache(settings)

orders = OrderManager(
    settings=settings,
    client=groww,
    instruments=instruments,
    state=state,
)

tracker = PositionTracker(
    settings=settings,
    client=groww,
    state=state,
    orders=orders,
)


@asynccontextmanager
async def lifespan(
    application: FastAPI,
):
    tracker_task = asyncio.create_task(
        tracker.run_forever()
    )

    try:
        yield

    finally:
        tracker_task.cancel()

        try:
            await tracker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="TraderBoy Groww Trading Bot",
    version="1.0.0",
    lifespan=lifespan,
)


def is_weekend() -> bool:
    return tracker.now().weekday() >= 5


def market_is_open_for_entries() -> bool:
    if is_weekend():
        return False

    current = tracker.now().time()

    return (
        settings.market_start_time
        <= current
        <= settings.market_end_time
    )


@app.get("/")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "live_trading": settings.live_trading,
        "trade_date": state.data.get(
            "trade_date"
        ),
        "active_trade_count": len(
            state.active_trades()
        ),
        "last_reconciled_at": state.data.get(
            "last_reconciled_at"
        ),
    }


@app.get("/tracker")
async def tracker_status() -> dict[str, Any]:
    return {
        "trade_date": state.data.get(
            "trade_date"
        ),
        "last_reconciled_at": state.data.get(
            "last_reconciled_at"
        ),
        "trades": state.data.get(
            "trades",
            {},
        ),
    }


@app.get("/debug/auth")
async def debug_auth() -> dict[str, Any]:
    try:
        user = await groww.get_user_detail()

        return {
            "status": "ok",
            "user": user,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.get("/debug/expiries/{underlying}")
async def debug_expiries(
    underlying: str,
) -> dict[str, Any]:
    try:
        symbol = underlying.strip().upper()

        expiries = await groww.get_expiries(
            symbol
        )

        return {
            "underlying": symbol,
            "expiries": expiries,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.get("/orders/{order_id}")
async def order_diagnostics(
    order_id: str,
) -> dict[str, Any]:
    try:
        status = await groww.get_order_status(
            order_id=order_id,
            segment=settings.option_segment,
        )

        detail = await groww.get_order_detail(
            order_id=order_id,
            segment=settings.option_segment,
        )

        return {
            "status": status,
            "detail": detail,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.post("/chartink/amo-webhook")
async def chartink_amo_webhook(
    request: Request,
) -> dict[str, Any]:
    if not settings.amo_enabled:
        return {
            "status": "disabled",
            "mode": "AMO",
            "reason": (
                "Set AMO_ENABLED=true in .env"
            ),
        }

    try:
        payload = await request.json()

    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Request body must be valid JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="JSON body must be an object",
        )

    try:
        stocks = parse_chartink_payload(payload)

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    metadata = {
        "triggered_at": payload.get(
            "triggered_at"
        ),
        "scan_name": payload.get(
            "scan_name"
        ),
        "scan_url": payload.get(
            "scan_url"
        ),
        "alert_name": payload.get(
            "alert_name"
        ),
        "webhook_url": payload.get(
            "webhook_url"
        ),
    }

    ranked = await rank_stocks(
        client=groww,
        stocks=stocks,
        limit=settings.amo_max_stocks,
    )

    results: list[dict[str, Any]] = []

    for item in ranked:
        if (
            len(state.active_trades())
            >= settings.max_active_trades
        ):
            results.append(
                {
                    "status": "skipped",
                    "symbol": item["symbol"],
                    "reason": (
                        "Maximum active/pending trade "
                        "limit reached"
                    ),
                }
            )
            continue

        stock = ChartinkStock(
            symbol=item["symbol"],
            trigger_price=item[
                "trigger_price"
            ],
        )

        try:
            trade = await orders.enter_amo_trade(
                stock=stock
            )

            results.append(
                {
                    "status": "amo_submitted",
                    "rank_data": item,
                    "trade": trade.to_dict(),
                }
            )

        except Exception as exc:
            results.append(
                {
                    "status": "failed",
                    "symbol": stock.symbol,
                    "error": str(exc),
                }
            )

    return {
        "status": "accepted",
        "mode": "AMO",
        "live_trading": settings.live_trading,
        "amo_enabled": settings.amo_enabled,
        "metadata": metadata,
        "ranked_stocks": ranked,
        "orders": results,
    }


@app.post("/chartink/webhook")
async def chartink_webhook(
    request: Request,
) -> dict[str, Any]:
    if not market_is_open_for_entries():
        return {
            "status": "ignored",
            "reason": (
                "Market is closed for new regular orders"
            ),
        }

    try:
        payload = await request.json()

    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Request must contain valid JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="JSON body must be an object",
        )

    try:
        stocks = parse_chartink_payload(payload)

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    ranked = await rank_stocks(
        client=groww,
        stocks=stocks,
        limit=settings.max_active_trades,
    )

    results: list[dict[str, Any]] = []

    for item in ranked:
        if (
            len(state.active_trades())
            >= settings.max_active_trades
        ):
            break

        stock = ChartinkStock(
            symbol=item["symbol"],
            trigger_price=item[
                "trigger_price"
            ],
        )

        try:
            trade = await orders.enter_trade(
                stock
            )

            results.append(
                {
                    "status": "entered",
                    "trade": trade.to_dict(),
                }
            )

        except Exception as exc:
            results.append(
                {
                    "status": "failed",
                    "symbol": stock.symbol,
                    "error": str(exc),
                }
            )

    return {
        "status": "accepted",
        "live_trading": settings.live_trading,
        "ranked_stocks": ranked,
        "orders": results,
    }