"""
Tests for Options/trading_engine.py's _sync_pending_orders stale-entry-order
handling - user request 15 Sep 2026, real incident: ICICIPRULI's BUY
market order sat PENDING at the broker for 10+ minutes straight during
live market hours, with _sync_pending_orders re-logging the same PENDING
status every monitor tick forever, never doing anything about it. The
user manually cancelled it and asked for an automatic fix: after
config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS (default 300s) of no resolution,
cancel the stuck order and make exactly ONE retry attempt (a fresh market
order for the identical contract/quantity - a market order always fills
at whatever the CURRENT price is, so re-submitting IS the "adjust to
current price" retry); if that retry ALSO times out, abandon the entry
(release the reservation) rather than retrying forever.

Coverage:
  1. A BUY order stuck non-terminal past the timeout gets cancelled and
     retried exactly once (new order_id tracked, retry_count=1, old order
     marked CANCELLED in our own store).
  2. A retried order (retry_count=1) that ALSO times out gets abandoned -
     reservation released, NOT retried a second time (no infinite loop).
  3. An order that actually filled at the broker in the same instant the
     cancel raced against it gets promoted to a real Position (using the
     broker's real quantity/LTP) instead of retried or abandoned.
  4. A genuinely AMO-queued order (is_amo=True) is NEVER treated as stale
     regardless of age - it's supposed to sit non-terminal until the next
     session.
  5. An order younger than the timeout is left completely alone.

HOW TO RUN:
    uv run python tests/test_stale_entry_order_retry.py
"""
import asyncio
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.dhan_client as odc
import Options.position_store as ops
import Options.trading_engine as ote
from Options.dhan_client import OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def install_mocks():
    originals = {
        "refresh_order_status": odc.dhan_wrapper.refresh_order_status,
        "cancel_order": odc.dhan_wrapper.cancel_order,
        "get_broker_net_quantity": odc.dhan_wrapper.get_broker_net_quantity,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "refresh_ema_cross_signal": odc.dhan_wrapper.refresh_ema_cross_signal,
        "get_cached_ema_cross_candle_start": odc.dhan_wrapper.get_cached_ema_cross_candle_start,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
    }
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None
    odc.dhan_wrapper.refresh_ema_cross_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_ema_cross_candle_start = lambda sym: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


def _stale_order(order_id="OID-1", retry_count=0, age_seconds=400):
    return ops.OrderRecord(
        order_id=order_id, underlying_symbol="RELIANCE", trading_symbol="RELIANCE FAKE EXP CE",
        transaction_type="BUY", quantity=500, status=OrderStatus.PENDING, is_amo=False,
        lot_size=500, option_type="CE", retry_count=retry_count,
        placed_at=datetime.now() - timedelta(seconds=age_seconds),
        owned_by_placer=False,
    )


