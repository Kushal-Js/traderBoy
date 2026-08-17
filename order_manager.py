from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from config import IST, Settings
from groww_client import GrowwClient, first_value
from instruments import InstrumentCache
from models import (
    ChartinkStock,
    TradeState,
    TradeStatus,
)
from state_store import StateStore
from strategy import select_atm_option
import logging

logger = logging.getLogger(__name__)


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
        return len(self.state.active_trades())

    def build_order(
        self,
        *,
        symbol: str,
        quantity: int,
        transaction_type: str,
        order_type: str,
        price: float,
        product: str,
    ) -> dict[str, Any]:
        return {
            "trading_symbol": symbol,
            "quantity": quantity,
            "price": round(price, 2),
            "trigger_price": 0,
            "validity": "DAY",
            "exchange": self.settings.option_exchange,
            "segment": self.settings.option_segment,
            "product": product,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "order_reference_id": (
                f"GRW{datetime.now(IST):%H%M%S}"
            ),
        }

    async def submit_market_order(
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
            product=self.settings.option_product,
        )

        if not self.settings.live_trading:
            return {
                "status": "DRY_RUN",
                "order_status": "DRY_RUN",
                "groww_order_id": "DRY_RUN",
                **body,
            }

        return await self.client.create_order(body)

    async def submit_limit_order(
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
            product=self.settings.option_product,
        )

        if not self.settings.live_trading:
            return {
                "status": "DRY_RUN",
                "order_status": "DRY_RUN",
                "groww_order_id": "DRY_RUN",
                **body,
            }

        return await self.client.create_order(body)

    async def wait_for_execution_price(
        self,
        order_id: str,
        symbol: str,
    ) -> float:
        if (
            not self.settings.live_trading
            or order_id == "DRY_RUN"
        ):
            return await self.client.get_ltp(
                symbol=symbol,
                segment=self.settings.option_segment,
                exchange=self.settings.option_exchange,
            )

        failure_states = {
            "REJECTED",
            "FAILED",
            "CANCELLED",
        }

        success_states = {
            "EXECUTED",
            "COMPLETED",
        }

        started = asyncio.get_running_loop().time()

        while True:
            status = await self.client.get_order_status(
                order_id=order_id,
                segment=self.settings.option_segment,
            )

            order_status = str(
                status.get(
                    "order_status",
                    status.get("status", ""),
                )
            ).upper()

            logger_message = (
                "Order status | id=%s | status=%s | "
                "remark=%s"
            )

            # Avoid importing application logger here.
            print(
                logger_message
                % (
                    order_id,
                    order_status,
                    status.get("remark"),
                )
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
                >= self.settings.order_poll_timeout_seconds
            ):
                raise RuntimeError(
                    f"Order still pending after "
                    f"{elapsed:.0f}s: {status}"
                )

            await asyncio.sleep(
                self.settings.order_poll_interval_seconds
            )

        trades = await self.client.get_order_trades(
            order_id=order_id,
            segment=self.settings.option_segment,
        )

        total_qty = 0
        total_value = 0.0

        for trade in trades:
            price = first_value(
                trade,
                (
                    "price",
                    "trade_price",
                    "average_price",
                ),
            )

            quantity = first_value(
                trade,
                (
                    "quantity",
                    "filled_quantity",
                    "qty",
                ),
            )

            if price is None or quantity is None:
                continue

            price = float(price)
            quantity = int(quantity)

            if price <= 0 or quantity <= 0:
                continue

            total_qty += quantity
            total_value += price * quantity

        if total_qty <= 0:
            raise RuntimeError(
                f"No fills found for executed order "
                f"{order_id}: {trades}"
            )

        return total_value / total_qty

    async def submit_amo_limit_order(
        self,
        *,
        symbol: str,
        quantity: int,
        limit_price: float,
    ) -> dict[str, Any]:
        if limit_price <= 0:
            raise ValueError(
                "AMO limit price must be positive"
            )

        body = self.build_order(
            symbol=symbol,
            quantity=quantity,
            transaction_type="BUY",
            order_type="LIMIT",
            price=limit_price,
            product=self.settings.amo_product,
        )

        if not self.settings.live_trading:
            return {
                "status": "DRY_RUN",
                "order_status": "DRY_RUN",
                "amo_status": "DRY_RUN",
                "groww_order_id": "DRY_RUN",
                **body,
            }

        response = await self.client.create_order(
            body
        )

        logger.info(
            "AMO response | symbol=%s | order_id=%s | "
            "order_status=%s | amo_status=%s | "
            "remark=%s",
            symbol,
            response.get("groww_order_id"),
            response.get("order_status"),
            response.get("amo_status"),
            response.get("remark"),
        )

        order_status = str(
            response.get(
                "order_status",
                "",
            )
        ).upper()

        amo_status = str(
            response.get(
                "amo_status",
                "",
            )
        ).upper()

        if order_status in {
            "REJECTED",
            "FAILED",
            "CANCELLED",
        }:
            raise RuntimeError(
                f"AMO order failed: {response}"
            )

        if amo_status == "FAILED":
            raise RuntimeError(
                f"AMO processing failed: {response}"
            )

        return response


    async def enter_amo_trade(
        self,
        stock: ChartinkStock,
    ) -> TradeState:
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
                    "Maximum active/pending trade limit "
                    "already reached"
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

            # For a BUY limit AMO, a small positive buffer can
            # improve execution probability. It does not guarantee
            # execution.
            limit_price = round(
                option_ltp
                * (
                    1
                    + self.settings.amo_price_buffer_percent
                    / 100
                ),
                2,
            )

            reserved = TradeState(
                trade_date=self.state.today_key(),
                underlying=stock.symbol,
                option_symbol=option_symbol,
                option_type=self.settings.option_type,
                expiry_date=expiry,
                quantity=quantity,
                status=TradeStatus.RESERVED,
                entry_order_type="AMO_LIMIT",
                entry_amo_status="PENDING",
                current_price=option_ltp,
                updated_at=self.now_iso(),
            )

            # Reserve before submitting the order. This protects
            # against duplicate Chartink requests.
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

                order_id = str(
                    response.get(
                        "groww_order_id",
                        "DRY_RUN",
                    )
                )

                reserved.entry_order_id = order_id
                reserved.entry_order_type = (
                    "AMO_LIMIT"
                )
                reserved.entry_amo_status = (
                    response.get("amo_status")
                    or "PENDING"
                )
                reserved.status = (
                    TradeStatus.ENTRY_PENDING
                )
                reserved.last_error = None
                reserved.updated_at = self.now_iso()

                self.state.put_trade(reserved)
                await self.state.save()

                logger.info(
                    "AMO reserved | underlying=%s | "
                    "option=%s | quantity=%d | "
                    "limit_price=%.2f | order_id=%s",
                    stock.symbol,
                    option_symbol,
                    quantity,
                    limit_price,
                    order_id,
                )

                return reserved

            except Exception as exc:
                reserved.status = TradeStatus.FAILED
                reserved.last_error = str(exc)
                reserved.updated_at = self.now_iso()

                self.state.put_trade(reserved)
                await self.state.save()

                raise

            
    async def enter_trade(
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
                    "Maximum active trade limit reached"
                )

            if self.state.has_reserved_or_active(
                stock.symbol
            ):
                raise RuntimeError(
                    f"{stock.symbol} already has a "
                    "pending or active trade"
                )

            option_symbol, option_ltp, expiry = (
                await select_atm_option(
                    client=self.client,
                    settings=self.settings,
                    underlying=stock.symbol,
                )
            )

            quantity = await self.instruments.lot_size(
                option_symbol
            )

            reserved = TradeState(
                trade_date=self.state.today_key(),
                underlying=stock.symbol,
                option_symbol=option_symbol,
                option_type=self.settings.option_type,
                expiry_date=expiry,
                quantity=quantity,
                status=TradeStatus.RESERVED,
                current_price=option_ltp,
                updated_at=self.now_iso(),
            )

            self.state.put_trade(reserved)
            await self.state.save()

            try:
                response = (
                    await self.submit_market_order(
                        symbol=option_symbol,
                        quantity=quantity,
                        transaction_type="BUY",
                    )
                )

                order_id = str(
                    response.get(
                        "groww_order_id",
                        "DRY_RUN",
                    )
                )

                reserved.entry_order_id = order_id
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
                    )
                )

                reserved.status = TradeStatus.OPEN
                reserved.entry_price = entry_price
                reserved.current_price = entry_price
                reserved.highest_price = entry_price
                reserved.stop_price = (
                    entry_price
                    * (
                        1
                        - self.settings.stop_loss_percent
                        / 100
                    )
                )
                reserved.target_price = (
                    entry_price
                    * (
                        1
                        + self.settings.target_percent
                        / 100
                    )
                )
                reserved.updated_at = self.now_iso()

                self.state.put_trade(reserved)
                await self.state.save()

                return reserved

            except Exception as exc:
                reserved.status = TradeStatus.FAILED
                reserved.last_error = str(exc)
                reserved.updated_at = self.now_iso()

                self.state.put_trade(reserved)
                await self.state.save()

                raise

    async def submit_exit(
        self,
        trade: TradeState,
        reason: str,
    ) -> None:
        if trade.status != TradeStatus.OPEN:
            return

        response = await self.submit_market_order(
            symbol=trade.option_symbol,
            quantity=trade.quantity,
            transaction_type="SELL",
        )

        order_id = str(
            response.get(
                "groww_order_id",
                "DRY_RUN",
            )
        )

        trade.status = TradeStatus.EXIT_PENDING
        trade.exit_order_id = order_id
        trade.exit_reason = reason
        trade.updated_at = self.now_iso()

        self.state.put_trade(trade)
        await self.state.save()