from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

import httpx

from config import IST, Settings


logger = logging.getLogger(__name__)


def unwrap_payload(value: Any) -> Any:
    """
    Return the payload from a standard Groww response.

    Example:
        {
            "status": "SUCCESS",
            "payload": {...},
        }

    becomes:
        {...}
    """
    if not isinstance(value, dict):
        return value

    if "payload" in value:
        return value["payload"]

    return value


def first_value(
    data: dict[str, Any],
    keys: tuple[str, ...],
) -> Any:
    """
    Return the first non-None value matching the supplied
    keys, case-insensitively.
    """
    lowered = {
        str(key).lower(): value
        for key, value in data.items()
    }

    for key in keys:
        value = lowered.get(
            key.lower()
        )

        if value is not None:
            return value

    return None


def make_reference_id() -> str:
    """
    Generate a short unique order reference ID.

    The ID is limited to 20 characters.
    """
    value = (
        "GRW"
        + datetime.now(IST).strftime(
            "%H%M%S"
        )
        + uuid.uuid4().hex[:8].upper()
    )

    return value[:20]


class GrowwClient:
    def __init__(
        self,
        settings: Settings,
    ) -> None:
        self.settings = settings

    # ========================================================
    # Common helpers
    # ========================================================

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": (
                "Bearer "
                f"{self.settings.groww_access_token}"
            ),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-VERSION": (
                self.settings.groww_api_version
            ),
            "X-Client-Id": (
                self.settings.groww_client_id
            ),
        }

    @staticmethod
    def normalize_segment(
        segment: str,
    ) -> str:
        normalized = segment.strip().upper()

        if normalized not in {
            "CASH",
            "FNO",
            "COMMODITY",
        }:
            raise ValueError(
                "segment must be CASH, FNO, "
                "or COMMODITY"
            )

        return normalized

    @staticmethod
    def normalize_exchange(
        exchange: str,
    ) -> str:
        normalized = exchange.strip().upper()

        if not normalized:
            raise ValueError(
                "exchange is required"
            )

        return normalized

    @staticmethod
    def normalize_symbol(
        symbol: str,
    ) -> str:
        normalized = symbol.strip().upper()

        if not normalized:
            raise ValueError(
                "symbol is required"
            )

        return normalized

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Any = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Make an authenticated request to Groww.

        The API base URL is configured through
        GROWW_API_BASE_URL.
        """
        url = (
            f"{self.settings.groww_api_base_url}/"
            f"{endpoint.lstrip('/')}"
        )

        logger.debug(
            "Groww request | method=%s | endpoint=%s "
            "| params=%s",
            method,
            endpoint,
            params,
        )

        try:
            async with httpx.AsyncClient(
                timeout=(
                    self.settings
                    .request_timeout_seconds
                ),
            ) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=self.headers(),
                    params=params,
                    json=body,
                )

        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Groww HTTP request failed: {exc}"
            ) from exc

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

        api_status = str(
            data.get(
                "status",
                "",
            )
        ).upper()

        if api_status == "FAILURE":
            raise RuntimeError(
                f"Groww API failure: {data}"
            )

        return data

    # ========================================================
    # LTP and market data
    # ========================================================

    @staticmethod
    def _extract_ltp(
        *,
        response: dict[str, Any],
        exchange_symbol: str,
    ) -> float:
        """
        Extract LTP from Groww's response.

        Primary response shape:

            {
                "status": "SUCCESS",
                "payload": {
                    "NSE_TCS": 2286.5
                }
            }
        """
        if not isinstance(response, dict):
            raise RuntimeError(
                "Groww LTP response must be an object: "
                f"{response!r}"
            )

        status = str(
            response.get(
                "status",
                "",
            )
        ).upper()

        if status not in {
            "",
            "SUCCESS",
        }:
            raise RuntimeError(
                "Groww LTP request failed: "
                f"{response!r}"
            )

        payload = response.get(
            "payload",
            {},
        )

        if isinstance(payload, dict):
            wanted_key = (
                exchange_symbol.strip().upper()
            )

            for key, raw_value in (
                payload.items()
            ):
                if (
                    str(key).strip().upper()
                    != wanted_key
                ):
                    continue

                try:
                    ltp = float(raw_value)

                except (
                    TypeError,
                    ValueError,
                ) as exc:
                    raise RuntimeError(
                        "Groww returned a non-numeric "
                        f"LTP for {wanted_key}: "
                        f"{raw_value!r}"
                    ) from exc

                if ltp <= 0:
                    raise RuntimeError(
                        "Groww returned an invalid "
                        f"LTP for {wanted_key}: "
                        f"{ltp}"
                    )

                return ltp

        # Tolerate alternate response formats.
        candidates: list[Any] = [
            response.get("ltp"),
            response.get("last_price"),
            response.get(
                "last_traded_price"
            ),
        ]

        if isinstance(payload, dict):
            candidates.extend(
                [
                    payload.get("ltp"),
                    payload.get("last_price"),
                    payload.get(
                        "last_traded_price"
                    ),
                ]
            )

        for candidate in candidates:
            try:
                ltp = float(candidate)

            except (
                TypeError,
                ValueError,
            ):
                continue

            if ltp > 0:
                return ltp

        raise RuntimeError(
            "Could not extract LTP for "
            f"{exchange_symbol}: {response!r}"
        )

    async def get_ltp(
        self,
        *,
        symbol: str,
        segment: str,
        exchange: str,
    ) -> float:
        normalized_symbol = (
            self.normalize_symbol(symbol)
        )

        normalized_segment = (
            self.normalize_segment(segment)
        )

        normalized_exchange = (
            self.normalize_exchange(exchange)
        )

        exchange_symbol = (
            f"{normalized_exchange}_"
            f"{normalized_symbol}"
        )

        response = await self.request(
            "GET",
            "/live-data/ltp",
            params={
                "segment": normalized_segment,
                "exchange_symbols": [
                    exchange_symbol,
                ],
            },
        )

        return self._extract_ltp(
            response=response,
            exchange_symbol=exchange_symbol,
        )

    async def get_quote(
        self,
        symbol: str,
    ) -> dict[str, Any]:
        normalized_symbol = (
            self.normalize_symbol(symbol)
        )

        data = await self.request(
            "GET",
            "/live-data/quote",
            params={
                "exchange": (
                    self.settings
                    .underlying_exchange
                ),
                "segment": (
                    self.settings
                    .underlying_segment
                ),
                "trading_symbol": (
                    normalized_symbol
                ),
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid quote response: {data}"
            )

        return payload

    # ========================================================
    # User and account
    # ========================================================

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
                f"Invalid user response: {data}"
            )

        return payload

    # ========================================================
    # Instruments and derivatives
    # ========================================================

    async def get_expiries(
        self,
        underlying: str,
    ) -> list[str]:
        normalized_underlying = (
            self.normalize_symbol(underlying)
        )

        data = await self.request(
            "GET",
            "/historical/expiries",
            params={
                "exchange": (
                    self.settings
                    .option_exchange
                ),
                "underlying_symbol": (
                    normalized_underlying
                ),
                "year": datetime.now(
                    IST
                ).year,
            },
        )

        payload = unwrap_payload(data)

        if isinstance(payload, list):
            values = payload

        elif isinstance(payload, dict):
            values = (
                payload.get("expiries")
                or payload.get(
                    "expiry_dates"
                )
                or payload.get("data")
                or []
            )

        else:
            values = []

        return sorted(
            {
                str(item).strip()
                for item in values
                if item
            }
        )

    async def get_option_chain(
        self,
        underlying: str,
        expiry_date: str,
    ) -> dict[str, Any]:
        normalized_underlying = (
            self.normalize_symbol(underlying)
        )

        normalized_expiry = (
            expiry_date.strip()
        )

        if not normalized_expiry:
            raise ValueError(
                "expiry_date is required"
            )

        data = await self.request(
            "GET",
            (
                "/option-chain/exchange/"
                f"{self.settings.option_exchange}"
                "/underlying/"
                f"{normalized_underlying}"
            ),
            params={
                "expiry_date": (
                    normalized_expiry
                ),
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Invalid option chain response: "
                f"{data}"
            )

        return payload

    # ========================================================
    # Orders
    # ========================================================

    async def create_order(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        logger.info(
            "Creating Groww order | "
            "symbol=%s | quantity=%s | "
            "exchange=%s | segment=%s | "
            "product=%s | order_type=%s | "
            "transaction_type=%s",
            body.get("trading_symbol"),
            body.get("quantity"),
            body.get("exchange"),
            body.get("segment"),
            body.get("product"),
            body.get("order_type"),
            body.get("transaction_type"),
        )

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

        logger.info(
            "Groww order response | "
            "order_id=%s | order_status=%s",
            payload.get(
                "groww_order_id"
            ),
            payload.get(
                "order_status"
            ),
        )

        return payload

    async def get_order_status(
        self,
        order_id: str,
        segment: str,
    ) -> dict[str, Any]:
        normalized_order_id = (
            order_id.strip()
        )

        normalized_segment = (
            self.normalize_segment(segment)
        )

        if not normalized_order_id:
            raise ValueError(
                "order_id is required"
            )

        data = await self.request(
            "GET",
            (
                "/order/status/"
                f"{normalized_order_id}"
            ),
            params={
                "segment": (
                    normalized_segment
                ),
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid status response: {data}"
            )

        return payload

    async def get_order_detail(
        self,
        order_id: str,
        segment: str,
    ) -> dict[str, Any]:
        normalized_order_id = (
            order_id.strip()
        )

        normalized_segment = (
            self.normalize_segment(segment)
        )

        if not normalized_order_id:
            raise ValueError(
                "order_id is required"
            )

        data = await self.request(
            "GET",
            (
                "/order/detail/"
                f"{normalized_order_id}"
            ),
            params={
                "segment": (
                    normalized_segment
                ),
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid detail response: {data}"
            )

        return payload

    async def get_order_trades(
        self,
        order_id: str,
        segment: str,
    ) -> list[dict[str, Any]]:
        normalized_order_id = (
            order_id.strip()
        )

        normalized_segment = (
            self.normalize_segment(segment)
        )

        if not normalized_order_id:
            raise ValueError(
                "order_id is required"
            )

        data = await self.request(
            "GET",
            (
                "/order/trades/"
                f"{normalized_order_id}"
            ),
            params={
                "segment": (
                    normalized_segment
                ),
                "page": 1,
                "page_size": 50,
            },
        )

        payload = unwrap_payload(data)

        if isinstance(payload, list):
            return [
                row
                for row in payload
                if isinstance(row, dict)
            ]

        if isinstance(payload, dict):
            values = (
                payload.get("trades")
                or payload.get("items")
                or payload.get("data")
                or []
            )

            if isinstance(values, list):
                return [
                    row
                    for row in values
                    if isinstance(row, dict)
                ]

        raise RuntimeError(
            "Invalid trades response: "
            f"{data}"
        )

    async def get_positions(self) -> Any:
        data = await self.request(
            "GET",
            "/positions/user",
        )

        return unwrap_payload(data)

    async def get_position_for_symbol(
            self,
            *,
            symbol: str,
            segment: str,
    ) -> dict[str, Any] | None:
        normalized_symbol = (
            self.normalize_symbol(symbol)
        )

        normalized_segment = (
            self.normalize_segment(segment)
        )

        data = await self.request(
            "GET",
            "/positions/trading-symbol",
            params={
                "trading_symbol": normalized_symbol,
                "segment": normalized_segment,
            },
        )

        payload = unwrap_payload(data)

        if isinstance(payload, dict):
            return payload

        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue

                item_symbol = str(
                    item.get(
                        "trading_symbol",
                        "",
                    )
                ).upper()

                if item_symbol == normalized_symbol:
                    return item

        return None

    async def get_order_list(
        self,
        segment: str,
    ) -> Any:
        normalized_segment = (
            self.normalize_segment(segment)
        )

        data = await self.request(
            "GET",
            "/order/list",
            params={
                "segment": normalized_segment,
                "page": 1,
                "page_size": 100,
            },
        )

        return unwrap_payload(data)