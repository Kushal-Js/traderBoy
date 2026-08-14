from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from Dhan_Tradehull import Tradehull


# ============================================================
# Configuration
# ============================================================

load_dotenv()

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")

if not DHAN_CLIENT_ID or not DHAN_ACCESS_TOKEN:
    raise RuntimeError(
        "DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set in .env"
    )

LIVE_TRADING = (
    os.getenv("LIVE_TRADING", "false").strip().lower() == "true"
)

UNDERLYING_EXCHANGE = os.getenv(
    "UNDERLYING_EXCHANGE",
    "NSE",
)

OPTION_EXCHANGE = os.getenv(
    "OPTION_EXCHANGE",
    "NFO",
)

TRADE_TYPE = os.getenv(
    "TRADE_TYPE",
    "MIS",
)

TOP_STOCK_COUNT = 3
OPTION_TYPE = "CE"
OPTION_EXPIRY_INDEX = 0

TARGET_PERCENT = 10.0
INITIAL_STOP_LOSS_PERCENT = 3.0
TRAILING_STOP_LOSS_PERCENT = 1.0

FORCE_EXIT_TIME = time(15, 15)
MONITOR_INTERVAL_SECONDS = 5

STATE_FILE = Path("open_trades.json")

# The server's system clock must be configured for IST.
# For production, use a timezone-aware clock instead of relying on
# the system timezone.


# ============================================================
# Logging and application setup
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Chartink Dhan Options Trading Bot",
    version="1.0.0",
)

tsl = Tradehull(
    DHAN_CLIENT_ID,
    DHAN_ACCESS_TOKEN,
)


# ============================================================
# Data structures
# ============================================================

@dataclass
class ChartinkStock:
    symbol: str
    trigger_price: float


@dataclass
class Trade:
    underlying: str
    option_symbol: str
    option_type: str
    quantity: int
    entry_price: float
    highest_price: float
    stop_price: float
    target_price: float
    entry_order_id: str | None = None
    exited: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Trade":
        return cls(**value)


# ============================================================
# Persistent trade state
# ============================================================

def load_open_trades() -> list[Trade]:
    if not STATE_FILE.exists():
        return []

    try:
        raw_data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(raw_data, list):
            return []

        return [
            Trade.from_dict(item)
            for item in raw_data
            if isinstance(item, dict)
        ]

    except Exception:
        logger.exception("Unable to load saved trade state")
        return []


def save_open_trades() -> None:
    STATE_FILE.write_text(
        json.dumps(
            [trade.to_dict() for trade in open_trades],
            indent=2,
        ),
        encoding="utf-8",
    )


open_trades: list[Trade] = load_open_trades()


# ============================================================
# General helpers
# ============================================================

def normalize_symbol(value: Any) -> str:
    return str(value).strip().upper()


def is_live_trading() -> bool:
    return LIVE_TRADING


def current_time() -> time:
    return datetime.now().time()


def is_force_exit_time() -> bool:
    return current_time() >= FORCE_EXIT_TIME


def find_case_insensitive_key(
    data: dict[str, Any],
    requested_key: str,
) -> Any:
    requested_key = requested_key.lower()

    for key, value in data.items():
        if str(key).lower() == requested_key:
            return value

    return None


# ============================================================
# Chartink payload parsing
# ============================================================

def parse_comma_separated_string(value: Any) -> list[str]:
    """
    Parse a Chartink comma-separated field.

    Example:
        "AXISBANK, TCS, INFY"

    Returns:
        ["AXISBANK", "TCS", "INFY"]
    """
    if not isinstance(value, str):
        raise ValueError(
            "Chartink fields stocks and trigger_prices must be strings"
        )

    return [
        item.strip()
        for item in value.split(",")
        if item.strip()
    ]


