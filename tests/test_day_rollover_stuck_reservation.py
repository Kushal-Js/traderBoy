"""
Tests for the day-rollover stuck-reservation fix (audit finding 1.4,
CODE_AUDIT_2026-09-24.md / CODE_AUDIT_2026-09-25.md - confirmed in
Options, Futures, AND Luxury's own position_store.py).

Bug: maybe_reset_for_new_day() used to clear orders_today
UNCONDITIONALLY on every day rollover, while reserved_symbols was only
cleared `if config.ENABLE_SQUARE_OFF`. When ENABLE_SQUARE_OFF=False
(a real, supported deployed mode for all three packages), an entry
order still non-terminal at midnight rollover lost its OrderRecord
(so _sync_pending_orders could never find/resolve/release it) while
its reserved_symbols entry survived forever - permanently blocking new
entries on that underlying.

Fix: before clearing orders_today, walk it for any BUY order that's
still non-terminal, not yet promoted to a live Position, and not
currently owned by an in-flight placer - release its reserved_symbols
entry explicitly, since this is its last chance to ever be released.
A symbol that already has a live Position must NOT be touched (that's
a legitimate, separate reservation carrying overnight by design).

Covers all three packages with the identical scenario, since the fix
is a near-verbatim copy across Options/Futures/Luxury.

HOW TO RUN:
    uv run python tests/test_day_rollover_stuck_reservation.py
"""
import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
import tempfile
scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_day_rollover_test_"))
trade_history.HISTORY_DIR = scratch_dir

from Options.dhan_client import OrderStatus

import Options.position_store as ops
import Futures.position_store as fps
import Luxury.position_store as lps


async def _run_for_package(name, module, config):
    real_square_off = config.ENABLE_SQUARE_OFF
    config.ENABLE_SQUARE_OFF = False
    try:
        store = module.PositionStore()
        store._trading_day = date.today() - timedelta(days=1)  # force a rollover on the next check

        # A live position - MUST survive the rollover's reservation cleanup
        # untouched, regardless of what happens to the stuck order below.
        live_pos = module.Position(
            underlying_symbol="LIVESTOCK", option_trading_symbol="LIVESTOCK FAKE CE", option_type="CE",
            quantity=500, lot_size=500, entry_price=50.0, highest_price=50.0, target_price=60.0,
            hard_stop_loss=40.0, order_id="LIVE-ORDER-1", product_type="MARGIN",
        )
        store.live_positions["LIVESTOCK"] = live_pos
        store.reserved_symbols["LIVESTOCK"] = "CE"

        # The stuck order: reserved but never resolved, never promoted to
        # a Position - exactly the bug scenario (e.g. an AMO BUY placed
        # right before midnight rollover, still TRANSIT/PENDING at Dhan).
        store.reserved_symbols["STUCKSTOCK"] = "CE"
        store.orders_today["STUCK-ORDER-1"] = module.OrderRecord(
            order_id="STUCK-ORDER-1", underlying_symbol="STUCKSTOCK", trading_symbol="STUCKSTOCK FAKE CE",
            transaction_type="BUY", quantity=500, status=OrderStatus.TRANSIT, owned_by_placer=False,
        )

        # A second reserved symbol whose order already resolved TERMINAL
        # (e.g. REJECTED) but release_symbol was never called for some
        # unrelated reason - NOT this fix's job to clean up (that's a
        # different bug class, if it exists at all); included only to
        # confirm this fix doesn't touch terminal orders' reservations.
        store.reserved_symbols["REJECTEDSTOCK"] = "CE"
        store.orders_today["REJECTED-ORDER-1"] = module.OrderRecord(
            order_id="REJECTED-ORDER-1", underlying_symbol="REJECTEDSTOCK", trading_symbol="REJECTEDSTOCK FAKE CE",
            transaction_type="BUY", quantity=500, status=OrderStatus.REJECTED, owned_by_placer=False,
        )

        await store.maybe_reset_for_new_day()

        assert "LIVESTOCK" in store.reserved_symbols and "LIVESTOCK" in store.live_positions, \
            f"[{name}] a live position's reservation must survive the day rollover untouched"
        assert "STUCKSTOCK" not in store.reserved_symbols, \
            f"[{name}] the stuck non-terminal order's reservation MUST be released at rollover - " \
            f"got reserved_symbols={store.reserved_symbols}"
        assert len(store.orders_today) == 0, f"[{name}] orders_today must still be cleared as before"
        print(f"  [{name}] stuck reservation released, live position untouched, orders_today cleared: PASSED")
    finally:
        config.ENABLE_SQUARE_OFF = real_square_off


async def test_1_stuck_reservation_released_at_rollover_all_three_packages():
    print("1. Day-rollover stuck-reservation fix, ENABLE_SQUARE_OFF=False, all three packages:")
    await _run_for_package("Options", ops, ops.config)
    await _run_for_package("Futures", fps, fps.config)
    await _run_for_package("Luxury", lps, lps.config)
    print("   ALL PASSED")


async def test_2_enable_square_off_true_still_clears_everything_as_before():
    """Regression guard: ENABLE_SQUARE_OFF=True's existing unconditional
    reserved_symbols.clear() must be completely unaffected by this fix."""
    real_square_off = ops.config.ENABLE_SQUARE_OFF
    ops.config.ENABLE_SQUARE_OFF = True
    try:
        store = ops.PositionStore()
        store._trading_day = date.today() - timedelta(days=1)
        store.reserved_symbols["ANYSTOCK"] = "CE"
        store.live_positions["ANYSTOCK"] = ops.Position(
            underlying_symbol="ANYSTOCK", option_trading_symbol="ANYSTOCK FAKE CE", option_type="CE",
            quantity=500, lot_size=500, entry_price=50.0, highest_price=50.0, target_price=60.0,
            hard_stop_loss=40.0, order_id="O1", product_type="MARGIN",
        )
        await store.maybe_reset_for_new_day()
        assert store.live_positions == {} and store.reserved_symbols == {}, \
            "ENABLE_SQUARE_OFF=true must still clear everything unconditionally, unchanged by this fix"
        print("2. ENABLE_SQUARE_OFF=true's existing full-clear behavior is unaffected by this fix: PASSED")
    finally:
        ops.config.ENABLE_SQUARE_OFF = real_square_off


async def main():
    print("=== Day-rollover stuck-reservation fix test suite ===\n")
    await test_1_stuck_reservation_released_at_rollover_all_three_packages()
    await test_2_enable_square_off_true_still_clears_everything_as_before()
    print("\nALL day-rollover stuck-reservation tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
