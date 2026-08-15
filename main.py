import os
import uuid
import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("GROWW_ACCESS_TOKEN")
CLIENT_ID = os.getenv("GROWW_CLIENT_ID")

if not ACCESS_TOKEN:
    raise RuntimeError("GROWW_ACCESS_TOKEN is not set")

if not CLIENT_ID:
    raise RuntimeError("GROWW_CLIENT_ID is not set")


API_URL = "https://api.groww.in/v1/order/create"


def create_reference_id() -> str:
    """
    Creates an 8–20 character alphanumeric reference ID.
    """
    return f"SBIAMO{uuid.uuid4().hex[:8].upper()}"


def place_sbi_amo_order(
    price: float,
    order_type: str = "LIMIT",
):
    """
    Places an AMO buy order for exactly one SBI share.

    price:
        Limit price in INR. Required for LIMIT orders.
    """

    if price <= 0:
        raise ValueError("Price must be greater than zero")

    if order_type not in {"LIMIT", "MARKET"}:
        raise ValueError("order_type must be LIMIT or MARKET")

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Client-Id": CLIENT_ID,
    }

    payload = {
        "trading_symbol": "SBIN",
        "quantity": 1,

        # For MARKET orders, use 0 if required by your API version.
        "price": price if order_type == "LIMIT" else 0,

        "trigger_price": 0,

        # Confirm the accepted AMO/validity value in your Groww account.
        "validity": "DAY",

        "exchange": "NSE",
        "segment": "CASH",

        # Delivery product for holding one share.
        # Confirm the exact product enum supported by your account.
        "product": "CNC",

        "order_type": order_type,
        "transaction_type": "BUY",

        "order_reference_id": create_reference_id(),
    }

    response = requests.post(
        API_URL,
        headers=headers,
        json=payload,
        timeout=15,
    )

    try:
        data = response.json()
    except ValueError:
        data = {"raw_response": response.text}

    if not response.ok:
        raise RuntimeError(
            f"Groww API returned HTTP {response.status_code}: {data}"
        )

    return data


if __name__ == "__main__":
    # Replace this with your intended SBI limit price.
    LIMIT_PRICE = 800.00

    print("This will submit a LIVE AMO order:")
    print("  Symbol:   SBIN")
    print("  Quantity: 1 share")
    print(f"  Price:    ₹{LIMIT_PRICE:.2f}")
    print("  Side:     BUY")
    print("  Segment:  CASH")

    confirmation = input(
        "\nType PLACE LIVE ORDER to continue: "
    )

    if confirmation != "PLACE LIVE ORDER":
        print("Order cancelled.")
    else:
        result = place_sbi_amo_order(
            price=LIMIT_PRICE,
            order_type="LIMIT",
        )

        print("\nGroww API response:")
        print(result)