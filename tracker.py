from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from config import IST, Settings
from groww_client import GrowwClient, first_value
from models import TradeStatus
from order_manager import OrderManager
from state_store import StateStore


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
        for trade in self.state.active_trades():
            if trade.status != TradeStatus.ENTRY_PENDING:
                continue

            if not trade.entry_order_id:
                continue

            status = await self.client.get_order_status(
                order_id=trade.entry_order_id,
                segment=self.settings.option_segment,
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

            amo_status = str(
                status.get(
                    "amo_status",
                    "",
                )
            ).upper()

            trade.entry_amo_status = (
                amo_status
                or trade.entry_amo_status
            )

            logger.info(
                "Pending entry | symbol=%s | "
                "order_id=%s | order_status=%s | "
                "amo_status=%s | remark=%s",
                trade.option_symbol,
                trade.entry_order_id,
                order_status,
                amo_status,
                status.get("remark"),
            )

            if (
                order_status in {
                    "REJECTED",
                    "FAILED",
                    "CANCELLED",
                }
                or amo_status == "FAILED"
            ):
                trade.status = TradeStatus.FAILED
                trade.last_error = str(
                    status.get(
                        "remark",
                        status,
                    )
                )
                trade.updated_at = (
                    self.now().isoformat()
                )

                self.state.put_trade(trade)
                continue

            if order_status not in {
                "EXECUTED",
                "COMPLETED",
            }:
                # PENDING, DISPATCHED, PARKED, PLACED,
                # NEW, ACKED, or APPROVED:
                # keep the order pending.
                self.state.put_trade(trade)
                continue

            # Only now retrieve fills and convert the trade
            # to OPEN.
            trades = await self.client.get_order_trades(
                order_id=trade.entry_order_id,
                segment=self.settings.option_segment,
            )

            entry_price = (
                self.calculate_average_fill_price(
                    trades
                )
            )

            if entry_price is None:
                continue

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
            trade.updated_at = (
                self.now().isoformat()
            )

            self.state.put_trade(trade)



    async def update_open_trade(
        self,
        trade,
    ) -> None:
        if trade.status != TradeStatus.OPEN:
            return

        current_price = await self.client.get_ltp(
                symbol=trade.option_symbol,
                segment=trade.segment,
                exchange=trade.exchange,
            )

        trade.current_price = current_price

        if (
            trade.highest_price is None
            or current_price > trade.highest_price
        ):
            trade.highest_price = current_price

        if trade.entry_price is None:
            raise RuntimeError(
                f"Missing entry price for "
                f"{trade.option_symbol}"
            )

        initial_stop = (
            trade.entry_price
            * (
                1
                - self.settings.stop_loss_percent
                / 100
            )
        )

        trailing_stop = (
            trade.highest_price
            * (
                1
                - self.settings.trailing_stop_percent
                / 100
            )
        )

        previous_stop = trade.stop_price or 0

        trade.stop_price = max(
            previous_stop,
            initial_stop,
            trailing_stop,
        )

        trade.target_price = (
            trade.entry_price
            * (
                1
                + self.settings.target_percent
                / 100
            )
        )

        trade.updated_at = self.now().isoformat()

        self.state.put_trade(trade)

        if self.force_exit_reached():
            await self.orders.submit_exit(
                trade=trade,
                reason="FORCED_EXIT_15_15",
            )
            return

        if (
            trade.target_price is not None
            and current_price >= trade.target_price
        ):
            await self.orders.submit_exit(
                trade=trade,
                reason="TARGET_REACHED",
            )
            return

        if (
            trade.stop_price is not None
            and current_price <= trade.stop_price
        ):
            await self.orders.submit_exit(
                trade=trade,
                reason="STOP_LOSS_OR_TRAILING_STOP",
            )

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
                segment=self.settings.option_segment,
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