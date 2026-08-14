from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from Dhan_Tradehull import Tradehull


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

load_dotenv()

# CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
# ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")

CLIENT_ID = "1107559760"
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzg2NzYwMTc0LCJpYXQiOjE3ODY2NzM3NzQsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTA3NTU5NzYwIn0.awcYg5dnboGc6MVOZP7Y3_xiy_TTUfp-3YiArs21CLy5SnGATkztL_9KVUkE2MGfEdJhZm77bRjRn-Xktd5oJQ"


if not CLIENT_ID or not ACCESS_TOKEN:
    raise RuntimeError(
        "Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in the .env file"
    )

# LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"
LIVE_TRADING = "true"
TOP_N = 3
TARGET_PERCENT = 10.0
INITIAL_STOP_PERCENT = 4.0
TRAILING_STOP_PERCENT = 1.0

# Exit time in IST. The machine running this script should use IST,
# or set the timezone correctly before deploying.
FORCE_EXIT_TIME = time(23, 55)

# For individual stock options, the nearest expiry is generally selected
# with Expiry=0 by the Tradehull helper.
OPTION_EXPIRY_INDEX = 0

# Use the exchange required by your Dhan account/package setup.
UNDERLYING_EXCHANGE = "NSE"
OPTION_EXCHANGE = "NFO"
TRADE_TYPE = "MIS"

STATE_FILE = Path("open_trades.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Dhan Tradehull Options Strategy")
tsl = Tradehull(CLIENT_ID, ACCESS_TOKEN)


# ---------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------

@dataclass
class Trade:
    underlying: str
    option_symbol: str
    quantity: int
    entry_price: float
    highest_price: float
    stop_price: float
    target_price: float
    entry_order_id: str | None = None
    exited: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "option_symbol": self.option_symbol,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "highest_price": self.highest_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "entry_order_id": self.entry_order_id,
            "exited": self.exited,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trade":
        return cls(**data)


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------

def load_trades() -> list[Trade]:
    if not STATE_FILE.exists():
        return []

    try:
        raw = json.loads(STATE_FILE.read_text())
        return [Trade.from_dict(item) for item in raw]
    except Exception:
        logger.exception("Could not load state file")
        return []


def save_trades(trades: list[Trade]) -> None:
    STATE_FILE.write_text(
        json.dumps(
            [trade.to_dict() for trade in trades],
            indent=2,
        )
    )


open_trades: list[Trade] = load_trades()


# ---------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------

def normalize_symbol(value: Any) -> str:
    return str(value).strip().upper()


def parse_stock_array(value: Any) -> list[str]:
    """
    Supports:

        "AXISBANK,TCS,INFY"
        ["AXISBANK", "TCS", "INFY"]
    """
    if isinstance(value, str):
        values = re.split(r"[,\s]+", value)
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError("stocks must be a string or an array")

    symbols: list[str] = []

    for value in values:
        symbol = normalize_symbol(value)

        if symbol and symbol not in symbols:
            symbols.append(symbol)

    return symbols


def is_live() -> bool:
    return LIVE_TRADING


def get_ltp(symbol: str) -> float:
    """
    Read one LTP from Tradehull.

    The documented package usage is:
        tsl.get_ltp_data(names=["SYMBOL"])
    """
    response = tsl.get_ltp_data(names=[symbol])

    if response is None:
        raise RuntimeError(f"No LTP response for {symbol}")

    if isinstance(response, dict):
        value = response.get(symbol)

        # Some API responses may use different capitalization.
        if value is None:
            for key, candidate in response.items():
                if normalize_symbol(key) == normalize_symbol(symbol):
                    value = candidate
                    break

        if isinstance(value, dict):
            for field in ("ltp", "LTP", "last_price", "lastPrice", "close"):
                if field in value:
                    value = value[field]
                    break

        if value is None:
            raise RuntimeError(f"LTP not found for {symbol}: {response}")

        return float(value)

    return float(response)


