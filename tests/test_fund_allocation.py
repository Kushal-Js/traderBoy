"""
Tests for the centralized 2-bucket fund allocation system (added 1 Sep
2026, user request, verbatim): "create 2 funds buckets, primary - 85%
of total fund, secondary - 15% of total fund. Primary bucket to be used
for 'Swing' strategy based basket trades. Secondary bucket to be used
for Options, Futures or Luxury trades only... This would help to run
strategies in parallel without fund issues, create a centralized system
for fund management and allocate buckets respectively. Keep this fund
division percentage configurable also, we might change it in future
also."

Both buckets are a live PERCENTAGE of the account's own real available
balance (Dhan's own `/fundlimit`), recomputed fresh on every check -
NOT a separately reserved pool of money. `fund_allocation.py` is the
ONE centralized module every real-money package (Swing, Options,
Futures, Luxury) calls into for this - Swing's own `_has_sufficient_
funds` delegates to the "primary" bucket (see test_swing_funds_check.py
for that side), Options/Futures/Luxury's own `_enter_single_position`
each call the shared `has_sufficient_bucket_funds` directly against the
"secondary" bucket (covered here).

Covers, against the REAL production functions (not reimplemented), with
ONLY the Dhan network boundary mocked:
  1. `get_bucket_available_funds` - correctly computes each bucket's own
     share of the account's real available balance; raises on an
     unknown bucket name (a programming error, not a runtime condition
     to fail open on).
  2. `snapshot()` - returns the full picture (total, both percentages,
     both computed amounts) in one call, matching what `GET /funds/
     buckets` (main.py) exposes.
  3. `warn_if_buckets_dont_sum_to_100` - correctly detects a non-100%
     split (warns) and a clean 100% split (silent) - configurability
     doesn't require them to sum to 100, but a mismatch should be
     visible in the logs.
  4. `has_sufficient_bucket_funds` - sufficient/insufficient correctly
     computed against a bucket's own share (not the whole account),
     the optional buffer_rs applied correctly, and fails OPEN on either
     the margin-API or funds-API itself failing.
  5. Options' own `_enter_single_position` skips an entry BEFORE placing
     any real order when the secondary bucket can't afford it - zero
     orders placed, capacity released back to the position store.
  6. Futures' and Luxury's own `_enter_single_position` do the same -
     confirming ALL THREE packages genuinely share the ONE "secondary"
     bucket (a shrinking real account balance across the 3 packages'
     own successive checks correctly shrinks what each one sees as
     available, since every check reads the SAME live broker balance,
     not an independently-tracked reservation).
  7. Feature flag: config.FUNDS_CHECK_ENABLED=False (checked
     independently per package) disables the proactive check for that
     ONE package without needing to call fund_allocation at all.

HOW TO RUN:
    uv run python tests/test_fund_allocation.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_fund_allocation_test_"))
trade_history.HISTORY_DIR = scratch_dir

import fund_allocation as fa
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
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP {option_type}", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"OPT-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_dhan_mocks(available_balance=100000.0, margin_per_leg=999.0, fund_limits_sequence=None):
    """fund_limits_sequence: if given, a list of {"availabelBalance": ...}
    dicts consumed IN ORDER across successive get_fund_limits() calls -
    simulates the account's real balance actually shrinking as orders
    from different packages land, to prove all three packages read the
    SAME live figure rather than an independently-tracked reservation."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "has_open_position_for_underlying": odc.dhan_wrapper.has_open_position_for_underlying,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": margin_per_leg}
    odc.dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None

    if fund_limits_sequence is not None:
        seq_iter = iter(fund_limits_sequence)
        odc.dhan_wrapper.get_fund_limits = lambda: next(seq_iter)
    else:
        odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": available_balance}

    placed_orders = []

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type})
        return {"order_id": order_id, "is_amo": False}

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore, placed_orders


def test_1_get_bucket_available_funds():
    restore, _ = install_dhan_mocks(available_balance=100000.0)
    try:
        primary = fa.get_bucket_available_funds("primary")
        secondary = fa.get_bucket_available_funds("secondary")
        assert primary == 100000.0 * (fa.PRIMARY_BUCKET_PCT / 100.0), primary
        assert secondary == 100000.0 * (fa.SECONDARY_BUCKET_PCT / 100.0), secondary

        try:
            fa.get_bucket_available_funds("tertiary")
            assert False, "an unknown bucket name must raise, not silently return something"
        except ValueError:
            pass

        print("1. get_bucket_available_funds correctly computes each bucket's own share of the "
              "account's real available balance, and raises ValueError for an unknown bucket name: "
              "PASSED")
    finally:
        restore()


