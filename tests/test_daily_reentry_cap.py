"""
Tests for the daily re-entry cap on Options/Futures/Luxury - user request
1 Sep 2026: "only allow entry into same trade max 3 times a day for
Luxury, Options and Future package."

Independent of MAX_LIVE_POSITIONS_CE/_PE (that caps how many can be LIVE
at once); this caps how many times the SAME underlying can be entered
across the whole day, even after each prior entry has already been
exited. Backed by trade_history.count_opened_today() - a read of the
SAME durable on-disk log record_opened_position() already writes to -
rather than an in-memory counter, specifically so the cap survives a
mid-day restart (scenario 3 below is the test that actually proves this).

Covers, against the REAL production functions (not reimplemented):
  1. trade_history.count_opened_today - counts correctly, isolates by
     strategy (two strategies opening the SAME symbol never share a
     count), isolates by underlying_symbol, and returns 0 when today's
     log file doesn't exist at all (fails open, never guesses/blocks).
  2. Full real integration for Options: 3 genuine enter->exit cycles for
     the SAME symbol through the real _process_one_entry/close_position,
     then a 4th real _process_one_entry attempt for that SAME symbol is
     rejected with daily_reentry_cap_reached and places NO order -
     verified via a real order-count check, not just the status field -
     while a DIFFERENT symbol still enters fine (per-symbol, not global).
  3. The cap survives a mid-day restart: with the SAME on-disk log
     already showing 3 entries for a symbol (from scenario 2), a BRAND
     NEW PositionStore (simulating a fresh process after a restart)
     still blocks a 4th real entry attempt for it - proving this is
     backed by the durable log, not an in-memory counter that a restart
     would silently reset to 0.
  4. Futures and Luxury each enforce their own independent
     MAX_DAILY_ENTRIES_PER_SYMBOL / count_opened_today("Futures"/
     "Luxury", ...) wiring - seeded via the real record_opened_position,
     confirmed via each package's own real _process_one_entry.
  5. The cap is genuinely configurable, not hardcoded to 3.

HOW TO RUN:
    uv run python tests/test_daily_reentry_cap.py
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_reentry_cap_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Options.position_store as ops
import Options.trading_engine as ote
import Futures.position_store as fps
import Futures.trading_engine as fte
import Luxury.position_store as lps
import Luxury.trading_engine as lte
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP {option_type}", strike=1000.0,
                      option_type=option_type, lot_size=500, security_id=f"SECID-{symbol}",
                      expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    """Same shape as test_deep_integration.py's/test_luxury_package.py's
    own helper - mocks every Dhan network call the entry path touches, no
    live session needed. Every unique order gets its own order_id
    (time_ns suffix) so concurrent/repeated entries for the same symbol
    across this file's several enter->exit cycles never collide."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None

    placed_orders = []

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{time.time_ns()}"
        placed_orders.append(order_id)
        return {"order_id": order_id, "is_amo": False}

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore, placed_orders


async def test_1_count_opened_today():
    real_dir = trade_history.HISTORY_DIR
    scratch = Path(tempfile.mkdtemp(prefix="dhanboy_count_opened_today_test_"))
    trade_history.HISTORY_DIR = scratch
    try:
        assert trade_history.count_opened_today("Options", "RELIANCE") == 0, \
            "no log file at all yet must count as 0, never an error"

        pos = ops.Position(
            underlying_symbol="RELIANCE", option_trading_symbol="RELIANCE FAKE EXP CE",
            option_type="CE", quantity=500, lot_size=500, entry_price=50.0, highest_price=50.0,
            target_price=62.5, hard_stop_loss=42.0, order_id="OID1", product_type="MARGIN",
        )
        await trade_history.record_opened_position("Options", pos)
        await trade_history.record_opened_position("Options", pos)
        await asyncio.sleep(0.2)
        assert trade_history.count_opened_today("Options", "RELIANCE") == 2

        # Isolated by strategy - the SAME symbol opened by a different
        # strategy must not add to Options' own count.
        await trade_history.record_opened_position("Futures", pos)
        await asyncio.sleep(0.2)
        assert trade_history.count_opened_today("Options", "RELIANCE") == 2, \
            "a different strategy's own open of the same symbol must not count toward Options"
        assert trade_history.count_opened_today("Futures", "RELIANCE") == 1

        # Isolated by symbol - a different symbol for the SAME strategy
        # must not add to RELIANCE's own count.
        pos2 = ops.Position(
            underlying_symbol="TCS", option_trading_symbol="TCS FAKE EXP CE",
            option_type="CE", quantity=500, lot_size=500, entry_price=50.0, highest_price=50.0,
            target_price=62.5, hard_stop_loss=42.0, order_id="OID2", product_type="MARGIN",
        )
        await trade_history.record_opened_position("Options", pos2)
        await asyncio.sleep(0.2)
        assert trade_history.count_opened_today("Options", "RELIANCE") == 2
        assert trade_history.count_opened_today("Options", "TCS") == 1

        print("1. trade_history.count_opened_today counts correctly, isolates by strategy AND by "
              "underlying_symbol, and returns 0 (never an error) when no log exists yet: PASSED")
    finally:
        trade_history.HISTORY_DIR = real_dir


