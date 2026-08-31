"""
Deep integration test suite - exercises the REAL webhook handler / entry /
exit / reconciliation code end-to-end, not reimplemented and not purely
unit-level mocks. The only thing ever faked is the Dhan NETWORK boundary
(ranking's day-change fetch, ATM lookup, broker-position lookup, order
placement/fill confirmation) - every bit of actual LOGIC (locking,
capacity, dedup, retry-wrapping, attribution, exit-reason evaluation) runs
for real, through the real production functions.

Written 31 Aug 2026 (user request: "do deep paper testing to make sure
everything works fine when market opens and real trades comes in"),
covering the concurrency/reconciliation/retry/lag fixes from that same
session (see NOTES.md entries #43-52 for the full history and the real
bugs each test scenario is guarding against).

WHY EVERY DHAN CALL IS MOCKED, NOT JUST ORDER PLACEMENT: the first version
of this suite used real (read-only) Dhan calls for ranking/ATM lookup and
tripped Dhan's own authentication rate limiter after the many separate
local test-script logins already run that same night ("Too many attempts.
Please try again after sometime.") - a real operational constraint, not a
bug (confirmed the live droplet was completely unaffected throughout - it
uses its own already-cached session, independent of any local script's
own fresh login). This version needs no live Dhan session at all, so it
can be run anytime - repeatedly, offline, before a deploy, whenever you
want confidence in the concurrency/retry/reconciliation logic - without
that risk or dependency.

SAFETY: never calls place_market_order/wait_for_order_result for real
(mocked in every scenario) - this file cannot place a real order no
matter how it's run. Uses a scratch history/ directory and scratch,
standalone PositionStore instances per test - never touches the real
module-level singletons or the real trade_history.py logs, so running
this can never affect real bot state.

HOW TO RUN:
    uv run python tests/test_deep_integration.py

Not wired into any CI/deploy step and not pytest-based (a plain
asyncio.run(main()) script, matching every other test in this codebase's
history - see scratchpad tests referenced in NOTES.md) - run manually
whenever you want to validate the concurrency/exit/reconciliation logic
still holds, e.g. after touching position_store.py, trading_engine.py, or
trade_history.py in either Options/ or Futures/.
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")  # harmless placeholder - .env's real value below takes precedence
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_deep_integration_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Options.position_store as ops
import Options.trading_engine as ote
import Options.option_main as om
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

REAL_STOCKS = ["RELIANCE", "TCS", "SBIN", "HDFCBANK", "ICICIBANK"]
FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP CALL" if option_type == "CE" else f"{symbol} FAKE EXP PUT",
                      strike=1000.0, option_type=option_type, lot_size=500,
                      security_id=f"SECID-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    """Mocks EVERY Dhan network call the entry/exit path touches - no live
    session needed at all (see module docstring for why). Returns a
    restore function; callers MUST call it in a finally block."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "get_open_fno_positions": odc.dhan_wrapper.get_open_fno_positions,
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
        return {"order_id": f"FAKE-{trading_symbol}-{transaction_type}-{time.time_ns()}", "is_amo": False}

    def fake_wait_for_order_result(order_id, is_amo=False):
        return OrderResult(order_id=order_id, status=OrderStatus.TRADED, remark="",
                            fill_price=50.0, filled_quantity=500, is_amo=False)

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.wait_for_order_result = fake_wait_for_order_result

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)

    return restore


def fake_ranked(stocks, top_n, prefer_highest):
    """Stand-in for rank_and_pick_top_stocks - deterministic, no network.
    Mirrors production's real behavior of trimming to top_n BEFORE
    capacity is even checked - see test 1's own comment for why this
    matters to get right."""
    return [(s, float(i)) for i, s in enumerate(stocks[:top_n if top_n > 0 else len(stocks)])]


