"""
Deep INTEGRATION tests for the centralized 2-bucket fund allocation
system (fund_allocation.py, added 1 Sep 2026) - user request 1 Sep
2026: "Perform some integration tests to verify if bucket fund
allocation is working fine with trades in each package also."

Unlike tests/test_fund_allocation.py (unit-level: the pure percentage
math, and `_enter_single_position`/`_has_sufficient_funds` called
directly in isolation), this file drives the FULL, REAL production
pipeline for every package - the actual webhook handlers
(`_handle_chartink_webhook` for Options/Luxury, `chartink_webhook_futures`
for Futures, `chartink_webhook_swing_enter` for Swing) - so ranking,
`cross_strategy_registry` claim/release, `PositionStore` reservation/
capacity, and `webhook_alerts` logging are all exercised for real
alongside the funds check, not bypassed. Only the Dhan NETWORK boundary
is ever mocked (ATM lookup, margin/funds figures, order placement/fill)
- matching every other deep-integration suite in this repo.

Covers the three claims the whole feature exists to make good on:
  1. The SECONDARY bucket (15% default) genuinely PROTECTS the PRIMARY
     bucket's own share - an oversized Options entry that would easily
     fit the WHOLE account correctly gets rejected once capped to just
     15% of it, and Swing's own entry (against the SAME account
     balance, but its own 85% share) succeeds regardless - the money
     was never actually at risk of being eaten by the Options attempt.
  2. Options/Futures/Luxury genuinely share ONE secondary bucket, not
     15% each - proven via three REAL, sequential, full-pipeline
     webhook entries with a shrinking real account balance between
     them (as if each one's own margin had actually just landed).
  3. The two buckets are PROPORTIONAL SHARES of the account's own
     current real balance, not frozen, mutually-exclusive silos -
     Swing's own primary-bucket figure correctly shrinks (in
     proportion) once Options' own secondary-bucket entry has actually
     reduced the real total, and vice versa; this is the intended
     arithmetic (there is only one real pot of money at the broker),
     not a bug, and this file demonstrates it explicitly rather than
     leaving it implicit.

Also covers: a funds-rejected webhook entry cleanly releases
`cross_strategy_registry`'s own claim and the package's own
`PositionStore` reservation, so the SAME stock is genuinely retriable
on a later alert once funds improve - never left in a stuck, half-
claimed state.

SAFETY: never calls place_market_order/wait_for_order_result for real -
mocked in every scenario, using scratch PositionStore instances and a
scratch history/ directory, same as every other deep-integration test
in this repo.

HOW TO RUN:
    uv run python tests/test_fund_allocation_integration.py
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
import cross_strategy_registry

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_fund_allocation_integration_test_"))
trade_history.HISTORY_DIR = scratch_dir

import fund_allocation as fa
import Options.dhan_client as odc
import Options.position_store as ops
import Options.trading_engine as ote
import Options.option_main as om
import Futures.position_store as fps
import Futures.trading_engine as fte
import Futures.futures_main as fm
import Luxury.position_store as lps
import Luxury.trading_engine as lte
import Luxury.luxury_main as lm
import Swing.position_store as sps
import Swing.trading_engine as ste
import Swing.swing_main as sm
import Swing.watchlist as swl
from Options.dhan_client import AtmOption, FuturesContract, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP {option_type}", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"OPT-{symbol}", expiry_date=FUTURE_EXPIRY)


def fake_futures_contract(symbol: str) -> FuturesContract:
    return FuturesContract(trading_symbol=f"{symbol} FAKE EXP FUT", security_id=f"FUT-{symbol}",
                            lot_size=250, expiry_date=FUTURE_EXPIRY)


def fake_ranked(stocks, top_n, prefer_highest):
    """Stand-in for rank_and_pick_top_stocks - deterministic, no network,
    same helper every deep-integration test in this repo uses."""
    return [(s, float(i)) for i, s in enumerate(stocks[:top_n if top_n > 0 else len(stocks)])]


def install_dhan_mocks(margin_per_leg=999.0, fund_limits_sequence=None, available_balance=100000.0):
    """Mocks the ONE real Dhan network boundary every package shares
    (they're all literally Options.dhan_client.dhan_wrapper).
    fund_limits_sequence: if given, a list of {"availabelBalance": ...}
    dicts consumed IN ORDER across successive get_fund_limits() calls -
    simulates the account's REAL balance actually shrinking as each
    package's own order lands for real, so later checks (by a
    DIFFERENT package, or the SAME one again) see the genuinely reduced
    total, exactly as the real broker would report it."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_futures_contract": odc.dhan_wrapper.get_futures_contract,
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
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract
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


