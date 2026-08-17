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
# Configuration AMO
# ============================================================

AMO_ENABLED = (
    os.getenv("AMO_ENABLED", "true")
    .strip()
    .lower()
    == "true"
)

AMO_LIMIT_PRICE_MODE = os.getenv(
    "AMO_LIMIT_PRICE_MODE",
    "OPTION_LTP",
).strip().upper()

AMO_PRICE_BUFFER_PERCENT = float(
    os.getenv("AMO_PRICE_BUFFER_PERCENT", "0.5")
)

AMO_PRODUCT = os.getenv(
    "AMO_PRODUCT",
    "AMO_PRODUCT",
).strip().upper()

AMO_VALIDITY = os.getenv(
    "AMO_VALIDITY",
    "DAY",
).strip().upper()

AMO_MAX_STOCKS = min(
    int(os.getenv("AMO_MAX_STOCKS", "3")),
    3,
)
# ============================================================
# Configuration NRML
# ============================================================
load_dotenv()

GROWW_ACCESS_TOKEN = os.getenv(
    "GROWW_ACCESS_TOKEN",
    "",
).strip()

GROWW_CLIENT_ID = os.getenv(
    "GROWW_CLIENT_ID",
    "",
).strip()

if not GROWW_ACCESS_TOKEN:
    raise RuntimeError(
        "GROWW_ACCESS_TOKEN is required in .env"
    )

if not GROWW_CLIENT_ID:
    raise RuntimeError(
        "GROWW_CLIENT_ID is required in .env"
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

OPTION_TYPE = os.getenv(
    "OPTION_TYPE",
    "CE",
).strip().upper()

OPTION_EXPIRY_DATE = os.getenv(
    "OPTION_EXPIRY_DATE",
    "",
).strip()

OPTION_PRODUCT = os.getenv(
    "OPTION_PRODUCT",
    "NRML",
).strip().upper()

TOP_STOCK_COUNT = min(
    int(os.getenv("TOP_STOCK_COUNT", "3")),
    3,
)

TARGET_PERCENT = float(
    os.getenv("TARGET_PERCENT", "10")
)

STOP_LOSS_PERCENT = float(
    os.getenv("STOP_LOSS_PERCENT", "3")
)

TRAILING_STOP_PERCENT = float(
    os.getenv("TRAILING_STOP_PERCENT", "1")
)

FORCE_EXIT_TIME = time(
    int(os.getenv("FORCE_EXIT_HOUR", "15")),
    int(os.getenv("FORCE_EXIT_MINUTE", "15")),
)

MONITOR_INTERVAL_SECONDS = float(
    os.getenv("MONITOR_INTERVAL_SECONDS", "5")
)

REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("REQUEST_TIMEOUT_SECONDS", "20")
)

STATE_FILE = Path(
    os.getenv("STATE_FILE", "open_trades.json")
)

UNDERLYING_EXCHANGE = "NSE"
UNDERLYING_SEGMENT = "CASH"

OPTION_EXCHANGE = "NSE"
OPTION_SEGMENT = "FNO"


# ============================================================
# Logging and FastAPI
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Chartink Groww Options Bot",
    version="1.0.0",
)


# ============================================================
# Data models
# ============================================================

@dataclass
class ChartinkStock:
    symbol: str
    trigger_price: float


@dataclass
class Trade:
    trade_date: str
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
    exit_order_id: str | None = None
    exited: bool = False
    exit_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
    ) -> "Trade":
        return cls(**value)


# ============================================================
# Day and state management
# ============================================================

def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_trades() -> list[Trade]:
    if not STATE_FILE.exists():
        return []

    try:
        content = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(content, list):
            return []

        trades: list[Trade] = []

        for item in content:
            if isinstance(item, dict):
                try:
                    trades.append(Trade.from_dict(item))
                except TypeError:
                    logger.exception(
                        "Invalid trade state ignored: %s",
                        item,
                    )

        return trades

    except Exception:
        logger.exception(
            "Could not load trade state"
        )
        return []


open_trades: list[Trade] = load_trades()