async def test_1_real_concurrent_entry_through_real_webhook_handler():
    store = ops.PositionStore()
    om.position_store = store
    ote.position_store = store
    ote.config.MAX_LIVE_POSITIONS_CE = 3

    restore = install_all_dhan_mocks()
    real_rank = om.rank_and_pick_top_stocks
    om.rank_and_pick_top_stocks = fake_ranked
    try:
        payload = om.ChartinkWebhookPayload(
            stocks=",".join(REAL_STOCKS), trigger_prices="1,1,1,1,1",
            triggered_at="9:20 am", scan_name="deep-test-1", scan_url="deep-test-1",
            alert_name="Deep integration test 1 - concurrent entry + capacity",
        )
        start = time.monotonic()
        result = await om._handle_chartink_webhook(payload, "CE", True)
        elapsed = time.monotonic() - start

        assert result["status"] == "processed", result
        entered = [e for e in result["entries"] if e["status"] == "entered"]
        skipped = [e for e in result["entries"] if e["status"] == "skipped"]
        print(f"  entries: {[(e['symbol'], e['status']) for e in result['entries']]}")
        # Ranking itself trims 5 stocks -> config.TOP_N_STOCKS (4) candidates
        # BEFORE capacity is even checked, exactly like real production -
        # so only 4 of the 5 ever reach entry, of which 3 (capacity) enter
        # and 1 is skipped. The 5th never appears in entries at all - that's
        # correct, not a bug, since ranking itself never selected it.
        assert len(entered) == 3, f"expected exactly 3 entered (capacity=3), got {len(entered)}"
        assert len(skipped) == 1, f"expected exactly 1 skipped (4 ranked - 3 capacity), got {len(skipped)}"
        assert len(result["entries"]) == 4, f"expected 4 total (TOP_N_STOCKS), got {len(result['entries'])}"
        remaining = await store.remaining_capacity("CE")
        assert remaining == 0, f"expected capacity fully consumed, got {remaining} remaining"
        assert len(store.live_positions) == 3
        for sym in [e["symbol"] for e in entered]:
            assert sym in store.reserved_symbols

        await asyncio.sleep(0.3)  # let the async trade_history writes land
        opened = trade_history.read_all_jsonl("position_opened")
        assert len(opened) == 3, f"expected 3 position_opened log entries, got {len(opened)}"
        assert all(r["strategy"] == "Options" for r in opened)

        print(f"1. Real concurrent entry through the real webhook handler: 3 entered, 1 skipped, "
              f"capacity exhausted correctly, 3 position_opened log entries, {elapsed:.2f}s elapsed: PASSED")
    finally:
        restore()
        om.rank_and_pick_top_stocks = real_rank


async def test_2_duplicate_webhook_delivery_race():
    """A real-world scenario: Chartink (or a flaky network) delivers the
    exact same alert twice, near-simultaneously. reserve_symbol's atomic
    lock must ensure only ONE of the two concurrent deliveries actually
    enters each symbol - fired as REAL concurrent coroutines via
    asyncio.gather, not sequentially."""
    store = ops.PositionStore()
    om.position_store = store
    ote.position_store = store
    ote.config.MAX_LIVE_POSITIONS_CE = 10  # capacity isn't what's under test here

    restore = install_all_dhan_mocks()
    real_rank = om.rank_and_pick_top_stocks
    om.rank_and_pick_top_stocks = fake_ranked
    try:
        payload = om.ChartinkWebhookPayload(
            stocks="RELIANCE,TCS", trigger_prices="1,1", triggered_at="9:20 am",
            scan_name="deep-test-2", scan_url="deep-test-2",
            alert_name="Deep integration test 2 - duplicate delivery race",
        )
        r1, r2 = await asyncio.gather(
            om._handle_chartink_webhook(payload, "CE", True),
            om._handle_chartink_webhook(payload, "CE", True),
        )
        all_entries = r1["entries"] + r2["entries"]
        entered_symbols = [e["symbol"] for e in all_entries if e["status"] == "entered"]
        skipped_symbols = [e["symbol"] for e in all_entries if e["status"] == "skipped"]

        for sym in ["RELIANCE", "TCS"]:
            count = entered_symbols.count(sym)
            assert count == 1, f"{sym} entered {count} times across the two concurrent deliveries - DUPLICATE ENTRY BUG"
        assert len(store.live_positions) == 2, f"expected exactly 2 live positions, got {len(store.live_positions)}"
        print(f"2. Duplicate webhook delivery race: each of RELIANCE/TCS entered exactly once "
              f"despite 2 concurrent identical deliveries (skipped: {skipped_symbols}): PASSED")
    finally:
        restore()
        om.rank_and_pick_top_stocks = real_rank


