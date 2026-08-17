from __future__ import annotations

from datetime import date, datetime
from typing import Any

from config import Settings
from groww_client import GrowwClient, first_value
from models import ChartinkStock


def normalize_symbol(value: Any) -> str:
    return str(value).strip().upper()


def parse_chartink_payload(
    payload: dict[str, Any],
) -> list[ChartinkStock]:
    raw_symbols = payload.get("stocks")
    raw_prices = payload.get("trigger_prices")

    if not isinstance(raw_symbols, str):
        raise ValueError(
            "stocks must be a comma-separated string"
        )

    if not isinstance(raw_prices, str):
        raise ValueError(
            "trigger_prices must be a comma-separated string"
        )

    symbols = [
        normalize_symbol(item)
        for item in raw_symbols.split(",")
        if item.strip()
    ]

    prices = [
        item.strip()
        for item in raw_prices.split(",")
        if item.strip()
    ]

    if len(symbols) != len(prices):
        raise ValueError(
            "stocks and trigger_prices must have "
            "the same number of values"
        )

    result: list[ChartinkStock] = []
    seen: set[str] = set()

    for symbol, raw_price in zip(
        symbols,
        prices,
    ):
        if symbol in seen:
            continue

        try:
            trigger_price = float(
                raw_price.replace(",", "")
            )

        except ValueError as exc:
            raise ValueError(
                f"Invalid trigger price for {symbol}: "
                f"{raw_price}"
            ) from exc

        if trigger_price <= 0:
            raise ValueError(
                f"Trigger price must be positive for {symbol}"
            )

        result.append(
            ChartinkStock(
                symbol=symbol,
                trigger_price=trigger_price,
            )
        )

        seen.add(symbol)

    return result


async def today_change_percent(
    client: GrowwClient,
    symbol: str,
) -> float:
    quote = await client.get_quote(symbol)

    direct_change = first_value(
        quote,
        (
            "day_change_perc",
            "day_change_percentage",
            "change_percent",
        ),
    )

    if direct_change is not None:
        return float(direct_change)

    open_price = first_value(
        quote,
        (
            "open",
            "open_price",
        ),
    )

    last_price = first_value(
        quote,
        (
            "last_price",
            "ltp",
            "price",
        ),
    )

    if open_price is None or last_price is None:
        raise RuntimeError(
            f"Cannot calculate change for {symbol}: "
            f"{quote}"
        )

    open_price = float(open_price)
    last_price = float(last_price)

    if open_price <= 0:
        raise RuntimeError(
            f"Invalid open price for {symbol}"
        )

    return (
        (last_price - open_price)
        / open_price
        * 100
    )


async def rank_stocks(
    client: GrowwClient,
    stocks: list[ChartinkStock],
    limit: int,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []

    for stock in stocks:
        try:
            change = await today_change_percent(
                client=client,
                symbol=stock.symbol,
            )

            ranked.append(
                {
                    "symbol": stock.symbol,
                    "trigger_price": (
                        stock.trigger_price
                    ),
                    "today_percent_change": change,
                }
            )

        except Exception:
            # One bad quote must not stop every stock.
            continue

    ranked.sort(
        key=lambda item: item[
            "today_percent_change"
        ],
        reverse=True,
    )

    return ranked[:limit]


def expiry_is_future(
    expiry: str,
) -> bool:
    try:
        value = datetime.strptime(
            expiry,
            "%Y-%m-%d",
        ).date()

        return value >= date.today()

    except ValueError:
        return False


async def choose_expiry(
    client: GrowwClient,
    settings: Settings,
    underlying: str,
) -> str:
    available = await client.get_expiries(
        underlying
    )

    future = [
        expiry
        for expiry in available
        if expiry_is_future(expiry)
    ]

    if not future:
        raise RuntimeError(
            f"No future expiry for {underlying}"
        )

    configured = settings.configured_expiry_date

    if configured in future:
        return configured

    return sorted(future)[0]


def chain_strikes(
    value: Any,
) -> list[tuple[float, dict[str, Any]]]:
    if isinstance(value, dict):
        result: list[
            tuple[float, dict[str, Any]]
        ] = []

        for raw_strike, contract_data in value.items():
            try:
                strike = float(raw_strike)

            except (TypeError, ValueError):
                continue

            if isinstance(contract_data, dict):
                result.append(
                    (strike, contract_data)
                )

        return result

    if isinstance(value, list):
        result = []

        for item in value:
            if not isinstance(item, dict):
                continue

            raw_strike = (
                item.get("strike_price")
                or item.get("strike")
                or item.get("strikePrice")
            )

            try:
                strike = float(raw_strike)

            except (TypeError, ValueError):
                continue

            result.append(
                (strike, item)
            )

        return result

    return []


async def select_atm_option(
    client: GrowwClient,
    settings: Settings,
    underlying: str,
) -> tuple[str, float, str]:
    expiry = await choose_expiry(
        client=client,
        settings=settings,
        underlying=underlying,
    )

    chain = await client.get_option_chain(
        underlying=underlying,
        expiry_date=expiry,
    )

    spot_value = first_value(
        chain,
        (
            "underlying_ltp",
            "underlying_last_price",
        ),
    )

    if spot_value is None:
        spot = await client.get_ltp(
            symbol=underlying,
            segment=settings.underlying_segment,
            exchange=settings.underlying_exchange,
        )

    else:
        spot = float(spot_value)

    strikes = chain_strikes(
        chain.get("strikes")
    )

    if not strikes:
        raise RuntimeError(
            f"No valid strikes found for {underlying} "
            f"expiry {expiry}"
        )

    selected_strike, selected_data = min(
        strikes,
        key=lambda item: abs(
            item[0] - spot
        ),
    )

    contract = None

    for key, value in selected_data.items():
        if str(key).upper() == settings.option_type:
            if isinstance(value, dict):
                contract = value

            break

    if contract is None:
        raise RuntimeError(
            f"{settings.option_type} contract unavailable "
            f"for {underlying} strike {selected_strike}"
        )

    option_symbol = (
        contract.get("trading_symbol")
        or contract.get("symbol")
    )

    option_ltp = first_value(
        contract,
        (
            "ltp",
            "last_price",
            "price",
        ),
    )

    if not option_symbol:
        raise RuntimeError(
            f"Option symbol missing: {contract}"
        )

    if option_ltp is None:
        raise RuntimeError(
            f"Option LTP missing: {contract}"
        )

    option_ltp = float(option_ltp)

    if option_ltp <= 0:
        raise RuntimeError(
            f"Invalid option LTP: {option_ltp}"
        )

    return (
        str(option_symbol),
        option_ltp,
        expiry,
    )