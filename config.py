from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


load_dotenv()


IST = ZoneInfo("Asia/Kolkata")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"{name} is required in .env"
        )

    return value


def env_bool(
    name: str,
    default: bool = False,
) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_int(
    name: str,
    default: int,
) -> int:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        return int(value.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be an integer"
        ) from exc


def env_float(
    name: str,
    default: float,
) -> float:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        return float(value.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be a number"
        ) from exc


def validate_buy_strategy(
    value: str,
) -> str:
    normalized = value.strip().upper()

    allowed = {
        "OPTIONS",
        "EQUITY_MTF",
    }

    if normalized not in allowed:
        raise RuntimeError(
            "BUY_STRATEGY must be OPTIONS "
            "or EQUITY_MTF"
        )

    return normalized


def validate_option_type(
    value: str,
) -> str:
    normalized = value.strip().upper()

    if normalized not in {"CE", "PE"}:
        raise RuntimeError(
            "OPTION_TYPE must be CE or PE"
        )

    return normalized


def validate_positive_int(
    name: str,
    value: int,
) -> int:
    if value <= 0:
        raise RuntimeError(
            f"{name} must be positive"
        )

    return value


def validate_non_negative_float(
    name: str,
    value: float,
) -> float:
    if value < 0:
        raise RuntimeError(
            f"{name} cannot be negative"
        )

    return value


