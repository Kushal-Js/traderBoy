"""
Thin wrapper around the official `growwapi` SDK.

Reference: https://groww.in/trade-api/docs/python-sdk
All method names / params below match the documented SDK exactly:
  - GrowwAPI.get_access_token(...)               (Introduction)
  - groww.get_quote(...)                          (Live Data)
  - groww.get_ltp(...)                            (Live Data)
  - groww.get_option_chain(...)                   (Live Data)
  - groww.get_all_instruments()                   (Instruments)
  - groww.place_order(...) / get_order_detail(...)(Orders)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd
import pyotp
from growwapi import GrowwAPI

import config

logger = logging.getLogger("groww_client")


@dataclass
class AtmOption:
    trading_symbol: str
    strike: float
    option_type: str          # "CE" or "PE"
    lot_size: int
    expiry_date: str
    underlying_ltp: float
    option_ltp: float


class GrowwWrapper:
    """Lazily-authenticated singleton wrapper around GrowwAPI."""

    def __init__(self) -> None:
        self._client: Optional[GrowwAPI] = None
        self._instruments_df: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def authenticate(self) -> None:
        if config.AUTH_MODE == "SECRET":
            access_token = GrowwAPI.get_access_token(
                api_key=config.GROWW_API_KEY,
                secret=config.GROWW_API_SECRET,
            )
        elif config.AUTH_MODE == "TOTP":
            totp = pyotp.TOTP(config.GROWW_TOTP_SECRET).now()
            access_token = GrowwAPI.get_access_token(
                api_key=config.GROWW_API_KEY,
                totp=totp,
            )
        else:
            raise ValueError(f"Unknown GROWW_AUTH_MODE: {config.AUTH_MODE}")

        self._client = GrowwAPI(access_token)
        logger.info("Authenticated with Groww API (mode=%s)", config.AUTH_MODE)

    @property
    def client(self) -> GrowwAPI:
        if self._client is None:
            self.authenticate()
        return self._client

    # ------------------------------------------------------------------ #
    # Instruments (cached in-process; refresh once a day is plenty)
    # ------------------------------------------------------------------ #
    def instruments(self) -> pd.DataFrame:
        if self._instruments_df is None:
            self._instruments_df = self.client.get_all_instruments()
        return self._instruments_df

    def refresh_instruments(self) -> None:
        self._instruments_df = self.client.get_all_instruments()

    def nearest_expiry_for_underlying(self, underlying_symbol: str) -> str:
        """Returns the nearest (>= today) expiry_date, as 'YYYY-MM-DD', for
        the FNO options chain of the given underlying (e.g. 'TCS', 'NIFTY')."""
        df = self.instruments()
        today = date.today()

        subset = df[
            (df["underlying_symbol"] == underlying_symbol)
            & (df["segment"] == "FNO")
            & (df["instrument_type"].isin(["CE", "PE"]))
        ].copy()

        if subset.empty:
            raise ValueError(f"No FNO instruments found for underlying {underlying_symbol}")

        subset["expiry_dt"] = pd.to_datetime(subset["expiry_date"]).dt.date
        subset = subset[subset["expiry_dt"] >= today]

        if subset.empty:
            raise ValueError(f"No unexpired FNO contracts found for {underlying_symbol}")

        nearest = subset["expiry_dt"].min()
        return nearest.strftime("%Y-%m-%d")

    # ------------------------------------------------------------------ #
    # Live data
    # ------------------------------------------------------------------ #
    def get_day_change_pct(self, trading_symbol: str) -> float:
        """% change on the day for a CASH-segment equity symbol."""
        quote = self.client.get_quote(
            exchange=self.client.EXCHANGE_NSE,
            segment=self.client.SEGMENT_CASH,
            trading_symbol=trading_symbol,
        )
        return float(quote["day_change_perc"])

    def get_equity_ltp(self, trading_symbol: str) -> float:
        resp = self.client.get_ltp(
            segment=self.client.SEGMENT_CASH,
            exchange_trading_symbols=f"NSE_{trading_symbol}",
        )
        return float(resp[f"NSE_{trading_symbol}"])

    def get_option_ltp(self, trading_symbol: str) -> float:
        resp = self.client.get_ltp(
            segment=self.client.SEGMENT_FNO,
            exchange_trading_symbols=f"NSE_{trading_symbol}",
        )
        return float(resp[f"NSE_{trading_symbol}"])

    # ------------------------------------------------------------------ #
    # ATM option selection
    # ------------------------------------------------------------------ #
    def get_atm_option(self, underlying_symbol: str, option_type: str) -> AtmOption:
        """Finds the ATM (closest-to-spot strike) CE/PE for the nearest expiry."""
        expiry_date = self.nearest_expiry_for_underlying(underlying_symbol)

        chain = self.client.get_option_chain(
            exchange=self.client.EXCHANGE_NSE,
            underlying=underlying_symbol,
            expiry_date=expiry_date,
        )

        underlying_ltp = float(chain["underlying_ltp"])
        strikes = chain["strikes"]

        closest_strike = min(
            strikes.keys(),
            key=lambda s: abs(float(s) - underlying_ltp),
        )
        leg = strikes[closest_strike].get(option_type)
        if leg is None:
            raise ValueError(
                f"No {option_type} leg found at strike {closest_strike} for {underlying_symbol}"
            )

        trading_symbol = leg["trading_symbol"]

        # Look up lot size from the instruments master
        df = self.instruments()
        row = df[df["trading_symbol"] == trading_symbol]
        lot_size = int(row.iloc[0]["lot_size"]) if not row.empty else config.LOT_SIZE_FALLBACK

        return AtmOption(
            trading_symbol=trading_symbol,
            strike=float(closest_strike),
            option_type=option_type,
            lot_size=lot_size,
            expiry_date=expiry_date,
            underlying_ltp=underlying_ltp,
            option_ltp=float(leg["ltp"]),
        )

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    def place_market_order(
        self,
        trading_symbol: str,
        quantity: int,
        transaction_type: str,
        order_reference_id: Optional[str] = None,
    ) -> dict:
        kwargs = dict(
            trading_symbol=trading_symbol,
            quantity=quantity,
            validity=self.client.VALIDITY_DAY,
            exchange=self.client.EXCHANGE_NSE,
            segment=self.client.SEGMENT_FNO,
            product=self.client.PRODUCT_MIS,
            order_type=self.client.ORDER_TYPE_MARKET,
            transaction_type=(
                self.client.TRANSACTION_TYPE_BUY
                if transaction_type == "BUY"
                else self.client.TRANSACTION_TYPE_SELL
            ),
        )
        if order_reference_id:
            kwargs["order_reference_id"] = order_reference_id

        logger.info("Placing %s order: %s x%s", transaction_type, trading_symbol, quantity)
        return self.client.place_order(**kwargs)

    def get_fill_price(self, groww_order_id: str, retries: int = 6, delay: float = 1.0) -> float:
        """Polls order detail until an average_fill_price is available
        (market orders on FNO fill almost immediately during market hours)."""
        for attempt in range(retries):
            detail = self.client.get_order_detail(
                groww_order_id=groww_order_id,
                segment=self.client.SEGMENT_FNO,
            )
            price = detail.get("average_fill_price") or 0
            if price:
                return float(price)
            time.sleep(delay)
        # Fall back to LTP if fill price still isn't populated
        logger.warning(
            "average_fill_price unavailable for %s after %s retries; caller should fall back to LTP",
            groww_order_id, retries,
        )
        return 0.0


groww_wrapper = GrowwWrapper()
