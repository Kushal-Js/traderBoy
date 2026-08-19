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
    # Required fields must come first.
    trade_date: str
    underlying: str
    option_symbol: str
    option_type: str
    expiry_date: str
    quantity: int
    status: TradeStatus

    # Defaults must come after required fields.
    instrument_type: str = "OPTION"
    exchange: str = "NSE"
    segment: str = "FNO"
    product: str = "NRML"

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
        data = asdict(self)

        # Store enum values as plain strings in JSON.
        data["status"] = self.status.value

        return data

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
    ) -> "TradeState":
        data = dict(value)

        # Backward-compatible defaults for older state files.
        data.setdefault(
            "instrument_type",
            "OPTION",
        )
        data.setdefault(
            "exchange",
            "NSE",
        )
        data.setdefault(
            "segment",
            "FNO",
        )
        data.setdefault(
            "product",
            "NRML",
        )

        data.setdefault(
            "entry_order_id",
            None,
        )
        data.setdefault(
            "entry_order_type",
            None,
        )
        data.setdefault(
            "entry_amo_status",
            None,
        )

        data.setdefault(
            "entry_price",
            None,
        )
        data.setdefault(
            "current_price",
            None,
        )
        data.setdefault(
            "highest_price",
            None,
        )
        data.setdefault(
            "stop_price",
            None,
        )
        data.setdefault(
            "target_price",
            None,
        )

        data.setdefault(
            "exit_order_id",
            None,
        )
        data.setdefault(
            "exit_reason",
            None,
        )
        data.setdefault(
            "last_error",
            None,
        )
        data.setdefault(
            "updated_at",
            None,
        )

        # Older JSON files normally contain a string.
        raw_status = data.get(
            "status",
            TradeStatus.FAILED.value,
        )

        if isinstance(raw_status, TradeStatus):
            data["status"] = raw_status
        else:
            data["status"] = TradeStatus(
                str(raw_status).upper()
            )

        # Normalize common values from old state files.
        data["underlying"] = str(
            data["underlying"]
        ).strip().upper()

        data["option_symbol"] = str(
            data["option_symbol"]
        ).strip().upper()

        data["option_type"] = str(
            data.get("option_type", "")
        ).strip().upper()

        data["instrument_type"] = str(
            data.get(
                "instrument_type",
                "OPTION",
            )
        ).strip().upper()

        data["exchange"] = str(
            data.get(
                "exchange",
                "NSE",
            )
        ).strip().upper()

        data["segment"] = str(
            data.get(
                "segment",
                "FNO",
            )
        ).strip().upper()

        data["product"] = str(
            data.get(
                "product",
                "NRML",
            )
        ).strip().upper()

        return cls(**data)