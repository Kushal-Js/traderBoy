from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from config import IST, Settings
from groww_client import GrowwClient, first_value
from models import TradeState, TradeStatus
from order_manager import OrderManager
from state_store import StateStore


logger = logging.getLogger(__name__)


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

    # ========================================================
    # Time helpers
    # ========================================================

    @staticmethod
    def now() -> datetime:
        return datetime.now(IST)

    def now_iso(self) -> str:
        return self.now().isoformat()

    def is_market_day(self) -> bool:
        return self.now().weekday() < 5

    def force_exit_reached(self) -> bool:
        return (
            self.now().time()
            >= self.settings.force_exit_time
        )

    # ========================================================
    # Fill parsing
    # ========================================================

    @staticmethod
    def calculate_average_fill_price(
        trades: list[dict[str, Any]],
    ) -> float | None:
        total_quantity = 0
        total_value = 0.0

        for trade in trades:
            if not isinstance(trade, dict):
                continue

            raw_price = first_value(
                trade,
                (
                    "price",
                    "trade_price",
                    "fill_price",
                    "average_price",
                    "avg_fill_price",
                    "avgFillPrice",
                ),
            )

            raw_quantity = first_value(
                trade,
                (
                    "quantity",
                    "filled_quantity",
                    "trade_quantity",
                    "filled_qty",
                    "filledQty",
                    "qty",
                ),
            )

            if (
                raw_price is None
                or raw_quantity is None
            ):
                continue

            try:
                price = float(raw_price)
                quantity = int(raw_quantity)
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
            return None

        return round(
            total_value / total_quantity,
            4,
        )

    @staticmethod
    def extract_fill_price(
        status: dict[str, Any],
    ) -> float | None:
        """
        Extract a fill price from order status if available.
        """
        if not isinstance(status, dict):
            return None

        candidates: list[Any] = [
            status.get("average_price"),
            status.get("avg_price"),
            status.get("filled_price"),
            status.get("fill_price"),
            status.get("avg_fill_price"),
            status.get("avgFillPrice"),
            status.get("averageFillPrice"),
            status.get("executed_price"),
        ]

        nested_data = status.get("data")

        if isinstance(nested_data, dict):
            candidates.extend(
                [
                    nested_data.get(
                        "average_price"
                    ),
                    nested_data.get(
                        "avg_price"
                    ),
                    nested_data.get(
                        "filled_price"
                    ),
                    nested_data.get(
                        "fill_price"
                    ),
                    nested_data.get(
                        "avg_fill_price"
                    ),
                    nested_data.get(
                        "avgFillPrice"
                    ),
                    nested_data.get(
                        "averageFillPrice"
                    ),
                    nested_data.get(
                        "executed_price"
                    ),
                ]
            )

        for candidate in candidates:
            try:
                price = float(candidate)
            except (
                TypeError,
                ValueError,
            ):
                continue

            if price > 0:
                return price

        return None

    @staticmethod
    def extract_order_status(
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

    async def get_entry_fill_price(
        self,
        *,
        trade: TradeState,
        status: dict[str, Any],
    ) -> float | None:
        """
        Try status first, then the order-trades endpoint.
        """
        status_price = (
            self.extract_fill_price(status)
        )

        if status_price is not None:
            return status_price

        if not trade.entry_order_id:
            return None

        try:
            raw_trades = (
                await self.client.get_order_trades(
                    order_id=(
                        trade.entry_order_id
                    ),
                    segment=trade.segment,
                )
            )

        except Exception as exc:
            logger.warning(
                "Could not retrieve entry trades | "
                "symbol=%s | order_id=%s | "
                "segment=%s | error=%s",
                trade.option_symbol,
                trade.entry_order_id,
                trade.segment,
                exc,
            )
            return None

        if not isinstance(raw_trades, list):
            return None

        return self.calculate_average_fill_price(
            raw_trades
        )

    # ========================================================
    # Pending-entry reconciliation
    # ========================================================

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
                    await self.client
                    .get_order_status(
                        order_id=(
                            trade.entry_order_id
                        ),
                        segment=trade.segment,
                    )
                )

                order_status = (
                    self.extract_order_status(
                        status
                    )
                )

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
                    continue

                if order_status in {
                    "EXECUTED",
                    "COMPLETED",
                    "TRADED",
                }:
                    entry_price = (
                        await self
                        .get_entry_fill_price(
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

            except Exception as exc:
                logger.exception(
                    "Pending entry reconciliation "
                    "failed | symbol=%s | "
                    "order_id=%s | segment=%s",
                    trade.option_symbol,
                    trade.entry_order_id,
                    trade.segment,
                )

                trade.status = (
                    TradeStatus.ENTRY_PENDING
                )
                trade.last_error = str(exc)
                trade.updated_at = (
                    self.now_iso()
                )

                self.state.put_trade(trade)

        await self.state.save()

    # ========================================================
    # Open-trade updates
    # ========================================================

    async def update_open_trade(
        self,
        trade: TradeState,
    ) -> None:
        """
        Update an open position and apply:
          - target exit
          - initial stop-loss
          - trailing stop-loss
          - force exit
        """
        if trade.status != TradeStatus.OPEN:
            return

        if (
            trade.entry_price is None
            or trade.entry_price <= 0
        ):
            logger.warning(
                "Open trade has no valid entry price | "
                "symbol=%s",
                trade.option_symbol,
            )
            return

        current_price = (
            await self.client.get_ltp(
                symbol=trade.option_symbol,
                segment=trade.segment,
                exchange=trade.exchange,
            )
        )

        if current_price <= 0:
            logger.warning(
                "Invalid current price | "
                "symbol=%s | price=%s",
                trade.option_symbol,
                current_price,
            )
            return

        trade.current_price = current_price

        if (
            trade.highest_price is None
            or current_price > trade.highest_price
        ):
            trade.highest_price = current_price

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

        if trade.stop_price is None:
            trade.stop_price = max(
                initial_stop,
                trailing_stop,
            )
        else:
            trade.stop_price = max(
                trade.stop_price,
                initial_stop,
                trailing_stop,
            )

        if trade.target_price is None:
            trade.target_price = (
                trade.entry_price
                * (
                    1
                    + self.settings.target_percent
                    / 100
                )
            )

        reason: str | None = None

        if self.force_exit_reached():
            reason = "FORCE_EXIT"

        elif (
            current_price
            >= trade.target_price
        ):
            reason = "TARGET_REACHED"

        elif (
            trade.stop_price is not None
            and current_price <= trade.stop_price
        ):
            if current_price <= trailing_stop:
                reason = "TRAILING_STOP"
            else:
                reason = "STOP_LOSS"

        trade.updated_at = self.now_iso()

        self.state.put_trade(trade)
        await self.state.save()

        logger.info(
            "Open trade updated | "
            "symbol=%s | segment=%s | "
            "entry=%.4f | current=%.4f | "
            "highest=%.4f | stop=%.4f | "
            "target=%.4f | reason=%s",
            trade.option_symbol,
            trade.segment,
            trade.entry_price,
            current_price,
            trade.highest_price,
            trade.stop_price,
            trade.target_price,
            reason,
        )

        if reason is not None:
            await self.orders.submit_exit(
                trade=trade,
                reason=reason,
            )

    # ========================================================
    # Exit-order reconciliation
    # ========================================================

    async def reconcile_exit_orders(
        self,
    ) -> None:
        changed = False

        for trade in list(
            self.state.active_trades()
        ):
            if (
                trade.status
                != TradeStatus.EXIT_PENDING
            ):
                continue

            if not trade.exit_order_id:
                logger.warning(
                    "Exit-pending trade has no exit "
                    "order ID | symbol=%s",
                    trade.option_symbol,
                )
                continue

            if trade.exit_order_id == "DRY_RUN":
                trade.status = (
                    TradeStatus.CLOSED
                )
                trade.updated_at = (
                    self.now_iso()
                )
                self.state.put_trade(trade)
                changed = True
                continue

            try:
                status = (
                    await self.client
                    .get_order_status(
                        order_id=(
                            trade.exit_order_id
                        ),
                        segment=trade.segment,
                    )
                )

                order_status = (
                    self.extract_order_status(
                        status
                    )
                )

                logger.info(
                    "Exit reconciliation | "
                    "symbol=%s | order_id=%s | "
                    "segment=%s | status=%s",
                    trade.option_symbol,
                    trade.exit_order_id,
                    trade.segment,
                    order_status,
                )

                if order_status in {
                    "EXECUTED",
                    "COMPLETED",
                    "TRADED",
                }:
                    trade.status = (
                        TradeStatus.CLOSED
                    )
                    trade.updated_at = (
                        self.now_iso()
                    )
                    self.state.put_trade(trade)
                    changed = True

                elif order_status in {
                    "REJECTED",
                    "FAILED",
                    "CANCELLED",
                }:
                    trade.last_error = str(
                        status.get(
                            "remark",
                            status,
                        )
                    )
                    trade.status = (
                        TradeStatus.OPEN
                    )
                    trade.exit_order_id = None
                    trade.updated_at = (
                        self.now_iso()
                    )
                    self.state.put_trade(trade)
                    changed = True

            except Exception as exc:
                logger.exception(
                    "Exit reconciliation failed | "
                    "symbol=%s | order_id=%s | "
                    "segment=%s",
                    trade.option_symbol,
                    trade.exit_order_id,
                    trade.segment,
                )

                trade.last_error = str(exc)
                trade.updated_at = (
                    self.now_iso()
                )
                self.state.put_trade(trade)
                changed = True

        if changed:
            await self.state.save()

    # ========================================================
    # Main tracker loop
    # ========================================================

    async def run_once(self) -> None:
        self.state.reset_if_new_day()

        if not self.is_market_day():
            await self.state.save()
            return

        await self.reconcile_pending_entries()

        await self.reconcile_exit_orders()

        for trade in list(
            self.state.open_trades()
        ):
            try:
                await self.update_open_trade(
                    trade
                )

            except Exception as exc:
                logger.exception(
                    "Open trade update failed | "
                    "symbol=%s | segment=%s",
                    trade.option_symbol,
                    trade.segment,
                )

                trade.last_error = str(exc)
                trade.updated_at = (
                    self.now_iso()
                )
                self.state.put_trade(trade)

        self.state.data[
            "last_reconciled_at"
        ] = self.now_iso()

        await self.state.save()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()

            except asyncio.CancelledError:
                logger.info(
                    "Position tracker cancellation received"
                )
                raise

            except Exception:
                logger.exception(
                    "Tracker iteration failed"
                )

            await asyncio.sleep(
                self.settings
                .tracker_interval_seconds
            )

    # ========================================================
    # Trade initialization
    # ========================================================

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