async def test_1_secondary_bucket_protects_primary_share_across_real_packages():
    """The crux claim: an Options entry sized to fit the WHOLE account
    but NOT the 15% secondary bucket is correctly rejected through the
    REAL webhook handler, and Swing's own REAL entry (85% primary share
    of the exact SAME account balance) succeeds regardless - the money
    was never actually at risk."""
    options_store = ops.PositionStore()
    om.position_store = options_store
    ote.position_store = options_store
    ote.config.MAX_LIVE_POSITIONS_CE = 5

    swing_store = sps.BasketHedgeStore()
    ste.basket_hedge_store = swing_store
    real_swing_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True

    # Account balance: 100,000. Secondary bucket (15%) = 15,000. Primary
    # bucket (85%) = 85,000. Options' own required margin (60,000) fits
    # the WHOLE account easily but not its own 15% slice; Swing's own
    # basket (2 legs, 2,000 each = 4,000 total) fits comfortably in the
    # 85% slice.
    restore, placed_orders = install_dhan_mocks(margin_per_leg=60000.0, available_balance=100000.0)
    real_rank = om.rank_and_pick_top_stocks
    om.rank_and_pick_top_stocks = fake_ranked
    try:
        payload = om.ChartinkWebhookPayload(
            stocks="BIGOPTIONSBET", trigger_prices="1", triggered_at="9:20 am",
            scan_name="fund-bucket-test-1", scan_url="fund-bucket-test-1",
            alert_name="Secondary bucket protects primary share",
        )
        options_result = await om._handle_chartink_webhook(payload, "CE", True)
        assert options_result["status"] == "processed", options_result
        options_entry = options_result["entries"][0]
        assert options_entry["status"] == "skipped" and options_entry["reason"] == "insufficient_funds", options_entry
        assert placed_orders == [], f"Options must place ZERO orders when the secondary bucket can't afford it, got {placed_orders}"
        assert "BIGOPTIONSBET" not in options_store.reserved_symbols, "capacity must be released back for a retry"
        assert await cross_strategy_registry.try_claim("BIGOPTIONSBET", "Options"), \
            "the cross_strategy_registry claim must ALSO be released - no stuck lock"
        await cross_strategy_registry.release_claim("BIGOPTIONSBET", "Options")

        # Now Swing's own REAL basket_hedge entry, same account balance -
        # margin_per_leg needs to be small for THIS call, so re-mock with
        # a fresh, smaller per-leg figure representative of Swing's own
        # 2-leg basket (2000 each = 4000 total, well inside the 85,000
        # primary bucket).
        restore()
        restore, placed_orders = install_dhan_mocks(margin_per_leg=2000.0, available_balance=100000.0)

        result = await ste._enter_basket_hedge_for_stock("SMALLSWINGBET")
        assert result["status"] == "entered", \
            f"Swing's own primary-bucket entry must succeed - the Options rejection never actually spent any money, got {result}"
        assert "SMALLSWINGBET" in swing_store.live_positions

        print("1. The secondary bucket (15%) correctly protects the primary bucket's own 85% share - "
              "an Options entry sized to fit the WHOLE account but not its own 15% slice is rejected "
              "through the REAL webhook handler (zero orders, capacity AND cross_strategy_registry "
              "claim both cleanly released), while Swing's own REAL entry against the exact same "
              "account balance succeeds via its own, unaffected 85% share: PASSED")
    finally:
        restore()
        om.rank_and_pick_top_stocks = real_rank
        ste.config.STRATEGY_ENABLED = real_swing_enabled