async def test_1_stale_order_is_cancelled_and_retried_once():
    store = ops.PositionStore()
    ote.position_store = store
    real_timeout = ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS
    ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = 300
    restore = install_mocks()
    cancelled = []
    placed = []
    try:
        stale = _stale_order(order_id="OID-STALE-1", retry_count=0, age_seconds=400)
        store.orders_today[stale.order_id] = stale

        odc.dhan_wrapper.refresh_order_status = lambda order_id, is_amo=False: OrderResult(
            order_id=order_id, status=OrderStatus.PENDING, remark="", fill_price=0, filled_quantity=0)
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled.append(order_id)
        odc.dhan_wrapper.get_broker_net_quantity = lambda trading_symbol, segment="NSE_FNO": 0

        def fake_place(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
            order_id = f"OID-RETRY-{time.time_ns()}"
            placed.append({"trading_symbol": trading_symbol, "quantity": quantity, "order_id": order_id})
            return {"order_id": order_id, "is_amo": False}
        odc.dhan_wrapper.place_market_order = fake_place

        await ote._sync_pending_orders()

        assert cancelled == ["OID-STALE-1"], cancelled
        assert len(placed) == 1, "must place exactly one retry order"
        assert placed[0]["trading_symbol"] == "RELIANCE FAKE EXP CE"
        assert placed[0]["quantity"] == 500

        assert store.orders_today["OID-STALE-1"].status == OrderStatus.CANCELLED, \
            "the old stale order must be marked CANCELLED in our own store, not left showing PENDING forever"

        new_order_id = placed[0]["order_id"]
        assert new_order_id in store.orders_today, "the retry order must be tracked"
        new_record = store.orders_today[new_order_id]
        assert new_record.retry_count == 1, new_record.retry_count
        assert new_record.owned_by_placer is False, \
            "must be pickup-able by a later _sync_pending_orders tick"

        assert "RELIANCE" not in store.live_positions, "no position should exist yet - only the retry was placed"

        print("1. A stale BUY order past the timeout is cancelled and retried exactly once "
              "(new order tracked with retry_count=1, old order marked CANCELLED): PASSED")
    finally:
        restore()
        ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = real_timeout


async def test_2_a_second_stale_timeout_abandons_the_entry_no_infinite_retry():
    store = ops.PositionStore()
    ote.position_store = store
    real_timeout = ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS
    ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = 300
    restore = install_mocks()
    cancelled = []
    placed = []
    try:
        # This order was ITSELF already a retry (retry_count=1) and is now
        # ALSO stuck past the timeout.
        already_retried = _stale_order(order_id="OID-RETRY-1", retry_count=1, age_seconds=400)
        store.orders_today[already_retried.order_id] = already_retried
        store.reserved_symbols["RELIANCE"] = "CE"

        odc.dhan_wrapper.refresh_order_status = lambda order_id, is_amo=False: OrderResult(
            order_id=order_id, status=OrderStatus.PENDING, remark="", fill_price=0, filled_quantity=0)
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled.append(order_id)
        odc.dhan_wrapper.get_broker_net_quantity = lambda trading_symbol, segment="NSE_FNO": 0
        odc.dhan_wrapper.place_market_order = lambda *a, **k: placed.append(1) or {"order_id": "SHOULD-NOT-HAPPEN", "is_amo": False}

        await ote._sync_pending_orders()

        assert cancelled == ["OID-RETRY-1"], cancelled
        assert placed == [], "must NOT place a second retry - one retry is the cap"
        assert "RELIANCE" not in store.reserved_symbols, \
            "the entry must be abandoned (reservation released) after the retry also times out"

        print("2. A SECOND stale timeout (on an already-once-retried order) abandons the entry "
              "and releases the reservation - no infinite retry loop: PASSED")
    finally:
        restore()
        ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = real_timeout


async def test_3_actually_filled_during_the_cancel_race_is_promoted_to_a_real_position():
    store = ops.PositionStore()
    ote.position_store = store
    real_timeout = ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS
    ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = 300
    restore = install_mocks()
    try:
        stale = _stale_order(order_id="OID-RACE-1", retry_count=0, age_seconds=400)
        store.orders_today[stale.order_id] = stale

        odc.dhan_wrapper.refresh_order_status = lambda order_id, is_amo=False: OrderResult(
            order_id=order_id, status=OrderStatus.PENDING, remark="", fill_price=0, filled_quantity=0)
        odc.dhan_wrapper.cancel_order = lambda order_id: None
        # Broker shows it's actually filled now (race: filled right as we tried to cancel).
        odc.dhan_wrapper.get_broker_net_quantity = lambda trading_symbol, segment="NSE_FNO": 500
        odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 12.5

        await ote._sync_pending_orders()

        assert "RELIANCE" in store.live_positions, "a real fill discovered post-cancel must become a live Position"
        pos = store.live_positions["RELIANCE"]
        assert pos.quantity == 500, pos.quantity
        assert pos.entry_price == 12.5, pos.entry_price

        print("3. An order that actually filled during the cancel race is promoted to a real Position "
              "using the broker's real quantity/LTP, not retried or abandoned: PASSED")
    finally:
        restore()
        ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = real_timeout


async def test_4_a_genuinely_queued_amo_order_is_never_treated_as_stale():
    store = ops.PositionStore()
    ote.position_store = store
    real_timeout = ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS
    ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = 300
    restore = install_mocks()
    cancelled = []
    try:
        amo_order = ops.OrderRecord(
            order_id="OID-AMO-1", underlying_symbol="RELIANCE", trading_symbol="RELIANCE FAKE EXP CE",
            transaction_type="BUY", quantity=500, status=OrderStatus.PENDING, is_amo=True,
            lot_size=500, option_type="CE", retry_count=0,
            placed_at=datetime.now() - timedelta(hours=10),  # very old - would trip the timeout if not AMO-exempt
            owned_by_placer=False,
        )
        store.orders_today[amo_order.order_id] = amo_order

        odc.dhan_wrapper.refresh_order_status = lambda order_id, is_amo=False: OrderResult(
            order_id=order_id, status=OrderStatus.PENDING, remark="", fill_price=0, filled_quantity=0)
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled.append(order_id)

        await ote._sync_pending_orders()

        assert cancelled == [], "a genuinely-queued AMO order must never be cancelled by the stale-order check"
        assert store.orders_today["OID-AMO-1"].status == OrderStatus.PENDING, \
            "an AMO order's status must be untouched by the stale-order logic"

        print("4. A genuinely AMO-queued order is never treated as stale regardless of age "
              "(it's supposed to sit non-terminal until the next session): PASSED")
    finally:
        restore()
        ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = real_timeout


async def test_5_an_order_younger_than_the_timeout_is_left_alone():
    store = ops.PositionStore()
    ote.position_store = store
    real_timeout = ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS
    ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = 300
    restore = install_mocks()
    cancelled = []
    try:
        young_order = _stale_order(order_id="OID-YOUNG-1", retry_count=0, age_seconds=30)
        store.orders_today[young_order.order_id] = young_order

        odc.dhan_wrapper.refresh_order_status = lambda order_id, is_amo=False: OrderResult(
            order_id=order_id, status=OrderStatus.PENDING, remark="", fill_price=0, filled_quantity=0)
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled.append(order_id)

        await ote._sync_pending_orders()

        assert cancelled == [], "an order younger than the timeout must not be touched yet"
        assert store.orders_today["OID-YOUNG-1"].status == OrderStatus.PENDING

        print("5. An order younger than the stale-order timeout is left completely alone: PASSED")
    finally:
        restore()
        ote.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = real_timeout


async def main():
    print("=== Stale entry-order retry/abandon test suite ===\n")
    await test_1_stale_order_is_cancelled_and_retried_once()
    await test_2_a_second_stale_timeout_abandons_the_entry_no_infinite_retry()
    await test_3_actually_filled_during_the_cancel_race_is_promoted_to_a_real_position()
    await test_4_a_genuinely_queued_amo_order_is_never_treated_as_stale()
    await test_5_an_order_younger_than_the_timeout_is_left_alone()
    print("\nALL STALE ENTRY-ORDER RETRY CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