async def test_3_transient_failure_recovers_within_real_concurrent_flow():
    """Exercises the get_open_fno_positions retry-wrap fix NOT in
    isolation, but inside the real concurrent multi-stock entry flow - one
    of several concurrently-checked stocks hits a transient failure on its
    FIRST has_open_position_for_underlying call; the retry must recover it
    and that stock must still enter successfully, not be abandoned."""
    store = ops.PositionStore()
    om.position_store = store
    ote.position_store = store
    ote.config.MAX_LIVE_POSITIONS_CE = 10

    restore = install_all_dhan_mocks()
    real_rank = om.rank_and_pick_top_stocks
    om.rank_and_pick_top_stocks = fake_ranked

    call_counts = {"n": 0}

    def flaky_once():
        call_counts["n"] += 1
        if call_counts["n"] == 2:  # fail exactly one call, somewhere mid-burst
            raise RuntimeError("simulated transient Dhan rate-limit failure")
        return []

    odc.dhan_wrapper._get_open_fno_positions_once = flaky_once
    try:
        payload = om.ChartinkWebhookPayload(
            stocks="RELIANCE,TCS,SBIN", trigger_prices="1,1,1", triggered_at="9:20 am",
            scan_name="deep-test-3", scan_url="deep-test-3",
            alert_name="Deep integration test 3 - transient failure mid-burst",
        )
        result = await om._handle_chartink_webhook(payload, "CE", True)
        entered = [e["symbol"] for e in result["entries"] if e["status"] == "entered"]
        errored = [e for e in result["entries"] if e["status"] == "error"]
        print(f"  entries: {[(e['symbol'], e['status']) for e in result['entries']]}, "
              f"total get_open_fno_positions calls (incl. retries): {call_counts['n']}")
        assert len(errored) == 0, f"a stock's entry was abandoned instead of retried-and-recovered: {errored}"
        assert len(entered) == 3, f"expected all 3 to enter despite 1 transient failure, got {len(entered)}: {result['entries']}"
        print("3. Transient Dhan failure mid-burst recovers via retry within the REAL concurrent "
              "entry flow - all 3 stocks entered, none abandoned: PASSED")
    finally:
        restore()
        om.rank_and_pick_top_stocks = real_rank


async def test_4_real_exit_path_target_and_stoploss():
    """Takes real open positions through the REAL exit-check logic
    (_exit_reason_for, close_position) via simulated price ticks - one
    hitting TARGET, one hitting STOP_LOSS - confirming close_position's
    async trade_history logging still fires correctly and the symbol is
    freed for re-entry afterward."""
    store = ops.PositionStore()
    ote.position_store = store

    pos_target = ops.Position(
        underlying_symbol="RELIANCE", option_trading_symbol="RELIANCE 25 SEP 1400 CALL",
        option_type="CE", quantity=500, lot_size=500, entry_price=20.0, highest_price=20.0,
        target_price=25.0, hard_stop_loss=16.8, order_id="OID1", product_type="MARGIN",
        opened_at=datetime.now(),
    )
    pos_sl = ops.Position(
        # Small quantity deliberately, so (entry-ltp)*quantity stays well
        # under MAX_LOSS_PER_TRADE_RS (1200) at the stop-loss price - keeps
        # this an unambiguous STOP_LOSS_HIT test. _exit_reason_for checks
        # MAX_LOSS_PER_TRADE_RS FIRST by design (see its own docstring), so
        # a large-quantity position hitting both thresholds at once would
        # correctly report MAX_LOSS_HIT, not STOP_LOSS_HIT - that's real
        # production behavior, not something to route around here.
        underlying_symbol="TCS", option_trading_symbol="TCS 25 SEP 4000 CALL",
        option_type="CE", quantity=10, lot_size=10, entry_price=50.0, highest_price=50.0,
        target_price=62.5, hard_stop_loss=42.0, order_id="OID2", product_type="MARGIN",
        opened_at=datetime.now(),
    )
    store.live_positions["RELIANCE"] = pos_target
    store.live_positions["TCS"] = pos_sl
    store.reserved_symbols["RELIANCE"] = "CE"
    store.reserved_symbols["TCS"] = "CE"

    reason_target = ote._exit_reason_for(pos_target, 25.5, False)
    reason_sl = ote._exit_reason_for(pos_sl, 41.0, False)
    assert reason_target == "TARGET_HIT", reason_target
    assert reason_sl == "STOP_LOSS_HIT", reason_sl

    assert await store.try_start_exit("RELIANCE")
    assert await store.try_start_exit("TCS")
    await store.close_position("RELIANCE", 25.5, reason_target)
    await store.close_position("TCS", 41.0, reason_sl)

    assert "RELIANCE" not in store.live_positions
    assert "TCS" not in store.live_positions
    assert "RELIANCE" not in store.reserved_symbols, "symbol must be freed for re-entry after close"
    assert "TCS" not in store.reserved_symbols

    await asyncio.sleep(0.3)
    closed = trade_history.read_all_jsonl("real_trades")
    reasons = {c["underlying_symbol"]: c["exit_reason"] for c in closed}
    assert reasons.get("RELIANCE") == "TARGET_HIT"
    assert reasons.get("TCS") == "STOP_LOSS_HIT"
    print("4. Real exit path (target + stop-loss) via the actual _exit_reason_for/close_position: "
          "both closed correctly, symbols freed, trade_history logged both: PASSED")


