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

    return int(value)


def env_float(
    name: str,
    default: float,
) -> float:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    return float(value)


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
        option_type = os.getenv(
            "OPTION_TYPE",
            "CE",
        ).strip().upper()

        if option_type not in {"CE", "PE"}:
            raise RuntimeError(
                "OPTION_TYPE must be CE or PE"
            )

        max_active_trades = min(
            env_int(
                "MAX_ACTIVE_TRADES",
                3,
            ),
            3,
        )

        if max_active_trades <= 0:
            raise RuntimeError(
                "MAX_ACTIVE_TRADES must be positive"
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
            ).rstrip("/"),
            groww_api_version=os.getenv(
                "GROWW_API_VERSION",
                "1.0",
            ),
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
            amo_enabled=env_bool(
                "AMO_ENABLED",
                False,
            ),
            amo_product=os.getenv(
                "AMO_PRODUCT",
                "NRML",
            ).strip().upper(),
            amo_price_buffer_percent=env_float(
                "AMO_PRICE_BUFFER_PERCENT",
                0.5,
            ),
            amo_max_stocks=min(
                env_int(
                    "AMO_MAX_STOCKS",
                    3,
                ),
                3,
            ),
            max_active_trades=max_active_trades,
            target_percent=env_float(
                "TARGET_PERCENT",
                10.0,
            ),
            stop_loss_percent=env_float(
                "STOP_LOSS_PERCENT",
                3.0,
            ),
            trailing_stop_percent=env_float(
                "TRAILING_STOP_PERCENT",
                1.0,
            ),
            force_exit_time=time(
                env_int(
                    "FORCE_EXIT_HOUR",
                    15,
                ),
                env_int(
                    "FORCE_EXIT_MINUTE",
                    15,
                ),
            ),
            market_start_time=time(
                9,
                15,
            ),
            market_end_time=time(
                15,
                30,
            ),
            tracker_interval_seconds=env_float(
                "TRACKER_INTERVAL_SECONDS",
                5.0,
            ),
            order_poll_interval_seconds=env_float(
                "ORDER_POLL_INTERVAL_SECONDS",
                2.0,
            ),
            order_poll_timeout_seconds=env_float(
                "ORDER_POLL_TIMEOUT_SECONDS",
                90.0,
            ),
            request_timeout_seconds=env_float(
                "REQUEST_TIMEOUT_SECONDS",
                20.0,
            ),
            instruments_csv_url=required_env(
                "INSTRUMENTS_CSV_URL"
            ),
            state_file=Path(
                os.getenv(
                    "STATE_FILE",
                    "trading_state.json",
                )
            ),
        )