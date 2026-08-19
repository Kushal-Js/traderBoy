from __future__ import annotations

import csv
import logging
from io import StringIO

import httpx

from config import Settings


logger = logging.getLogger(__name__)


class InstrumentCache:
    def __init__(
        self,
        settings: Settings,
    ) -> None:
        self.settings = settings
        self.lot_sizes: dict[str, int] = {}
        self.loaded = False
        self.loaded_at: str | None = None

    async def refresh(self) -> int:
        """
        Download and parse the Groww instrument CSV.

        This method should normally be called once during
        FastAPI startup, not for every webhook.
        """
        logger.info(
            "Loading Groww instrument CSV"
        )

        async with httpx.AsyncClient(
            timeout=(
                self.settings.request_timeout_seconds
            )
        ) as client:
            response = await client.get(
                self.settings.instruments_csv_url
            )

        response.raise_for_status()

        new_lot_sizes: dict[str, int] = {}

        reader = csv.DictReader(
            StringIO(response.text)
        )

        for row in reader:
            symbol = (
                row.get("trading_symbol")
                or row.get("groww_symbol")
                or row.get("symbol")
                or ""
            ).strip()

            if not symbol:
                continue

            raw_lot_size = (
                row.get("lot_size")
                or row.get("lotSize")
                or ""
            ).strip()

            if not raw_lot_size:
                continue

            try:
                lot_size = int(
                    float(raw_lot_size)
                )

            except ValueError:
                continue

            if lot_size > 0:
                new_lot_sizes[symbol] = lot_size

        if not new_lot_sizes:
            raise RuntimeError(
                "Groww instrument CSV contained no "
                "usable lot sizes"
            )

        # Replace the old cache only after a successful parse.
        self.lot_sizes = new_lot_sizes
        self.loaded = True

        from datetime import datetime

        self.loaded_at = datetime.now(
            self.settings_timezone()
        ).isoformat()

        logger.info(
            "Groww instrument cache loaded | "
            "symbols=%d | loaded_at=%s",
            len(self.lot_sizes),
            self.loaded_at,
        )

        return len(self.lot_sizes)

    def settings_timezone(self):
        from config import IST

        return IST

    async def ensure_loaded(self) -> None:
        if not self.loaded:
            raise RuntimeError(
                "Instrument cache is not loaded. "
                "Startup initialization may have failed."
            )

    async def lot_size(
        self,
        trading_symbol: str,
    ) -> int:
        await self.ensure_loaded()

        symbol = trading_symbol.strip()

        lot_size = self.lot_sizes.get(symbol)

        if lot_size is None:
            raise RuntimeError(
                f"Lot size not found for {symbol}"
            )

        return lot_size

    def status(self) -> dict[str, object]:
        return {
            "loaded": self.loaded,
            "symbol_count": len(
                self.lot_sizes
            ),
            "loaded_at": self.loaded_at,
        }