def get_today_percent_change(symbol: str) -> float:
    """
    Calculate today's percentage change:

        (latest price - today's open) / today's open * 100

    This uses today's historical data, then falls back to quote data if
    the returned structure contains open and close/LTP values.
    """
    try:
        data = tsl.get_historical_data(
            tradingsymbol=symbol,
            exchange=UNDERLYING_EXCHANGE,
            timeframe="DAY",
        )

        if isinstance(data, pd.DataFrame) and not data.empty:
            data = data.copy()
            data.columns = [str(column).lower() for column in data.columns]

            open_column = next(
                (
                    column
                    for column in ("open", "open_price")
                    if column in data.columns
                ),
                None,
            )

            close_column = next(
                (
                    column
                    for column in ("close", "ltp", "last_price")
                    if column in data.columns
                ),
                None,
            )

            if open_column and close_column:
                today_open = float(data.iloc[-1][open_column])
                today_close = float(data.iloc[-1][close_column])

                if today_open != 0:
                    return (today_close - today_open) / today_open * 100

    except Exception:
        logger.exception("Historical percentage-change lookup failed for %s", symbol)

    # Fallback to quote data.
    quote = tsl.get_quote_data(names=[symbol])

    if not isinstance(quote, dict):
        raise RuntimeError(f"Unexpected quote response for {symbol}: {quote}")

    row = quote.get(symbol)

    if row is None:
        for key, candidate in quote.items():
            if normalize_symbol(key) == normalize_symbol(symbol):
                row = candidate
                break

    if not isinstance(row, dict):
        raise RuntimeError(f"Unexpected quote row for {symbol}: {row}")

    today_open = row.get("open") or row.get("open_price")
    today_close = (
        row.get("close")
        or row.get("ltp")
        or row.get("last_price")
        or row.get("lastPrice")
    )

    if today_open is None or today_close is None:
        raise RuntimeError(
            f"Could not find open/close fields for {symbol}: {row}"
        )

    today_open = float(today_open)
    today_close = float(today_close)

    if today_open == 0:
        raise RuntimeError(f"Today's open is zero for {symbol}")

    return (today_close - today_open) / today_open * 100


def get_top_stocks(stocks: list[str]) -> list[tuple[str, float]]:
    changes: list[tuple[str, float]] = []

    for stock in stocks:
        try:
            percent_change = get_today_percent_change(stock)
            changes.append((stock, percent_change))
            logger.info("%s: %.2f%% today", stock, percent_change)
        except Exception:
            logger.exception("Could not calculate change for %s", stock)

    changes.sort(key=lambda item: item[1], reverse=True)
    return changes[:TOP_N]


def get_lot_size(option_symbol: str) -> int:
    """
    Use one lot per selected option.

    Confirm the returned value and your risk sizing before going live.
    """
    lot_size = tsl.get_lot_size(tradingsymbol=option_symbol)

    if lot_size is None:
        raise RuntimeError(f"Lot size unavailable for {option_symbol}")

    lot_size = int(lot_size)

    if lot_size <= 0:
        raise RuntimeError(
            f"Invalid lot size for {option_symbol}: {lot_size}"
        )

    return lot_size


# ---------------------------------------------------------------------
# Order handling
# ---------------------------------------------------------------------

def place_buy_order(
    option_symbol: str,
    quantity: int,
) -> str | None:
    logger.info(
        "BUY %s quantity=%d live=%s",
        option_symbol,
        quantity,
        is_live(),
    )

    if not is_live():
        logger.warning("DRY RUN: BUY order was not sent")
        return "DRY_RUN_ENTRY"

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
        raise RuntimeError(f"BUY order failed for {option_symbol}")

    return str(order_id)


def place_sell_order(
    option_symbol: str,
    quantity: int,
    reason: str,
) -> str | None:
    logger.info(
        "SELL %s quantity=%d reason=%s live=%s",
        option_symbol,
        quantity,
        reason,
        is_live(),
    )

    if not is_live():
        logger.warning("DRY RUN: SELL order was not sent")
        return "DRY_RUN_EXIT"

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
        raise RuntimeError(f"SELL order failed for {option_symbol}")

    return str(order_id)


def get_executed_entry_price(
    order_id: str | None,
    option_symbol: str,
) -> float:
    if order_id == "DRY_RUN_ENTRY":
        return get_ltp(option_symbol)

    if not order_id:
        raise RuntimeError("Missing entry order ID")

    price = tsl.get_executed_price(orderid=order_id)

    if price is None or float(price) <= 0:
        raise RuntimeError(
            f"Could not retrieve executed price for order {order_id}"
        )

    return float(price)


def already_trading(underlying: str) -> bool:
    return any(
        trade.underlying == underlying and not trade.exited
        for trade in open_trades
    )