def parse_chartink_payload(
    payload: dict[str, Any],
) -> list[ChartinkStock]:
    """
    Parse the payload:

    {
      "stocks": "AXISBANK, TCS, INFY",
      "trigger_prices": "1250.00,3400.00,1600.00"
    }
    """

    if "stocks" not in payload:
        raise ValueError("Missing required field: stocks")

    if "trigger_prices" not in payload:
        raise ValueError("Missing required field: trigger_prices")

    raw_stocks = parse_comma_separated_string(
        payload["stocks"]
    )

    raw_trigger_prices = parse_comma_separated_string(
        payload["trigger_prices"]
    )

    if len(raw_stocks) != len(raw_trigger_prices):
        raise ValueError(
            "stocks and trigger_prices must contain the same "
            "number of values"
        )

    parsed: list[ChartinkStock] = []
    seen_symbols: set[str] = set()

    for raw_symbol, raw_price in zip(
        raw_stocks,
        raw_trigger_prices,
    ):
        symbol = normalize_symbol(raw_symbol)

        if symbol in seen_symbols:
            logger.warning(
                "Duplicate stock ignored from Chartink payload: %s",
                symbol,
            )
            continue

        try:
            trigger_price = float(
                raw_price.replace(",", "").strip()
            )
        except ValueError as exc:
            raise ValueError(
                f"Invalid trigger price for {symbol}: {raw_price}"
            ) from exc

        if trigger_price <= 0:
            raise ValueError(
                f"Trigger price must be positive for {symbol}"
            )

        parsed.append(
            ChartinkStock(
                symbol=symbol,
                trigger_price=trigger_price,
            )
        )

        seen_symbols.add(symbol)

    return parsed


# ============================================================
# Dhan-Tradehull market-data helpers
# ============================================================

def extract_ltp_from_response(
    response: Any,
    symbol: str,
) -> float:
    """
    Extract LTP from common Tradehull response shapes.
    """

    if response is None:
        raise RuntimeError(
            f"No LTP response received for {symbol}"
        )

    if isinstance(response, (int, float)):
        return float(response)

    if isinstance(response, dict):
        value = find_case_insensitive_key(response, symbol)

        if isinstance(value, dict):
            for key in (
                "ltp",
                "last_price",
                "lastPrice",
                "close",
                "price",
            ):
                candidate = find_case_insensitive_key(
                    value,
                    key,
                )

                if candidate is not None:
                    return float(candidate)

        if value is not None:
            return float(value)

        for key in (
            "ltp",
            "last_price",
            "lastPrice",
            "close",
            "price",
        ):
            candidate = find_case_insensitive_key(
                response,
                key,
            )

            if candidate is not None:
                return float(candidate)

    raise RuntimeError(
        f"Could not extract LTP for {symbol}: {response}"
    )


def get_ltp(symbol: str) -> float:
    response = tsl.get_ltp_data(
        names=[symbol],
        debug="NO",
    )

    ltp = extract_ltp_from_response(
        response=response,
        symbol=symbol,
    )

    if ltp <= 0:
        raise RuntimeError(
            f"Invalid LTP for {symbol}: {ltp}"
        )

    return ltp


def get_today_percent_change(symbol: str) -> float:
    """
    Calculate today's percentage change as:

        (today close/current price - today's open)
        / today's open * 100

    The method first attempts to use daily historical data.
    """

    try:
        historical = tsl.get_historical_data(
            tradingsymbol=symbol,
            exchange=UNDERLYING_EXCHANGE,
            timeframe="DAY",
        )

        if isinstance(historical, pd.DataFrame):
            if not historical.empty:
                data = historical.copy()
                data.columns = [
                    str(column).lower()
                    for column in data.columns
                ]

                open_column = next(
                    (
                        column
                        for column in (
                            "open",
                            "open_price",
                        )
                        if column in data.columns
                    ),
                    None,
                )

                close_column = next(
                    (
                        column
                        for column in (
                            "close",
                            "ltp",
                            "last_price",
                            "lastprice",
                        )
                        if column in data.columns
                    ),
                    None,
                )

                if open_column and close_column:
                    today_open = float(
                        data.iloc[-1][open_column]
                    )

                    today_close = float(
                        data.iloc[-1][close_column]
                    )

                    if today_open > 0:
                        return (
                            (today_close - today_open)
                            / today_open
                            * 100
                        )

    except Exception:
        logger.exception(
            "Historical data lookup failed for %s",
            symbol,
        )

    raise RuntimeError(
        f"Could not calculate today's percentage change for {symbol}"
    )


