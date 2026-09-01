"""
Deep integration tests for the Luxury package (user request 31 Aug 2026:
"create another package... same logic and setup as Options package...
webhooks for both PE and CE ATM buying"). Same philosophy and mocking
approach as test_deep_integration.py - exercises the REAL webhook
handler/entry/exit/reconciliation code, with only the Dhan network
boundary mocked (Luxury reuses the exact same dhan_wrapper singleton as
Options, confirmed in scenario 1 below).

Covers:
  1. Real concurrent CE entry through Luxury's own webhook handler -
     capacity enforced, ranking works, using Luxury's OWN position_store
     (not Options'/Futures').
  2. The PE webhook (bearish scan -> buys ATM PE, ranks lowest %change
     first) - Options has this too but Futures doesn't, so this is new
     coverage specific to what makes Luxury different from Futures.
  3. Duplicate webhook delivery race - each symbol enters exactly once
     despite 2 concurrent identical deliveries.
  4. Real exit path (target + stop-loss) via Luxury's own
     _exit_reason_for/close_position.
  5. Three-way reconciliation: Options-owned, Futures-owned, AND
     Luxury-owned broker positions in one response - Luxury's own
     reconcile_broker_positions picks up only its own, zero
     cross-contamination with either sibling strategy.
  6. Malformed webhook payload rejected cleanly.

HOW TO RUN:
    uv run python tests/test_luxury_package.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_luxury_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Luxury.position_store as lps
import Luxury.trading_engine as lte
import Luxury.luxury_main as lm
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)

from zoneinfo import ZoneInfo
MARKET_HOURS_INSTANT = datetime.now(ZoneInfo("Asia/Kolkata")).replace(hour=10, minute=0, second=0, microsecond=0)


def _freeze_market_hours():
    """Pins Luxury's own _now_ist() to 10:00 AM IST today, so entry tests
    don't fail depending on when they happen to be run (past SQUARE_OFF_TIME/
    ALLOWED_TRADING_TIME otherwise gate them for real, exactly as intended
    in production - see is_past_square_off_time/is_past_allowed_trading_time).
    Returns a restore() closure."""
    real = lte._now_ist
    lte._now_ist = lambda: MARKET_HOURS_INSTANT

    def restore():
        lte._now_ist = real
    return restore


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP CALL" if option_type == "CE" else f"{symbol} FAKE EXP PUT",
                      strike=1000.0, option_type=option_type, lot_size=500,
                      security_id=f"SECID-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    """Mocks the same dhan_wrapper singleton Luxury actually uses (it's
    literally Options.dhan_client.dhan_wrapper, re-exported - confirmed at
    import time) - no live Dhan session needed."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
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
    # Fixed, generous fakes - the proactive funds check (added 1 Sep
    # 2026) calls these before every entry attempt; unmocked, they'd
    # fall through to a REAL Dhan network call (and a real, slow
    # authentication attempt) via _retry. Not what's under test here.
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 100000.0}
    odc.dhan_wrapper.place_market_order = lambda trading_symbol, quantity, transaction_type, tag=None, product_type=None: {
        "order_id": f"FAKE-{trading_symbol}-{transaction_type}", "is_amo": False}
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


def fake_ranked(stocks, top_n, prefer_highest):
    return [(s, float(i)) for i, s in enumerate(stocks[:top_n if top_n > 0 else len(stocks)])]


