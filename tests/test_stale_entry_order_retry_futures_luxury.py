"""
Smoke tests confirming Futures/trading_engine.py's and Luxury/trading_engine.py's
own _sync_pending_orders (near-verbatim copies of Options/trading_engine.py's,
which tests/test_stale_entry_order_retry.py covers in full depth) correctly
carry the same 15 Sep 2026 stale-entry-order retry/abandon fix - same
scenario (test_1) from that file, run against each package's own module to
catch any package-specific import/attribute-name issue the copy-paste
might have introduced, without re-deriving the full 5-case suite three
times over (the logic itself is byte-identical, already proven correct
against Options).

HOW TO RUN:
    uv run python tests/test_stale_entry_order_retry_futures_luxury.py
"""
import asyncio
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from datetime import datetime

import Options.dhan_client as odc
from Options.dhan_client import OrderResult, OrderStatus

import Futures.position_store as fps
import Futures.trading_engine as fte
import Luxury.position_store as lps
import Luxury.trading_engine as lte


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


async def _run_stale_retry_smoke_test(package_name, position_store_module, trading_engine_module):
    store = position_store_module.PositionStore()
    trading_engine_module.position_store = store
    real_timeout = trading_engine_module.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS
    trading_engine_module.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = 300
    restore = install_mocks()
    cancelled = []
    placed = []
    try:
        stale = position_store_module.OrderRecord(
            order_id="OID-STALE-1", underlying_symbol="RELIANCE", trading_symbol="RELIANCE FAKE EXP CE",
            transaction_type="BUY", quantity=500, status=OrderStatus.PENDING, is_amo=False,
            lot_size=500, option_type="CE", retry_count=0,
            placed_at=datetime.now() - timedelta(seconds=400),
            owned_by_placer=False,
        )
        store.orders_today[stale.order_id] = stale

        odc.dhan_wrapper.refresh_order_status = lambda order_id, is_amo=False: OrderResult(
            order_id=order_id, status=OrderStatus.PENDING, remark="", fill_price=0, filled_quantity=0)
        odc.dhan_wrapper.cancel_order = lambda order_id: cancelled.append(order_id)
        odc.dhan_wrapper.get_broker_net_quantity = lambda trading_symbol, segment="NSE_FNO": 0

        def fake_place(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
            order_id = f"OID-RETRY-{time.time_ns()}"
            placed.append({"trading_symbol": trading_symbol, "quantity": quantity})
            return {"order_id": order_id, "is_amo": False}
        odc.dhan_wrapper.place_market_order = fake_place

        await trading_engine_module._sync_pending_orders()

        assert cancelled == ["OID-STALE-1"], f"{package_name}: {cancelled}"
        assert len(placed) == 1, f"{package_name}: must place exactly one retry order, got {placed}"
        assert store.orders_today["OID-STALE-1"].status == OrderStatus.CANCELLED, package_name

        new_records = [r for oid, r in store.orders_today.items() if oid != "OID-STALE-1"]
        assert len(new_records) == 1 and new_records[0].retry_count == 1, package_name

        print(f"{package_name}: stale BUY order cancelled and retried exactly once "
              f"(new order tracked with retry_count=1, old order marked CANCELLED): PASSED")
    finally:
        restore()
        trading_engine_module.config.STALE_ENTRY_ORDER_TIMEOUT_SECONDS = real_timeout


async def main():
    print("=== Futures/Luxury stale entry-order retry smoke test suite ===\n")
    await _run_stale_retry_smoke_test("Futures", fps, fte)
    await _run_stale_retry_smoke_test("Luxury", lps, lte)
    print("\nALL FUTURES/LUXURY STALE ENTRY-ORDER SMOKE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
