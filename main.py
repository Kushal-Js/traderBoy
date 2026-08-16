from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request


# ============================================================
# Configuration
# ============================================================

load_dotenv()

GROWW_ACCESS_TOKEN = os.getenv("GROWW_ACCESS_TOKEN")
GROWW_CLIENT_ID = os.getenv("GROWW_CLIENT_ID")

if not GROWW_ACCESS_TOKEN:
    raise RuntimeError(
        "GROWW_ACCESS_TOKEN must be set in .env"
    )

if not GROWW_CLIENT_ID:
    raise RuntimeError(
        "GROWW_CLIENT_ID must be set in .env"
    )

GROWW_API_BASE_URL = os.getenv(
    "GROWW_API_BASE_URL",
    "https://api.groww.in/v1",
).rstrip("/")

GROWW_API_VERSION = os.getenv(
    "GROWW_API_VERSION",
    "1.0",
)

LIVE_TRADING = (
    os.getenv("LIVE_TRADING", "false")
    .strip()
    .lower()
    == "true"
)

UNDERLYING_EXCHANGE = os.getenv(
    "UNDERLYING_EXCHANGE",
    "NSE",
)

OPTION_EXCHANGE = os.getenv(
    "OPTION_EXCHANGE",
    "NSE",
)

UNDERLYING_SEGMENT = os.getenv(
    "UNDERLYING_SEGMENT",
    "CASH",
)

OPTION_SEGMENT = os.getenv(
    "OPTION_SEGMENT",
    "FNO",
)

OPTION_PRODUCT = os.getenv(
    "OPTION_PRODUCT",
    "NRML",
)

OPTION_TYPE = os.getenv(
    "OPTION_TYPE",
    "CE",
).strip().upper()

OPTION_EXPIRY_DATE = os.getenv(
    "OPTION_EXPIRY_DATE"
)

TOP_STOCK_COUNT = int(
    os.getenv("TOP_STOCK_COUNT", "3")
)

TARGET_PERCENT = float(
    os.getenv("TARGET_PERCENT", "10.0")
)

INITIAL_STOP_LOSS_PERCENT = float(
    os.getenv("INITIAL_STOP_LOSS_PERCENT", "3.0")
)

TRAILING_STOP_LOSS_PERCENT = float(
    os.getenv("TRAILING_STOP_LOSS_PERCENT", "1.0")
)

FORCE_EXIT_TIME = time(
    int(os.getenv("FORCE_EXIT_HOUR", "15")),
    int(os.getenv("FORCE_EXIT_MINUTE", "15")),
)

MONITOR_INTERVAL_SECONDS = int(
    os.getenv("MONITOR_INTERVAL_SECONDS", "5")
)

REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("REQUEST_TIMEOUT_SECONDS", "20")
)

STATE_FILE = Path(
    os.getenv("STATE_FILE", "open_trades.json")
)


# ============================================================
# Logging and application
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Chartink Groww Options Trading Bot",
    version="3.0.0",
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
    expiry_date: str
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
    def from_dict(
        cls,
        value: dict[str, Any],
    ) -> "Trade":
        return cls(**value)


# ============================================================
# Persistent state
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
        logger.exception(
            "Unable to load saved trade state"
        )
        return []


open_trades: list[Trade] = load_open_trades()


