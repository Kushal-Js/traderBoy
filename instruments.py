from __future__ import annotations

import csv
from io import StringIO
from typing import Any

import httpx

from config import Settings


class InstrumentCache:
    def __init__(
        self,
        settings: Settings,
    ) -> None:
        self.settings = settings
        self.rows: list[dict[str, str]] = []

    async def refresh(self) -> None:
        async with httpx.AsyncClient(
            timeout=(
                self.settings.request_timeout_seconds
            )
        ) as client:
            response = await client.get(
                self.settings.instruments_csv_url
            )

        response.raise_for_status()

        reader = csv.DictReader(
            StringIO(response.text)
        )

        self.rows = [
            {
                str(key).strip(): (
                    value.strip()
                    if isinstance(value, str)
                    else ""
                )
                for key, value in row.items()
            }
            for row in reader
        ]

    async def ensure_loaded(self) -> None:
        if not self.rows:
            await self.refresh()

    async def find(
        self,
        trading_symbol: str,
    ) -> dict[str, str]:
        await self.ensure_loaded()

        for row in self.rows:
            candidate = (
                row.get("trading_symbol")
                or row.get("groww_symbol")
                or row.get("symbol")
                or ""
            )

            if candidate == trading_symbol:
                return row

        raise RuntimeError(
            f"Instrument not found: {trading_symbol}"
        )

    async def lot_size(
        self,
        trading_symbol: str,
    ) -> int:
        row = await self.find(
            trading_symbol
        )

        raw_value = (
            row.get("lot_size")
            or row.get("lotSize")
        )

        if not raw_value:
            raise RuntimeError(
                f"Lot size missing for {trading_symbol}"
            )

        value = int(float(raw_value))

        if value <= 0:
            raise RuntimeError(
                f"Invalid lot size for "
                f"{trading_symbol}: {value}"
            )

        return value