#!/usr/bin/env python3

import json
import os
import sys
import uuid
from typing import Any

import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

API_BASE_URL = "https://api.groww.in/v1"

TRADING_SYMBOL = "SBIN"
EXCHANGE = "NSE"
SEGMENT = "CASH"
PRODUCT = "CNC"
QUANTITY = 1
ORDER_TYPE = "LIMIT"
TRANSACTION_TYPE = "BUY"
VALIDITY = "DAY"

REQUEST_TIMEOUT_SECONDS = 20


# Load .env from the current working directory.
load_dotenv()


def required_env(name: str) -> str:
    """Read a required environment variable."""
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Missing environment variable: {name}\n"
            "Create a .env file containing the required Groww credentials."
        )

    return value


ACCESS_TOKEN = required_env("GROWW_ACCESS_TOKEN")
CLIENT_ID = required_env("GROWW_CLIENT_ID")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def print_json(title: str, value: Any) -> None:
    """Print JSON in a readable format."""
    print(f"\n{title}")
    print("-" * len(title))
    print(json.dumps(value, indent=2, default=str))


def create_order_reference_id() -> str:
    """
    Groww requires a user-provided alphanumeric reference ID
    between 8 and 20 characters, with at most two hyphens.
    """
    return f"SBIAMO{uuid.uuid4().hex[:10].upper()}"


def groww_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "X-Client-Id": CLIENT_ID,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def parse_response(response: requests.Response) -> dict[str, Any]:
    """Parse JSON while preserving a useful error for non-JSON responses."""
    try:
        data = response.json()
    except ValueError:
        data = {
            "raw_response": response.text,
        }

    if not isinstance(data, dict):
        return {"response": data}

    return data


def raise_for_api_failure(
    response: requests.Response,
    data: dict[str, Any],
) -> None:
    """
    Raise a useful error for HTTP failures or Groww application failures.
    """
    if not response.ok:
        raise RuntimeError(
            f"Groww HTTP error {response.status_code}: "
            f"{data.get('remark', data)}"
        )

    # Groww's API may return HTTP 200 with status=FAILURE.
    if str(data.get("status", "")).upper() == "FAILURE":
        raise RuntimeError(
            f"Groww rejected the request: "
            f"{data.get('remark', data)}"
        )


# ---------------------------------------------------------------------
# Groww API functions
# ---------------------------------------------------------------------

def place_sbi_amo_order(limit_price: float) -> dict[str, Any]:
    """
    Place a live AMO BUY order for one SBI equity share.

    This is a LIMIT order:
      - Symbol: SBIN
      - Exchange: NSE
      - Segment: CASH
      - Product: CNC
      - Quantity: 1
    """

    if limit_price <= 0:
        raise ValueError("Limit price must be greater than zero.")

    order_reference_id = create_order_reference_id()

    payload = {
        "trading_symbol": TRADING_SYMBOL,
        "quantity": QUANTITY,
        "price": round(limit_price, 2),
        "trigger_price": 0,
        "validity": VALIDITY,
        "exchange": EXCHANGE,
        "segment": SEGMENT,
        "product": PRODUCT,
        "order_type": ORDER_TYPE,
        "transaction_type": TRANSACTION_TYPE,
        "order_reference_id": order_reference_id,
    }

    print_json("Order payload", payload)

    response = requests.post(
        f"{API_BASE_URL}/order/create",
        headers=groww_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    data = parse_response(response)

    print_json(
        f"Groww order response — HTTP {response.status_code}",
        data,
    )

    raise_for_api_failure(response, data)

    return data


def get_order_status(groww_order_id: str) -> dict[str, Any]:
    """
    Get the current status of an order.

    Groww documents the status endpoint as:
    GET /v1/order/status/{groww_order_id}
    """

    if not groww_order_id:
        raise ValueError("groww_order_id is required.")

    response = requests.get(
        f"{API_BASE_URL}/order/status/{groww_order_id}",
        headers=groww_headers(),
        params={"segment": SEGMENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    data = parse_response(response)

    print_json(
        f"Order status response — HTTP {response.status_code}",
        data,
    )

    raise_for_api_failure(response, data)

    return data


def get_order_details(groww_order_id: str) -> dict[str, Any]:
    """
    Get detailed information for an order, including the remark.
    """

    if not groww_order_id:
        raise ValueError("groww_order_id is required.")

    response = requests.get(
        f"{API_BASE_URL}/order/detail/{groww_order_id}",
        headers=groww_headers(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    data = parse_response(response)

    print_json(
        f"Order detail response — HTTP {response.status_code}",
        data,
    )

    raise_for_api_failure(response, data)

    return data


def get_cash_order_list() -> dict[str, Any]:
    """
    Retrieve today's CASH orders.

    This is useful if the create response did not contain an order ID.
    """

    response = requests.get(
        f"{API_BASE_URL}/order/list",
        headers=groww_headers(),
        params={
            "segment": SEGMENT,
            "page": 1,
            "page_size": 100,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    data = parse_response(response)

    print_json(
        f"CASH order list response — HTTP {response.status_code}",
        data,
    )

    raise_for_api_failure(response, data)

    return data


# ---------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------

def main() -> int:
    print("Groww SBI AMO order script")
    print("==========================")
    print(f"Symbol:      {TRADING_SYMBOL}")
    print(f"Exchange:    {EXCHANGE}")
    print(f"Segment:     {SEGMENT}")
    print(f"Product:     {PRODUCT}")
    print(f"Transaction: {TRANSACTION_TYPE}")
    print(f"Quantity:    {QUANTITY}")
    print(f"Order type:  {ORDER_TYPE}")
    print(f"Validity:    {VALIDITY}")

    try:
        price_text = input(
            "\nEnter the SBI limit price in INR: "
        ).strip()

        limit_price = float(price_text)

        print(
            f"\nYou are about to submit a LIVE AMO BUY order for "
            f"{QUANTITY} share of {TRADING_SYMBOL} at "
            f"₹{limit_price:.2f}."
        )

        confirmation = input(
            "\nType PLACE LIVE ORDER to continue: "
        ).strip()

        if confirmation != "PLACE LIVE ORDER":
            print("Order cancelled locally. No API order was submitted.")
            return 0

        result = place_sbi_amo_order(limit_price)

        groww_order_id = result.get("groww_order_id")

        if not groww_order_id:
            print(
                "\nNo groww_order_id was returned. "
                "Fetching today's CASH order list."
            )
            get_cash_order_list()
            return 0

        print(f"\nGroww order ID: {groww_order_id}")

        # Fetch the status immediately. An AMO can remain pending until
        # Groww processes it, so this is only the current status.
        get_order_status(groww_order_id)

        # Fetch details to expose the cancellation/rejection remark.
        get_order_details(groww_order_id)

        return 0

    except requests.exceptions.Timeout:
        print(
            "\nERROR: Groww API request timed out.",
            file=sys.stderr,
        )
        return 1

    except requests.exceptions.RequestException as error:
        print(
            f"\nERROR: Network/API request failed: {error}",
            file=sys.stderr,
        )
        return 1

    except ValueError as error:
        print(f"\nERROR: Invalid input: {error}", file=sys.stderr)
        return 1

    except RuntimeError as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())