def save_open_trades() -> None:
    temporary_file = STATE_FILE.with_suffix(".tmp")

    temporary_file.write_text(
        json.dumps(
            [
                trade.to_dict()
                for trade in open_trades
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary_file.replace(STATE_FILE)


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


def first_value(
    data: dict[str, Any],
    keys: tuple[str, ...],
) -> Any:
    lowered = {
        str(key).lower(): value
        for key, value in data.items()
    }

    for key in keys:
        value = lowered.get(key.lower())

        if value is not None:
            return value

    return None


def unwrap_payload(data: Any) -> Any:
    if isinstance(data, dict):
        payload = data.get("payload")

        if payload is not None:
            return payload

    return data


def create_order_reference_id(
    prefix: str = "GRW",
) -> str:
    """
    Groww requires an 8–20 character alphanumeric reference ID,
    with at most two hyphens.
    """
    value = (
        f"{prefix}"
        f"{datetime.now():%H%M%S}"
        f"{uuid.uuid4().hex[:5].upper()}"
    )

    return re.sub(
        r"[^A-Za-z0-9-]",
        "",
        value,
    )[:20]


def get_expiry_date() -> str:
    if not OPTION_EXPIRY_DATE:
        raise RuntimeError(
            "OPTION_EXPIRY_DATE is missing from .env. "
            "Use YYYY-MM-DD format."
        )

    try:
        datetime.strptime(
            OPTION_EXPIRY_DATE,
            "%Y-%m-%d",
        )

    except ValueError as exc:
        raise RuntimeError(
            "OPTION_EXPIRY_DATE must use YYYY-MM-DD format."
        ) from exc

    return OPTION_EXPIRY_DATE


# ============================================================
# Groww API client
# ============================================================

class GrowwClient:
    def __init__(
        self,
        access_token: str,
        client_id: str,
    ) -> None:
        self.access_token = access_token
        self.client_id = client_id

    def headers(self) -> dict[str, str]:
        """
        Groww documents these common API headers:
          Authorization: Bearer {ACCESS_TOKEN}
          Accept: application/json
          X-API-VERSION: 1.0

        X-Client-Id is included because the application is configured
        with GROWW_CLIENT_ID. If Groww rejects this optional header,
        remove only X-Client-Id from this method.
        """
        return {
            "Authorization": (
                f"Bearer {self.access_token}"
            ),
            "Accept": "application/json",
            "X-API-VERSION": GROWW_API_VERSION,
            "X-Client-Id": self.client_id,
            "Content-Type": "application/json",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = (
            f"{GROWW_API_BASE_URL}/"
            f"{path.lstrip('/')}"
        )

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=self.headers(),
                params=params,
                json=json_body,
            )

        try:
            data = response.json()

        except ValueError:
            data = {
                "status": "FAILURE",
                "error": {
                    "message": response.text,
                },
            }

        if not isinstance(data, dict):
            data = {
                "status": "FAILURE",
                "payload": data,
            }

        if not response.is_success:
            raise RuntimeError(
                f"Groww HTTP {response.status_code}: "
                f"{data}"
            )

        if str(data.get("status", "")).upper() == "FAILURE":
            raise RuntimeError(
                f"Groww API failure: {data}"
            )

        return data

    async def get_user_detail(
        self,
    ) -> dict[str, Any]:
        data = await self.request(
            "GET",
            "/user/detail",
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid user-detail response: {data}"
            )

        return payload

    async def get_quote(
        self,
        exchange: str,
        segment: str,
        trading_symbol: str,
    ) -> dict[str, Any]:
        data = await self.request(
            "GET",
            "/live-data/quote",
            params={
                "exchange": exchange,
                "segment": segment,
                "trading_symbol": trading_symbol,
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid quote response: {data}"
            )

        return payload

    async def get_ltp(
        self,
        exchange: str,
        segment: str,
        trading_symbol: str,
    ) -> float:
        """
        Groww's documented LTP parameter is an array named
        exchange_symbols. httpx sends a repeated query parameter
        when given a list.
        """
        data = await self.request(
            "GET",
            "/live-data/ltp",
            params=[
                (
                    "segment",
                    segment,
                ),
                (
                    "exchange_symbols",
                    f"{exchange}_{trading_symbol}",
                ),
            ],
        )

        payload = unwrap_payload(data)

        if isinstance(payload, dict):
            direct_ltp = first_value(
                payload,
                (
                    "ltp",
                    "last_price",
                    "price",
                ),
            )

            if direct_ltp is not None:
                ltp = float(direct_ltp)

                if ltp > 0:
                    return ltp

            nested = (
                payload.get(
                    f"{exchange}_{trading_symbol}"
                )
                or payload.get(trading_symbol)
            )

            if isinstance(nested, dict):
                nested_ltp = first_value(
                    nested,
                    (
                        "ltp",
                        "last_price",
                        "price",
                    ),
                )

                if nested_ltp is not None:
                    ltp = float(nested_ltp)

                    if ltp > 0:
                        return ltp

        raise RuntimeError(
            f"Could not extract LTP for "
            f"{exchange}_{trading_symbol}: {data}"
        )

    async def get_option_chain(
        self,
        exchange: str,
        underlying: str,
        expiry_date: str,
    ) -> dict[str, Any]:
        data = await self.request(
            "GET",
            (
                "/option-chain/exchange/"
                f"{exchange}/underlying/{underlying}"
            ),
            params={
                "expiry_date": expiry_date,
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid option-chain response: {data}"
            )

        return payload

    async def place_order(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        data = await self.request(
            "POST",
            "/order/create",
            json_body=payload,
        )

        payload_data = unwrap_payload(data)

        if not isinstance(payload_data, dict):
            raise RuntimeError(
                f"Invalid order response: {data}"
            )

        return payload_data

    async def get_order_status(
        self,
        groww_order_id: str,
        segment: str,
    ) -> dict[str, Any]:
        data = await self.request(
            "GET",
            f"/order/status/{groww_order_id}",
            params={
                "segment": segment,
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid order-status response: {data}"
            )

        return payload

    async def get_order_detail(
        self,
        groww_order_id: str,
        segment: str,
    ) -> dict[str, Any]:
        data = await self.request(
            "GET",
            f"/order/detail/{groww_order_id}",
            params={
                "segment": segment,
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid order-detail response: {data}"
            )

        return payload

    async def get_trade_list_for_order(
        self,
        groww_order_id: str,
        segment: str,
    ) -> list[dict[str, Any]]:
        data = await self.request(
            "GET",
            f"/order/trades/{groww_order_id}",
            params={
                "segment": segment,
                "page": 1,
                "page_size": 50,
            },
        )

        payload = unwrap_payload(data)

        if isinstance(payload, list):
            return [
                item
                for item in payload
                if isinstance(item, dict)
            ]

        if isinstance(payload, dict):
            trades = (
                payload.get("trades")
                or payload.get("items")
                or payload.get("data")
                or []
            )

            if isinstance(trades, list):
                return [
                    item
                    for item in trades
                    if isinstance(item, dict)
                ]

        raise RuntimeError(
            f"Invalid trade-list response: {data}"
        )

    async def get_order_list(
        self,
        segment: str,
    ) -> Any:
        data = await self.request(
            "GET",
            "/order/list",
            params={
                "segment": segment,
                "page": 1,
                "page_size": 100,
            },
        )

        return unwrap_payload(data)


groww = GrowwClient(
    access_token=GROWW_ACCESS_TOKEN,
    client_id=GROWW_CLIENT_ID,
)


# ============================================================
# Chartink parsing
# ============================================================

def parse_chartink_payload(
    payload: dict[str, Any],
) -> list[ChartinkStock]:
    required_fields = (
        "stocks",
        "trigger_prices",
    )

    for field in required_fields:
        if field not in payload:
            raise ValueError(
                f"Missing required field: {field}"
            )

    raw_stocks = payload["stocks"]
    raw_trigger_prices = payload["trigger_prices"]

    if not isinstance(raw_stocks, str):
        raise ValueError(
            "stocks must be a comma-separated string"
        )

    if not isinstance(raw_trigger_prices, str):
        raise ValueError(
            "trigger_prices must be a comma-separated string"
        )

    symbols = [
        normalize_symbol(symbol)
        for symbol in raw_stocks.split(",")
        if symbol.strip()
    ]

    price_strings = [
        price.strip()
        for price in raw_trigger_prices.split(",")
        if price.strip()
    ]

    if len(symbols) != len(price_strings):
        raise ValueError(
            "stocks and trigger_prices must contain "
            "the same number of values"
        )

    results: list[ChartinkStock] = []
    seen_symbols: set[str] = set()

    for symbol, raw_price in zip(
        symbols,
        price_strings,
    ):
        if symbol in seen_symbols:
            logger.warning(
                "Duplicate symbol ignored: %s",
                symbol,
            )
            continue

        try:
            trigger_price = float(
                raw_price.replace(",", "")
            )

        except ValueError as exc:
            raise ValueError(
                f"Invalid trigger price for {symbol}: "
                f"{raw_price}"
            ) from exc

        if trigger_price <= 0:
            raise ValueError(
                f"Trigger price must be positive for {symbol}"
            )

        results.append(
            ChartinkStock(
                symbol=symbol,
                trigger_price=trigger_price,
            )
        )

        seen_symbols.add(symbol)

    return results

# ============================================================
# Market data
# ============================================================

async def get_ltp(
    symbol: str,
    exchange: str,
    segment: str,
) -> float:
    ltp = await groww.get_ltp(
        exchange=exchange,
        segment=segment,
        trading_symbol=symbol,
    )

    if ltp <= 0:
        raise RuntimeError(
            f"Invalid LTP for {symbol}: {ltp}"
        )

    return ltp


async def get_today_percent_change(
    symbol: str,
) -> float:
    quote = await groww.get_quote(
        exchange=UNDERLYING_EXCHANGE,
        segment=UNDERLYING_SEGMENT,
        trading_symbol=symbol,
    )

    day_change_percent = first_value(
        quote,
        (
            "day_change_perc",
            "day_change_percentage",
            "change_percent",
        ),
    )

    if day_change_percent is not None:
        return float(day_change_percent)

    open_price = first_value(
        quote,
        (
            "open",
            "open_price",
        ),
    )

    last_price = first_value(
        quote,
        (
            "last_price",
            "ltp",
            "price",
        ),
    )

    if open_price is None or last_price is None:
        raise RuntimeError(
            f"Quote lacks open/last price: {quote}"
        )

    open_price = float(open_price)
    last_price = float(last_price)

    if open_price <= 0:
        raise RuntimeError(
            f"Invalid open price for {symbol}: "
            f"{open_price}"
        )

    return (
        (last_price - open_price)
        / open_price
        * 100
    )


async def rank_stocks_by_today_change(
    stocks: list[ChartinkStock],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []

    for stock in stocks:
        try:
            percent_change = (
                await get_today_percent_change(
                    stock.symbol
                )
            )

            ranked.append(
                {
                    "symbol": stock.symbol,
                    "trigger_price": stock.trigger_price,
                    "today_percent_change": (
                        percent_change
                    ),
                }
            )

            logger.info(
                "%s | trigger=%.2f | day_change=%.2f%%",
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
        key=lambda item: item[
            "today_percent_change"
        ],
        reverse=True,
    )

    return ranked[:TOP_STOCK_COUNT]


# ============================================================
# Option selection
# ============================================================

async def select_atm_option(
    underlying: str,
    option_type: str,
) -> tuple[str, float]:
    if option_type not in {"CE", "PE"}:
        raise ValueError(
            "OPTION_TYPE must be CE or PE"
        )

    expiry_date = get_expiry_date()

    chain = await groww.get_option_chain(
        exchange=OPTION_EXCHANGE,
        underlying=underlying,
        expiry_date=expiry_date,
    )

    underlying_ltp_value = first_value(
        chain,
        (
            "underlying_ltp",
            "underlying_last_price",
            "ltp",
        ),
    )

    if underlying_ltp_value is None:
        underlying_ltp = await get_ltp(
            symbol=underlying,
            exchange=UNDERLYING_EXCHANGE,
            segment=UNDERLYING_SEGMENT,
        )

    else:
        underlying_ltp = float(
            underlying_ltp_value
        )

    strikes = chain.get("strikes")

    if not isinstance(strikes, dict):
        raise RuntimeError(
            f"Option-chain strikes missing: {chain}"
        )

    parsed_strikes: list[
        tuple[float, dict[str, Any]]
    ] = []

    for raw_strike, strike_data in strikes.items():
        try:
            strike = float(raw_strike)

        except (TypeError, ValueError):
            continue

        if isinstance(strike_data, dict):
            parsed_strikes.append(
                (strike, strike_data)
            )

    if not parsed_strikes:
        raise RuntimeError(
            f"No valid strikes found for {underlying}"
        )

    selected_strike, selected_data = min(
        parsed_strikes,
        key=lambda item: abs(
            item[0] - underlying_ltp
        ),
    )

    contract = selected_data.get(option_type)

    if not isinstance(contract, dict):
        raise RuntimeError(
            f"{option_type} unavailable at strike "
            f"{selected_strike}: {selected_data}"
        )

    option_symbol = (
        contract.get("trading_symbol")
        or contract.get("symbol")
    )

    option_ltp = first_value(
        contract,
        (
            "ltp",
            "last_price",
            "price",
        ),
    )

    if not option_symbol:
        raise RuntimeError(
            f"Option trading symbol missing: {contract}"
        )

    if option_ltp is None:
        raise RuntimeError(
            f"Option LTP missing: {contract}"
        )

    option_ltp = float(option_ltp)

    if option_ltp <= 0:
        raise RuntimeError(
            f"Invalid option LTP: {option_ltp}"
        )

    logger.info(
        "ATM option | underlying=%s | underlying_ltp=%.2f | "
        "strike=%.2f | option=%s | option_ltp=%.2f",
        underlying,
        underlying_ltp,
        selected_strike,
        option_symbol,
        option_ltp,
    )

    return str(option_symbol), option_ltp


async def get_option_lot_size(
    option_symbol: str,
) -> int:
    """
    Uses the Groww instruments endpoint.

    Verify the exact instrument response for your account. The
    code accepts common list and field names.
    """
    data = await groww.request(
        "GET",
        "/instruments",
        params={
            "segment": OPTION_SEGMENT,
            "exchange": OPTION_EXCHANGE,
        },
    )

    instruments = unwrap_payload(data)

    if isinstance(instruments, dict):
        instruments = (
            instruments.get("instruments")
            or instruments.get("items")
            or instruments.get("data")
        )

    if not isinstance(instruments, list):
        raise RuntimeError(
            "Groww instruments response is not a list."
        )

    for instrument in instruments:
        if not isinstance(instrument, dict):
            continue

        symbol = first_value(
            instrument,
            (
                "trading_symbol",
                "groww_symbol",
                "symbol",
            ),
        )

        if str(symbol) != option_symbol:
            continue

        lot_size = first_value(
            instrument,
            (
                "lot_size",
                "lotSize",
            ),
        )

        if lot_size is None:
            break

        lot_size = int(lot_size)

        if lot_size <= 0:
            break

        return lot_size

    raise RuntimeError(
        f"Lot size unavailable for {option_symbol}"
    )


# ============================================================
# Order operations
# ============================================================

async def place_order(
    trading_symbol: str,
    quantity: int,
    transaction_type: str,
    exchange: str,
    segment: str,
    product: str,
    order_type: str = "MARKET",
    price: float = 0,
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError(
            "Quantity must be greater than zero"
        )

    if transaction_type not in {"BUY", "SELL"}:
        raise ValueError(
            "transaction_type must be BUY or SELL"
        )

    if order_type not in {"MARKET", "LIMIT"}:
        raise ValueError(
            "order_type must be MARKET or LIMIT"
        )

    if order_type == "LIMIT" and price <= 0:
        raise ValueError(
            "LIMIT order requires a positive price"
        )

    payload = {
        "trading_symbol": trading_symbol,
        "quantity": quantity,
        "price": round(price, 2),
        "trigger_price": 0,
        "validity": "DAY",
        "exchange": exchange,
        "segment": segment,
        "product": product,
        "order_type": order_type,
        "transaction_type": transaction_type,
        "order_reference_id": (
            create_order_reference_id()
        ),
    }

    logger.info(
        "Order payload | %s",
        payload,
    )

    if not is_live_trading():
        logger.warning(
            "DRY RUN: order was not submitted"
        )

        return {
            "status": "DRY_RUN",
            "groww_order_id": "DRY_RUN",
            "order_status": "DRY_RUN",
            "remark": "LIVE_TRADING=false",
            **payload,
        }

    result = await groww.place_order(payload)

    order_status = str(
        result.get("order_status", "")
    ).upper()

    if order_status in {
        "REJECTED",
        "FAILED",
        "CANCELLED",
    }:
        raise RuntimeError(
            "Groww order failed: "
            f"{result.get('remark', result)}"
        )

    return result


async def place_buy_order(
    option_symbol: str,
    quantity: int,
) -> str:
    response = await place_order(
        trading_symbol=option_symbol,
        quantity=quantity,
        transaction_type="BUY",
        exchange=OPTION_EXCHANGE,
        segment=OPTION_SEGMENT,
        product=OPTION_PRODUCT,
        order_type="MARKET",
        price=0,
    )

    order_id = response.get(
        "groww_order_id"
    )

    if not order_id:
        raise RuntimeError(
            f"BUY response has no order ID: {response}"
        )

    return str(order_id)


async def place_sell_order(
    option_symbol: str,
    quantity: int,
    reason: str,
) -> str:
    logger.info(
        "SELL | option=%s | quantity=%d | reason=%s",
        option_symbol,
        quantity,
        reason,
    )

    response = await place_order(
        trading_symbol=option_symbol,
        quantity=quantity,
        transaction_type="SELL",
        exchange=OPTION_EXCHANGE,
        segment=OPTION_SEGMENT,
        product=OPTION_PRODUCT,
        order_type="MARKET",
        price=0,
    )

    order_id = response.get(
        "groww_order_id"
    )

    if not order_id:
        raise RuntimeError(
            f"SELL response has no order ID: {response}"
        )

    return str(order_id)


async def get_entry_price(
    order_id: str,
    option_symbol: str,
) -> float:
    if not is_live_trading() or order_id == "DRY_RUN":
        return await get_ltp(
            symbol=option_symbol,
            exchange=OPTION_EXCHANGE,
            segment=OPTION_SEGMENT,
        )

    for attempt in range(1, 7):
        try:
            trades = (
                await groww.get_trade_list_for_order(
                    groww_order_id=order_id,
                    segment=OPTION_SEGMENT,
                )
            )

            if trades:
                total_quantity = 0
                total_value = 0.0

                for trade in trades:
                    price = first_value(
                        trade,
                        (
                            "price",
                            "trade_price",
                        ),
                    )

                    quantity = first_value(
                        trade,
                        (
                            "quantity",
                            "filled_quantity",
                        ),
                    )

                    if price is None or quantity is None:
                        continue

                    price = float(price)
                    quantity = int(quantity)

                    total_quantity += quantity
                    total_value += (
                        price * quantity
                    )

                if total_quantity > 0:
                    return (
                        total_value
                        / total_quantity
                    )

        except Exception:
            logger.warning(
                "Trade lookup attempt %d failed for %s",
                attempt,
                order_id,
                exc_info=True,
            )

        await asyncio.sleep(2)

    raise RuntimeError(
        f"Could not obtain execution price for "
        f"order {order_id}"
    )


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


async def enter_trade(
    underlying: str,
    option_type: str,
) -> Trade:
    if has_open_trade_for_underlying(underlying):
        raise RuntimeError(
            f"Open trade already exists for {underlying}"
        )

    option_symbol, _ = await select_atm_option(
        underlying=underlying,
        option_type=option_type,
    )

    quantity = await get_option_lot_size(
        option_symbol=option_symbol
    )

    order_id = await place_buy_order(
        option_symbol=option_symbol,
        quantity=quantity,
    )

    entry_price = await get_entry_price(
        order_id=order_id,
        option_symbol=option_symbol,
    )

    stop_price = entry_price * (
        1 - INITIAL_STOP_LOSS_PERCENT / 100
    )

    target_price = entry_price * (
        1 + TARGET_PERCENT / 100
    )

    trade = Trade(
        underlying=underlying,
        option_symbol=option_symbol,
        option_type=option_type,
        expiry_date=get_expiry_date(),
        quantity=quantity,
        entry_price=entry_price,
        highest_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        entry_order_id=order_id,
    )

    open_trades.append(trade)
    save_open_trades()

    logger.info(
        "TRADE ENTERED | underlying=%s | option=%s | "
        "quantity=%d | entry=%.2f | stop=%.2f | target=%.2f",
        underlying,
        option_symbol,
        quantity,
        entry_price,
        stop_price,
        target_price,
    )

    return trade


async def exit_trade(
    trade: Trade,
    reason: str,
) -> None:
    if trade.exited:
        return

    try:
        await place_sell_order(
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


async def update_trade(
    trade: Trade,
) -> None:
    if trade.exited:
        return

    if is_force_exit_time():
        await exit_trade(
            trade=trade,
            reason="FORCED_EXIT",
        )
        return

    current_ltp = await get_ltp(
        symbol=trade.option_symbol,
        exchange=OPTION_EXCHANGE,
        segment=OPTION_SEGMENT,
    )

    if current_ltp > trade.highest_price:
        trade.highest_price = current_ltp

        trailing_stop = (
            trade.highest_price
            * (1 - TRAILING_STOP_LOSS_PERCENT / 100)
        )

        trade.stop_price = max(
            trade.stop_price,
            trailing_stop,
        )

        save_open_trades()

        logger.info(
            "TRAILING STOP | option=%s | ltp=%.2f | "
            "high=%.2f | stop=%.2f",
            trade.option_symbol,
            current_ltp,
            trade.highest_price,
            trade.stop_price,
        )

    if current_ltp >= trade.target_price:
        await exit_trade(
            trade=trade,
            reason="TARGET_REACHED",
        )
        return

    if current_ltp <= trade.stop_price:
        await exit_trade(
            trade=trade,
            reason="STOP_LOSS",
        )


# ============================================================
# Background monitor
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
                        await exit_trade(
                            trade=trade,
                            reason="FORCED_EXIT",
                        )

            else:
                for trade in list(open_trades):
                    if not trade.exited:
                        try:
                            await update_trade(trade)

                        except Exception:
                            logger.exception(
                                "Could not update %s",
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
# Health and diagnostics
# ============================================================

@app.get("/")
async def health_check() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "chartink-groww-options-bot",
        "live_trading": is_live_trading(),
        "groww_client_id_configured": bool(
            GROWW_CLIENT_ID
        ),
        "groww_access_token_configured": bool(
            GROWW_ACCESS_TOKEN
        ),
        "open_trades": [
            trade.to_dict()
            for trade in open_trades
            if not trade.exited
        ],
    }


@app.get("/debug/outbound-ip")
async def get_outbound_ip() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=10
        ) as client:
            response = await client.get(
                "https://api.ipify.org?format=json"
            )

            response.raise_for_status()

        data = response.json()

        return {
            "outbound_ip": data.get("ip"),
            "note": (
                "Public IP visible to external APIs"
            ),
        }

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.get("/debug/auth")
async def test_authentication() -> dict[str, Any]:
    """
    Authentication-only endpoint.

    This calls Groww user detail and does not place an order.
    """
    try:
        user = await groww.get_user_detail()

        return {
            "status": "ok",
            "groww_user_detail": user,
        }

    except Exception as exc:
        logger.exception(
            "Groww authentication test failed"
        )

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.get("/orders/{groww_order_id}")
async def order_diagnostics(
    groww_order_id: str,
) -> dict[str, Any]:
    try:
        status = await groww.get_order_status(
            groww_order_id=groww_order_id,
            segment=OPTION_SEGMENT,
        )

        detail = await groww.get_order_detail(
            groww_order_id=groww_order_id,
            segment=OPTION_SEGMENT,
        )

        return {
            "status": status,
            "detail": detail,
        }

    except Exception as exc:
        logger.exception(
            "Unable to inspect order %s",
            groww_order_id,
        )

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


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
            detail="Request body must be valid JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="JSON payload must be an object",
        )

    try:
        stocks = parse_chartink_payload(payload)

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

    metadata = {
        "triggered_at": payload.get("triggered_at"),
        "scan_name": payload.get("scan_name"),
        "scan_url": payload.get("scan_url"),
        "alert_name": payload.get("alert_name"),
        "webhook_url": payload.get("webhook_url"),
    }

    logger.info(
        "Chartink alert received | metadata=%s",
        metadata,
    )

    if is_force_exit_time():
        return {
            "status": "ignored",
            "reason": (
                "Alert received at or after force-exit time"
            ),
            "metadata": metadata,
        }

    ranked = await rank_stocks_by_today_change(
        stocks
    )

    if not ranked:
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not calculate today's percentage "
                "change for received stocks"
            ),
        )

    order_results: list[dict[str, Any]] = []

    for item in ranked:
        symbol = item["symbol"]

        try:
            trade = await enter_trade(
                underlying=symbol,
                option_type=OPTION_TYPE,
            )

            order_results.append(
                {
                    "status": "entered",
                    "underlying": symbol,
                    "option_symbol": trade.option_symbol,
                    "option_type": trade.option_type,
                    "expiry_date": trade.expiry_date,
                    "quantity": trade.quantity,
                    "entry_price": trade.entry_price,
                    "stop_price": trade.stop_price,
                    "target_price": trade.target_price,
                    "entry_order_id": (
                        trade.entry_order_id
                    ),
                }
            )

        except Exception as exc:
            logger.exception(
                "Could not enter trade for %s",
                symbol,
            )

            order_results.append(
                {
                    "status": "failed",
                    "underlying": symbol,
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
            for stock in stocks
        ],
        "top_stocks": ranked,
        "orders": order_results,
    }