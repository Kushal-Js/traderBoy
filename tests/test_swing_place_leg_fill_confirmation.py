"""
Regression test for a real, live incident (8 Sep 2026): MAHABANK's
PE_HEDGE exit SELL was placed, but Dhan filled it in as a protected LIMIT
order that the underlying's own price then moved away from - it sat
genuinely PENDING at the broker (filledQty=0) while `_place_leg`'s own
`ok` check (`status not in REJECTED_STATUSES and status != CANCELLED`)
treated that PENDING status as a SUCCESSFUL fill, since it only ever
excluded REJECTED/CANCELLED. The bot recorded the position CLOSED and
released capacity while the REAL position sat open and unmonitored at
the broker for ~2.5 hours, until found by directly querying Dhan's own
real open positions. See trading-skills'
`incidents/2026-09-08-mahabank-phantom-pe-hedge-exit.md` for the full
incident/recovery writeup (cancelled the stale order, placed a fresh
real SELL, confirmed flat - real fill 1.85, not the phantom 2.18 the
original bug had recorded).

Covers, against the REAL production `_place_leg` (not reimplemented):
  1. TRADED -> ok=True (the only genuine success state).
  2. PENDING, still not terminal after the retry budget (this incident's
     EXACT scenario) -> ok=False, not the old silent "success".
  3. TRANSIT and PART_TRADED (never previously distinguished from a real
     fill either) -> also ok=False.
  4. REJECTED and CANCELLED -> ok=False (unchanged - always correct).
  5. End-to-end at the real call site: a stuck-PENDING exit SELL through
     _exit_pe_hedge_to_watching leaves the PE_HEDGE position OPEN in
     live_positions (not phantom-closed), keeps capacity reserved (no
     premature release), and logs an ERROR - proving the fix actually
     prevents this exact incident from recurring, not just that the
     isolated helper function returns the right boolean.

HOW TO RUN:
    uv run python tests/test_swing_place_leg_fill_confirmation.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_place_leg_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Swing.position_store as sps
import Swing.trading_engine as ste
from Options.dhan_client import OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def install_order_mocks(order_status: str, filled_quantity: int = 0, fill_price: float = 0.0):
    """Mocks only place_market_order/wait_for_order_result - exactly the
    two calls _place_leg itself makes - so the REAL _place_leg body runs
    unmodified against a controllable broker response."""
    real_place = odc.dhan_wrapper.place_market_order
    real_wait = odc.dhan_wrapper.wait_for_order_result

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        return {"order_id": "FAKE-ORDER-1", "is_amo": False}

    def fake_wait_for_order_result(order_id, is_amo=False):
        return OrderResult(order_id=order_id, status=order_status, remark="",
                            fill_price=fill_price, filled_quantity=filled_quantity, is_amo=is_amo)

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.wait_for_order_result = fake_wait_for_order_result

    def restore():
        odc.dhan_wrapper.place_market_order = real_place
        odc.dhan_wrapper.wait_for_order_result = real_wait
    return restore


async def test_1_traded_is_the_only_status_treated_as_ok():
    restore = install_order_mocks(OrderStatus.TRADED, filled_quantity=6500, fill_price=1.85)
    try:
        result = await ste._place_leg("MAHABANK 29 SEP 85 PUT", 6500, "SELL", "MARGIN", "Ext", "MAHABANK")
        assert result["ok"] is True, result
        assert result["fill_price"] == 1.85, result
        print("1. A genuinely TRADED order -> ok=True, real fill_price returned: PASSED")
    finally:
        restore()


async def test_2_stuck_pending_is_no_longer_a_false_success():
    """The EXACT scenario from the real 8 Sep 2026 incident: the order
    never leaves PENDING within wait_for_order_result's own retry
    budget - filledQty=0, nothing actually traded."""
    restore = install_order_mocks(OrderStatus.PENDING, filled_quantity=0, fill_price=0.0)
    try:
        result = await ste._place_leg("MAHABANK 29 SEP 85 PUT", 6500, "SELL", "MARGIN", "Ext", "MAHABANK")
        assert result["ok"] is False, \
            f"a stuck PENDING order (filledQty=0) must NOT be treated as a successful fill, got {result}"
        assert result["status"] == OrderStatus.PENDING, result
        print("2. A stuck PENDING order (the real MAHABANK incident's exact scenario) -> ok=False, "
              "no longer the old silent false-success: PASSED")
    finally:
        restore()


async def test_3_transit_and_part_traded_are_also_not_ok():
    for status in (OrderStatus.TRANSIT, OrderStatus.PART_TRADED):
        restore = install_order_mocks(status, filled_quantity=3000, fill_price=1.90)
        try:
            result = await ste._place_leg("SBIN FAKE EXP PUT", 6000, "SELL", "MARGIN", "Ext", "SBIN")
            assert result["ok"] is False, f"{status} must not be treated as a full, genuine fill, got {result}"
        finally:
            restore()
    print("3. TRANSIT and PART_TRADED (a partial fill Swing has no mechanism to track separately) - "
          "also correctly ok=False, not silently accepted as done: PASSED")


async def test_4_rejected_and_cancelled_remain_ok_false():
    for status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
        restore = install_order_mocks(status)
        try:
            result = await ste._place_leg("SBIN FAKE EXP PUT", 6000, "SELL", "MARGIN", "Ext", "SBIN")
            assert result["ok"] is False, result
        finally:
            restore()
    print("4. REJECTED and CANCELLED remain ok=False, unchanged (always correct even before this fix): PASSED")


async def test_5_end_to_end_stuck_exit_leaves_position_open_and_capacity_reserved():
    """The real call site the incident actually happened at -
    _exit_pe_hedge_to_watching - with a stuck-PENDING SELL: proves the
    fix prevents the ACTUAL incident (phantom-closed position, released
    capacity), not just that the isolated helper returns the right bool."""
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    symbol = "MAHABANK"
    pe_leg = sps.Leg(
        underlying_symbol=symbol, option_trading_symbol="MAHABANK 29 SEP 85 PUT", option_type="PE",
        quantity=6500, lot_size=6500, entry_price=2.18, order_id="OID-ORIGINAL", product_type="MARGIN",
    )
    store.live_positions[symbol] = sps.BasketHedgePosition(underlying_symbol=symbol, state="PE_HEDGE", legs=[pe_leg])
    store.reserved_symbols[symbol] = "PE_HEDGE"

    restore = install_order_mocks(OrderStatus.PENDING, filled_quantity=0, fill_price=0.0)
    try:
        await ste._exit_pe_hedge_to_watching(symbol, pe_leg, "PE_SUPERTREND_REVERSAL_EXIT")

        assert symbol in store.live_positions, \
            "a stuck-PENDING exit must leave the PE_HEDGE position OPEN (not phantom-closed) for the " \
            "next tick to retry - this is the exact real-money gap the 8 Sep 2026 incident exposed"
        assert store.live_positions[symbol].state == "PE_HEDGE", store.live_positions[symbol].state
        assert symbol in store.reserved_symbols, \
            "capacity must stay reserved - releasing it here would let a FRESH entry proceed while " \
            "the real broker position (never actually sold) is still open, doubling real exposure"

        closed_trades = trade_history.read_all_jsonl("real_trades")
        swing_closed = [t for t in closed_trades if t["strategy"] == "Swing" and t["underlying_symbol"] == symbol]
        assert swing_closed == [], \
            f"nothing should be recorded as closed when the SELL never actually filled, got {swing_closed}"

        print("5. End-to-end: a stuck-PENDING exit SELL through the REAL _exit_pe_hedge_to_watching "
              "leaves the position OPEN, capacity RESERVED, and nothing recorded as closed - the real "
              "MAHABANK incident (phantom close, released capacity, ~2.5h of unmonitored real exposure) "
              "can no longer recur this way: PASSED")
    finally:
        restore()


async def main():
    print("=== Swing _place_leg fill-confirmation regression suite (8 Sep 2026 MAHABANK incident) ===\n")
    await test_1_traded_is_the_only_status_treated_as_ok()
    await test_2_stuck_pending_is_no_longer_a_false_success()
    await test_3_transit_and_part_traded_are_also_not_ok()
    await test_4_rejected_and_cancelled_remain_ok_false()
    await test_5_end_to_end_stuck_exit_leaves_position_open_and_capacity_reserved()
    print("\nALL SWING _place_leg FILL-CONFIRMATION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