async def test_5_reconciliation_realistic_mixed_scenario():
    """3 broker positions in one get_open_fno_positions response - 2
    genuinely opened by Options, 1 by Futures (via the real
    record_opened_position calls, not fabricated) - confirms BOTH real
    reconcile_broker_positions functions pick up only their own, using the
    real attribute_open_broker_position filter, against a mocked
    get_open_fno_positions (no live broker state needed for this
    scenario)."""
    import Futures.trading_engine as fte

    class _FakePos:
        def __init__(self, symbol, ts, entry_price):
            self.underlying_symbol = symbol
            self.option_trading_symbol = ts
            self.option_type = "CE"
            self.quantity = 100
            self.entry_price = entry_price
            self.opened_at = datetime.now()
            self.order_id = "OID"

    await trade_history.record_opened_position("Options", _FakePos("RELIANCE", "RELIANCE 25 SEP 1400 CALL", 20.0))
    await trade_history.record_opened_position("Options", _FakePos("TCS", "TCS 25 SEP 4000 CALL", 50.0))
    await trade_history.record_opened_position("Futures", _FakePos("SBIN", "SBIN 25 SEP 800 CALL", 15.0))
    await asyncio.sleep(0.2)

    fake_broker_positions = [
        {"trading_symbol": "RELIANCE 25 SEP 1400 CALL", "underlying_symbol": "RELIANCE", "option_type": "CE",
         "lot_size": 500, "quantity": 500, "avg_price": 20.0, "product_type": "MARGIN"},
        {"trading_symbol": "TCS 25 SEP 4000 CALL", "underlying_symbol": "TCS", "option_type": "CE",
         "lot_size": 150, "quantity": 150, "avg_price": 50.0, "product_type": "MARGIN"},
        {"trading_symbol": "SBIN 25 SEP 800 CALL", "underlying_symbol": "SBIN", "option_type": "CE",
         "lot_size": 750, "quantity": 750, "avg_price": 15.0, "product_type": "MARGIN"},
    ]
    real_get_open = odc.dhan_wrapper.get_open_fno_positions
    real_subscribe = odc.dhan_wrapper.subscribe_option_price
    odc.dhan_wrapper.get_open_fno_positions = lambda: fake_broker_positions
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    try:
        options_reconciled = await ote.reconcile_broker_positions()
        futures_reconciled = await fte.reconcile_broker_positions()

        options_syms = {p.underlying_symbol for p in options_reconciled}
        futures_syms = {p.underlying_symbol for p in futures_reconciled}
        assert options_syms == {"RELIANCE", "TCS"}, f"Options reconciled wrong set: {options_syms}"
        assert futures_syms == {"SBIN"}, f"Futures reconciled wrong set: {futures_syms}"
        assert not (options_syms & futures_syms), "cross-contamination between strategies!"
        print(f"5. Reconciliation with a realistic mixed 3-position scenario: Options correctly got "
              f"{options_syms}, Futures correctly got {futures_syms}, zero cross-contamination: PASSED")
    finally:
        odc.dhan_wrapper.get_open_fno_positions = real_get_open
        odc.dhan_wrapper.subscribe_option_price = real_subscribe


async def test_6_malformed_payloads_rejected_cleanly():
    """A real Chartink alert could arrive malformed (empty stocks field,
    missing required field) - confirms pydantic validation rejects these
    cleanly via a ValidationError, not a crash or - worse - a silent
    pass-through that reaches order placement with garbage data."""
    from pydantic import ValidationError

    try:
        om.ChartinkWebhookPayload(stocks="", trigger_prices="", triggered_at="9:20 am",
                                   scan_name="x", scan_url="x", alert_name="x")
        assert False, "empty stocks should have raised ValidationError"
    except ValidationError:
        pass

    try:
        om.ChartinkWebhookPayload(trigger_prices="1", triggered_at="9:20 am", scan_name="x", scan_url="x", alert_name="x")
        assert False, "missing required 'stocks' field should have raised ValidationError"
    except ValidationError:
        pass

    print("6. Malformed webhook payloads (empty stocks, missing field) rejected cleanly by "
          "pydantic validation, not silently passed through: PASSED")


async def main():
    print("=== Deep integration test suite (real handlers/logic, all Dhan network calls mocked) ===\n")
    await test_1_real_concurrent_entry_through_real_webhook_handler()
    await test_2_duplicate_webhook_delivery_race()
    await test_3_transient_failure_recovers_within_real_concurrent_flow()
    await test_4_real_exit_path_target_and_stoploss()
    await test_5_reconciliation_realistic_mixed_scenario()
    await test_6_malformed_payloads_rejected_cleanly()
    print("\nALL DEEP INTEGRATION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
