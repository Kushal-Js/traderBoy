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
from growwapi import GrowwAPI, GrowwFeed
from growwapi.groww.exceptions import GrowwFeedNotSubscribedException

import config

logger = logging.getLogger("groww_client")


class OrderStatus:
    """Order status values, verbatim from Groww's API docs (Annexures ->
    Order Status): https://groww.in/trade-api/docs/python-sdk/annexures"""
    NEW = "NEW"
    ACKED = "ACKED"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    EXECUTED = "EXECUTED"
    DELIVERY_AWAITED = "DELIVERY_AWAITED"
    CANCELLED = "CANCELLED"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"
    MODIFICATION_REQUESTED = "MODIFICATION_REQUESTED"
    COMPLETED = "COMPLETED"

    # The order failed to result in a trade.
    REJECTED_STATUSES = frozenset({REJECTED, FAILED})
    # No further status change is expected.
    TERMINAL_STATUSES = frozenset({REJECTED, FAILED, CANCELLED, COMPLETED, EXECUTED})
    # Still live / working at the exchange.
    OPEN_STATUSES = frozenset({
        NEW, ACKED, TRIGGER_PENDING, APPROVED, DELIVERY_AWAITED,
        CANCELLATION_REQUESTED, MODIFICATION_REQUESTED,
    })