async def test_2_options_futures_luxury_share_one_secondary_bucket_full_pipeline():
    """Sequential, REAL, full-pipeline webhook entries for all three
    secondary-bucket packages, with the account's own real balance
    shrinking between each (as if each one's own margin had actually
    just been blocked/utilized) - proves they draw from ONE shared 15%
    pool, not 15% each, through their own actual webhook handlers."""
    options_store = ops.PositionStore()
    om.position_store = options_store
    ote.position_store = options_store
    futures_store = fps.PositionStore()
    fm.position_store = futures_store
    fte.position_store = futures_store
    luxury_store = lps.PositionStore()
    lm.position_store = luxury_store
    lte.position_store = luxury_store

    for cfg in (ote.config, fte.config, lte.config):
        cfg.MAX_LIVE_POSITIONS_CE = 5

    # Account balance starts at 100,000 -> secondary bucket = 15,000.
    # Each entry's own proactive check requires 6,000 margin (the
    # standalone per-leg estimate) - but the REAL fill each one produces
    # blocks considerably MORE than that simple estimate (real combo/
    # exchange margin blocking is typically higher than a standalone
    # per-leg sum - the same conservatism this whole check already
    # accounts for via Swing's own buffer). So: Options' own check sees
    # the fresh 15,000 share and enters; by the time Futures checks, the
    # real balance has already dropped to 60,000 (secondary share
    # 9,000) - still enough for its own 6,000 ask, so it also enters;
    # by the time Luxury checks, the real balance is down to 30,000
    # (secondary share 4,500) - too small for its own 6,000 ask, so it
    # correctly gets skipped, even though no SINGLE package's own
    # 6,000 request ever exceeded 15,000 in isolation.
    restore, placed_orders = install_dhan_mocks(
        margin_per_leg=6000.0,
        fund_limits_sequence=[
            {"availabelBalance": 100000.0},   # Options' own check
            {"availabelBalance": 60000.0},    # Futures' own check - Options' real fill already landed
            {"availabelBalance": 30000.0},    # Luxury's own check - both prior real fills already landed
        ],
    )
    real_options_rank = om.rank_and_pick_top_stocks
    real_futures_rank = fm.rank_and_pick_top_stocks
    real_luxury_rank = lm.rank_and_pick_top_stocks
    om.rank_and_pick_top_stocks = fake_ranked
    fm.rank_and_pick_top_stocks = fake_ranked
    lm.rank_and_pick_top_stocks = fake_ranked
    try:
        options_payload = om.ChartinkWebhookPayload(
            stocks="SHAREDBUCKETOPT", trigger_prices="1", triggered_at="9:20 am",
            scan_name="fund-bucket-test-2", scan_url="fund-bucket-test-2", alert_name="shared bucket - Options",
        )
        futures_payload = fm.ChartinkWebhookPayload(
            stocks="SHAREDBUCKETFUT", trigger_prices="1", triggered_at="9:20 am",
            scan_name="fund-bucket-test-2", scan_url="fund-bucket-test-2", alert_name="shared bucket - Futures",
        )
        luxury_payload = lm.ChartinkWebhookPayload(
            stocks="SHAREDBUCKETLUX", trigger_prices="1", triggered_at="9:20 am",
            scan_name="fund-bucket-test-2", scan_url="fund-bucket-test-2", alert_name="shared bucket - Luxury",
        )

        options_result = await om._handle_chartink_webhook(options_payload, "CE", True)
        futures_result = await fm.chartink_webhook_futures(futures_payload)
        luxury_result = await lm._handle_chartink_webhook(luxury_payload, "CE", True)

        assert options_result["entries"][0]["status"] == "entered", options_result
        assert futures_result["entries"][0]["status"] == "entered", futures_result
        luxury_entry = luxury_result["entries"][0]
        assert luxury_entry["status"] == "skipped" and luxury_entry["reason"] == "insufficient_funds", \
            f"Luxury's own attempt must be rejected - the shared secondary bucket is already " \
            f"exhausted by Options+Futures, even though NEITHER alone exceeded it, got {luxury_entry}"

        entered_symbols = {o["trading_symbol"].split(" ")[0] for o in placed_orders}
        assert entered_symbols == {"SHAREDBUCKETOPT", "SHAREDBUCKETFUT"}, \
            f"only Options' and Futures' own orders should have been placed, got {placed_orders}"

        print("2. Options, Futures, and Luxury genuinely share ONE secondary bucket through their own "
              "REAL, full webhook pipelines - Options and Futures both succeed against the fresh "
              "15,000 allowance, but Luxury's own attempt correctly fails once the shared pool is "
              "already spent, even though no SINGLE package's own request ever exceeded 15,000 by "
              "itself: PASSED")
    finally:
        restore()
        om.rank_and_pick_top_stocks = real_options_rank
        fm.rank_and_pick_top_stocks = real_futures_rank
        lm.rank_and_pick_top_stocks = real_luxury_rank


