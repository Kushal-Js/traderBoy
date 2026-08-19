from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request

from config import Settings
from groww_client import GrowwClient
from instruments import InstrumentCache
from models import ChartinkStock, TradeState
from order_manager import OrderManager
from state_store import StateStore
from strategy import (
    parse_chartink_payload,
    rank_stocks,
)
from tracker import PositionTracker


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | %(message)s"
    ),
)

logger = logging.getLogger(__name__)


# ============================================================
# Application objects
# ============================================================

settings = Settings.from_env()

groww = GrowwClient(
    settings=settings,
)

state = StateStore(
    path=settings.state_file,
)

instruments = InstrumentCache(
    settings=settings,
)

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


# ============================================================
# Runtime controls
# ============================================================

entry_webhook_lock = asyncio.Lock()
tracker_task: asyncio.Task[None] | None = None

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def is_weekend() -> bool:
    return now_ist().weekday() >= 5


def is_regular_market_hours() -> bool:
    if is_weekend():
        return False

    current = now_ist().time()

    return (
        settings.market_start_time
        <= current
        <= settings.market_end_time
    )


def is_after_market_hours() -> bool:
    return not is_regular_market_hours()


def force_exit_reached() -> bool:
    return (
        now_ist().time()
        >= settings.force_exit_time
    )


# ============================================================
# Lifespan
# ============================================================

@asynccontextmanager
async def lifespan(
    application: FastAPI,
):
    """
    Startup:
      1. Load the instrument CSV.
      2. Start exactly one tracker task.

    Shutdown:
      1. Cancel the tracker task.
      2. Wait for it to stop cleanly.
    """
    global tracker_task

    del application

    logger.info(
        "Application startup | "
        "buy_strategy=%s | "
        "equity_quantity=%d | "
        "equity_product=%s | "
        "live_trading=%s | "
        "amo_enabled=%s",
        settings.buy_strategy,
        settings.equity_quantity,
        settings.equity_product,
        settings.live_trading,
        settings.amo_enabled,
    )

    try:
        loaded_count = await instruments.refresh()

        logger.info(
            "Instrument cache initialized | "
            "symbol_count=%d",
            loaded_count,
        )

    except Exception:
        logger.exception(
            "Instrument cache initialization failed"
        )
        raise

    tracker_task = asyncio.create_task(
        tracker.run_forever(),
        name="traderboy-position-tracker",
    )

    logger.info(
        "Background position tracker started"
    )

    try:
        yield

    finally:
        logger.info(
            "Application shutdown started"
        )

        if tracker_task is not None:
            tracker_task.cancel()

            try:
                await tracker_task

            except asyncio.CancelledError:
                logger.info(
                    "Background tracker cancelled"
                )

        tracker_task = None

        logger.info(
            "Application shutdown completed"
        )


app = FastAPI(
    title="TraderBoy Groww Trading Bot",
    version="2.0.0",
    lifespan=lifespan,
)


# ============================================================
# Internal helpers
# ============================================================

def metadata_from_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
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


async def enter_by_strategy(
    stock: ChartinkStock,
) -> TradeState:
    if settings.buy_strategy == "OPTIONS":
        return await orders.enter_trade(
            stock=stock,
        )

    if settings.buy_strategy == "EQUITY_MTF":
        return await orders.enter_equity_trade(
            stock=stock,
        )

    raise RuntimeError(
        "Unsupported BUY_STRATEGY: "
        f"{settings.buy_strategy}"
    )


async def read_json_object(
    request: Request,
) -> dict[str, Any]:
    try:
        payload = await request.json()

    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "Request body must contain valid JSON"
            ),
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail=(
                "JSON body must be an object"
            ),
        )

    return payload