@dataclass
class OrderResult:
    groww_order_id: str
    status: str          # one of OrderStatus.*
    remark: str
    fill_price: float
    filled_quantity: int


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
        self._feed: Optional[GrowwFeed] = None
        # groww_order_id -> latest order-update dict pushed over the socket
        self._order_updates: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def authenticate(self) -> None:
        if config.AUTH_MODE == "TOKEN":
            access_token = config.GROWW_ACCESS_TOKEN
            if not access_token:
                raise ValueError("GROWW_ACCESS_TOKEN is not set")
        elif config.AUTH_MODE == "SECRET":
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
    # Live feed (WebSocket)
    # ------------------------------------------------------------------ #
    @property
    def feed(self) -> GrowwFeed:
        if self._feed is None:
            self._feed = GrowwFeed(self.client)
            self._feed.subscribe_fno_order_updates(self._on_fno_order_update)
            logger.info("Groww WebSocket feed connected; subscribed to FNO order updates.")
        return self._feed

    def start_feed(self) -> None:
        """Eagerly opens the socket connection (otherwise it lazily opens on
        first use). Call once at app startup."""
        if not config.ENABLE_WS_FEED:
            logger.info("WebSocket feed disabled (ENABLE_WS_FEED=false); running REST-only.")
            return
        _ = self.feed

    def _on_fno_order_update(self, _meta: Optional[dict] = None) -> None:
        update = self.feed.get_fno_order_update()
        if update and update.get("growwOrderId"):
            self._order_updates[update["growwOrderId"]] = update

    def _exchange_token(self, trading_symbol: str) -> str:
        df = self.instruments()
        row = df[df["trading_symbol"] == trading_symbol]
        if row.empty:
            raise ValueError(f"No instrument found for trading_symbol {trading_symbol}")
        return str(row.iloc[0]["exchange_token"])

    def subscribe_option_price(self, trading_symbol: str) -> None:
        if not config.ENABLE_WS_FEED:
            return
        token = self._exchange_token(trading_symbol)
        self.feed.subscribe_ltp([{
            "exchange": self.client.EXCHANGE_NSE,
            "segment": self.client.SEGMENT_FNO,
            "exchange_token": token,
        }])

    def unsubscribe_option_price(self, trading_symbol: str) -> None:
        if not config.ENABLE_WS_FEED:
            return
        token = self._exchange_token(trading_symbol)
        self.feed.unsubscribe_ltp([{
            "exchange": self.client.EXCHANGE_NSE,
            "segment": self.client.SEGMENT_FNO,
            "exchange_token": token,
        }])

    def get_cached_option_ltp(self, trading_symbol: str) -> Optional[float]:
        """Returns the last price pushed over the WebSocket feed for this
        option, or None if not subscribed yet / no tick has arrived yet /
        the feed is disabled (ENABLE_WS_FEED=false)."""
        if not config.ENABLE_WS_FEED:
            return None
        token = self._exchange_token(trading_symbol)
        try:
            data = self.feed.get_ltp()
        except GrowwFeedNotSubscribedException:
            return None
        leg = data.get(self.client.EXCHANGE_NSE, {}).get(self.client.SEGMENT_FNO, {}).get(token)
        if not leg:
            return None
        return float(leg["ltp"])

    def _order_snapshot_from_cache(self, groww_order_id: str) -> Optional[dict]:
        """Normalizes a socket order-update push to the same shape as a REST
        get_order_detail() response, or None if nothing has arrived yet."""
        update = self._order_updates.get(groww_order_id)
        if not update:
            return None
        return {
            "order_status": update.get("orderStatus", ""),
            "remark": update.get("remark", ""),
            "average_fill_price": update.get("avgFillPrice") or 0,
            "filled_quantity": update.get("filledQty") or 0,
        }

    def _order_snapshot_from_rest(self, groww_order_id: str) -> dict:
        detail = self.client.get_order_detail(
            groww_order_id=groww_order_id,
            segment=self.client.SEGMENT_FNO,
        )
        return {
            "order_status": detail.get("order_status", ""),
            "remark": detail.get("remark", ""),
            "average_fill_price": detail.get("average_fill_price") or 0,
            "filled_quantity": detail.get("filled_quantity") or 0,
        }

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
    # Portfolio (positions already open at the broker)
    # ------------------------------------------------------------------ #
    def get_open_fno_positions(self) -> list[dict]:
        """Every FNO position currently open at Groww (net quantity != 0),
        normalized with underlying_symbol/option_type/lot_size looked up
        from the instruments master. avg_price is Groww's own reported
        average price for the position (credit_price, falling back to
        net_price), not something we computed ourselves."""
        resp = self.client.get_positions_for_user(segment=self.client.SEGMENT_FNO)
        raw_positions = resp.get("positions") or []
        df = self.instruments()

        open_positions = []
        for p in raw_positions:
            quantity = int(p.get("quantity") or 0)
            if quantity == 0:
                continue

            trading_symbol = p.get("trading_symbol", "")
            row = df[df["trading_symbol"] == trading_symbol]
            if row.empty:
                logger.warning(
                    "Open broker position %s not found in instruments master; "
                    "skipping it for reconciliation.", trading_symbol,
                )
                continue

            open_positions.append({
                "trading_symbol": trading_symbol,
                "underlying_symbol": str(row.iloc[0]["underlying_symbol"]),
                "option_type": str(row.iloc[0]["instrument_type"]),
                "lot_size": int(row.iloc[0]["lot_size"]),
                "quantity": quantity,
                "avg_price": float(p.get("credit_price") or p.get("net_price") or 0),
            })
        return open_positions

    def has_open_position_for_underlying(self, underlying_symbol: str) -> bool:
        """Broker-side check (in addition to our own local dedup) that
        there's no existing open FNO position for this underlying - guards
        against duplicate entries from another process instance, a manual
        trade, or state we haven't reconciled yet."""
        return any(
            p["underlying_symbol"] == underlying_symbol
            for p in self.get_open_fno_positions()
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

    def wait_for_order_result(
        self, groww_order_id: str, retries: int = 6, delay: float = 1.0
    ) -> OrderResult:
        """Polls (WebSocket order-updates cache first, then REST
        get_order_detail) until the order reaches a terminal status - see
        OrderStatus.TERMINAL_STATUSES - or retries are exhausted. Market
        orders on FNO settle almost immediately during market hours, so a
        terminal status is expected well within the default retry budget.

        Always returns whatever the last-seen status was; callers MUST check
        `.status` (e.g. against OrderStatus.REJECTED_STATUSES) rather than
        assuming the order filled just because this returned."""
        snapshot: dict = {}
        for attempt in range(retries):
            cached = self._order_snapshot_from_cache(groww_order_id)
            if cached and cached["order_status"] in OrderStatus.TERMINAL_STATUSES:
                snapshot = cached
                break
            snapshot = self._order_snapshot_from_rest(groww_order_id)
            if snapshot["order_status"] in OrderStatus.TERMINAL_STATUSES:
                break
            time.sleep(delay)
        else:
            logger.warning(
                "Order %s still not in a terminal status after %s retries (last status=%s)",
                groww_order_id, retries, snapshot.get("order_status"),
            )

        return OrderResult(
            groww_order_id=groww_order_id,
            status=snapshot.get("order_status") or OrderStatus.NEW,
            remark=snapshot.get("remark", ""),
            fill_price=float(snapshot.get("average_fill_price") or 0),
            filled_quantity=int(snapshot.get("filled_quantity") or 0),
        )


groww_wrapper = GrowwWrapper()
