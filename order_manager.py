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
    Create a unique client-side order reference ID.

    The value is kept below 20 characters.
    """
    return (
        "TRB"
        + datetime.now(IST).strftime("%H%M%S")
        + uuid.uuid4().hex[:8].upper()
    )[:20]


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

    # ========================================================
    # Order construction and submission
    # ========================================================

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
        if quantity <= 0:
            raise ValueError(
                "Order quantity must be positive"
            )

        normalized_symbol = (
            symbol.strip().upper()
        )
        normalized_transaction = (
            transaction_type.strip().upper()
        )
        normalized_order_type = (
            order_type.strip().upper()
        )
        normalized_exchange = (
            exchange.strip().upper()
        )
        normalized_segment = (
            segment.strip().upper()
        )
        normalized_product = (
            product.strip().upper()
        )

        if not normalized_symbol:
            raise ValueError(
                "Order symbol is required"
            )

        if normalized_transaction not in {
            "BUY",
            "SELL",
        }:
            raise ValueError(
                "transaction_type must be BUY "
                "or SELL"
            )

        if normalized_order_type not in {
            "MARKET",
            "LIMIT",
            "SL",
            "SL-M",
        }:
            raise ValueError(
                "Unsupported order_type: "
                f"{normalized_order_type}"
            )

        if not normalized_exchange:
            raise ValueError(
                "exchange is required"
            )

        if not normalized_segment:
            raise ValueError(
                "segment is required"
            )

        if not normalized_product:
            raise ValueError(
                "product is required"
            )

        if (
            normalized_order_type != "MARKET"
            and price <= 0
        ):
            raise ValueError(
                "Price must be positive for "
                f"{normalized_order_type} orders"
            )

        return {
            "trading_symbol": normalized_symbol,
            "quantity": int(quantity),
            "price": (
                0
                if normalized_order_type
                == "MARKET"
                else round(price, 2)
            ),
            "trigger_price": 0,
            "validity": "DAY",
            "exchange": normalized_exchange,
            "segment": normalized_segment,
            "product": normalized_product,
            "order_type": normalized_order_type,
            "transaction_type": (
                normalized_transaction
            ),
            "order_reference_id": (
                make_reference_id()
            ),
        }

    async def create_order(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
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
                "Groww order response must be "
                "a JSON object"
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
            exchange=(
                self.settings.equity_exchange
            ),
            segment=(
                self.settings.equity_segment
            ),
            product=(
                self.settings.equity_product
            ),
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
            exchange=(
                self.settings.option_exchange
            ),
            segment=(
                self.settings.option_segment
            ),
            product=(
                self.settings.option_product
            ),
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
            exchange=(
                self.settings.option_exchange
            ),
            segment=(
                self.settings.option_segment
            ),
            product=(
                self.settings.option_product
            ),
        )

        return await self.create_order(body)

    async def submit_amo_limit_order(
        self,
        *,
        symbol: str,
        quantity: int,
        limit_price: float,
    ) -> dict[str, Any]:
        body = self.build_order(
            symbol=symbol,
            quantity=quantity,
            transaction_type="BUY",
            order_type="LIMIT",
            price=limit_price,
            exchange=(
                self.settings.option_exchange
            ),
            segment=(
                self.settings.option_segment
            ),
            product=self.settings.amo_product,
        )

        response = await self.create_order(body)

        if response.get("status") == "DRY_RUN":
            response.setdefault(
                "amo_status",
                "DRY_RUN",
            )

        return response

    # ========================================================
    # Entry methods
    # ========================================================

    async def enter_equity_trade(
        self,
        stock: ChartinkStock,
    ) -> TradeState:
        async with self.entry_lock:
            self.state.reset_if_new_day()

            self.ensure_entry_allowed(
                symbol=stock.symbol
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
                    await self
                    .submit_equity_market_order(
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
                    await self
                    .wait_for_execution_price(
                        order_id=order_id,
                        symbol=stock.symbol,
                        segment=(
                            self.settings
                            .equity_segment
                        ),
                        exchange=(
                            self.settings
                            .equity_exchange
                        ),
                    )
                )

                self.initialize_open_trade(
                    trade=reserved,
                    entry_price=entry_price,
                )

                self.state.put_trade(reserved)
                await self.state.save()

                logger.info(
                    "Equity trade OPEN | "
                    "symbol=%s | quantity=%d | "
                    "entry_price=%.4f | "
                    "product=%s",
                    stock.symbol,
                    quantity,
                    entry_price,
                    reserved.product,
                )

                return reserved

            except Exception as exc:
                await self.reconcile_entry_failure(
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

            self.ensure_entry_allowed(
                symbol=stock.symbol
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
                    await self
                    .submit_option_market_order(
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
                    await self
                    .wait_for_execution_price(
                        order_id=order_id,
                        symbol=option_symbol,
                        segment=(
                            self.settings
                            .option_segment
                        ),
                        exchange=(
                            self.settings
                            .option_exchange
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
                await self.reconcile_entry_failure(
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

            self.ensure_entry_allowed(
                symbol=stock.symbol
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
                    + (
                        self.settings
                        .amo_price_buffer_percent
                        / 100
                    )
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
                    await self
                    .submit_amo_limit_order(
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
                    response.get(
                        "amo_status"
                    )
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
                    "quantity=%d | "
                    "limit_price=%.2f | "
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

    # ========================================================
    # Execution and fill handling
    # ========================================================

    async def wait_for_execution_price(
        self,
        *,
        order_id: str,
        symbol: str,
        segment: str,
        exchange: str,
    ) -> float:
        """
        Wait for execution and retrieve average fill price.

        Groww may report EXECUTED before the separate trades
        endpoint immediately returns fill rows. The trade list
        is therefore retried.
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

        loop = asyncio.get_running_loop()
        started = loop.time()

        while True:
            status = (
                await self.client.get_order_status(
                    order_id=order_id,
                    segment=segment,
                )
            )

            order_status = self.extract_status(
                status
            )

            logger.info(
                "Execution poll | "
                "order_id=%s | segment=%s | "
                "status=%s | remark=%s",
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

            elapsed = loop.time() - started

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

        fill_started = loop.time()
        last_trades: Any = None
        last_status: dict[str, Any] = status

        while True:
            last_trades = (
                await self.client
                .get_order_trades(
                    order_id=order_id,
                    segment=segment,
                )
            )

            trade_count = (
                len(last_trades)
                if isinstance(
                    last_trades,
                    list,
                )
                else 0
            )

            logger.info(
                "Fill poll | "
                "order_id=%s | segment=%s | "
                "trades=%d",
                order_id,
                segment,
                trade_count,
            )

            if last_trades:
                try:
                    return (
                        self
                        .calculate_average_fill_price(
                            last_trades
                        )
                    )

                except RuntimeError as exc:
                    logger.warning(
                        "Fill rows are not usable yet | "
                        "order_id=%s | error=%s",
                        order_id,
                        exc,
                    )

            fill_elapsed = (
                loop.time() - fill_started
            )

            if (
                fill_elapsed
                >= self.settings
                .order_poll_timeout_seconds
            ):
                break

            await asyncio.sleep(
                self.settings
                .order_poll_interval_seconds
            )

        # Fallback 1:
        # The status response may contain a filled quantity
        # and average/filled price even when trade rows are
        # temporarily unavailable.
        status_price = (
            self.extract_fill_price(
                last_status
            )
        )

        if status_price is not None:
            logger.warning(
                "Using order-status fill price | "
                "order_id=%s | price=%.4f",
                order_id,
                status_price,
            )
            return status_price

        # Fallback 2:
        # Use current LTP so an executed broker order is not
        # incorrectly marked FAILED. This is approximate.
        fallback_price = await self.client.get_ltp(
            symbol=symbol,
            segment=segment,
            exchange=exchange,
        )

        logger.error(
            "Executed order has no trade rows or "
            "fill price after %.1fs; using LTP fallback | "
            "order_id=%s | symbol=%s | "
            "fallback_price=%.4f | "
            "last_trades=%s",
            fill_elapsed,
            order_id,
            symbol,
            fallback_price,
            last_trades,
        )

        return fallback_price

    async def reconcile_entry_failure(
        self,
        *,
        trade: TradeState,
        error: Exception,
    ) -> None:
        """
        Do not immediately mark a trade FAILED after an order
        ID has been received. The broker may have executed it
        even if fill retrieval failed.
        """
        trade.last_error = str(error)
        trade.updated_at = self.now_iso()

        if not trade.entry_order_id:
            trade.status = TradeStatus.FAILED

        elif trade.entry_order_id == "DRY_RUN":
            trade.status = TradeStatus.FAILED

        else:
            try:
                broker_status = (
                    await self.client
                    .get_order_status(
                        order_id=(
                            trade.entry_order_id
                        ),
                        segment=trade.segment,
                    )
                )

                status_value = (
                    self.extract_status(
                        broker_status
                    )
                )

                logger.warning(
                    "Entry processing failed; "
                    "broker status checked | "
                    "symbol=%s | order_id=%s | "
                    "status=%s | error=%s",
                    trade.option_symbol,
                    trade.entry_order_id,
                    status_value,
                    error,
                )

                if status_value in {
                    "EXECUTED",
                    "COMPLETED",
                    "TRADED",
                }:
                    trade.status = (
                        TradeStatus.OPEN
                    )

                    # If the fill price could not be obtained,
                    # initialize with current LTP so the tracker
                    # has usable risk values.
                    if not trade.entry_price:
                        fallback_price = (
                            await self.client
                            .get_ltp(
                                symbol=(
                                    trade.option_symbol
                                ),
                                segment=(
                                    trade.segment
                                ),
                                exchange=(
                                    trade.exchange
                                ),
                            )
                        )

                        self.initialize_open_trade(
                            trade=trade,
                            entry_price=fallback_price,
                        )

                elif status_value in {
                    "NEW",
                    "PENDING",
                    "OPEN",
                    "TRIGGER_PENDING",
                    "VALIDATION_PENDING",
                }:
                    trade.status = (
                        TradeStatus.ENTRY_PENDING
                    )

                else:
                    trade.status = (
                        TradeStatus.FAILED
                    )

            except Exception as status_exc:
                logger.exception(
                    "Could not verify broker order "
                    "status | order_id=%s",
                    trade.entry_order_id,
                )

                trade.last_error = (
                    f"{trade.last_error}; "
                    "status_check_failed="
                    f"{status_exc}"
                )

                # Unknown broker state must not be treated as
                # safely failed. Keep it pending for reconciliation.
                trade.status = (
                    TradeStatus.ENTRY_PENDING
                )

        self.state.put_trade(trade)
        await self.state.save()

    # ========================================================
    # Exit handling
    # ========================================================

    async def submit_exit(
        self,
        trade: TradeState,
        reason: str,
    ) -> None:
        """
        Submit a market SELL for either an equity or option
        trade.
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

        trade.status = (
            TradeStatus.EXIT_PENDING
        )
        trade.exit_order_id = order_id
        trade.exit_reason = reason
        trade.updated_at = self.now_iso()

        self.state.put_trade(trade)
        await self.state.save()

    # ========================================================
    # State and validation helpers
    # ========================================================

    def ensure_entry_allowed(
        self,
        *,
        symbol: str,
    ) -> None:
        if (
            self.active_count()
            >= self.settings.max_active_trades
        ):
            raise RuntimeError(
                "Maximum active/pending trade "
                "limit already reached"
            )

        if self.state.has_reserved_or_active(
            symbol
        ):
            raise RuntimeError(
                f"{symbol} already has a "
                "pending or active trade"
            )

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

    # ========================================================
    # Response parsing helpers
    # ========================================================

    @staticmethod
    def extract_order_id(
        response: dict[str, Any],
    ) -> str:
        candidates = (
            response.get(
                "groww_order_id"
            ),
            response.get("order_id"),
            response.get("orderId"),
            response.get("id"),
        )

        for candidate in candidates:
            if candidate is None:
                continue

            value = str(candidate).strip()

            if value:
                return value

        raise RuntimeError(
            "Order response does not contain an "
            f"order ID: {response}"
        )

    @staticmethod
    def extract_status(
        response: dict[str, Any],
    ) -> str:
        if not isinstance(response, dict):
            return ""

        value = (
            response.get("order_status")
            or response.get("status")
            or response.get("state")
            or ""
        )

        return str(value).strip().upper()

    @staticmethod
    def extract_fill_price(
        response: dict[str, Any],
    ) -> float | None:
        """
        Extract a possible fill price from an order-status
        response.

        Groww response field names may vary by endpoint/version,
        so common names are supported.
        """
        if not isinstance(response, dict):
            return None

        candidates: list[Any] = [
            response.get("average_price"),
            response.get("avg_price"),
            response.get("filled_price"),
            response.get("fill_price"),
            response.get("executed_price"),
            response.get("trade_price"),
        ]

        nested = response.get("data")

        if isinstance(nested, dict):
            candidates.extend(
                [
                    nested.get(
                        "average_price"
                    ),
                    nested.get("avg_price"),
                    nested.get(
                        "filled_price"
                    ),
                    nested.get("fill_price"),
                    nested.get(
                        "executed_price"
                    ),
                ]
            )

        for value in candidates:
            try:
                price = float(value)

            except (
                TypeError,
                ValueError,
            ):
                continue

            if price > 0:
                return price

        return None

    @staticmethod
    def calculate_average_fill_price(
        trades: Any,
    ) -> float:
        """
        Calculate weighted average fill price.

        Supported inputs:
          - list[dict]
          - {"trades": [...]}
          - {"data": [...]}
          - {"results": [...]}
        """
        if isinstance(trades, dict):
            raw_items = (
                trades.get("trades")
                or trades.get("items")
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
                or item.get("executed_price")
            )

            quantity_value = (
                item.get("quantity")
                or item.get(
                    "trade_quantity"
                )
                or item.get(
                    "filled_quantity"
                )
                or item.get(
                    "executed_quantity"
                )
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
            total_value += (
                price * quantity
            )

        if total_quantity <= 0:
            raise RuntimeError(
                "No valid fills found in trade "
                f"response: {trades}"
            )

        return round(
            total_value / total_quantity,
            4,
        )