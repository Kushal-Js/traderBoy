from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from config import IST, Settings
from groww_client import GrowwClient
from instruments import InstrumentCache
from models import (
    ChartinkStock,
    TradeState,
    TradeStatus,
)
from state_store import StateStore
from strategy import select_atm_option


logger = logging.getLogger(__name__)


def make_reference_id() -> str:
    """
    Creates a unique client-side order reference.

    Groww accepts an optional user-defined order reference ID.
    """
    suffix = uuid.uuid4().hex[:12].upper()

    return f"TRB-{suffix}"


class OrderManager:
    def __init__(
        self,
        settings: Settings,
        client: GrowwClient,
        instruments: InstrumentCache,
        state: StateStore,
    ) -> None:
        self.settings = settings
        self.client = client
        self.instruments = instruments
        self.state = state
        self.entry_lock = asyncio.Lock()

    def now_iso(self) -> str:
        return datetime.now(IST).isoformat()

    def active_count(self) -> int:
        return len(
            self.state.active_trades()
        )

    def build_order(
        self,
        *,
        symbol: str,
        quantity: int,
        transaction_type: str,
        order_type: str,
        price: float,
        exchange: str,
        segment: str,
        product: str,
    ) -> dict[str, Any]:
        """
        Build one common Groww order payload.

        The exchange, segment, and product are passed explicitly
        because options and equity use different values.
        """
        if quantity <= 0:
            raise ValueError(
                "Order quantity must be positive"
            )

        normalized_transaction = (
            transaction_type.strip().upper()
        )

        if normalized_transaction not in {
            "BUY",
            "SELL",
        }:
            raise ValueError(
                "transaction_type must be BUY or SELL"
            )

        normalized_order_type = (
            order_type.strip().upper()
        )

        if normalized_order_type not in {
            "MARKET",
            "LIMIT",
            "SL",
            "SL-M",
        }:
            raise ValueError(
                "Unsupported order_type"
            )

        return {
            "trading_symbol": symbol.strip().upper(),
            "quantity": int(quantity),
            "price": (
                round(price, 2)
                if normalized_order_type
                != "MARKET"
                else 0
            ),
            "trigger_price": 0,
            "validity": "DAY",
            "exchange": exchange.strip().upper(),
            "segment": segment.strip().upper(),
            "product": product.strip().upper(),
            "order_type": normalized_order_type,
            "transaction_type": normalized_transaction,
            "order_reference_id": (
                make_reference_id()
            ),
        }

    async def create_order(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Submit an order or return a dry-run response.
        """
        logger.info(
            "Order payload | "
            "symbol=%s | quantity=%s | "
            "exchange=%s | segment=%s | "
            "product=%s | order_type=%s | "
            "transaction_type=%s | live=%s",
            body.get("trading_symbol"),
            body.get("quantity"),
            body.get("exchange"),
            body.get("segment"),
            body.get("product"),
            body.get("order_type"),
            body.get("transaction_type"),
            self.settings.live_trading,
        )

        if not self.settings.live_trading:
            return {
                **body,
                "status": "DRY_RUN",
                "order_status": "DRY_RUN",
                "groww_order_id": "DRY_RUN",
            }

        response = await self.client.create_order(
            body
        )

        if not isinstance(response, dict):
            raise RuntimeError(
                "Groww order response is not a JSON object"
            )

        return response

    async def submit_equity_market_order(
        self,
        *,
        symbol: str,
        quantity: int,
        transaction_type: str,
    ) -> dict[str, Any]:
        body = self.build_order(
            symbol=symbol,
            quantity=quantity,
            transaction_type=transaction_type,
            order_type="MARKET",
            price=0,
            exchange=self.settings.equity_exchange,
            segment=self.settings.equity_segment,
            product=self.settings.equity_product,
        )

        return await self.create_order(body)

    async def submit_option_market_order(
        self,
        *,
        symbol: str,
        quantity: int,
        transaction_type: str,
    ) -> dict[str, Any]:
        body = self.build_order(
            symbol=symbol,
            quantity=quantity,
            transaction_type=transaction_type,
            order_type="MARKET",
            price=0,
            exchange=self.settings.option_exchange,
            segment=self.settings.option_segment,
            product=self.settings.option_product,
        )

        return await self.create_order(body)

    async def submit_option_limit_order(
        self,
        *,
        symbol: str,
        quantity: int,
        transaction_type: str,
        price: float,
    ) -> dict[str, Any]:
        body = self.build_order(
            symbol=symbol,
            quantity=quantity,
            transaction_type=transaction_type,
            order_type="LIMIT",
            price=price,
            exchange=self.settings.option_exchange,
            segment=self.settings.option_segment,
            product=self.settings.option_product,
        )

        return await self.create_order(body)

    async def submit_amo_limit_order(
        self,
        *,
        symbol: str,
        quantity: int,
        limit_price: float,
    ) -> dict[str, Any]:
        """
        Build and submit an option AMO limit order.

        The exact AMO behavior depends on Groww's current API
        and account rules.
        """
        body = self.build_order(
            symbol=symbol,
            quantity=quantity,
            transaction_type="BUY",
            order_type="LIMIT",
            price=limit_price,
            exchange=self.settings.option_exchange,
            segment=self.settings.option_segment,
            product=self.settings.amo_product,
        )

        response = await self.create_order(body)

        if response.get("status") == "DRY_RUN":
            response.setdefault(
                "amo_status",
                "DRY_RUN",
            )

        return response

    async def enter_equity_trade(
        self,
        stock: ChartinkStock,
    ) -> TradeState:
        async with self.entry_lock:
            self.state.reset_if_new_day()

            if (
                self.active_count()
                >= self.settings.max_active_trades
            ):
                raise RuntimeError(
                    "Maximum active/pending trade "
                    "limit already reached"
                )

            if self.state.has_reserved_or_active(
                stock.symbol
            ):
                raise RuntimeError(
                    f"{stock.symbol} already has a "
                    "pending or active trade"
                )

            quantity = self.settings.equity_quantity

            current_price = (
                await self.client.get_ltp(
                    symbol=stock.symbol,
                    segment=(
                        self.settings.equity_segment
                    ),
                    exchange=(
                        self.settings.equity_exchange
                    ),
                )
            )

            reserved = TradeState(
                trade_date=self.state.today_key(),
                underlying=stock.symbol,
                option_symbol=stock.symbol,
                option_type="",
                expiry_date="",
                quantity=quantity,
                status=TradeStatus.RESERVED,
                instrument_type="EQUITY",
                exchange=(
                    self.settings.equity_exchange
                ),
                segment=(
                    self.settings.equity_segment
                ),
                product=(
                    self.settings.equity_product
                ),
                current_price=current_price,
                updated_at=self.now_iso(),
            )

            self.state.put_trade(reserved)
            await self.state.save()

            try:
                response = (
                    await self.submit_equity_market_order(
                        symbol=stock.symbol,
                        quantity=quantity,
                        transaction_type="BUY",
                    )
                )

                order_id = self.extract_order_id(
                    response
                )

                reserved.entry_order_id = order_id
                reserved.entry_order_type = "MARKET"
                reserved.status = (
                    TradeStatus.ENTRY_PENDING
                )
                reserved.updated_at = self.now_iso()

                self.state.put_trade(reserved)
                await self.state.save()

                entry_price = (
                    await self.wait_for_execution_price(
                        order_id=order_id,
                        symbol=stock.symbol,
                        segment=(
                            self.settings.equity_segment
                        ),
                        exchange=(
                            self.settings.equity_exchange
                        ),
                    )
                )

                self.initialize_open_trade(
                    trade=reserved,
                    entry_price=entry_price,
                )

                self.state.put_trade(reserved)
                await self.state.save()

                return reserved

            except Exception as exc:
                await self.mark_failed(
                    trade=reserved,
                    error=exc,
                )
                raise

    async def enter_trade(
        self,
        stock: ChartinkStock,
    ) -> TradeState:
        """
        Enter a regular ATM option trade.
        """
        async with self.entry_lock:
            self.state.reset_if_new_day()

            if (
                self.active_count()
                >= self.settings.max_active_trades
            ):
                raise RuntimeError(
                    "Maximum active trade limit reached"
                )

            if self.state.has_reserved_or_active(
                stock.symbol
            ):
                raise RuntimeError(
                    f"{stock.symbol} already has a "
                    "pending or active trade"
                )

            (
                option_symbol,
                option_ltp,
                expiry,
            ) = await select_atm_option(
                client=self.client,
                settings=self.settings,
                underlying=stock.symbol,
            )

            quantity = await self.instruments.lot_size(
                option_symbol
            )

            reserved = TradeState(
                trade_date=self.state.today_key(),
                underlying=stock.symbol,
                option_symbol=option_symbol,
                option_type=(
                    self.settings.option_type
                ),
                expiry_date=expiry,
                quantity=quantity,
                status=TradeStatus.RESERVED,
                instrument_type="OPTION",
                exchange=(
                    self.settings.option_exchange
                ),
                segment=(
                    self.settings.option_segment
                ),
                product=(
                    self.settings.option_product
                ),
                current_price=option_ltp,
                updated_at=self.now_iso(),
            )

            self.state.put_trade(reserved)
            await self.state.save()

            try:
                response = (
                    await self.submit_option_market_order(
                        symbol=option_symbol,
                        quantity=quantity,
                        transaction_type="BUY",
                    )
                )

                order_id = self.extract_order_id(
                    response
                )

                reserved.entry_order_id = order_id
                reserved.entry_order_type = "MARKET"
                reserved.status = (
                    TradeStatus.ENTRY_PENDING
                )
                reserved.updated_at = self.now_iso()

                self.state.put_trade(reserved)
                await self.state.save()

                entry_price = (
                    await self.wait_for_execution_price(
                        order_id=order_id,
                        symbol=option_symbol,
                        segment=(
                            self.settings.option_segment
                        ),
                        exchange=(
                            self.settings.option_exchange
                        ),
                    )
                )

                self.initialize_open_trade(
                    trade=reserved,
                    entry_price=entry_price,
                )

                self.state.put_trade(reserved)
                await self.state.save()

                return reserved

            except Exception as exc:
                await self.mark_failed(
                    trade=reserved,
                    error=exc,
                )
                raise

    async def enter_amo_trade(
        self,
        stock: ChartinkStock,
    ) -> TradeState:
        """
        Enter an option AMO limit trade.
        """
        async with self.entry_lock:
            self.state.reset_if_new_day()

            if not self.settings.amo_enabled:
                raise RuntimeError(
                    "AMO_ENABLED=false"
                )

            if (
                self.active_count()
                >= self.settings.max_active_trades
            ):
                raise RuntimeError(
                    "Maximum active/pending trade "
                    "limit already reached"
                )

            if self.state.has_reserved_or_active(
                stock.symbol
            ):
                raise RuntimeError(
                    f"{stock.symbol} already has a "
                    "pending or active trade"
                )

            (
                option_symbol,
                option_ltp,
                expiry,
            ) = await select_atm_option(
                client=self.client,
                settings=self.settings,
                underlying=stock.symbol,
            )

            quantity = await self.instruments.lot_size(
                option_symbol
            )

            limit_price = round(
                option_ltp
                * (
                    1
                    + self.settings
                    .amo_price_buffer_percent
                    / 100
                ),
                2,
            )

            reserved = TradeState(
                trade_date=self.state.today_key(),
                underlying=stock.symbol,
                option_symbol=option_symbol,
                option_type=(
                    self.settings.option_type
                ),
                expiry_date=expiry,
                quantity=quantity,
                status=TradeStatus.RESERVED,
                instrument_type="OPTION",
                exchange=(
                    self.settings.option_exchange
                ),
                segment=(
                    self.settings.option_segment
                ),
                product=(
                    self.settings.amo_product
                ),
                entry_order_type="AMO_LIMIT",
                entry_amo_status="PENDING",
                current_price=option_ltp,
                updated_at=self.now_iso(),
            )

            self.state.put_trade(reserved)
            await self.state.save()

            try:
                response = (
                    await self.submit_amo_limit_order(
                        symbol=option_symbol,
                        quantity=quantity,
                        limit_price=limit_price,
                    )
                )

                order_id = self.extract_order_id(
                    response
                )

                reserved.entry_order_id = order_id
                reserved.entry_order_type = (
                    "AMO_LIMIT"
                )
                reserved.entry_amo_status = (
                    response.get("amo_status")
                    or (
                        "DRY_RUN"
                        if order_id == "DRY_RUN"
                        else "PENDING"
                    )
                )
                reserved.status = (
                    TradeStatus.ENTRY_PENDING
                )
                reserved.updated_at = self.now_iso()

                self.state.put_trade(reserved)
                await self.state.save()

                logger.info(
                    "AMO order submitted | "
                    "underlying=%s | option=%s | "
                    "quantity=%d | limit_price=%.2f | "
                    "order_id=%s",
                    stock.symbol,
                    option_symbol,
                    quantity,
                    limit_price,
                    order_id,
                )

                return reserved

            except Exception as exc:
                await self.mark_failed(
                    trade=reserved,
                    error=exc,
                )
                raise

    async def wait_for_execution_price(
        self,
        *,
        order_id: str,
        symbol: str,
        segment: str,
        exchange: str,
    ) -> float:
        """
        Wait for a live order to execute and calculate
        the average fill price.

        Dry-run orders use the current LTP.
        """
        if (
            not self.settings.live_trading
            or order_id == "DRY_RUN"
        ):
            return await self.client.get_ltp(
                symbol=symbol,
                segment=segment,
                exchange=exchange,
            )

        failure_states = {
            "REJECTED",
            "FAILED",
            "CANCELLED",
        }

        success_states = {
            "EXECUTED",
            "COMPLETED",
            "TRADED",
        }

        started = (
            asyncio.get_running_loop().time()
        )

        while True:
            status = (
                await self.client.get_order_status(
                    order_id=order_id,
                    segment=segment,
                )
            )

            order_status = str(
                status.get(
                    "order_status",
                    status.get(
                        "status",
                        "",
                    ),
                )
            ).upper()

            logger.info(
                "Execution poll | "
                "order_id=%s | "
                "segment=%s | "
                "status=%s | "
                "remark=%s",
                order_id,
                segment,
                order_status,
                status.get("remark"),
            )

            if order_status in failure_states:
                raise RuntimeError(
                    f"Order failed: {status}"
                )

            if order_status in success_states:
                break

            elapsed = (
                asyncio.get_running_loop().time()
                - started
            )

            if (
                elapsed
                >= self.settings
                .order_poll_timeout_seconds
            ):
                raise RuntimeError(
                    "Order still pending after "
                    f"{elapsed:.0f}s: {status}"
                )

            await asyncio.sleep(
                self.settings
                .order_poll_interval_seconds
            )

        trades = (
            await self.client.get_order_trades(
                order_id=order_id,
                segment=segment,
            )
        )

        return self.calculate_average_fill_price(
            trades
        )

    async def submit_exit(
        self,
        trade: TradeState,
        reason: str,
    ) -> None:
        """
        Submit a market sell for either an option or
        equity trade.
        """
        if trade.status != TradeStatus.OPEN:
            return

        body = self.build_order(
            symbol=trade.option_symbol,
            quantity=trade.quantity,
            transaction_type="SELL",
            order_type="MARKET",
            price=0,
            exchange=trade.exchange,
            segment=trade.segment,
            product=trade.product,
        )

        logger.info(
            "Exit order | "
            "symbol=%s | quantity=%d | "
            "exchange=%s | segment=%s | "
            "product=%s | reason=%s",
            trade.option_symbol,
            trade.quantity,
            trade.exchange,
            trade.segment,
            trade.product,
            reason,
        )

        response = await self.create_order(body)

        order_id = self.extract_order_id(
            response
        )

        trade.status = TradeStatus.EXIT_PENDING
        trade.exit_order_id = order_id
        trade.exit_reason = reason
        trade.updated_at = self.now_iso()

        self.state.put_trade(trade)
        await self.state.save()

    def initialize_open_trade(
        self,
        *,
        trade: TradeState,
        entry_price: float,
    ) -> None:
        if entry_price <= 0:
            raise RuntimeError(
                "Entry price must be positive"
            )

        trade.status = TradeStatus.OPEN
        trade.entry_price = entry_price
        trade.current_price = entry_price
        trade.highest_price = entry_price
        trade.stop_price = (
            entry_price
            * (
                1
                - self.settings.stop_loss_percent
                / 100
            )
        )
        trade.target_price = (
            entry_price
            * (
                1
                + self.settings.target_percent
                / 100
            )
        )
        trade.last_error = None
        trade.updated_at = self.now_iso()

    async def mark_failed(
        self,
        *,
        trade: TradeState,
        error: Exception,
    ) -> None:
        trade.status = TradeStatus.FAILED
        trade.last_error = str(error)
        trade.updated_at = self.now_iso()

        self.state.put_trade(trade)
        await self.state.save()

    @staticmethod
    def extract_order_id(
        response: dict[str, Any],
    ) -> str:
        candidates = (
            response.get("groww_order_id"),
            response.get("order_id"),
            response.get("orderId"),
            response.get("id"),
        )

        for candidate in candidates:
            if candidate is not None:
                value = str(candidate).strip()

                if value:
                    return value

        raise RuntimeError(
            "Order response does not contain an "
            f"order ID: {response}"
        )

    @staticmethod
    def calculate_average_fill_price(
        trades: Any,
    ) -> float:
        """
        Calculate weighted average fill price.

        Supports common response shapes:
          - list[dict]
          - {"trades": [...]}
          - {"data": [...]}
        """
        if isinstance(trades, dict):
            raw_items = (
                trades.get("trades")
                or trades.get("data")
                or trades.get("results")
                or []
            )
        else:
            raw_items = trades

        if not isinstance(raw_items, list):
            raise RuntimeError(
                "Unexpected trade-list response: "
                f"{trades}"
            )

        total_quantity = 0
        total_value = 0.0

        for item in raw_items:
            if not isinstance(item, dict):
                continue

            price_value = (
                item.get("price")
                or item.get("trade_price")
                or item.get("fill_price")
                or item.get("average_price")
            )

            quantity_value = (
                item.get("quantity")
                or item.get("trade_quantity")
                or item.get("filled_quantity")
            )

            if (
                price_value is None
                or quantity_value is None
            ):
                continue

            try:
                price = float(price_value)
                quantity = int(quantity_value)
            except (
                TypeError,
                ValueError,
            ):
                continue

            if price <= 0 or quantity <= 0:
                continue

            total_quantity += quantity
            total_value += price * quantity

        if total_quantity <= 0:
            raise RuntimeError(
                "No valid fills found in trade response: "
                f"{trades}"
            )

        return round(
            total_value / total_quantity,
            4,
        )