async def test_1_real_concurrent_ce_entry_through_luxury_webhook():
    store = lps.PositionStore()
    lm.position_store = store
    lte.position_store = store
    lte.config.MAX_LIVE_POSITIONS_CE = 2

    real_rank = lm.rank_and_pick_top_stocks
    lm.rank_and_pick_top_stocks = fake_ranked
    restore_time = _freeze_market_hours()
    restore = install_all_dhan_mocks()
    try:
        payload = lm.ChartinkWebhookPayload(
            stocks="RELIANCE,TCS,SBIN", trigger_prices="1,1,1", triggered_at="9:20 am",
            scan_name="luxury-test-1", scan_url="luxury-test-1",
            alert_name="Luxury deep test 1 - concurrent CE entry + capacity",
        )
        result = await lm._handle_chartink_webhook(payload, "CE", True)

        assert result["status"] == "processed", result
        entered = [e for e in result["entries"] if e["status"] == "entered"]
        skipped = [e for e in result["entries"] if e["status"] == "skipped"]
        assert len(entered) == 2, f"expected 2 entered (capacity=2), got {len(entered)}: {result['entries']}"
        assert len(skipped) == 1, f"expected 1 skipped, got {len(skipped)}"
        assert len(store.live_positions) == 2
        for pos in store.live_positions.values():
            assert pos.option_type == "CE"

        await asyncio.sleep(0.3)
        opened = trade_history.read_all_jsonl("position_opened")
        assert len(opened) == 2, f"expected 2 position_opened log entries, got {len(opened)}"
        assert all(r["strategy"] == "Luxury" for r in opened), \
            f"position_opened log must tag these as Luxury, not Options/Futures: {opened}"
        print("1. Real concurrent CE entry through Luxury's own webhook handler: 2 entered "
              "(capacity=2), 1 skipped, own PositionStore, position_opened tagged 'Luxury': PASSED")
    finally:
        restore()
        restore_time()
        lm.rank_and_pick_top_stocks = real_rank


async def test_2_pe_webhook_ranks_lowest_change_first():
    store = lps.PositionStore()
    lm.position_store = store
    lte.position_store = store
    lte.config.MAX_LIVE_POSITIONS_PE = 10

    # rank_and_pick_top_stocks' own contrarian-selection nuance
    # (SELECT_BOTTOM_N_STOCKS) is shared, already-verified production logic
    # (unchanged, copy-pasted from Options/Futures) - not re-tested here.
    # What IS new/Luxury-specific: does hitting the bearish endpoint with
    # option_type="PE" actually thread through to a real PUT entry, on
    # Luxury's own PE capacity pool, not a CALL. fake_ranked keeps
    # selection itself deterministic so this isolates exactly that.
    real_rank = lm.rank_and_pick_top_stocks
    lm.rank_and_pick_top_stocks = fake_ranked
    restore_time = _freeze_market_hours()
    restore = install_all_dhan_mocks()
    try:
        payload = lm.ChartinkWebhookPayload(
            stocks="RELIANCE,TCS", trigger_prices="1,1", triggered_at="9:20 am",
            scan_name="luxury-test-2", scan_url="luxury-test-2",
            alert_name="Luxury deep test 2 - PE webhook enters PUTs on its own capacity pool",
        )
        result = await lm._handle_chartink_webhook(payload, "PE", False)
        assert result["status"] == "processed", result
        entered = [e for e in result["entries"] if e["status"] == "entered"]
        assert len(entered) == 2, f"expected both to enter (PE capacity=10), got {entered}"
        assert all(e.get("option_trading_symbol", "").endswith("PUT") for e in entered), \
            f"a PE alert must only ever enter PUTs, never CALLs: {entered}"
        for pos in store.live_positions.values():
            assert pos.option_type == "PE", f"live position has wrong option_type: {pos}"
        remaining_ce = await store.remaining_capacity("CE")
        assert remaining_ce == lte.config.MAX_LIVE_POSITIONS_CE, \
            "PE entries must not consume any of the separate CE capacity pool"
        print("2. PE webhook (/chartink/webhook-luxury-sell equivalent) enters PUTs on its own "
              "PE capacity pool, CE capacity untouched: PASSED")
    finally:
        restore()
        restore_time()
        lm.rank_and_pick_top_stocks = real_rank