def rank_stocks_by_today_change(
    stocks: list[ChartinkStock],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []

    for stock in stocks:
        try:
            percent_change = get_today_percent_change(
                stock.symbol
            )

            ranked.append(
                {
                    "symbol": stock.symbol,
                    "trigger_price": stock.trigger_price,
                    "today_percent_change": percent_change,
                }
            )

            logger.info(
                "%s | trigger=%.2f | today_change=%.2f%%",
                stock.symbol,
                stock.trigger_price,
                percent_change,
            )

        except Exception:
            logger.exception(
                "Unable to rank %s",
                stock.symbol,
            )

    ranked.sort(
        key=lambda item: item["today_percent_change"],
        reverse=True,
    )

    return ranked[:TOP_STOCK_COUNT]


# ============================================================
# Option selection
# ============================================================

def select_atm_option(
    underlying: str,
    option_type: str = OPTION_TYPE,
) -> tuple[str, int]:
    """
    Select the nearest ATM option using Tradehull.

    No instrument ID is used.
    """

    if option_type not in {"CE", "PE"}:
        raise ValueError(
            "option_type must be either CE or PE"
        )

    ce_symbol, pe_symbol, strike = (
        tsl.ATM_Strike_Selection(
            Underlying=underlying,
            Expiry=OPTION_EXPIRY_INDEX,
        )
    )

    selected_symbol = (
        ce_symbol
        if option_type == "CE"
        else pe_symbol
    )

    if not selected_symbol:
        raise RuntimeError(
            f"ATM {option_type} option unavailable for "
            f"{underlying}"
        )

    return str(selected_symbol), int(strike)


def get_option_lot_size(
    option_symbol: str,
) -> int:
    lot_size = tsl.get_lot_size(
        tradingsymbol=option_symbol,
    )

    if lot_size is None:
        raise RuntimeError(
            f"Lot size unavailable for {option_symbol}"
        )

    lot_size = int(lot_size)

    if lot_size <= 0:
        raise RuntimeError(
            f"Invalid lot size for {option_symbol}: {lot_size}"
        )

    return lot_size


# ============================================================
# Order functions
# ============================================================

def place_buy_order(
    option_symbol: str,
    quantity: int,
) -> str:
    logger.info(
        "BUY %s | quantity=%d | live=%s",
        option_symbol,
        quantity,
        is_live_trading(),
    )

    if not is_live_trading():
        logger.warning(
            "DRY RUN: BUY order not sent for %s",
            option_symbol,
        )
        return "DRY_RUN_BUY"

    order_id = tsl.order_placement(
        tradingsymbol=option_symbol,
        exchange=OPTION_EXCHANGE,
        quantity=quantity,
        price=0,
        trigger_price=0,
        order_type="MARKET",
        transaction_type="BUY",
        trade_type=TRADE_TYPE,
        validity="DAY",
    )

    if not order_id:
        raise RuntimeError(
            f"BUY order failed for {option_symbol}"
        )

    return str(order_id)


def place_sell_order(
    option_symbol: str,
    quantity: int,
    reason: str,
) -> str:
    logger.info(
        "SELL %s | quantity=%d | reason=%s | live=%s",
        option_symbol,
        quantity,
        reason,
        is_live_trading(),
    )

    if not is_live_trading():
        logger.warning(
            "DRY RUN: SELL order not sent for %s",
            option_symbol,
        )
        return "DRY_RUN_SELL"

    order_id = tsl.order_placement(
        tradingsymbol=option_symbol,
        exchange=OPTION_EXCHANGE,
        quantity=quantity,
        price=0,
        trigger_price=0,
        order_type="MARKET",
        transaction_type="SELL",
        trade_type=TRADE_TYPE,
        validity="DAY",
    )

    if not order_id:
        raise RuntimeError(
            f"SELL order failed for {option_symbol}"
        )

    return str(order_id)


def get_entry_price(
    order_id: str,
    option_symbol: str,
) -> float:
    if order_id == "DRY_RUN_BUY":
        return get_ltp(option_symbol)

    executed_price = tsl.get_executed_price(
        orderid=order_id,
    )

    if executed_price is None:
        raise RuntimeError(
            f"Executed price unavailable for order {order_id}"
        )

    executed_price = float(executed_price)

    if executed_price <= 0:
        raise RuntimeError(
            f"Invalid executed price: {executed_price}"
        )

    return executed_price


# ============================================================
# Trade lifecycle
# ============================================================

def has_open_trade_for_underlying(
    underlying: str,
) -> bool:
    return any(
        trade.underlying == underlying
        and not trade.exited
        for trade in open_trades
    )


def enter_trade(
    underlying: str,
    option_type: str = OPTION_TYPE,
) -> Trade:
    if has_open_trade_for_underlying(underlying):
        raise RuntimeError(
            f"An open trade already exists for {underlying}"
        )

    option_symbol, strike = select_atm_option(
        underlying=underlying,
        option_type=option_type,
    )

    quantity = get_option_lot_size(
        option_symbol=option_symbol,
    )

    order_id = place_buy_order(
        option_symbol=option_symbol,
        quantity=quantity,
    )

    entry_price = get_entry_price(
        order_id=order_id,
        option_symbol=option_symbol,
    )

    initial_stop = entry_price * (
        1 - INITIAL_STOP_LOSS_PERCENT / 100
    )

    target_price = entry_price * (
        1 + TARGET_PERCENT / 100
    )

    trade = Trade(
        underlying=underlying,
        option_symbol=option_symbol,
        option_type=option_type,
        quantity=quantity,
        entry_price=entry_price,
        highest_price=entry_price,
        stop_price=initial_stop,
        target_price=target_price,
        entry_order_id=order_id,
    )

    open_trades.append(trade)
    save_open_trades()

    logger.info(
        "TRADE ENTERED | underlying=%s | option=%s | "
        "strike=%d | entry=%.2f | stop=%.2f | target=%.2f",
        underlying,
        option_symbol,
        strike,
        entry_price,
        initial_stop,
        target_price,
    )

    return trade


def exit_trade(
    trade: Trade,
    reason: str,
) -> None:
    if trade.exited:
        return

    try:
        place_sell_order(
            option_symbol=trade.option_symbol,
            quantity=trade.quantity,
            reason=reason,
        )

        trade.exited = True
        save_open_trades()

        logger.info(
            "TRADE EXITED | option=%s | reason=%s",
            trade.option_symbol,
            reason,
        )

    except Exception:
        logger.exception(
            "Exit failed for %s",
            trade.option_symbol,
        )


def update_trade(trade: Trade) -> None:
    if trade.exited:
        return

    if is_force_exit_time():
        exit_trade(
            trade=trade,
            reason="FORCED_EXIT_15_15",
        )
        return

    current_ltp = get_ltp(
        trade.option_symbol,
    )

    # Update the highest observed option premium.
    if current_ltp > trade.highest_price:
        trade.highest_price = current_ltp

        trailing_stop = trade.highest_price * (
            1 - TRAILING_STOP_LOSS_PERCENT / 100
        )

        # The stop can only move upward.
        trade.stop_price = max(
            trade.stop_price,
            trailing_stop,
        )

        save_open_trades()

        logger.info(
            "TRAILING STOP UPDATED | option=%s | "
            "ltp=%.2f | high=%.2f | stop=%.2f",
            trade.option_symbol,
            current_ltp,
            trade.highest_price,
            trade.stop_price,
        )

    if current_ltp >= trade.target_price:
        exit_trade(
            trade=trade,
            reason=f"TARGET_{TARGET_PERCENT:.2f}_PERCENT",
        )
        return

    if current_ltp <= trade.stop_price:
        exit_trade(
            trade=trade,
            reason="INITIAL_OR_TRAILING_STOP",
        )


# ============================================================
# Background monitoring
# ============================================================

async def monitoring_loop() -> None:
    logger.info(
        "Monitoring loop started | live=%s",
        is_live_trading(),
    )

    while True:
        try:
            if is_force_exit_time():
                for trade in list(open_trades):
                    if not trade.exited:
                        exit_trade(
                            trade=trade,
                            reason="FORCED_EXIT_15_15",
                        )

            else:
                for trade in list(open_trades):
                    if not trade.exited:
                        try:
                            update_trade(trade)
                        except Exception:
                            logger.exception(
                                "Could not update trade %s",
                                trade.option_symbol,
                            )

        except Exception:
            logger.exception(
                "Unexpected monitoring-loop error"
            )

        await asyncio.sleep(
            MONITOR_INTERVAL_SECONDS
        )


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(
        monitoring_loop()
    )


# ============================================================
# Health endpoint
# ============================================================

@app.get("/")
def health_check() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "chartink-dhan-options-bot",
        "live_trading": is_live_trading(),
        "open_trades": [
            trade.to_dict()
            for trade in open_trades
            if not trade.exited
        ],
    }


