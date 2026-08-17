from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class TradeStatus(StrEnum):
    RESERVED = "RESERVED"
    ENTRY_PENDING = "ENTRY_PENDING"
    OPEN = "OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


@dataclass
class ChartinkStock:
    symbol: str
    trigger_price: float


@dataclass
class TradeState:
    trade_date: str
    underlying: str
    option_symbol: str
    option_type: str
    expiry_date: str
    quantity: int
    status: str

    entry_order_id: str | None = None
    entry_order_type: str | None = None
    entry_amo_status: str | None = None
    entry_price: float | None = None
    current_price: float | None = None
    highest_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None

    exit_order_id: str | None = None
    exit_reason: str | None = None
    last_error: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
    ) -> "TradeState":
        return cls(**value)