async def test_2_options_full_cycle_blocks_the_fourth_entry():
    store = ops.PositionStore()
    ote.position_store = store
    real_cap = ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL
    real_ce_cap = ote.config.MAX_LIVE_POSITIONS_CE
    ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL = 3
    ote.config.MAX_LIVE_POSITIONS_CE = 10  # capacity itself isn't what's under test here
    restore, placed_orders = install_all_dhan_mocks()
    try:
        symbol = "RELIANCE"
        for i in range(3):
            result = await ote._process_one_entry(symbol, "CE")
            assert result["status"] == "entered", f"entry #{i + 1} should have succeeded, got {result}"
            assert await store.try_start_exit(symbol)
            await store.close_position(symbol, 55.0, "TARGET_HIT")
            assert symbol not in store.live_positions and symbol not in store.reserved_symbols

        await asyncio.sleep(0.3)  # let the 3 fire-and-forget record_opened_position writes land
        orders_before_4th = len(placed_orders)

        result_4 = await ote._process_one_entry(symbol, "CE")
        assert result_4["status"] == "skipped", result_4
        assert result_4["reason"] == "daily_reentry_cap_reached", result_4
        assert len(placed_orders) == orders_before_4th, \
            "the 4th attempt must place NO order at all, not even one that gets unwound"
        assert symbol not in store.reserved_symbols, \
            "a rejected-by-cap attempt must not leave a stale reservation behind"

        # A DIFFERENT symbol is completely unaffected - this is a
        # per-symbol cap, not a global one.
        other_result = await ote._process_one_entry("TCS", "CE")
        assert other_result["status"] == "entered", \
            f"a different symbol must still enter fine, got {other_result}"

        print("2. Options: 3 real enter->exit cycles for the SAME symbol succeed, a real 4th "
              "_process_one_entry attempt is rejected (daily_reentry_cap_reached) and places NO "
              "order, while a DIFFERENT symbol enters normally: PASSED")
    finally:
        restore()
        ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL = real_cap
        ote.config.MAX_LIVE_POSITIONS_CE = real_ce_cap


async def test_3_cap_survives_a_simulated_restart():
    """RELIANCE already has exactly 3 real Options entries logged on disk
    from test_2 above (same scratch HISTORY_DIR, same day). A BRAND NEW
    PositionStore - standing in for a freshly restarted process, which
    would have lost every in-memory counter position_store.py itself
    keeps - must still block a 4th entry, since count_opened_today reads
    the durable on-disk log, not anything in-memory."""
    fresh_store = ops.PositionStore()
    ote.position_store = fresh_store
    real_cap = ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL
    ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL = 3
    restore, placed_orders = install_all_dhan_mocks()
    try:
        assert trade_history.count_opened_today("Options", "RELIANCE") == 3, \
            "sanity check: test_2 should have left exactly 3 real entries logged for RELIANCE"
        result = await ote._process_one_entry("RELIANCE", "CE")
        assert result["status"] == "skipped", result
        assert result["reason"] == "daily_reentry_cap_reached", result
        assert placed_orders == [], "a fresh (post-restart) store must still place NO order for a capped symbol"
        print("3. The daily re-entry cap SURVIVES a simulated restart (a brand-new PositionStore "
              "with zero in-memory history still blocks a 4th entry) - proving it's backed by the "
              "durable on-disk log, not an in-memory counter: PASSED")
    finally:
        restore()
        ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL = real_cap