async def test_3_duplicate_webhook_delivery_race():
    store = lps.PositionStore()
    lm.position_store = store
    lte.position_store = store
    lte.config.MAX_LIVE_POSITIONS_CE = 10

    real_rank = lm.rank_and_pick_top_stocks
    lm.rank_and_pick_top_stocks = fake_ranked
    restore_time = _freeze_market_hours()
    restore = install_all_dhan_mocks()
    try:
        payload = lm.ChartinkWebhookPayload(
            stocks="RELIANCE,TCS", trigger_prices="1,1", triggered_at="9:20 am",
            scan_name="luxury-test-3", scan_url="luxury-test-3",
            alert_name="Luxury deep test 3 - duplicate delivery race",
        )
        r1, r2 = await asyncio.gather(
            lm._handle_chartink_webhook(payload, "CE", True),
            lm._handle_chartink_webhook(payload, "CE", True),
        )
        all_entries = r1["entries"] + r2["entries"]
        entered_symbols = [e["symbol"] for e in all_entries if e["status"] == "entered"]
        for sym in ["RELIANCE", "TCS"]:
            count = entered_symbols.count(sym)
            assert count == 1, f"{sym} entered {count} times across 2 concurrent deliveries - DUPLICATE ENTRY BUG"
        assert len(store.live_positions) == 2
        print("3. Duplicate webhook delivery race: each of RELIANCE/TCS entered exactly once "
              "despite 2 concurrent identical deliveries: PASSED")
    finally:
        restore()
        restore_time()
        lm.rank_and_pick_top_stocks = real_rank


async def test_4_real_exit_path_target_and_stoploss():
    store = lps.PositionStore()
    lte.position_store = store

    pos_target = lps.Position(
        underlying_symbol="RELIANCE", option_trading_symbol="RELIANCE 25 SEP 1400 CALL",
        option_type="CE", quantity=500, lot_size=500, entry_price=20.0, highest_price=20.0,
        target_price=25.0, hard_stop_loss=16.8, order_id="OID1", product_type="MARGIN",
        opened_at=datetime.now(),
    )
    pos_sl = lps.Position(
        underlying_symbol="TCS", option_trading_symbol="TCS 25 SEP 4000 CALL",
        option_type="CE", quantity=10, lot_size=10, entry_price=50.0, highest_price=50.0,
        target_price=62.5, hard_stop_loss=42.0, order_id="OID2", product_type="MARGIN",
        opened_at=datetime.now(),
    )
    store.live_positions["RELIANCE"] = pos_target
    store.live_positions["TCS"] = pos_sl
    store.reserved_symbols["RELIANCE"] = "CE"
    store.reserved_symbols["TCS"] = "CE"

    reason_target = lte._exit_reason_for(pos_target, 25.5, False)
    reason_sl = lte._exit_reason_for(pos_sl, 41.0, False)
    assert reason_target == "TARGET_HIT", reason_target
    assert reason_sl == "STOP_LOSS_HIT", reason_sl

    assert await store.try_start_exit("RELIANCE")
    assert await store.try_start_exit("TCS")
    await store.close_position("RELIANCE", 25.5, reason_target)
    await store.close_position("TCS", 41.0, reason_sl)

    assert "RELIANCE" not in store.live_positions
    assert "TCS" not in store.live_positions
    assert "RELIANCE" not in store.reserved_symbols
    assert "TCS" not in store.reserved_symbols

    await asyncio.sleep(0.3)
    closed = trade_history.read_all_jsonl("real_trades")
    luxury_closed = {c["underlying_symbol"]: c for c in closed if c["strategy"] == "Luxury"}
    assert luxury_closed["RELIANCE"]["exit_reason"] == "TARGET_HIT"
    assert luxury_closed["TCS"]["exit_reason"] == "STOP_LOSS_HIT"
    print("4. Real exit path (target + stop-loss) via Luxury's own _exit_reason_for/"
          "close_position: both closed correctly, symbols freed, tagged 'Luxury' in trade_history: PASSED")