# ============================================================
# Chartink webhook
# ============================================================

@app.post("/chartink/webhook")
async def chartink_webhook(
    request: Request,
) -> dict[str, Any]:
    try:
        payload = await request.json()

    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Request body must contain valid JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="JSON payload must be an object",
        )

    try:
        chartink_stocks = parse_chartink_payload(
            payload
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    if not chartink_stocks:
        raise HTTPException(
            status_code=422,
            detail="No valid stocks received",
        )

    metadata = {
        "triggered_at": payload.get("triggered_at"),
        "scan_name": payload.get("scan_name"),
        "scan_url": payload.get("scan_url"),
        "alert_name": payload.get("alert_name"),
        "webhook_url": payload.get("webhook_url"),
    }

    logger.info(
        "Chartink alert received | metadata=%s | stocks=%s",
        metadata,
        chartink_stocks,
    )

    if is_force_exit_time():
        return {
            "status": "ignored",
            "reason": "Alert received at or after 15:15",
            "metadata": metadata,
            "received_stocks": [
                {
                    "symbol": stock.symbol,
                    "trigger_price": stock.trigger_price,
                }
                for stock in chartink_stocks
            ],
        }

    ranked_stocks = rank_stocks_by_today_change(
        chartink_stocks
    )

    if not ranked_stocks:
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not calculate today's percentage "
                "change for any received stock"
            ),
        )

    order_results: list[dict[str, Any]] = []

    for ranked_stock in ranked_stocks:
        symbol = ranked_stock["symbol"]

        try:
            trade = enter_trade(
                underlying=symbol,
                option_type=OPTION_TYPE,
            )

            order_results.append(
                {
                    "status": "entered",
                    "underlying": symbol,
                    "trigger_price": ranked_stock[
                        "trigger_price"
                    ],
                    "today_percent_change": round(
                        ranked_stock[
                            "today_percent_change"
                        ],
                        2,
                    ),
                    "option_symbol": trade.option_symbol,
                    "option_type": trade.option_type,
                    "quantity": trade.quantity,
                    "entry_price": trade.entry_price,
                    "stop_price": trade.stop_price,
                    "target_price": trade.target_price,
                    "order_id": trade.entry_order_id,
                }
            )

        except Exception as exc:
            logger.exception(
                "Could not place trade for %s",
                symbol,
            )

            order_results.append(
                {
                    "status": "failed",
                    "underlying": symbol,
                    "trigger_price": ranked_stock[
                        "trigger_price"
                    ],
                    "today_percent_change": round(
                        ranked_stock[
                            "today_percent_change"
                        ],
                        2,
                    ),
                    "error": str(exc),
                }
            )

    return {
        "status": "accepted",
        "live_trading": is_live_trading(),
        "metadata": metadata,
        "received_stocks": [
            {
                "symbol": stock.symbol,
                "trigger_price": stock.trigger_price,
            }
            for stock in chartink_stocks
        ],
        "top_stocks": ranked_stocks,
        "orders": order_results,
    }