async def test_4_futures_and_luxury_enforce_their_own_independent_cap():
    # Futures
    f_store = fps.PositionStore()
    fte.position_store = f_store
    real_f_cap = fte.config.MAX_DAILY_ENTRIES_PER_SYMBOL
    fte.config.MAX_DAILY_ENTRIES_PER_SYMBOL = 2
    restore, placed_orders = install_all_dhan_mocks()
    try:
        symbol = "WIPRO"
        pos = fps.Position(
            underlying_symbol=symbol, option_trading_symbol=f"{symbol} FAKE EXP CE",
            option_type="CE", quantity=500, lot_size=500, entry_price=50.0, highest_price=50.0,
            target_price=62.5, hard_stop_loss=42.0, order_id="OID-F1", product_type="MARGIN",
        )
        await trade_history.record_opened_position("Futures", pos)
        await trade_history.record_opened_position("Futures", pos)
        await asyncio.sleep(0.2)

        result = await fte._process_one_entry(symbol, "CE")
        assert result["status"] == "skipped", result
        assert result["reason"] == "daily_reentry_cap_reached", result
        assert placed_orders == [], "Futures must place no order once its OWN cap (2) is reached"

        # Options' own count for the SAME symbol is untouched by Futures'
        # activity - independent strategies, independent caps.
        assert trade_history.count_opened_today("Options", symbol) == 0
        print("4a. Futures enforces its own independent daily re-entry cap "
              "(config.MAX_DAILY_ENTRIES_PER_SYMBOL / count_opened_today('Futures', ...)): PASSED")
    finally:
        restore()
        fte.config.MAX_DAILY_ENTRIES_PER_SYMBOL = real_f_cap

    # Luxury
    l_store = lps.PositionStore()
    lte.position_store = l_store
    real_l_cap = lte.config.MAX_DAILY_ENTRIES_PER_SYMBOL
    lte.config.MAX_DAILY_ENTRIES_PER_SYMBOL = 2
    restore, placed_orders = install_all_dhan_mocks()
    try:
        symbol = "INFY"
        pos = lps.Position(
            underlying_symbol=symbol, option_trading_symbol=f"{symbol} FAKE EXP PE",
            option_type="PE", quantity=500, lot_size=500, entry_price=50.0, highest_price=50.0,
            target_price=62.5, hard_stop_loss=42.0, order_id="OID-L1", product_type="MARGIN",
        )
        await trade_history.record_opened_position("Luxury", pos)
        await trade_history.record_opened_position("Luxury", pos)
        await asyncio.sleep(0.2)

        result = await lte._process_one_entry(symbol, "PE")
        assert result["status"] == "skipped", result
        assert result["reason"] == "daily_reentry_cap_reached", result
        assert placed_orders == [], "Luxury must place no order once its OWN cap (2) is reached"
        print("4b. Luxury enforces its own independent daily re-entry cap "
              "(config.MAX_DAILY_ENTRIES_PER_SYMBOL / count_opened_today('Luxury', ...)): PASSED")
    finally:
        restore()
        lte.config.MAX_DAILY_ENTRIES_PER_SYMBOL = real_l_cap


async def test_5_cap_is_genuinely_configurable():
    store = ops.PositionStore()
    ote.position_store = store
    real_cap = ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL
    real_ce_cap = ote.config.MAX_LIVE_POSITIONS_CE
    ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL = 1
    ote.config.MAX_LIVE_POSITIONS_CE = 10
    restore, placed_orders = install_all_dhan_mocks()
    try:
        symbol = "HDFCBANK"
        result_1 = await ote._process_one_entry(symbol, "CE")
        assert result_1["status"] == "entered", result_1
        assert await store.try_start_exit(symbol)
        await store.close_position(symbol, 55.0, "TARGET_HIT")
        await asyncio.sleep(0.3)

        result_2 = await ote._process_one_entry(symbol, "CE")
        assert result_2["status"] == "skipped" and result_2["reason"] == "daily_reentry_cap_reached", result_2
        print("5. The daily re-entry cap is genuinely configurable (re-tested and enforced at 1, "
              "not hardcoded to 3): PASSED")
    finally:
        restore()
        ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL = real_cap
        ote.config.MAX_LIVE_POSITIONS_CE = real_ce_cap


async def main():
    print("=== Daily re-entry cap test suite (Options/Futures/Luxury) ===\n")
    await test_1_count_opened_today()
    await test_2_options_full_cycle_blocks_the_fourth_entry()
    await test_3_cap_survives_a_simulated_restart()
    await test_4_futures_and_luxury_enforce_their_own_independent_cap()
    await test_5_cap_is_genuinely_configurable()
    print("\nALL DAILY RE-ENTRY CAP CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