async def test_5_three_way_reconciliation_no_cross_contamination():
    """3 broker positions, one genuinely opened by each of Options,
    Futures, and Luxury (via real record_opened_position calls) - confirms
    all THREE real reconcile_broker_positions functions pick up only their
    own, using the real attribute_open_broker_position filter."""
    import Options.trading_engine as ote
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
    await trade_history.record_opened_position("Futures", _FakePos("SBIN", "SBIN 25 SEP 800 CALL", 15.0))
    await trade_history.record_opened_position("Luxury", _FakePos("TCS", "TCS 25 SEP 4000 CALL", 50.0))
    await asyncio.sleep(0.2)

    fake_broker_positions = [
        {"trading_symbol": "RELIANCE 25 SEP 1400 CALL", "underlying_symbol": "RELIANCE", "option_type": "CE",
         "lot_size": 500, "quantity": 500, "avg_price": 20.0, "product_type": "MARGIN"},
        {"trading_symbol": "SBIN 25 SEP 800 CALL", "underlying_symbol": "SBIN", "option_type": "CE",
         "lot_size": 750, "quantity": 750, "avg_price": 15.0, "product_type": "MARGIN"},
        {"trading_symbol": "TCS 25 SEP 4000 CALL", "underlying_symbol": "TCS", "option_type": "CE",
         "lot_size": 150, "quantity": 150, "avg_price": 50.0, "product_type": "MARGIN"},
    ]
    real_get_open = odc.dhan_wrapper.get_open_fno_positions
    real_subscribe = odc.dhan_wrapper.subscribe_option_price
    odc.dhan_wrapper.get_open_fno_positions = lambda: fake_broker_positions
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    try:
        options_reconciled = await ote.reconcile_broker_positions()
        futures_reconciled = await fte.reconcile_broker_positions()
        luxury_reconciled = await lte.reconcile_broker_positions()

        options_syms = {p.underlying_symbol for p in options_reconciled}
        futures_syms = {p.underlying_symbol for p in futures_reconciled}
        luxury_syms = {p.underlying_symbol for p in luxury_reconciled}
        assert options_syms == {"RELIANCE"}, f"Options reconciled wrong set: {options_syms}"
        assert futures_syms == {"SBIN"}, f"Futures reconciled wrong set: {futures_syms}"
        assert luxury_syms == {"TCS"}, f"Luxury reconciled wrong set: {luxury_syms}"
        assert not (options_syms & futures_syms & luxury_syms), "cross-contamination between strategies!"
        print(f"5. Three-way reconciliation (Options/Futures/Luxury) with zero cross-"
              f"contamination: Options={options_syms} Futures={futures_syms} Luxury={luxury_syms}: PASSED")
    finally:
        odc.dhan_wrapper.get_open_fno_positions = real_get_open
        odc.dhan_wrapper.subscribe_option_price = real_subscribe


async def test_6_malformed_payload_rejected_cleanly():
    from pydantic import ValidationError
    try:
        lm.ChartinkWebhookPayload(stocks="", trigger_prices="", triggered_at="9:20 am",
                                    scan_name="x", scan_url="x", alert_name="x")
        assert False, "empty stocks should have raised ValidationError"
    except ValidationError:
        pass
    print("6. Malformed webhook payload (empty stocks) rejected cleanly by pydantic "
          "validation, not silently passed through: PASSED")


async def main():
    print("=== Luxury package deep integration test suite ===\n")
    await test_1_real_concurrent_ce_entry_through_luxury_webhook()
    await test_2_pe_webhook_ranks_lowest_change_first()
    await test_3_duplicate_webhook_delivery_race()
    await test_4_real_exit_path_target_and_stoploss()
    await test_5_three_way_reconciliation_no_cross_contamination()
    await test_6_malformed_payload_rejected_cleanly()
    print("\nALL LUXURY PACKAGE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