async def rank_payload_stocks(
    payload: dict[str, Any],
) -> tuple[
    list[ChartinkStock],
    list[dict[str, Any]],
]:
    try:
        stocks = parse_chartink_payload(
            payload
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    if not stocks:
        raise HTTPException(
            status_code=422,
            detail="No valid stocks received",
        )

    ranked = await rank_stocks(
        client=groww,
        stocks=stocks,
        limit=settings.max_active_trades,
    )

    return stocks, ranked


def market_closed_response(
    mode: str,
) -> dict[str, Any]:
    return {
        "status": "ignored",
        "mode": mode,
        "reason": (
            "Market is closed for this operation"
        ),
        "time_ist": now_ist().isoformat(),
    }


def validate_order_segment(
    segment: str,
) -> str:
    normalized = segment.strip().upper()

    if normalized not in {
        "CASH",
        "FNO",
    }:
        raise HTTPException(
            status_code=422,
            detail="segment must be CASH or FNO",
        )

    return normalized


# ============================================================
# Health and readiness
# ============================================================

@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "traderboy-groww-bot",
        "status": "ok",
        "time_ist": now_ist().isoformat(),
        "buy_strategy": settings.buy_strategy,
        "live_trading": settings.live_trading,
        "amo_enabled": settings.amo_enabled,
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "time_ist": now_ist().isoformat(),
        "buy_strategy": settings.buy_strategy,
        "live_trading": settings.live_trading,
        "amo_enabled": settings.amo_enabled,
        "instrument_cache": instruments.status(),
        "active_trade_count": len(
            state.active_trades()
        ),
        "last_reconciled_at": (
            state.data.get(
                "last_reconciled_at"
            )
        ),
    }


@app.get("/ready")
async def readiness() -> dict[str, Any]:
    cache_status = instruments.status()

    if not cache_status["loaded"]:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "reason": (
                    "Instrument cache is not loaded"
                ),
                "cache": cache_status,
            },
        )

    return {
        "status": "ready",
        "time_ist": now_ist().isoformat(),
        "instrument_cache": cache_status,
    }


@app.get("/strategy")
async def strategy_status() -> dict[str, Any]:
    return {
        "buy_strategy": settings.buy_strategy,
        "equity_quantity": settings.equity_quantity,
        "equity_exchange": settings.equity_exchange,
        "equity_segment": settings.equity_segment,
        "equity_product": settings.equity_product,
        "option_type": settings.option_type,
        "option_product": settings.option_product,
        "live_trading": settings.live_trading,
        "amo_enabled": settings.amo_enabled,
    }


@app.get("/tracker")
async def tracker_status() -> dict[str, Any]:
    return {
        "status": "ok",
        "trade_date": state.data.get(
            "trade_date"
        ),
        "last_reconciled_at": (
            state.data.get(
                "last_reconciled_at"
            )
        ),
        "trades": state.data.get(
            "trades",
            {},
        ),
    }


# ============================================================
# Instrument cache endpoints
# ============================================================

@app.get("/debug/instruments")
async def instrument_status() -> dict[str, Any]:
    return instruments.status()


@app.post("/admin/instruments/refresh")
async def refresh_instruments(
    request: Request,
) -> dict[str, Any]:
    """
    Administrative endpoint.

    Protect this endpoint before exposing it publicly.
    """
    admin_token = os.getenv(
        "ADMIN_TOKEN",
        "",
    ).strip()

    supplied_token = request.headers.get(
        "X-Admin-Token",
        "",
    ).strip()

    if not admin_token:
        raise HTTPException(
            status_code=503,
            detail=(
                "ADMIN_TOKEN is not configured"
            ),
        )

    if supplied_token != admin_token:
        raise HTTPException(
            status_code=403,
            detail="Forbidden",
        )

    try:
        count = await instruments.refresh()

        return {
            "status": "refreshed",
            "symbol_count": count,
            "cache": instruments.status(),
        }

    except Exception as exc:
        logger.exception(
            "Manual instrument refresh failed"
        )

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


# ============================================================
# Authentication and Groww diagnostics
# ============================================================

