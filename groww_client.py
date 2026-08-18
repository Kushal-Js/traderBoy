from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import httpx

from config import Settings
import logging

logger = logging.getLogger(__name__)


def unwrap_payload(value: Any) -> Any:
    if not isinstance(value, dict):
        return value

    payload = value.get("payload")

    if payload is not None:
        return payload

    return value


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


def make_reference_id() -> str:
    value = (
        "GRW"
        + datetime.now().strftime("%H%M%S")
        + uuid.uuid4().hex[:8].upper()
    )

    return value[:20]


class GrowwClient:
    def __init__(
        self,
        settings: Settings,
    ) -> None:
        self.settings = settings

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

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Any = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = (
            f"{self.settings.groww_api_base_url}/"
            f"{endpoint.lstrip('/')}"
        )

        async with httpx.AsyncClient(
            timeout=(
                self.settings.request_timeout_seconds
            )
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
            raise RuntimeError(
                f"Groww HTTP {response.status_code}: "
                f"{data}"
            )

        if str(
            data.get("status", "")
        ).upper() == "FAILURE":
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
                f"Invalid user response: {data}"
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
                "exchange": (
                    self.settings.underlying_exchange
                ),
                "segment": (
                    self.settings.underlying_segment
                ),
                "trading_symbol": symbol,
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
        symbol: str,
        segment: str,
        exchange: str,
    ) -> float:
        params = [
            ("segment", segment),
            (
                "exchange_symbols",
                f"{exchange}_{symbol}",
            ),
        ]

        data = await self.request(
            "GET",
            "/live-data/ltp",
            params=params,
        )

        payload = unwrap_payload(data)

        if isinstance(payload, dict):
            direct = first_value(
                payload,
                (
                    "ltp",
                    "last_price",
                    "price",
                ),
            )

            if direct is not None:
                value = float(direct)

                if value > 0:
                    return value

            nested = (
                payload.get(
                    f"{exchange}_{symbol}"
                )
                or payload.get(symbol)
            )

            if isinstance(nested, dict):
                nested_value = first_value(
                    nested,
                    (
                        "ltp",
                        "last_price",
                        "price",
                    ),
                )

                if nested_value is not None:
                    value = float(nested_value)

                    if value > 0:
                        return value

        raise RuntimeError(
            f"Could not extract LTP for "
            f"{exchange}_{symbol}: {data}"
        )

    async def get_expiries(
        self,
        underlying: str,
    ) -> list[str]:
        data = await self.request(
            "GET",
            "/historical/expiries",
            params={
                "exchange": (
                    self.settings.option_exchange
                ),
                "underlying_symbol": underlying,
                "year": datetime.now().year,
            },
        )

        payload = unwrap_payload(data)

        if isinstance(payload, list):
            values = payload

        elif isinstance(payload, dict):
            values = (
                payload.get("expiries")
                or payload.get("expiry_dates")
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
        data = await self.request(
            "GET",
            (
                "/option-chain/exchange/"
                f"{self.settings.option_exchange}"
                "/underlying/"
                f"{underlying}"
            ),
            params={
                "expiry_date": expiry_date,
            },
        )

        payload = unwrap_payload(data)

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid option chain response: {data}"
            )

        return payload

    async def create_order(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        print(
            "Inside create order",
            body
        )
        data = await self.request(
            "POST",
            "/order/create",
            body=body,
        )

        payload = unwrap_payload(data)
        print(
            "After order created",
            payload
        )

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid order response: {data}"
            )

        return payload

    async def get_order_status(
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
                f"Invalid status response: {data}"
            )

        return payload

    async def get_order_detail(
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
                f"Invalid detail response: {data}"
            )

        return payload

    async def get_order_trades(
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
            f"Invalid trades response: {data}"
        )

    async def get_positions(self) -> Any:
        data = await self.request(
            "GET",
            "/positions/user",
        )

        return unwrap_payload(data)

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