from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from models import TradeState, TradeStatus


class StateStore:
    def __init__(
        self,
        path: Path,
    ) -> None:
        self.path = path
        self.lock = asyncio.Lock()

        self.data: dict[str, Any] = {
            "trade_date": self.today_key(),
            "last_reconciled_at": None,
            "trades": {},
        }

        self.load()

    @staticmethod
    def today_key() -> str:
        return datetime.now().strftime(
            "%Y-%m-%d"
        )

    def load(self) -> None:
        if not self.path.exists():
            return

        try:
            raw = json.loads(
                self.path.read_text(
                    encoding="utf-8"
                )
            )

            if isinstance(raw, dict):
                self.data = raw

        except Exception:
            # A corrupt state file should not silently
            # overwrite the original file.
            backup = self.path.with_suffix(
                ".corrupt"
            )

            try:
                self.path.replace(backup)
            except OSError:
                pass

    def reset_if_new_day(self) -> None:
        current_date = self.today_key()

        if self.data.get(
            "trade_date"
        ) == current_date:
            return

        self.data = {
            "trade_date": current_date,
            "last_reconciled_at": None,
            "trades": {},
        }

    async def save(self) -> None:
        async with self.lock:
            temporary_path = self.path.with_suffix(
                ".tmp"
            )

            temporary_path.write_text(
                json.dumps(
                    self.data,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )

            temporary_path.replace(self.path)

    def get_trade(
        self,
        underlying: str,
    ) -> TradeState | None:
        raw = self.data.get(
            "trades",
            {},
        ).get(underlying.upper())

        if not isinstance(raw, dict):
            return None

        return TradeState.from_dict(raw)

    def put_trade(
        self,
        trade: TradeState,
    ) -> None:
        self.data.setdefault(
            "trades",
            {},
        )[trade.underlying.upper()] = (
            trade.to_dict()
        )

    def delete_trade(
        self,
        underlying: str,
    ) -> None:
        self.data.setdefault(
            "trades",
            {},
        ).pop(underlying.upper(), None)

    def all_trades(self) -> list[TradeState]:
        result: list[TradeState] = []

        for raw in self.data.get(
            "trades",
            {},
        ).values():
            if isinstance(raw, dict):
                try:
                    result.append(
                        TradeState.from_dict(raw)
                    )
                except TypeError:
                    continue

        return result

    def active_trades(self) -> list[TradeState]:
        return [
            trade
            for trade in self.all_trades()
            if trade.status in {
                TradeStatus.RESERVED,
                TradeStatus.ENTRY_PENDING,
                TradeStatus.OPEN,
                TradeStatus.EXIT_PENDING,
            }
        ]

    def open_trades(self) -> list[TradeState]:
        return [
            trade
            for trade in self.all_trades()
            if trade.status == TradeStatus.OPEN
        ]

    def has_reserved_or_active(
        self,
        underlying: str,
    ) -> bool:
        trade = self.get_trade(underlying)

        if trade is None:
            return False

        return trade.status in {
            TradeStatus.RESERVED,
            TradeStatus.ENTRY_PENDING,
            TradeStatus.OPEN,
            TradeStatus.EXIT_PENDING,
        }