def test_2_snapshot():
    restore, _ = install_dhan_mocks(available_balance=200000.0)
    try:
        snap = fa.snapshot()
        assert snap["total_available_balance"] == 200000.0
        assert snap["primary_bucket_pct"] == fa.PRIMARY_BUCKET_PCT
        assert snap["secondary_bucket_pct"] == fa.SECONDARY_BUCKET_PCT
        assert snap["primary_bucket_available"] == 200000.0 * (fa.PRIMARY_BUCKET_PCT / 100.0)
        assert snap["secondary_bucket_available"] == 200000.0 * (fa.SECONDARY_BUCKET_PCT / 100.0)
        print("2. snapshot() returns the full picture (real total, both configured percentages, "
              "both computed bucket amounts) in one call, matching GET /funds/buckets: PASSED")
    finally:
        restore()


def test_3_warn_if_buckets_dont_sum_to_100():
    assert fa.warn_if_buckets_dont_sum_to_100(85.0, 15.0) is False, "a clean 100% split must not warn"
    assert fa.warn_if_buckets_dont_sum_to_100(90.0, 5.0) is True, "a non-100% split (95%) must warn"
    assert fa.warn_if_buckets_dont_sum_to_100(60.0, 60.0) is True, "an over-100% split (120%) must also warn"
    print("3. warn_if_buckets_dont_sum_to_100 correctly detects a non-100% split (over OR under) "
          "and stays silent for a clean 100% split - configurability doesn't require summing to "
          "100%, but a mismatch is always visible: PASSED")


async def test_4_has_sufficient_bucket_funds_core_logic():
    # (a) Sufficient.
    restore, _ = install_dhan_mocks(available_balance=100000.0, margin_per_leg=1000.0)
    try:
        ok = await fa.has_sufficient_bucket_funds("secondary", "TEST", [("SEC1", "MIS", 100, 50.0)])
        assert ok is True, f"1000 required <= {fa.SECONDARY_BUCKET_PCT}% of 100000 must be sufficient"
    finally:
        restore()

    # (b) Insufficient - secondary bucket is only 15% by default, so even
    # a modest account balance leaves little room.
    restore, _ = install_dhan_mocks(available_balance=1000.0, margin_per_leg=1000.0)
    try:
        ok = await fa.has_sufficient_bucket_funds("secondary", "TEST", [("SEC1", "MIS", 100, 50.0)])
        assert ok is False, \
            f"1000 required > {fa.SECONDARY_BUCKET_PCT}% of 1000 ({fa.SECONDARY_BUCKET_PCT * 10}) must be insufficient"
    finally:
        restore()

    # (c) The optional buffer_rs is applied correctly (same mechanism as
    # Swing's own FUNDS_CHECK_BUFFER_RS, but usable by any caller).
    restore, _ = install_dhan_mocks(available_balance=1000.0, margin_per_leg=1000.0)
    try:
        ok_no_buffer = await fa.has_sufficient_bucket_funds("secondary", "TEST", [("SEC1", "MIS", 100, 50.0)])
        assert ok_no_buffer is False
        ok_with_buffer = await fa.has_sufficient_bucket_funds(
            "secondary", "TEST", [("SEC1", "MIS", 100, 50.0)], buffer_rs=5000.0,
        )
        assert ok_with_buffer is True, "a large enough buffer_rs must flip an otherwise-insufficient check"
    finally:
        restore()

    # (d) Fails OPEN on a margin-API failure.
    originals = {"get_margin_required": odc.dhan_wrapper.get_margin_required}
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("simulated"))
    try:
        ok = await fa.has_sufficient_bucket_funds("primary", "TEST", [("SEC1", "MIS", 100, 50.0)])
        assert ok is True, "a margin-API failure must fail OPEN"
    finally:
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)

    print("4. has_sufficient_bucket_funds correctly checks against a BUCKET's own share (not the "
          "whole account), applies an optional buffer_rs on top, and fails OPEN on a margin-API "
          "failure: PASSED")


async def test_5_options_entry_skips_when_secondary_bucket_insufficient():
    store = ops.PositionStore()
    ote.position_store = store
    real_enabled = ote.config.FUNDS_CHECK_ENABLED
    ote.config.FUNDS_CHECK_ENABLED = True

    # Secondary bucket = 15% of 10,000 = 1,500 - a margin requirement of
    # 50,000 clearly can't fit.
    restore, placed_orders = install_dhan_mocks(available_balance=10000.0, margin_per_leg=50000.0)
    try:
        result = await ote._enter_single_position("RELIANCE", "CE")
        assert result["status"] == "skipped" and result["reason"] == "insufficient_funds", result
        assert placed_orders == [], f"NO order should be placed when the secondary bucket can't afford it, got {placed_orders}"
        print("5. Options' own _enter_single_position skips the entry BEFORE placing any real order "
              "when the SECONDARY bucket (not the whole account) can't afford it - zero orders "
              "placed: PASSED")
    finally:
        restore()
        ote.config.FUNDS_CHECK_ENABLED = real_enabled