def save_trades() -> None:
    temporary_path = STATE_FILE.with_suffix(".tmp")

    temporary_path.write_text(
        json.dumps(
            [
                trade.to_dict()
                for trade in open_trades
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary_path.replace(STATE_FILE)


def reset_old_day_state() -> None:
    """
    The daily restriction is based on trade_date. Historical records
    are retained, but only today's trades count as active.
    """
    current_day = today_key()

    for trade in open_trades:
        if trade.trade_date != current_day:
            trade.exited = True

    save_trades()


def todays_trades() -> list[Trade]:
    current_day = today_key()

    return [
        trade
        for trade in open_trades
        if trade.trade_date == current_day
    ]


def todays_active_trades() -> list[Trade]:
    return [
        trade
        for trade in todays_trades()
        if not trade.exited
    ]


def symbol_traded_today(underlying: str) -> bool:
    symbol = underlying.upper()

    return any(
        trade.underlying == symbol
        for trade in todays_trades()
    )


# ============================================================
# Helpers
# ============================================================

def normalize_symbol(value: Any) -> str:
    return str(value).strip().upper()


def first_value(
    data: dict[str, Any],
    keys: tuple[str, ...],
) -> Any:
    lowered = {
        str(key).lower(): value
        for key, value in data.items()
    }

    for key in keys:
        result = lowered.get(key.lower())

        if result is not None:
            return result

    return None


def unwrap_payload(value: Any) -> Any:
    """
    Supports both:
      {"status": "SUCCESS", "payload": {...}}
    and:
      {"status": "SUCCESS", "strikes": {...}}
    """
    if not isinstance(value, dict):
        return value

    payload = value.get("payload")

    if isinstance(payload, dict):
        return payload

    return value


def order_reference_id() -> str:
    value = (
        "GRW"
        + datetime.now().strftime("%H%M%S")
        + uuid.uuid4().hex[:5].upper()
    )

    return re.sub(
        r"[^A-Za-z0-9-]",
        "",
        value,
    )[:20]

from datetime import date


def is_valid_future_expiry(
    value: str,
) -> bool:
    try:
        expiry = datetime.strptime(
            value,
            "%Y-%m-%d",
        ).date()

        return expiry >= date.today()

    except ValueError:
        return False


async def choose_expiry(
    underlying: str,
) -> str:
    configured_expiry = OPTION_EXPIRY_DATE.strip()

    if configured_expiry:
        available = (
            await groww.get_available_expiries(
                underlying=underlying
            )
        )

        if configured_expiry in available:
            return configured_expiry

        logger.warning(
            "Configured expiry %s unavailable for %s. "
            "Available expiries: %s",
            configured_expiry,
            underlying,
            available,
        )

    available = (
        await groww.get_available_expiries(
            underlying=underlying
        )
    )

    future_expiries = [
        expiry
        for expiry in available
        if is_valid_future_expiry(expiry)
    ]

    if not future_expiries:
        raise RuntimeError(
            f"No future F&O expiry available for "
            f"{underlying}"
        )

    selected = sorted(future_expiries)[0]

    logger.info(
        "Selected expiry | underlying=%s | expiry=%s",
        underlying,
        selected,
    )

    return selected


def current_time() -> time:
    return datetime.now().time()


def force_exit_reached() -> bool:
    return current_time() >= FORCE_EXIT_TIME


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
        Groww documents Bearer-token authentication and the
        X-API-VERSION header.

        X-Client-Id is included because the application requires
        GROWW_CLIENT_ID. If your Groww account rejects this optional
        header, remove only X-Client-Id here.
        """
        return {
            "Authorization": (
                f"Bearer {self.access_token}"
            ),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-VERSION": GROWW_API_VERSION,
            "X-Client-Id": self.client_id,
        }

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Any = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = (
            f"{GROWW_API_BASE_URL}/"
            f"{endpoint.lstrip('/')}"
        )

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=self.headers(),
                params=params,
                json=body,
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
            logger.error(
                "Groww HTTP error | code=%s | body=%s",
                response.status_code,
                data,
            )

            raise RuntimeError(
                f"Groww HTTP {response.status_code}: "
                f"{data}"
            )

        if str(data.get("status", "")).upper() == "FAILURE":
            raise RuntimeError(
                f"Groww API failure: {data}"
            )

        return data

    async def get_user_detail(self) -> dict[str, Any]:
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
        symbol: str,
    ) -> dict[str, Any]:
        data = await self.request(
            "GET",
            "/live-data/quote",
            params={
                "exchange": UNDERLYING_EXCHANGE,
                "segment": UNDERLYING_SEGMENT,
                "trading_symbol": symbol,
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid quote response for {symbol}: {data}"
            )

        return payload

    async def get_ltp(
        self,
        symbol: str,
        segment: str,
    ) -> float:
        params = [
            ("segment", segment),
            (
                "exchange_symbols",
                f"{OPTION_EXCHANGE}_{symbol}",
            ),
        ]

        data = await self.request(
            "GET",
            "/live-data/ltp",
            params=params,
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
                value = float(direct_ltp)

                if value > 0:
                    return value

            nested = (
                payload.get(
                    f"{OPTION_EXCHANGE}_{symbol}"
                )
                or payload.get(symbol)
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
                    value = float(nested_ltp)

                    if value > 0:
                        return value

        raise RuntimeError(
            f"Could not extract LTP for {symbol}: {data}"
        )

    async def get_option_chain(
        self,
        underlying: str,
        expiry_date: str,
    ) -> dict[str, Any]:
        data = await self.request(
            "GET",
            (
                "/option-chain/exchange/"
                f"{OPTION_EXCHANGE}/underlying/"
                f"{underlying}"
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
        body: dict[str, Any],
    ) -> dict[str, Any]:
        data = await self.request(
            "POST",
            "/order/create",
            body=body,
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid order response: {data}"
            )

        return payload

    async def order_status(
        self,
        order_id: str,
        segment: str,
    ) -> dict[str, Any]:
        data = await self.request(
            "GET",
            f"/order/status/{order_id}",
            params={
                "segment": segment,
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid order status response: {data}"
            )

        return payload

    async def order_detail(
        self,
        order_id: str,
        segment: str,
    ) -> dict[str, Any]:
        data = await self.request(
            "GET",
            f"/order/detail/{order_id}",
            params={
                "segment": segment,
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid order detail response: {data}"
            )

        return payload

    async def trades_for_order(
        self,
        order_id: str,
        segment: str,
    ) -> list[dict[str, Any]]:
        data = await self.request(
            "GET",
            f"/order/trades/{order_id}",
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
            f"Invalid trades response: {data}"
        )

    async def order_list(
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
# Chartink payload
# ============================================================

def parse_chartink_payload(
    payload: dict[str, Any],
) -> list[ChartinkStock]:
    if "stocks" not in payload:
        raise ValueError(
            "Missing required field: stocks"
        )

    if "trigger_prices" not in payload:
        raise ValueError(
            "Missing required field: trigger_prices"
        )

    raw_stocks = payload["stocks"]
    raw_prices = payload["trigger_prices"]

    if not isinstance(raw_stocks, str):
        raise ValueError(
            "stocks must be a comma-separated string"
        )

    if not isinstance(raw_prices, str):
        raise ValueError(
            "trigger_prices must be a comma-separated string"
        )

    symbols = [
        normalize_symbol(value)
        for value in raw_stocks.split(",")
        if value.strip()
    ]

    prices = [
        value.strip()
        for value in raw_prices.split(",")
        if value.strip()
    ]

    if len(symbols) != len(prices):
        raise ValueError(
            "stocks and trigger_prices must contain "
            "the same number of values"
        )

    result: list[ChartinkStock] = []
    seen_symbols: set[str] = set()

    for symbol, raw_price in zip(
        symbols,
        prices,
    ):
        if symbol in seen_symbols:
            logger.info(
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

        result.append(
            ChartinkStock(
                symbol=symbol,
                trigger_price=trigger_price,
            )
        )

        seen_symbols.add(symbol)

    return result


# ============================================================
# Ranking
# ============================================================

async def get_today_change_percent(
    symbol: str,
) -> float:
    quote = await groww.get_quote(
        symbol=symbol
    )

    day_change = first_value(
        quote,
        (
            "day_change_perc",
            "day_change_percentage",
            "change_percent",
        ),
    )

    if day_change is not None:
        return float(day_change)

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
            f"Quote lacks required price fields: {quote}"
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


async def rank_stocks(
    stocks: list[ChartinkStock],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []

    for stock in stocks:
        try:
            percent_change = (
                await get_today_change_percent(
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
                "Rank input | symbol=%s | "
                "trigger=%.2f | change=%.2f%%",
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
# ATM option selection
# ============================================================

def parse_chain_strikes(
    strikes: Any,
) -> list[tuple[float, dict[str, Any]]]:
    rows: list[
        tuple[float, dict[str, Any]]
    ] = []

    if isinstance(strikes, dict):
        for raw_strike, strike_data in strikes.items():
            try:
                strike_price = float(raw_strike)

            except (TypeError, ValueError):
                continue

            if isinstance(strike_data, dict):
                rows.append(
                    (strike_price, strike_data)
                )

        return rows

    if isinstance(strikes, list):
        for item in strikes:
            if not isinstance(item, dict):
                continue

            raw_strike = (
                item.get("strike_price")
                or item.get("strike")
                or item.get("strikePrice")
            )

            try:
                strike_price = float(raw_strike)

            except (TypeError, ValueError):
                continue

            rows.append(
                (strike_price, item)
            )

        return rows

    return rows


async def select_atm_option(
    underlying: str,
) -> tuple[str, float, str]:
    if OPTION_TYPE not in {"CE", "PE"}:
        raise RuntimeError(
            "OPTION_TYPE must be CE or PE"
        )

    expiry_date = await choose_expiry(
        underlying=underlying
    )

    chain = await groww.get_option_chain(
        underlying=underlying,
        expiry_date=expiry_date,
    )

    underlying_ltp_value = first_value(
        chain,
        (
            "underlying_ltp",
            "underlying_last_price",
        ),
    )

    if underlying_ltp_value is None:
        underlying_ltp = await groww.get_ltp(
            symbol=underlying,
            segment=UNDERLYING_SEGMENT,
        )

    else:
        underlying_ltp = float(
            underlying_ltp_value
        )

    strikes = chain.get("strikes")

    if not isinstance(strikes, dict) or not strikes:
        raise RuntimeError(
            f"No option contracts found for "
            f"{underlying} expiry {expiry_date}"
        )

    valid_strikes: list[
        tuple[float, dict[str, Any]]
    ] = []

    for raw_strike, strike_data in strikes.items():
        try:
            strike_price = float(raw_strike)

        except (TypeError, ValueError):
            continue

        if isinstance(strike_data, dict):
            valid_strikes.append(
                (strike_price, strike_data)
            )

    if not valid_strikes:
        raise RuntimeError(
            f"No valid strikes found for "
            f"{underlying} expiry {expiry_date}"
        )

    selected_strike, selected_data = min(
        valid_strikes,
        key=lambda item: abs(
            item[0] - underlying_ltp
        ),
    )

    contract = None

    for key, value in selected_data.items():
        if str(key).upper() == OPTION_TYPE:
            if isinstance(value, dict):
                contract = value

            break

    if not contract:
        raise RuntimeError(
            f"{OPTION_TYPE} contract unavailable for "
            f"{underlying} strike {selected_strike}"
        )

    option_symbol = (
        contract.get("trading_symbol")
        or contract.get("symbol")
    )

    option_ltp = (
        contract.get("ltp")
        or contract.get("last_price")
        or contract.get("price")
    )

    if not option_symbol:
        raise RuntimeError(
            f"Option symbol missing: {contract}"
        )

    if option_ltp is None or float(option_ltp) <= 0:
        raise RuntimeError(
            f"Invalid option LTP: {contract}"
        )

    return (
        str(option_symbol),
        float(option_ltp),
        expiry_date,
    )


# ============================================================
# Lot size
# ============================================================

async def get_option_lot_size(
    option_symbol: str,
) -> int:
    """
    Downloads the Groww instruments CSV URL and finds the option
    by trading_symbol.

    Configure INSTRUMENTS_CSV_URL with the current URL from Groww's
    Instruments documentation if the default URL changes.
    """
    instruments_url = os.getenv(
        "INSTRUMENTS_CSV_URL"
    )

    if not instruments_url:
        raise RuntimeError(
            "INSTRUMENTS_CSV_URL is required to obtain "
            "the option lot size."
        )

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        response = await client.get(
            instruments_url
        )

    response.raise_for_status()

    lines = response.text.splitlines()

    if not lines:
        raise RuntimeError(
            "Instrument CSV is empty"
        )

    headers = [
        item.strip()
        for item in lines[0].split(",")
    ]

    symbol_index = None
    lot_size_index = None
    segment_index = None

    for index, name in enumerate(headers):
        lowered = name.lower()

        if lowered == "trading_symbol":
            symbol_index = index

        elif lowered == "lot_size":
            lot_size_index = index

        elif lowered == "segment":
            segment_index = index

    if symbol_index is None:
        raise RuntimeError(
            "trading_symbol column missing from instruments CSV"
        )

    if lot_size_index is None:
        raise RuntimeError(
            "lot_size column missing from instruments CSV"
        )

    import csv
    from io import StringIO

    reader = csv.DictReader(
        StringIO(response.text)
    )

    for row in reader:
        if (
            row.get("trading_symbol")
            != option_symbol
        ):
            continue

        if (
            segment_index is not None
            and row.get("segment") not in {
                None,
                "",
                OPTION_SEGMENT,
            }
        ):
            continue

        raw_lot_size = row.get("lot_size")

        if not raw_lot_size:
            break

        lot_size = int(float(raw_lot_size))

        if lot_size <= 0:
            break

        return lot_size

    raise RuntimeError(
        f"Lot size not found for {option_symbol}"
    )


# ============================================================
# Orders
# ============================================================

async def place_market_order(
    trading_symbol: str,
    quantity: int,
    transaction_type: str,
) -> dict[str, Any]:
    if transaction_type not in {"BUY", "SELL"}:
        raise ValueError(
            "transaction_type must be BUY or SELL"
        )

    body = {
        "trading_symbol": trading_symbol,
        "quantity": quantity,
        "price": 0,
        "trigger_price": 0,
        "validity": "DAY",
        "exchange": OPTION_EXCHANGE,
        "segment": OPTION_SEGMENT,
        "product": OPTION_PRODUCT,
        "order_type": "MARKET",
        "transaction_type": transaction_type,
        "order_reference_id": (
            order_reference_id()
        ),
    }

    logger.info(
        "Order request | symbol=%s | quantity=%d | "
        "transaction=%s | live=%s",
        trading_symbol,
        quantity,
        transaction_type,
        LIVE_TRADING(),
    )

    if not LIVE_TRADING():
        logger.warning(
            "DRY RUN: order not submitted | %s",
            body,
        )

        return {
            "status": "DRY_RUN",
            "groww_order_id": "DRY_RUN",
            "order_status": "DRY_RUN",
            "remark": "LIVE_TRADING=false",
            **body,
        }

    response = await groww.place_order(body)

    status = str(
        response.get("order_status", "")
    ).upper()

    if status in {
        "REJECTED",
        "FAILED",
        "CANCELLED",
    }:
        raise RuntimeError(
            f"Groww order failed: "
            f"{response.get('remark', response)}"
        )

    return response


async def wait_for_execution_price(
    order_id: str,
    option_symbol: str,
) -> float:
    if not LIVE_TRADING() or order_id == "DRY_RUN":
        return await groww.get_ltp(
            symbol=option_symbol,
            segment=OPTION_SEGMENT,
        )

    for attempt in range(1, 7):
        try:
            trades = (
                await groww.trades_for_order(
                    order_id=order_id,
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
                "Execution lookup failed | attempt=%d | "
                "order_id=%s",
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
# Entry and exit
# ============================================================

async def enter_trade(
    stock: ChartinkStock,
) -> Trade:
    if len(todays_active_trades()) >= 3:
        raise RuntimeError(
            "Three active trades already exist today"
        )

    if symbol_traded_today(stock.symbol):
        raise RuntimeError(
            f"{stock.symbol} already traded today"
        )

    option_symbol, _, expiry_date = (
        await select_atm_option(
            underlying=stock.symbol
        )
    )

    quantity = await get_option_lot_size(
        option_symbol=option_symbol
    )

    order_response = await place_market_order(
        trading_symbol=option_symbol,
        quantity=quantity,
        transaction_type="BUY",
    )

    order_id = str(
        order_response.get(
            "groww_order_id",
            "DRY_RUN",
        )
    )

    entry_price = (
        await wait_for_execution_price(
            order_id=order_id,
            option_symbol=option_symbol,
        )
    )

    stop_price = entry_price * (
        1 - STOP_LOSS_PERCENT / 100
    )

    target_price = entry_price * (
        1 + TARGET_PERCENT / 100
    )

    trade = Trade(
        trade_date=today_key(),
        underlying=stock.symbol,
        option_symbol=option_symbol,
        option_type=OPTION_TYPE,
        expiry_date=expiry_date,
        quantity=quantity,
        entry_price=entry_price,
        highest_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        entry_order_id=order_id,
    )

    open_trades.append(trade)
    save_trades()

    logger.info(
        "ENTRY | stock=%s | option=%s | quantity=%d | "
        "entry=%.2f | stop=%.2f | target=%.2f",
        stock.symbol,
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
        response = await place_market_order(
            trading_symbol=trade.option_symbol,
            quantity=trade.quantity,
            transaction_type="SELL",
        )

        trade.exit_order_id = response.get(
            "groww_order_id"
        )

        trade.exited = True
        trade.exit_reason = reason

        save_trades()

        logger.info(
            "EXIT | stock=%s | option=%s | reason=%s",
            trade.underlying,
            trade.option_symbol,
            reason,
        )

    except Exception:
        logger.exception(
            "Exit order failed | stock=%s | option=%s",
            trade.underlying,
            trade.option_symbol,
        )


async def monitor_trade(
    trade: Trade,
) -> None:
    if trade.exited:
        return

    if force_exit_reached():
        await exit_trade(
            trade=trade,
            reason="FORCED_EXIT_15_15",
        )
        return

    current_ltp = await groww.get_ltp(
        symbol=trade.option_symbol,
        segment=OPTION_SEGMENT,
    )

    # Update high-water mark and move the stop only upward.
    if current_ltp > trade.highest_price:
        trade.highest_price = current_ltp

        trailing_stop = (
            trade.highest_price
            * (1 - TRAILING_STOP_PERCENT / 100)
        )

        trade.stop_price = max(
            trade.stop_price,
            trailing_stop,
        )

        save_trades()

        logger.info(
            "TRAILING STOP | option=%s | ltp=%.2f | "
            "highest=%.2f | stop=%.2f",
            trade.option_symbol,
            current_ltp,
            trade.highest_price,
            trade.stop_price,
        )

    if current_ltp >= trade.target_price:
        await exit_trade(
            trade=trade,
            reason="TARGET_10_PERCENT",
        )
        return

    if current_ltp <= trade.stop_price:
        await exit_trade(
            trade=trade,
            reason="STOP_LOSS_3_PERCENT",
        )


# ============================================================
# Background monitor
# ============================================================

async def monitor_loop() -> None:
    logger.info(
        "Monitor started | live_trading=%s",
        LIVE_TRADING,
    )

    while True:
        try:
            reset_old_day_state()

            for trade in list(todays_active_trades()):
                try:
                    await monitor_trade(trade)

                except Exception:
                    logger.exception(
                        "Could not monitor %s",
                        trade.option_symbol,
                    )

        except Exception:
            logger.exception(
                "Unexpected monitor-loop error"
            )

        await asyncio.sleep(
            MONITOR_INTERVAL_SECONDS
        )


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(
        monitor_loop()
    )


# ============================================================
# Endpoints
# ============================================================

async def place_amo_limit_order(
    trading_symbol: str,
    quantity: int,
    limit_price: float,
) -> dict[str, Any]:
    """
    Submit an AMO limit order.

    This function intentionally uses a LIMIT order. The order is
    submitted after market hours, and Groww determines its AMO
    processing status.
    """

    if not AMO_ENABLED:
        return {
            "status": "DRY_RUN",
            "order_status": "DRY_RUN",
            "groww_order_id": "DRY_RUN",
            "remark": (
                "AMO_ENABLED=true; order not submitted"
            ),
        }

    if quantity <= 0:
        raise ValueError(
            "AMO quantity must be greater than zero"
        )

    if limit_price <= 0:
        raise ValueError(
            "AMO limit price must be greater than zero"
        )

    body = {
        "trading_symbol": trading_symbol,
        "quantity": quantity,
        "price": round(limit_price, 2),
        "trigger_price": 0,
        "validity": AMO_VALIDITY,
        "exchange": OPTION_EXCHANGE,
        "segment": OPTION_SEGMENT,
        "product": AMO_PRODUCT,
        "order_type": "LIMIT",
        "transaction_type": "BUY",
        "order_reference_id": (
            order_reference_id()
        ),
    }

    logger.info(
        "AMO request | symbol=%s | quantity=%d | "
        "price=%.2f | product=%s",
        trading_symbol,
        quantity,
        limit_price,
        AMO_PRODUCT,
    )

    if not LIVE_TRADING():
        logger.warning(
            "DRY RUN: AMO order was not submitted | %s",
            body,
        )

        return {
            "status": "DRY_RUN",
            "groww_order_id": "DRY_RUN",
            "order_status": "DRY_RUN",
            "amo_status": "DRY_RUN",
            "remark": "LIVE_TRADING=false",
            **body,
        }

    response = await groww.place_order(body)

    logger.info(
        "AMO response | order_id=%s | order_status=%s | "
        "amo_status=%s | remark=%s",
        response.get("groww_order_id"),
        response.get("order_status"),
        response.get("amo_status"),
        response.get("remark"),
    )

    order_status = str(
        response.get("order_status", "")
    ).upper()

    amo_status = str(
        response.get("amo_status", "")
    ).upper()

    failed_statuses = {
        "REJECTED",
        "FAILED",
        "CANCELLED",
    }

    if (
        order_status in failed_statuses
        or amo_status in failed_statuses
    ):
        raise RuntimeError(
            "Groww AMO order failed: "
            f"{response.get('remark', response)}"
        )

    return response

def calculate_amo_limit_price(
    option_ltp: float,
) -> float:
    if option_ltp <= 0:
        raise ValueError(
            "Option LTP must be positive"
        )

    if AMO_LIMIT_PRICE_MODE == "OPTION_LTP":
        return round(option_ltp, 2)

    if AMO_LIMIT_PRICE_MODE == "BUFFERED":
        return round(
            option_ltp
            * (
                1
                + AMO_PRICE_BUFFER_PERCENT / 100
            ),
            2,
        )

    raise ValueError(
        "AMO_LIMIT_PRICE_MODE must be "
        "OPTION_LTP or BUFFERED"
    )

async def enter_amo_trade(
    stock: ChartinkStock,
) -> dict[str, Any]:
    if len(todays_active_trades()) >= 3:
        raise RuntimeError(
            "Three active trades already exist today"
        )

    if symbol_traded_today(stock.symbol):
        raise RuntimeError(
            f"{stock.symbol} already traded today"
        )

    option_symbol, option_ltp = (
        await select_atm_option(
            underlying=stock.symbol
        )
    )

    quantity = await get_option_lot_size(
        option_symbol=option_symbol
    )

    amo_limit_price = calculate_amo_limit_price(
        option_ltp=option_ltp
    )

    order_response = await place_amo_limit_order(
        trading_symbol=option_symbol,
        quantity=quantity,
        limit_price=amo_limit_price,
    )

    order_id = order_response.get(
        "groww_order_id"
    )

    if not order_id:
        raise RuntimeError(
            "AMO response did not include groww_order_id"
        )

    logger.info(
        "AMO submitted | underlying=%s | option=%s | "
        "quantity=%d | limit_price=%.2f | "
        "order_id=%s | amo_status=%s",
        stock.symbol,
        option_symbol,
        quantity,
        amo_limit_price,
        order_id,
        order_response.get("amo_status"),
    )

    return {
        "underlying": stock.symbol,
        "option_symbol": option_symbol,
        "option_type": OPTION_TYPE,
        "expiry_date": get_expiry_date(),
        "quantity": quantity,
        "option_ltp": option_ltp,
        "amo_limit_price": amo_limit_price,
        "groww_order_id": order_id,
        "order_status": order_response.get(
            "order_status"
        ),
        "amo_status": order_response.get(
            "amo_status"
        ),
        "remark": order_response.get(
            "remark"
        ),
    }

@app.post("/chartink/amo-webhook")
async def chartink_amo_webhook(
    request: Request,
) -> dict[str, Any]:
    """
    Accepts the same Chartink payload as /chartink/webhook.

    This endpoint is intended for after-market submission.
    It does not create an active Trade object because an AMO
    may not be executed yet.
    """

    if not AMO_ENABLED:
        return {
            "status": "disabled",
            "reason": (
                "Set AMO_ENABLED=true to enable AMO submission"
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
            detail="Request JSON must be an object",
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
        "Chartink AMO alert received | metadata=%s",
        metadata,
    )

    # Do not submit AMO from both endpoints accidentally.
    # AMO endpoint is intended for a separate Chartink alert/webhook.
    ranked_stocks = await rank_stocks(stocks)

    if not ranked_stocks:
        raise HTTPException(
            status_code=422,
            detail=(
                "Unable to calculate today's change "
                "for received stocks"
            ),
        )

    results: list[dict[str, Any]] = []

    for ranked_stock in ranked_stocks[:AMO_MAX_STOCKS]:
        symbol = ranked_stock["symbol"]

        if symbol_traded_today(symbol):
            results.append(
                {
                    "status": "skipped",
                    "symbol": symbol,
                    "reason": (
                        "Symbol already has a trade "
                        "record for today"
                    ),
                }
            )
            continue

        try:
            stock = ChartinkStock(
                symbol=symbol,
                trigger_price=(
                    ranked_stock["trigger_price"]
                ),
            )

            amo_result = await enter_amo_trade(
                stock=stock
            )

            results.append(
                {
                    "status": "amo_submitted",
                    "rank_data": ranked_stock,
                    **amo_result,
                }
            )

        except Exception as exc:
            logger.exception(
                "Could not submit AMO for %s",
                symbol,
            )

            results.append(
                {
                    "status": "failed",
                    "symbol": symbol,
                    "error": str(exc),
                }
            )

    return {
        "status": "accepted",
        "mode": "AMO",
        "live_trading": LIVE_TRADING,
        "amo_enabled": AMO_ENABLED,
        "metadata": metadata,
        "ranked_stocks": ranked_stocks,
        "orders": results,
    }


# ============================================================
# Endpoints
# ============================================================

@app.get("/")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "chartink-groww-options-bot",
        "live_trading": LIVE_TRADING,
        "today": today_key(),
        "active_trade_count": len(
            todays_active_trades()
        ),
        "active_trades": [
            trade.to_dict()
            for trade in todays_active_trades()
        ],
    }


@app.get("/debug/auth")
async def debug_auth() -> dict[str, Any]:
    try:
        user_detail = await groww.get_user_detail()

        return {
            "status": "ok",
            "user_detail": user_detail,
        }

    except Exception as exc:
        logger.exception(
            "Groww authentication test failed"
        )

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.get("/orders/{order_id}")
async def order_diagnostics(
    order_id: str,
) -> dict[str, Any]:
    try:
        status = await groww.order_status(
            order_id=order_id,
            segment=OPTION_SEGMENT,
        )

        detail = await groww.order_detail(
            order_id=order_id,
            segment=OPTION_SEGMENT,
        )

        return {
            "status": status,
            "detail": detail,
        }

    except Exception as exc:
        logger.exception(
            "Unable to inspect order %s",
            order_id,
        )

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


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
            detail="Request JSON must be an object",
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

    if force_exit_reached():
        return {
            "status": "ignored",
            "reason": (
                "Webhook received at or after 3:15 PM"
            ),
            "metadata": metadata,
        }

    active_count = len(
        todays_active_trades()
    )

    if active_count >= 3:
        return {
            "status": "ignored",
            "reason": (
                "Three active trades already exist today"
            ),
            "metadata": metadata,
        }

    ranked = await rank_stocks(stocks)

    if not ranked:
        raise HTTPException(
            status_code=422,
            detail=(
                "Unable to calculate today's percentage "
                "change for any stock"
            ),
        )

    available_slots = 3 - len(
        todays_active_trades()
    )

    results: list[dict[str, Any]] = []

    for ranked_stock in ranked:
        if len(results) >= available_slots:
            break

        symbol = ranked_stock["symbol"]

        if symbol_traded_today(symbol):
            results.append(
                {
                    "status": "skipped",
                    "symbol": symbol,
                    "reason": (
                        "Symbol already traded today"
                    ),
                }
            )
            continue

        try:
            stock = ChartinkStock(
                symbol=symbol,
                trigger_price=(
                    ranked_stock["trigger_price"]
                ),
            )

            trade = await enter_trade(stock)

            results.append(
                {
                    "status": "entered",
                    "underlying": trade.underlying,
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

            results.append(
                {
                    "status": "failed",
                    "symbol": symbol,
                    "error": str(exc),
                }
            )

    return {
        "status": "accepted",
        "live_trading": LIVE_TRADING,
        "metadata": metadata,
        "received_stocks": [
            {
                "symbol": stock.symbol,
                "trigger_price": stock.trigger_price,
            }
            for stock in stocks
        ],
        "ranked_stocks": ranked,
        "active_trade_count": len(
            todays_active_trades()
        ),
        "orders": results,
    }