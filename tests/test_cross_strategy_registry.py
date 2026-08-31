"""
Tests for cross_strategy_registry.py and its wiring into Options/Futures/
Luxury's _process_one_entry - user request 31 Aug 2026, closing the race
window identified when explaining Luxury's design ("would there be any
race conditions if all 3 webhooks... received triggers for same stock at
same time?").

Covers, against the REAL production functions (not reimplemented):
  1. try_claim/release_claim's own semantics: a free symbol claims
     successfully, an already-claimed-by-another symbol is refused, a
     released claim can be re-claimed, and re-claiming your OWN existing
     claim is a harmless no-op (not an error).
  2. A genuine concurrent race for the SAME symbol via asyncio.gather -
     exactly one of two simultaneous try_claim calls wins, using REAL
     concurrency (not a mocked sequential stand-in for it).
  3. THE scenario this whole feature exists for: Options and Futures (two
     different real packages, two independent PositionStores) racing for
     the literal same underlying via their real _process_one_entry
     functions, fired together with asyncio.gather - exactly one enters,
     the other gets skipped with reason=claimed_by_another_strategy
     BEFORE it ever reaches its own broker check, not after a wasted
     redundant order attempt.
  4. Three-way version of the same race - Options, Futures, AND Luxury
     all racing for one stock at once - still exactly one winner.
  5. The claim is released after a resolved attempt (success or skip),
     so a LATER (non-concurrent) alert for the same stock from a
     different strategy is not permanently blocked by a stale claim.
  6. Different symbols never contend - Options entering RELIANCE and
     Futures entering TCS at the exact same instant both succeed, proving
     this is a per-symbol lock, not a global one (the whole point of
     keeping this from adding real latency to unrelated entries).

HOW TO RUN:
    uv run python tests/test_cross_strategy_registry.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_registry_test_"))
trade_history.HISTORY_DIR = scratch_dir

import cross_strategy_registry as csr
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
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP CALL", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"SECID-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks(entry_delay_seconds: float = 0.0):
    """Same approach as every other deep-integration test in this repo -
    mocks the one real Dhan network boundary all three packages share
    (they're literally the same dhan_wrapper singleton). entry_delay_seconds
    optionally slows down order placement itself, to widen the race window
    enough to reliably exercise it in a test (production has this delay
    for free - a real order-placement round trip; a test needs it injected
    since the mocks otherwise resolve instantly)."""
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

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        if entry_delay_seconds:
            import time
            time.sleep(entry_delay_seconds)
        return {"order_id": f"FAKE-{trading_symbol}-{transaction_type}", "is_amo": False}

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


async def test_1_basic_claim_release_semantics():
    assert await csr.try_claim("RELIANCE", "Options"), "a free symbol must claim successfully"
    assert not await csr.try_claim("RELIANCE", "Futures"), "an already-claimed symbol must refuse a different strategy"
    assert await csr.try_claim("RELIANCE", "Options"), "re-claiming your OWN existing claim must be a harmless no-op"
    await csr.release_claim("RELIANCE", "Futures")  # not the holder - must be a no-op, not an error
    assert csr.snapshot() == {"RELIANCE": "Options"}, "a release from a non-holder must not clear the real claim"
    await csr.release_claim("RELIANCE", "Options")
    assert csr.snapshot() == {}, "release from the actual holder must clear it"
    assert await csr.try_claim("RELIANCE", "Futures"), "a released symbol must be claimable again by anyone"
    await csr.release_claim("RELIANCE", "Futures")
    print("1. try_claim/release_claim basic semantics (claim, refuse, re-claim-own-is-noop, "
          "release-by-non-holder-is-noop, release-then-reclaim): PASSED")


async def test_2_real_concurrent_race_exactly_one_winner():
    results = await asyncio.gather(
        csr.try_claim("TCS", "Options"),
        csr.try_claim("TCS", "Futures"),
        csr.try_claim("TCS", "Luxury"),
    )
    winners = sum(results)
    assert winners == 1, f"expected exactly 1 winner among 3 real concurrent claims, got {winners}: {results}"
    await csr.release_claim("TCS", "Options")
    await csr.release_claim("TCS", "Futures")
    await csr.release_claim("TCS", "Luxury")
    assert csr.snapshot() == {}
    print("2. Real concurrent race (asyncio.gather, not sequential) for one symbol across "
          "3 simultaneous try_claim calls: exactly 1 winner: PASSED")


async def test_3_two_packages_race_for_the_same_stock_through_real_process_one_entry():
    """THE scenario this feature exists for: Options and Futures, two
    different real packages with two independent PositionStores, both
    receive an alert for the exact same underlying at the exact same
    moment. Fires their REAL _process_one_entry functions concurrently -
    not a simulation of the race, the actual race."""
    options_store = ops.PositionStore()
    ote.position_store = options_store
    futures_store = fps.PositionStore()
    fte.position_store = futures_store

    restore = install_all_dhan_mocks(entry_delay_seconds=0.05)
    try:
        options_result, futures_result = await asyncio.gather(
            ote._process_one_entry("RELIANCE", "CE"),
            fte._process_one_entry("RELIANCE", "CE"),
        )
        statuses = {options_result["status"], futures_result["status"]}
        entered_count = sum(1 for r in (options_result, futures_result) if r["status"] == "entered")
        claimed_count = sum(1 for r in (options_result, futures_result) if r.get("reason") == "claimed_by_another_strategy")

        assert entered_count == 1, f"expected exactly 1 real entry across Options+Futures, got {entered_count}: " \
                                    f"options={options_result} futures={futures_result}"
        assert claimed_count == 1, f"expected the loser to be skipped with claimed_by_another_strategy, got: " \
                                    f"options={options_result} futures={futures_result}"
        assert len(options_store.live_positions) + len(futures_store.live_positions) == 1, \
            "RELIANCE must end up live in exactly ONE of the two independent PositionStores, not both"
        assert csr.snapshot() == {}, "both claims must be released after their attempts resolve"
        print(f"3. Options vs Futures racing for the SAME stock via their REAL _process_one_entry, "
              f"fired concurrently: exactly 1 entered, 1 correctly rejected pre-emptively "
              f"(statuses: {statuses}): PASSED")
    finally:
        restore()


async def test_4_three_way_race_still_exactly_one_winner():
    """Same as test 3, but all three real packages (Options/Futures/
    Luxury) racing for one stock at once - confirms the registry scales
    to N strategies, not just 2."""
    options_store = ops.PositionStore()
    ote.position_store = options_store
    futures_store = fps.PositionStore()
    fte.position_store = futures_store
    luxury_store = lps.PositionStore()
    lte.position_store = luxury_store

    restore = install_all_dhan_mocks(entry_delay_seconds=0.05)
    try:
        results = await asyncio.gather(
            ote._process_one_entry("TCS", "CE"),
            fte._process_one_entry("TCS", "CE"),
            lte._process_one_entry("TCS", "CE"),
        )
        entered_count = sum(1 for r in results if r["status"] == "entered")
        claimed_count = sum(1 for r in results if r.get("reason") == "claimed_by_another_strategy")

        assert entered_count == 1, f"expected exactly 1 real entry across all 3 strategies, got {entered_count}: {results}"
        assert claimed_count == 2, f"expected both losers rejected via the registry, got {claimed_count}: {results}"
        total_live = len(options_store.live_positions) + len(futures_store.live_positions) + len(luxury_store.live_positions)
        assert total_live == 1, f"TCS must end up live in exactly ONE of the three independent stores, got {total_live}"
        assert csr.snapshot() == {}
        print("4. Three-way race (Options + Futures + Luxury) for one stock, fired concurrently "
              "via their real _process_one_entry functions: exactly 1 winner, 2 correctly rejected: PASSED")
    finally:
        restore()


async def test_5_claim_released_after_resolution_not_stuck():
    """A resolved attempt (whether it entered or was skipped) must free
    the claim for a LATER, non-concurrent alert - the registry must never
    permanently lock a symbol out after the attempt that claimed it is
    actually done."""
    store = ops.PositionStore()
    ote.position_store = store
    restore = install_all_dhan_mocks()
    try:
        first = await ote._process_one_entry("SBIN", "CE")
        assert first["status"] == "entered", first
        assert csr.snapshot() == {}, "claim must be released once the first attempt resolves"

        # A second, later (not concurrent) alert for the SAME stock -
        # must be rejected by the position_store's own dedup (already
        # live), NOT stuck behind a leftover registry claim.
        second = await ote._process_one_entry("SBIN", "CE")
        assert second["status"] == "skipped", second
        assert second["reason"] == "duplicate_or_capacity_full", \
            f"expected the position_store's own dedup to catch this, not a stale registry claim: {second}"
        print("5. A claim is released after its attempt resolves - a later, non-concurrent alert "
              "for the same stock is judged on its own merits (position_store dedup), not stuck "
              "behind a stale claim: PASSED")
    finally:
        restore()


async def test_6_different_symbols_never_contend():
    """Options entering RELIANCE and Futures entering TCS at the exact
    same instant must both succeed with no interaction at all - proves
    this is a per-symbol lock, not a global one. This is what keeps the
    registry from adding real latency: unrelated entries never wait on
    each other."""
    options_store = ops.PositionStore()
    ote.position_store = options_store
    futures_store = fps.PositionStore()
    fte.position_store = futures_store

    restore = install_all_dhan_mocks()
    try:
        options_result, futures_result = await asyncio.gather(
            ote._process_one_entry("RELIANCE", "CE"),
            fte._process_one_entry("TCS", "CE"),
        )
        assert options_result["status"] == "entered", options_result
        assert futures_result["status"] == "entered", futures_result
        print("6. Different symbols (RELIANCE via Options, TCS via Futures) claimed at the exact "
              "same instant never contend - both enter successfully, proving this is a per-symbol "
              "lock, not a global one: PASSED")
    finally:
        restore()


async def main():
    print("=== cross_strategy_registry.py test suite ===\n")
    await test_1_basic_claim_release_semantics()
    await test_2_real_concurrent_race_exactly_one_winner()
    await test_3_two_packages_race_for_the_same_stock_through_real_process_one_entry()
    await test_4_three_way_race_still_exactly_one_winner()
    await test_5_claim_released_after_resolution_not_stuck()
    await test_6_different_symbols_never_contend()
    print("\nALL CROSS-STRATEGY REGISTRY CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