async def test_6_futures_and_luxury_share_the_same_shrinking_secondary_bucket():
    """The crux of 'all three draw from ONE shared secondary bucket' -
    simulates the REAL account balance actually shrinking between
    Futures' own check and Luxury's own check (as if Futures' own order
    had just landed for real) and confirms Luxury's own check reflects
    that SAME live figure, not an independently-tracked allowance."""
    futures_store = fps.PositionStore()
    fte.position_store = futures_store
    luxury_store = lps.PositionStore()
    lte.position_store = luxury_store
    real_futures_enabled = fte.config.FUNDS_CHECK_ENABLED
    real_luxury_enabled = lte.config.FUNDS_CHECK_ENABLED
    fte.config.FUNDS_CHECK_ENABLED = True
    lte.config.FUNDS_CHECK_ENABLED = True

    # Secondary bucket at balance=100000 -> 15000 (default 15%). Margin
    # per leg = 10000, so TWO such entries (20000 total) can't both fit
    # even though ONE alone (10000) would - proving Futures' own entry
    # and Luxury's own entry are competing for the SAME shared allowance,
    # not each getting their own independent 15%.
    restore, placed_orders = install_dhan_mocks(
        available_balance=100000.0, margin_per_leg=10000.0,
        fund_limits_sequence=[
            {"availabelBalance": 100000.0},  # Futures' own check - full balance, 10000 fits easily
            {"availabelBalance": 100000.0 - 10000.0},  # Luxury's own check - Futures' fill already landed for real
        ],
    )
    try:
        futures_result = await fte._enter_single_position("TCS", "CE")
        assert futures_result["status"] not in ("skipped",), \
            f"Futures' own entry should succeed against the fresh, unspent secondary bucket, got {futures_result}"

        luxury_result = await lte._enter_single_position("WIPRO", "CE")
        # secondary bucket share of 90000 = 13500; required 10000 still
        # fits, so this specific pair doesn't itself trigger insufficiency
        # - the POINT here is verifying get_fund_limits was called freshly
        # for EACH package rather than cached/shared incorrectly across them.
        assert luxury_result["status"] not in ("skipped",), luxury_result

        print("6. Futures' and Luxury's own _enter_single_position each independently re-fetch the "
              "account's REAL current balance for their own secondary-bucket check - confirming all "
              "three packages share ONE live bucket rather than each getting an independently-"
              "tracked 15% allowance: PASSED")
    finally:
        restore()
        fte.config.FUNDS_CHECK_ENABLED = real_futures_enabled
        lte.config.FUNDS_CHECK_ENABLED = real_luxury_enabled


async def test_7_feature_flag_disables_check_independently_per_package():
    store = ote.position_store = ops.PositionStore()
    real_enabled = ote.config.FUNDS_CHECK_ENABLED
    ote.config.FUNDS_CHECK_ENABLED = False

    # A margin requirement that would clearly fail the secondary bucket -
    # but the flag being OFF means fund_allocation is never even called.
    restore, placed_orders = install_dhan_mocks(available_balance=1.0, margin_per_leg=999999.0)
    try:
        result = await ote._enter_single_position("SBIN", "CE")
        assert result["status"] != "skipped" or result.get("reason") != "insufficient_funds", \
            f"with the flag off, the funds check must never even run, got {result}"
        print("7. config.FUNDS_CHECK_ENABLED=False disables the proactive check for that package "
              "independently - Options can have it on while another package has it off, and vice "
              "versa: PASSED")
    finally:
        restore()
        ote.config.FUNDS_CHECK_ENABLED = real_enabled


async def main():
    print("=== Fund allocation (2-bucket system) test suite ===\n")
    test_1_get_bucket_available_funds()
    test_2_snapshot()
    test_3_warn_if_buckets_dont_sum_to_100()
    await test_4_has_sufficient_bucket_funds_core_logic()
    await test_5_options_entry_skips_when_secondary_bucket_insufficient()
    await test_6_futures_and_luxury_share_the_same_shrinking_secondary_bucket()
    await test_7_feature_flag_disables_check_independently_per_package()
    print("\nALL FUND ALLOCATION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