@app.get("/debug/auth")
async def debug_auth() -> dict[str, Any]:
    """
    Authenticates against Groww without placing an order.

    Protect this endpoint before exposing it publicly.
    """
    try:
        user = await groww.get_user_detail()

        return {
            "status": "ok",
            "user": user,
        }

    except Exception as exc:
        logger.exception(
            "Groww authentication test failed"
        )

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.get("/debug/expiries/{underlying}")
async def debug_expiries(
    underlying: str,
) -> dict[str, Any]:
    symbol = underlying.strip().upper()

    if not symbol:
        raise HTTPException(
            status_code=422,
            detail="Underlying is required",
        )

    try:
        expiries = await groww.get_expiries(
            symbol
        )

        return {
            "underlying": symbol,
            "expiries": expiries,
        }

    except Exception as exc:
        logger.exception(
            "Expiry lookup failed | "
            "underlying=%s",
            symbol,
        )

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.get("/debug/quote/{symbol}")
async def debug_quote(
    symbol: str,
) -> dict[str, Any]:
    normalized = symbol.strip().upper()

    if not normalized:
        raise HTTPException(
            status_code=422,
            detail="Symbol is required",
        )

    try:
        quote = await groww.get_quote(
            normalized
        )

        return {
            "symbol": normalized,
            "quote": quote,
        }

    except Exception as exc:
        logger.exception(
            "Quote lookup failed | symbol=%s",
            normalized,
        )

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.get("/orders/{order_id}")
async def order_diagnostics(
    order_id: str,
    segment: str = "FNO",
) -> dict[str, Any]:
    normalized_segment = validate_order_segment(
        segment
    )

    try:
        status = await groww.get_order_status(
            order_id=order_id,
            segment=normalized_segment,
        )

        detail = await groww.get_order_detail(
            order_id=order_id,
            segment=normalized_segment,
        )

        return {
            "order_id": order_id,
            "segment": normalized_segment,
            "status": status,
            "detail": detail,
        }

    except Exception as exc:
        logger.exception(
            "Order diagnostics failed | "
            "order_id=%s | segment=%s",
            order_id,
            normalized_segment,
        )

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.get("/orders/{order_id}/trades")
async def order_trades(
    order_id: str,
    segment: str = "FNO",
) -> dict[str, Any]:
    normalized_segment = validate_order_segment(
        segment
    )

    try:
        trades = await groww.get_order_trades(
            order_id=order_id,
            segment=normalized_segment,
        )

        return {
            "order_id": order_id,
            "segment": normalized_segment,
            "trades": trades,
        }

    except Exception as exc:
        logger.exception(
            "Order trades lookup failed | "
            "order_id=%s | segment=%s",
            order_id,
            normalized_segment,
        )

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


# ============================================================
# Debug payload and ranking endpoints
# ============================================================

@app.post("/debug/amo-payload")
async def debug_amo_payload(
    request: Request,
) -> dict[str, Any]:
    """
    Parses Chartink data only.
    It does not submit an order.
    """
    payload = await read_json_object(
        request
    )

    try:
        stocks = parse_chartink_payload(
            payload
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    return {
        "status": "ok",
        "parsed_stocks": [
            {
                "symbol": stock.symbol,
                "trigger_price": (
                    stock.trigger_price
                ),
            }
            for stock in stocks
        ],
        "note": (
            "No Groww order was submitted"
        ),
    }


@app.post("/debug/rank")
async def debug_rank(
    request: Request,
) -> dict[str, Any]:
    payload = await read_json_object(
        request
    )

    stocks, ranked = await rank_payload_stocks(
        payload
    )

    return {
        "status": "ok",
        "parsed_stocks": [
            {
                "symbol": stock.symbol,
                "trigger_price": (
                    stock.trigger_price
                ),
            }
            for stock in stocks
        ],
        "ranked_stocks": ranked,
    }


# ============================================================
# Regular Chartink webhook
# ============================================================

@app.post("/chartink/webhook")
async def chartink_webhook(
    request: Request,
) -> dict[str, Any]:
    """
    Regular market-order webhook.

    The entry instrument is selected using BUY_STRATEGY.
    """
    if not is_regular_market_hours():
        return market_closed_response(
            mode="REGULAR",
        )

    payload = await read_json_object(
        request
    )

    metadata = metadata_from_payload(
        payload
    )

    logger.info(
        "Regular Chartink webhook received | "
        "metadata=%s | strategy=%s",
        metadata,
        settings.buy_strategy,
    )

    async with entry_webhook_lock:
        stocks, ranked = await rank_payload_stocks(
            payload
        )

        if not ranked:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        "No stocks could be ranked"
                    ),
                    "received_symbols": [
                        stock.symbol
                        for stock in stocks
                    ],
                },
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
                            "Maximum active trade "
                            "limit reached"
                        ),
                    }
                )

                break

            stock = ChartinkStock(
                symbol=item["symbol"],
                trigger_price=item[
                    "trigger_price"
                ],
            )

            try:
                trade = await enter_by_strategy(
                    stock=stock,
                )

                results.append(
                    {
                        "status": "entered",
                        "rank_data": item,
                        "trade": trade.to_dict(),
                    }
                )

            except Exception as exc:
                logger.exception(
                    "Regular trade failed | "
                    "symbol=%s",
                    stock.symbol,
                )

                results.append(
                    {
                        "status": "failed",
                        "symbol": stock.symbol,
                        "error": str(exc),
                    }
                )

        return {
            "status": "accepted",
            "mode": "REGULAR",
            "buy_strategy": settings.buy_strategy,
            "live_trading": settings.live_trading,
            "metadata": metadata,
            "ranked_stocks": ranked,
            "orders": results,
        }