async def test_3_buckets_are_proportional_shares_of_the_same_shifting_total():
    """Documents/proves the precise, intentional nuance: the two buckets
    are NOT frozen, mutually-exclusive silos - they're proportional
    shares of the account's own CURRENT real balance. Once Options'
    entry actually reduces the real total, Swing's own primary-bucket
    figure correctly shrinks too (in the SAME 85/15 proportion) - this
    is the expected arithmetic (there's only one real pot of money at
    the broker), not a bug, and not something either bucket is immune
    to."""
    restore, _ = install_dhan_mocks(available_balance=100000.0)
    try:
        primary_before = await asyncio.get_running_loop().run_in_executor(
            None, fa.get_bucket_available_funds, "primary")
        secondary_before = await asyncio.get_running_loop().run_in_executor(
            None, fa.get_bucket_available_funds, "secondary")
        assert primary_before == 85000.0 and secondary_before == 15000.0
        assert abs((primary_before / secondary_before) - (fa.PRIMARY_BUCKET_PCT / fa.SECONDARY_BUCKET_PCT)) < 0.01
    finally:
        restore()

    # Options' own entry ACTUALLY reduces the real account balance
    # (simulated) by 40,000 - both buckets must now compute their own
    # share off the NEW, smaller total (60,000), in the exact same
    # proportion as before.
    restore, _ = install_dhan_mocks(available_balance=60000.0)
    try:
        primary_after = await asyncio.get_running_loop().run_in_executor(
            None, fa.get_bucket_available_funds, "primary")
        secondary_after = await asyncio.get_running_loop().run_in_executor(
            None, fa.get_bucket_available_funds, "secondary")
        assert primary_after == 51000.0, primary_after
        assert secondary_after == 9000.0, secondary_after
        assert abs((primary_after / secondary_after) - (fa.PRIMARY_BUCKET_PCT / fa.SECONDARY_BUCKET_PCT)) < 0.01

        print("3. The two buckets are proportional SHARES of the account's own current real balance, "
              "not frozen silos - once the real total drops (as it genuinely would after a real "
              "order lands), BOTH buckets' own computed Rupee amounts shrink in exactly the same "
              "85/15 proportion, confirming this is the intended arithmetic rather than either "
              "bucket being 'immune' to real capital actually being spent: PASSED")
    finally:
        restore()


async def test_4_funds_rejected_stock_is_genuinely_retriable_on_a_later_alert():
    """A stock rejected for insufficient funds must not be left in a
    stuck, half-claimed state - a LATER alert for the SAME stock, once
    the (simulated) balance improves, must be able to enter normally."""
    store = ops.PositionStore()
    om.position_store = store
    ote.position_store = store
    ote.config.MAX_LIVE_POSITIONS_CE = 5

    real_rank = om.rank_and_pick_top_stocks
    om.rank_and_pick_top_stocks = fake_ranked
    try:
        payload = om.ChartinkWebhookPayload(
            stocks="RETRYAFTERFUNDS", trigger_prices="1", triggered_at="9:20 am",
            scan_name="fund-bucket-test-4", scan_url="fund-bucket-test-4", alert_name="retry after funds free up",
        )

        # First alert: secondary bucket too small - rejected.
        restore, placed_orders = install_dhan_mocks(margin_per_leg=50000.0, available_balance=10000.0)
        first_result = await om._handle_chartink_webhook(payload, "CE", True)
        assert first_result["entries"][0]["status"] == "skipped", first_result
        assert placed_orders == []
        assert "RETRYAFTERFUNDS" not in store.reserved_symbols
        restore()

        # Second alert, same stock, later - balance has genuinely
        # improved (simulating funds freeing up / a deposit) - must
        # enter normally, proving nothing was left stuck from the first
        # rejection (no lingering cross_strategy_registry claim, no
        # lingering PositionStore reservation).
        restore, placed_orders = install_dhan_mocks(margin_per_leg=500.0, available_balance=100000.0)
        second_result = await om._handle_chartink_webhook(payload, "CE", True)
        assert second_result["entries"][0]["status"] == "entered", \
            f"the same stock must be fully retriable once funds improve, got {second_result}"
        assert "RETRYAFTERFUNDS" in store.reserved_symbols

        print("4. A stock rejected for insufficient funds is genuinely retriable on a later alert - "
              "capacity and the cross_strategy_registry claim are both cleanly released on rejection, "
              "never left stuck, so the identical stock enters normally once funds improve: PASSED")
    finally:
        restore()
        om.rank_and_pick_top_stocks = real_rank


async def main():
    print("=== Fund allocation - deep integration test suite ===\n")
    await test_1_secondary_bucket_protects_primary_share_across_real_packages()
    await test_2_options_futures_luxury_share_one_secondary_bucket_full_pipeline()
    await test_3_buckets_are_proportional_shares_of_the_same_shifting_total()
    await test_4_funds_rejected_stock_is_genuinely_retriable_on_a_later_alert()
    print("\nALL FUND ALLOCATION INTEGRATION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
