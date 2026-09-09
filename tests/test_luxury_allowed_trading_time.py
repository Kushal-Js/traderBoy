"""
Tests for Luxury's "no new entries past a given time" cutoff - user
request 9 Sep 2026: "add LUXURY_ENTRY_CUTOFF_TIME with value of 11 AM
and post this time no new trade placement is to be allowed, keep a
default time of 15:30 PM also in case no time value is kept for this
variable."

Found, while scoping this, that Luxury ALREADY has this exact mechanism
built and wired into the real webhook handler - `config.ENABLE_TRADING_
TIME_LIMIT` (default "false") + `config.ALLOWED_TRADING_TIME` (default
"11:30") + `trading_engine.is_past_allowed_trading_time()`, checked in
`luxury_main.py`'s `_handle_chartink_webhook` before any entry is
attempted. It was just never enabled/configured, and - this file's own
reason for existing - never actually had a dedicated test proving the
REJECTION path works end-to-end (every existing reference to it across
the test suite was either a docstring mention or a time-freeze helper
avoiding tripping it by accident, never a test exercising it on
purpose). Reused rather than duplicated with a second, differently-
named flag doing the same thing - see the user's own conversation for
the design discussion.

Covers, against the REAL production webhook handler (not reimplemented):
  1. Before the cutoff (with the limit enabled): a real alert enters
     normally, exactly as if the limit didn't exist.
  2. At/after the cutoff (with the limit enabled): a real alert is
     IGNORED with reason="past_allowed_trading_time", zero orders
     placed, zero positions opened, and the ignored alert is durably
     logged via record_webhook_alert (status="ignored").
  3. ENABLE_TRADING_TIME_LIMIT=False cleanly bypasses the check even
     well past the configured cutoff time - a real entry still succeeds.

HOW TO RUN:
    uv run python tests/test_luxury_allowed_trading_time.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_luxury_allowed_trading_time_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Luxury.position_store as lps
import Luxury.trading_engine as lte
import Luxury.luxury_main as lm
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)
IST = ZoneInfo("Asia/Kolkata")
BEFORE_CUTOFF_INSTANT = datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)
AFTER_CUTOFF_INSTANT = datetime.now(IST).replace(hour=12, minute=0, second=0, microsecond=0)


def _freeze_time_at(instant):
    real = lte._now_ist
    lte._now_ist = lambda: instant

    def restore():
        lte._now_ist = real
    return restore


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP CALL", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"SECID-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    """Same mocking approach as test_luxury_package.py's own helper -
    the real dhan_wrapper singleton, network boundary only."""
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
        "place_stop_loss_market_order": odc.dhan_wrapper.place_stop_loss_market_order,
        "place_stop_loss_limit_order": odc.dhan_wrapper.place_stop_loss_limit_order,
        "check_if_order_filled": odc.dhan_wrapper.check_if_order_filled,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 100000.0}
    odc.dhan_wrapper.place_market_order = lambda trading_symbol, quantity, transaction_type, tag=None, product_type=None: {
        "order_id": f"FAKE-{trading_symbol}-{transaction_type}", "is_amo": False}
    odc.dhan_wrapper.place_stop_loss_market_order = lambda trading_symbol, quantity, transaction_type, trigger_price, tag=None, product_type=None: {
        "order_id": f"FAKE-SL-{trading_symbol}"}
    odc.dhan_wrapper.place_stop_loss_limit_order = lambda trading_symbol, quantity, transaction_type, trigger_price, limit_price, tag=None, product_type=None: {
        "order_id": f"FAKE-SLL-{trading_symbol}"}
    odc.dhan_wrapper.check_if_order_filled = lambda order_id: None
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


def fake_ranked(stocks, top_n, prefer_highest):
    return [(s, float(i)) for i, s in enumerate(stocks[:top_n if top_n > 0 else len(stocks)])]


async def test_1_before_cutoff_entry_proceeds_normally():
    store = lps.PositionStore()
    lm.position_store = store
    lte.position_store = store
    lte.config.MAX_LIVE_POSITIONS_CE = 5

    real_enabled = lte.config.ENABLE_TRADING_TIME_LIMIT
    real_cutoff = lte.config.ALLOWED_TRADING_TIME
    lte.config.ENABLE_TRADING_TIME_LIMIT = True
    lte.config.ALLOWED_TRADING_TIME = "11:00"
    real_rank = lm.rank_and_pick_top_stocks
    lm.rank_and_pick_top_stocks = fake_ranked
    restore_time = _freeze_time_at(BEFORE_CUTOFF_INSTANT)  # 10:00 AM, before the 11:00 cutoff
    restore = install_all_dhan_mocks()
    try:
        payload = lm.ChartinkWebhookPayload(
            stocks="RELIANCE", trigger_prices="1", triggered_at="9:20 am",
            scan_name="luxury-cutoff-test-1", scan_url="luxury-cutoff-test-1",
            alert_name="Before cutoff",
        )
        result = await lm._handle_chartink_webhook(payload, "CE", True)
        assert result["status"] == "processed", result
        assert result["entries"][0]["status"] == "entered", result["entries"]
        assert "RELIANCE" in store.live_positions
        print("1. Before the configured cutoff (10:00 AM, cutoff=11:00), a real alert enters "
              "normally exactly as if the limit didn't exist: PASSED")
    finally:
        restore()
        restore_time()
        lm.rank_and_pick_top_stocks = real_rank
        lte.config.ENABLE_TRADING_TIME_LIMIT = real_enabled
        lte.config.ALLOWED_TRADING_TIME = real_cutoff


async def test_2_after_cutoff_entry_ignored_zero_orders():
    store = lps.PositionStore()
    lm.position_store = store
    lte.position_store = store
    lte.config.MAX_LIVE_POSITIONS_CE = 5

    real_enabled = lte.config.ENABLE_TRADING_TIME_LIMIT
    real_cutoff = lte.config.ALLOWED_TRADING_TIME
    lte.config.ENABLE_TRADING_TIME_LIMIT = True
    lte.config.ALLOWED_TRADING_TIME = "11:00"
    real_rank = lm.rank_and_pick_top_stocks
    lm.rank_and_pick_top_stocks = fake_ranked
    restore_time = _freeze_time_at(AFTER_CUTOFF_INSTANT)  # 12:00 PM, well past the 11:00 cutoff
    restore = install_all_dhan_mocks()
    order_calls = []
    real_place = odc.dhan_wrapper.place_market_order
    odc.dhan_wrapper.place_market_order = lambda *a, **k: order_calls.append((a, k)) or {
        "order_id": "SHOULD-NOT-HAPPEN", "is_amo": False}
    try:
        payload = lm.ChartinkWebhookPayload(
            stocks="TCS", trigger_prices="1", triggered_at="12:00 pm",
            scan_name="luxury-cutoff-test-2", scan_url="luxury-cutoff-test-2",
            alert_name="After cutoff",
        )
        result = await lm._handle_chartink_webhook(payload, "CE", True)
        assert result["status"] == "ignored", result
        assert result["reason"] == "past_allowed_trading_time", result
        assert order_calls == [], f"no order should ever be placed once past the cutoff, got {order_calls}"
        assert "TCS" not in store.live_positions
        assert store.live_positions == {}, "zero positions should have opened"

        await asyncio.sleep(0.3)
        alerts = trade_history.read_all_webhook_alerts("Luxury")
        matches = [a for a in alerts if a["reason"] == "past_allowed_trading_time"]
        assert len(matches) == 1 and matches[0]["status"] == "ignored", alerts

        print("2. At/after the configured cutoff (12:00 PM, cutoff=11:00), a real alert is IGNORED "
              "with reason='past_allowed_trading_time', ZERO orders placed, and the ignored alert "
              "is durably logged: PASSED")
    finally:
        odc.dhan_wrapper.place_market_order = real_place
        restore()
        restore_time()
        lm.rank_and_pick_top_stocks = real_rank
        lte.config.ENABLE_TRADING_TIME_LIMIT = real_enabled
        lte.config.ALLOWED_TRADING_TIME = real_cutoff


async def test_3_disabled_flag_bypasses_even_past_cutoff():
    store = lps.PositionStore()
    lm.position_store = store
    lte.position_store = store
    lte.config.MAX_LIVE_POSITIONS_CE = 5

    real_enabled = lte.config.ENABLE_TRADING_TIME_LIMIT
    real_cutoff = lte.config.ALLOWED_TRADING_TIME
    lte.config.ENABLE_TRADING_TIME_LIMIT = False
    lte.config.ALLOWED_TRADING_TIME = "11:00"
    real_rank = lm.rank_and_pick_top_stocks
    lm.rank_and_pick_top_stocks = fake_ranked
    restore_time = _freeze_time_at(AFTER_CUTOFF_INSTANT)  # 12:00 PM, past 11:00, but the flag is OFF
    restore = install_all_dhan_mocks()
    try:
        payload = lm.ChartinkWebhookPayload(
            stocks="SBIN", trigger_prices="1", triggered_at="12:00 pm",
            scan_name="luxury-cutoff-test-3", scan_url="luxury-cutoff-test-3",
            alert_name="Disabled flag, past cutoff time",
        )
        result = await lm._handle_chartink_webhook(payload, "CE", True)
        assert result["status"] == "processed", result
        assert result["entries"][0]["status"] == "entered", result["entries"]
        assert "SBIN" in store.live_positions
        print("3. ENABLE_TRADING_TIME_LIMIT=False cleanly bypasses the check even well past the "
              "configured cutoff time - a real entry still succeeds: PASSED")
    finally:
        restore()
        restore_time()
        lm.rank_and_pick_top_stocks = real_rank
        lte.config.ENABLE_TRADING_TIME_LIMIT = real_enabled
        lte.config.ALLOWED_TRADING_TIME = real_cutoff


async def main():
    print("=== Luxury allowed-trading-time cutoff test suite ===\n")
    await test_1_before_cutoff_entry_proceeds_normally()
    await test_2_after_cutoff_entry_ignored_zero_orders()
    await test_3_disabled_flag_bypasses_even_past_cutoff()
    print("\nALL LUXURY ALLOWED-TRADING-TIME CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