# ============================================================
# AMO Chartink webhook
# ============================================================

@app.post("/chartink/amo-webhook")
async def chartink_amo_webhook(
    request: Request,
) -> dict[str, Any]:
    """
    AMO endpoint for option entries only.
    """
    if not settings.amo_enabled:
        return {
            "status": "disabled",
            "mode": "AMO",
            "reason": (
                "AMO_ENABLED is false"
            ),
        }

    if settings.buy_strategy != "OPTIONS":
        raise HTTPException(
            status_code=422,
            detail=(
                "AMO webhook currently supports "
                "BUY_STRATEGY=OPTIONS only"
            ),
        )

    if is_regular_market_hours():
        return {
            "status": "rejected",
            "mode": "AMO",
            "reason": (
                "AMO endpoint is intended for "
                "outside regular market hours"
            ),
            "time_ist": now_ist().isoformat(),
        }

    payload = await read_json_object(
        request
    )

    metadata = metadata_from_payload(
        payload
    )

    logger.info(
        "AMO Chartink webhook received | "
        "metadata=%s",
        metadata,
    )

    async with entry_webhook_lock:
        stocks, ranked = await rank_payload_stocks(
            payload
        )

        if not ranked:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        "No stocks could be ranked; "
                        "no AMO order was attempted"
                    ),
                    "received_symbols": [
                        stock.symbol
                        for stock in stocks
                    ],
                },
            )

        results: list[dict[str, Any]] = []

        for item in ranked[
            :settings.amo_max_stocks
        ]:
            if (
                len(state.active_trades())
                >= settings.max_active_trades
            ):
                results.append(
                    {
                        "status": "skipped",
                        "symbol": item["symbol"],
                        "reason": (
                            "Maximum active/pending "
                            "trade limit reached"
                        ),
                    }
                )

                break

            stock = ChartinkStock(
                symbol=item["symbol"],
                trigger_price=item[
                    "trigger_price"
                ],
            )

            try:
                trade = (
                    await orders.enter_amo_trade(
                        stock=stock,
                    )
                )

                results.append(
                    {
                        "status": "amo_submitted",
                        "rank_data": item,
                        "trade": trade.to_dict(),
                    }
                )

            except Exception as exc:
                logger.exception(
                    "AMO trade failed | symbol=%s",
                    stock.symbol,
                )

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
            "buy_strategy": settings.buy_strategy,
            "live_trading": settings.live_trading,
            "amo_enabled": settings.amo_enabled,
            "metadata": metadata,
            "ranked_stocks": ranked,
            "orders": results,
        }