@dataclass(frozen=True)
class Settings:
    groww_access_token: str
    groww_client_id: str

    groww_api_base_url: str
    groww_api_version: str

    live_trading: bool

    underlying_exchange: str
    underlying_segment: str

    option_exchange: str
    option_segment: str
    option_type: str
    option_product: str
    configured_expiry_date: str

    buy_strategy: str

    equity_quantity: int
    equity_exchange: str
    equity_segment: str
    equity_product: str

    amo_enabled: bool
    amo_product: str
    amo_price_buffer_percent: float
    amo_max_stocks: int

    max_active_trades: int
    target_percent: float
    stop_loss_percent: float
    trailing_stop_percent: float

    force_exit_time: time
    market_start_time: time
    market_end_time: time

    tracker_interval_seconds: float
    order_poll_interval_seconds: float
    order_poll_timeout_seconds: float
    request_timeout_seconds: float

    instruments_csv_url: str
    state_file: Path

    @classmethod
    def from_env(cls) -> "Settings":
        buy_strategy = validate_buy_strategy(
            os.getenv(
                "BUY_STRATEGY",
                "OPTIONS",
            )
        )

        option_type = validate_option_type(
            os.getenv(
                "OPTION_TYPE",
                "CE",
            )
        )

        equity_quantity = validate_positive_int(
            "EQUITY_QUANTITY",
            env_int(
                "EQUITY_QUANTITY",
                100,
            ),
        )

        equity_exchange = os.getenv(
            "EQUITY_EXCHANGE",
            "NSE",
        ).strip().upper()

        equity_segment = os.getenv(
            "EQUITY_SEGMENT",
            "CASH",
        ).strip().upper()

        equity_product = os.getenv(
            "EQUITY_PRODUCT",
            "MTF",
        ).strip().upper()

        max_active_trades = min(
            validate_positive_int(
                "MAX_ACTIVE_TRADES",
                env_int(
                    "MAX_ACTIVE_TRADES",
                    3,
                ),
            ),
            3,
        )

        amo_max_stocks = min(
            validate_positive_int(
                "AMO_MAX_STOCKS",
                env_int(
                    "AMO_MAX_STOCKS",
                    3,
                ),
            ),
            3,
        )

        target_percent = (
            validate_non_negative_float(
                "TARGET_PERCENT",
                env_float(
                    "TARGET_PERCENT",
                    10.0,
                ),
            )
        )

        stop_loss_percent = (
            validate_non_negative_float(
                "STOP_LOSS_PERCENT",
                env_float(
                    "STOP_LOSS_PERCENT",
                    3.0,
                ),
            )
        )

        trailing_stop_percent = (
            validate_non_negative_float(
                "TRAILING_STOP_PERCENT",
                env_float(
                    "TRAILING_STOP_PERCENT",
                    1.0,
                ),
            )
        )

        amo_price_buffer_percent = (
            validate_non_negative_float(
                "AMO_PRICE_BUFFER_PERCENT",
                env_float(
                    "AMO_PRICE_BUFFER_PERCENT",
                    0.5,
                ),
            )
        )

        tracker_interval_seconds = (
            validate_positive_float(
                "TRACKER_INTERVAL_SECONDS",
                env_float(
                    "TRACKER_INTERVAL_SECONDS",
                    5.0,
                ),
            )
        )

        order_poll_interval_seconds = (
            validate_positive_float(
                "ORDER_POLL_INTERVAL_SECONDS",
                env_float(
                    "ORDER_POLL_INTERVAL_SECONDS",
                    2.0,
                ),
            )
        )

        order_poll_timeout_seconds = (
            validate_positive_float(
                "ORDER_POLL_TIMEOUT_SECONDS",
                env_float(
                    "ORDER_POLL_TIMEOUT_SECONDS",
                    90.0,
                ),
            )
        )

        request_timeout_seconds = (
            validate_positive_float(
                "REQUEST_TIMEOUT_SECONDS",
                env_float(
                    "REQUEST_TIMEOUT_SECONDS",
                    20.0,
                ),
            )
        )

        force_exit_hour = env_int(
            "FORCE_EXIT_HOUR",
            15,
        )

        force_exit_minute = env_int(
            "FORCE_EXIT_MINUTE",
            15,
        )

        if not 0 <= force_exit_hour <= 23:
            raise RuntimeError(
                "FORCE_EXIT_HOUR must be between 0 and 23"
            )

        if not 0 <= force_exit_minute <= 59:
            raise RuntimeError(
                "FORCE_EXIT_MINUTE must be between 0 and 59"
            )

        return cls(
            groww_access_token=required_env(
                "GROWW_ACCESS_TOKEN"
            ),
            groww_client_id=required_env(
                "GROWW_CLIENT_ID"
            ),
            groww_api_base_url=os.getenv(
                "GROWW_API_BASE_URL",
                "https://api.groww.in/v1",
            ).strip().rstrip("/"),
            groww_api_version=os.getenv(
                "GROWW_API_VERSION",
                "1.0",
            ).strip(),
            live_trading=env_bool(
                "LIVE_TRADING",
                False,
            ),
            underlying_exchange=os.getenv(
                "UNDERLYING_EXCHANGE",
                "NSE",
            ).strip().upper(),
            underlying_segment=os.getenv(
                "UNDERLYING_SEGMENT",
                "CASH",
            ).strip().upper(),
            option_exchange=os.getenv(
                "OPTION_EXCHANGE",
                "NSE",
            ).strip().upper(),
            option_segment=os.getenv(
                "OPTION_SEGMENT",
                "FNO",
            ).strip().upper(),
            option_type=option_type,
            option_product=os.getenv(
                "OPTION_PRODUCT",
                "NRML",
            ).strip().upper(),
            configured_expiry_date=os.getenv(
                "OPTION_EXPIRY_DATE",
                "",
            ).strip(),
            buy_strategy=buy_strategy,
            equity_quantity=equity_quantity,
            equity_exchange=equity_exchange,
            equity_segment=equity_segment,
            equity_product=equity_product,
            amo_enabled=env_bool(
                "AMO_ENABLED",
                False,
            ),
            amo_product=os.getenv(
                "AMO_PRODUCT",
                "NRML",
            ).strip().upper(),
            amo_price_buffer_percent=(
                amo_price_buffer_percent
            ),
            amo_max_stocks=amo_max_stocks,
            max_active_trades=max_active_trades,
            target_percent=target_percent,
            stop_loss_percent=stop_loss_percent,
            trailing_stop_percent=(
                trailing_stop_percent
            ),
            force_exit_time=time(
                force_exit_hour,
                force_exit_minute,
            ),
            market_start_time=time(
                9,
                15,
            ),
            market_end_time=time(
                15,
                30,
            ),
            tracker_interval_seconds=(
                tracker_interval_seconds
            ),
            order_poll_interval_seconds=(
                order_poll_interval_seconds
            ),
            order_poll_timeout_seconds=(
                order_poll_timeout_seconds
            ),
            request_timeout_seconds=(
                request_timeout_seconds
            ),
            instruments_csv_url=required_env(
                "INSTRUMENTS_CSV_URL"
            ),
            state_file=Path(
                os.getenv(
                    "STATE_FILE",
                    "trading_state.json",
                ).strip()
            ),
        )


def validate_positive_float(
    name: str,
    value: float,
) -> float:
    if value <= 0:
        raise RuntimeError(
            f"{name} must be positive"
        )

    return value