def enter_option_trade(
    underlying: str,
    option_type: str = "CE",
) -> Trade:
    """
    Select the nearest ATM strike and buy the requested option type.

    For a long-only strategy, CE is used by default. If you want to buy
    ATM puts instead, call this with option_type="PE".
    """
    if already_trading(underlying):
        raise RuntimeError(f"Already have an open trade for {underlying}")

    ce_symbol, pe_symbol, strike = tsl.ATM_Strike_Selection(
        Underlying=underlying,
        Expiry=OPTION_EXPIRY_INDEX,
    )

    option_symbol = ce_symbol if option_type == "CE" else pe_symbol

    if not option_symbol:
        raise RuntimeError(
            f"ATM {option_type} option was not found for {underlying}; "
            f"strike={strike}"
        )

    quantity = get_lot_size(option_symbol)
    order_id = place_buy_order(option_symbol, quantity)
    entry_price = get_executed_entry_price(order_id, option_symbol)

    trade = Trade(
        underlying=underlying,
        option_symbol=option_symbol,
        quantity=quantity,
        entry_price=entry_price,
        highest_price=entry_price,
        stop_price=entry_price * (1 - INITIAL_STOP_PERCENT / 100),
        target_price=entry_price * (1 + TARGET_PERCENT / 100),
        entry_order_id=order_id,
    )

    open_trades.append(trade)
    save_trades(open_trades)

    logger.info(
        "Entered %s: entry=%.2f stop=%.2f target=%.2f",
        option_symbol,
        trade.entry_price,
        trade.stop_price,
        trade.target_price,
    )

    return trade


def exit_trade(trade: Trade, reason: str) -> None:
    if trade.exited:
        return

    try:
        place_sell_order(
            option_symbol=trade.option_symbol,
            quantity=trade.quantity,
            reason=reason,
        )
        trade.exited = True
        save_trades(open_trades)
        logger.info("Exited %s: %s", trade.option_symbol, reason)
    except Exception:
        logger.exception("Exit failed for %s", trade.option_symbol)


# ---------------------------------------------------------------------
# Monitoring logic
# ---------------------------------------------------------------------

def current_time() -> time:
    return datetime.now().time()


def update_trade(trade: Trade) -> None:
    if trade.exited:
        return

    ltp = get_ltp(trade.option_symbol)

    # Exit everything at or after 15:15.
    if current_time() >= FORCE_EXIT_TIME:
        exit_trade(trade, "TIME_EXIT_15_15")
        return

    # Track the highest option premium after entry.
    if ltp > trade.highest_price:
        trade.highest_price = ltp

        # A 1% trailing stop follows the option price upward.
        trailing_stop = trade.highest_price * (
            1 - TRAILING_STOP_PERCENT / 100
        )

        # Never move the stop downward.
        trade.stop_price = max(trade.stop_price, trailing_stop)
        save_trades(open_trades)

    if ltp >= trade.target_price:
        exit_trade(trade, f"TARGET_{TARGET_PERCENT:.1f}%")
        return

    if ltp <= trade.stop_price:
        exit_trade(trade, "STOP_OR_TRAILING_STOP")


async def monitor_loop() -> None:
    while True:
        try:
            if open_trades:
                for trade in list(open_trades):
                    update_trade(trade)
        except Exception:
            logger.exception("Monitoring loop error")

        await asyncio.sleep(5)


# ---------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------

@app.post("/chartink/webhook")
async def chartink_webhook(request: Request) -> dict[str, Any]:
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

    if "stocks" not in payload:
        raise HTTPException(
            status_code=422,
            detail="Missing required field: stocks",
        )

    try:
        received_stocks = parse_stock_array(payload["stocks"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not received_stocks:
        raise HTTPException(
            status_code=422,
            detail="No stocks were received",
        )

    if current_time() >= FORCE_EXIT_TIME:
        return {
            "status": "ignored",
            "reason": "Past 15:15 exit time",
            "received_stocks": received_stocks,
        }

    top_stocks = get_top_stocks(received_stocks)

    if not top_stocks:
        raise HTTPException(
            status_code=422,
            detail="Could not calculate percentage change for any stock",
        )

    results: list[dict[str, Any]] = []

    for stock, percent_change in top_stocks:
        try:
            # This buys ATM calls. Change to "PE" for ATM puts.
            trade = enter_option_trade(
                underlying=stock,
                option_type="CE",
            )

            results.append(
                {
                    "underlying": stock,
                    "today_percent_change": round(percent_change, 2),
                    "option_symbol": trade.option_symbol,
                    "quantity": trade.quantity,
                    "entry_price": trade.entry_price,
                    "stop_price": trade.stop_price,
                    "target_price": trade.target_price,
                    "order_id": trade.entry_order_id,
                    "status": "entered",
                }
            )

        except Exception as exc:
            logger.exception("Could not enter trade for %s", stock)
            results.append(
                {
                    "underlying": stock,
                    "today_percent_change": round(percent_change, 2),
                    "status": "failed",
                    "error": str(exc),
                }
            )

    return {
        "status": "accepted",
        "live_trading": is_live(),
        "received_stocks": received_stocks,
        "top_stocks": [
            {
                "symbol": symbol,
                "today_percent_change": round(change, 2),
            }
            for symbol, change in top_stocks
        ],
        "orders": results,
    }


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(monitor_loop())