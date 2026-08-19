from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from config import IST, Settings
from groww_client import GrowwClient, first_value
from models import TradeStatus
from order_manager import OrderManager
from state_store import StateStore

from models import (
    TradeState,
    TradeStatus,
)


class PositionTracker:
    def __init__(
        self,
        settings: Settings,
        client: GrowwClient,
        state: StateStore,
        orders: OrderManager,
    ) -> None:
        self.settings = settings
        self.client = client
        self.state = state
        self.orders = orders

    @staticmethod
    def now() -> datetime:
        return datetime.now(IST)

    def is_market_day(self) -> bool:
        return self.now().weekday() < 5

    def force_exit_reached(self) -> bool:
        return (
            self.now().time()
            >= self.settings.force_exit_time
        )

    @staticmethod
    def calculate_average_fill_price(
        trades: list[dict[str, Any]],
    ) -> float | None:
        total_quantity = 0
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

            total_quantity += quantity
            total_value += price * quantity

        if total_quantity <= 0:
            return None

        return total_value / total_quantity
    

    async def reconcile_pending_entries(
            self,
        ) -> None:
            trades = list(
                self.state.active_trades()
            )

            for trade in trades:
                if trade.status not in {
                    TradeStatus.RESERVED,
                    TradeStatus.ENTRY_PENDING,
                }:
                    continue

                if not trade.entry_order_id:
                    logger.warning(
                        "Pending trade has no entry order ID | "
                        "symbol=%s",
                        trade.option_symbol,
                    )
                    continue

                if trade.entry_order_id == "DRY_RUN":
                    continue

                try:
                    status = (
                        await self.client.get_order_status(
                            order_id=(
                                trade.entry_order_id
                            ),
                            segment=trade.segment,
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
                        "Pending entry reconciliation | "
                        "symbol=%s | order_id=%s | "
                        "segment=%s | status=%s",
                        trade.option_symbol,
                        trade.entry_order_id,
                        trade.segment,
                        order_status,
                    )

                    if order_status in {
                        "REJECTED",
                        "FAILED",
                        "CANCELLED",
                    }:
                        trade.status = (
                            TradeStatus.FAILED
                        )
                        trade.last_error = str(
                            status.get(
                                "remark",
                                status,
                            )
                        )
                        trade.updated_at = (
                            self.now_iso()
                        )

                        self.state.put_trade(trade)
                        await self.state.save()
                        continue

                    if order_status in {
                        "NEW",
                        "PENDING",
                        "OPEN",
                        "TRIGGER_PENDING",
                        "VALIDATION_PENDING",
                    }:
                        trade.status = (
                            TradeStatus.ENTRY_PENDING
                        )
                        trade.updated_at = (
                            self.now_iso()
                        )

                        self.state.put_trade(trade)
                        await self.state.save()
                        continue

                    if order_status in {
                        "EXECUTED",
                        "COMPLETED",
                        "TRADED",
                    }:
                        entry_price = (
                            await self.get_entry_fill_price(
                                trade=trade,
                                status=status,
                            )
                        )

                        if entry_price is None:
                            logger.warning(
                                "Executed entry has no fill "
                                "price yet | symbol=%s | "
                                "order_id=%s",
                                trade.option_symbol,
                                trade.entry_order_id,
                            )
                            continue

                        self.initialize_open_trade(
                            trade=trade,
                            entry_price=entry_price,
                        )

                        self.state.put_trade(trade)
                        await self.state.save()

                except Exception as exc:
                    logger.exception(
                        "Pending entry reconciliation failed | "
                        "symbol=%s | order_id=%s | "
                        "segment=%s | error=%s",
                        trade.option_symbol,
                        trade.entry_order_id,
                        trade.segment,
                        exc,
                    )

                    # Keep the trade pending rather than repeatedly
                    # treating an unknown broker state as failed.
                    trade.status = (
                        TradeStatus.ENTRY_PENDING
                    )
                    trade.last_error = str(exc)
                    trade.updated_at = (
                        self.now_iso()
                    )

                    self.state.put_trade(trade)
                    await self.state.save()

    async def reconcile_exit_orders(
        self,
    ) -> None:
        for trade in self.state.active_trades():
            if trade.status != TradeStatus.EXIT_PENDING:
                continue

            if not trade.exit_order_id:
                continue

            status = await self.client.get_order_status(
                order_id=trade.exit_order_id,
                segment=trade.segment,
            )

            order_status = str(
                status.get(
                    "order_status",
                    status.get("status", ""),
                )
            ).upper()

            if order_status in {
                "EXECUTED",
                "COMPLETED",
            }:
                trade.status = TradeStatus.CLOSED
                trade.updated_at = (
                    self.now().isoformat()
                )
                self.state.put_trade(trade)

            elif order_status in {
                "REJECTED",
                "FAILED",
                "CANCELLED",
            }:
                trade.last_error = str(
                    status.get("remark", status)
                )
                trade.status = TradeStatus.OPEN
                trade.exit_order_id = None
                trade.updated_at = (
                    self.now().isoformat()
                )
                self.state.put_trade(trade)

    async def run_once(self) -> None:
        self.state.reset_if_new_day()

        if not self.is_market_day():
            await self.state.save()
            return

        await self.reconcile_pending_entries()

        for trade in list(
            self.state.open_trades()
        ):
            await self.update_open_trade(trade)

        await self.reconcile_exit_orders()

        self.state.data[
            "last_reconciled_at"
        ] = self.now().isoformat()

        await self.state.save()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()

            except Exception:
                # The loop must remain alive after one API error.
                import logging

                logging.getLogger(
                    __name__
                ).exception(
                    "Tracker iteration failed"
                )

            await asyncio.sleep(
                self.settings.tracker_interval_seconds
            )