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
        self.loaded_at: str | None = None

    async def refresh(self) -> int:
        """
        Re-download and replace the in-memory instrument cache.
        Returns the number of loaded rows.
        """
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

        new_rows = [
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

        if not new_rows:
            raise RuntimeError(
                "Instrument CSV returned no rows"
            )

        # Replace only after a successful download and parse.
        self.rows = new_rows

        from datetime import datetime
        from config import IST

        self.loaded_at = datetime.now(
            IST
        ).isoformat()

        return len(self.rows)

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

            if candidate.strip() == trading_symbol.strip():
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

        raw_lot_size = (
            row.get("lot_size")
            or row.get("lotSize")
        )

        if not raw_lot_size:
            raise RuntimeError(
                f"Lot size missing for {trading_symbol}"
            )

        value = int(float(raw_lot_size))

        if value <= 0:
            raise RuntimeError(
                f"Invalid lot size for "
                f"{trading_symbol}: {value}"
            )

        return value

    def status(self) -> dict[str, Any]:
        return {
            "loaded": bool(self.rows),
            "row_count": len(self.rows),
            "loaded_at": self.loaded_at,
        }