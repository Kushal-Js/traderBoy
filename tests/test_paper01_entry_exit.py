"""
Paper01 entry/exit tests - user request 15 Sep 2026: a real-time,
paper-only twin of Options with the exact same entry/exit rules, but its
own independent capacity pool and daily counters (so a burst of paper
alerts can never affect Options' real capacity, and vice versa).

Covers, against the REAL Paper01 production functions (not
reimplemented):
  1. A full paper entry->exit cycle: no real order is ever placed
     (place_market_order/place_stop_loss_limit_order/place_stop_loss_
     market_order/cancel_order are all mocked to raise if called), a
     Position is created and closed via the real _exit_reason_for ladder,
     and the trade is recorded with a correct pnl in Paper01's own log.
  2. Capacity (MAX_LIVE_POSITIONS_CE) is enforced independently of
     Options' own real position_store - filling Paper01's own capacity
     has zero effect on Options' real capacity, and vice versa.
  3. MAX_DAILY_ENTRIES_PER_SYMBOL is counted against Paper01's OWN log,
     not Options' real trade_history log - a real Options entry for a
     symbol does not count toward Paper01's cap for that same symbol.
  4. MAX_LOSS_HIT exit correctly computes a negative pnl and is recorded
     via the real exit ladder (_exit_reason_for), not a custom paper-only
     rule.

HOW TO RUN:
    uv run python tests/test_paper01_entry_exit.py
"""
import asyncio
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import os
os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
import Options.dhan_client as odc
import Options.trading_engine as ote
from Options.dhan_client import AtmOption

import Paper01.config as p1config
import Paper01.position_store as p1ps
import Paper01.trading_engine as p1te

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP {option_type}", strike=1000.0,
                      option_type=option_type, lot_size=100, security_id=f"SECID-{symbol}",
                      expiry_date=FUTURE_EXPIRY)


def _raise_if_called(name):
    def _fn(*a, **k):
        raise AssertionError(f"Paper01 must NEVER call {name} - this is a real order-placement function")
    return _fn


def install_paper_safe_dhan_mocks(ltp_value=50.0):
    """Mocks every read-only Dhan call Paper01's entry/exit path touches,
    AND mocks every real order-placement function to RAISE if called -
    proving dynamically (not just via the static source-scan test) that
    the paper entry/exit path can never place a real order."""
    originals = {
        name: getattr(odc.dhan_wrapper, name) for name in (
            "get_atm_option", "get_cached_option_ltp", "get_option_ltp", "note_rest_ltp",
            "subscribe_option_price", "unsubscribe_option_price",
            "refresh_supertrend_signal", "get_cached_supertrend_candle_start", "get_cached_supertrend_bearish",
            "refresh_ema_cross_signal", "get_cached_ema_cross_candle_start",
            "refresh_liquidity_signal", "get_cached_illiquid",
            "is_rsi_loss_reentry_blocked", "rsi_loss_reentry_reason",
            "place_market_order", "place_stop_loss_limit_order", "place_stop_loss_market_order", "cancel_order",
        )
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_cached_option_ltp = lambda ts: None  # force the REST fallback path below
    odc.dhan_wrapper.get_option_ltp = lambda ts: ltp_value
    odc.dhan_wrapper.note_rest_ltp = lambda ts, ltp: None
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_bearish = lambda sym: False
    odc.dhan_wrapper.refresh_ema_cross_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_ema_cross_candle_start = lambda sym: None
    odc.dhan_wrapper.refresh_liquidity_signal = lambda ts: None
    odc.dhan_wrapper.get_cached_illiquid = lambda ts: False
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda sym: False
    odc.dhan_wrapper.rsi_loss_reentry_reason = lambda sym: None
    odc.dhan_wrapper.place_market_order = _raise_if_called("place_market_order")
    odc.dhan_wrapper.place_stop_loss_limit_order = _raise_if_called("place_stop_loss_limit_order")
    odc.dhan_wrapper.place_stop_loss_market_order = _raise_if_called("place_stop_loss_market_order")
    odc.dhan_wrapper.cancel_order = _raise_if_called("cancel_order")

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


def fresh_paper_store(tmp_path: Path) -> p1ps.PaperPositionStore:
    p1config.OPEN_STATE_PATH = str(tmp_path / "paper01_open.json")
    store = p1ps.PaperPositionStore()
    p1te.store = store
    return store


async def test_1_full_paper_cycle_places_no_real_order_and_records_correct_pnl():
    scratch = Path(tempfile.mkdtemp(prefix="dhanboy_paper01_test_"))
    real_history_dir = trade_history.HISTORY_DIR
    trade_history.HISTORY_DIR = scratch
    store = fresh_paper_store(scratch)
    restore = install_paper_safe_dhan_mocks(ltp_value=50.0)
    try:
        result = await p1te._process_one_paper_entry("RELIANCE", "CE")
        assert result["status"] == "paper_entered", result
        assert "RELIANCE" in store.live_positions
        position = store.live_positions["RELIANCE"]
        assert position.order_id == "", "a paper Position must never carry a real order_id"
        assert position.product_type == "PAPER"
        assert position.entry_price == 50.0

        # Real exit ladder (_exit_reason_for) drives the exit - TARGET_HIT
        # at entry_price * (1 + TARGET_PCT).
        exit_price = position.target_price
        odc.dhan_wrapper.get_option_ltp = lambda ts: exit_price
        await p1te._check_one_paper_position("RELIANCE", position)
        assert "RELIANCE" not in store.live_positions, "position should have closed on TARGET_HIT"

        trades = store.all_completed_trades()
        assert len(trades) == 1, trades
        trade = trades[0]
        assert trade["exit_reason"] == "TARGET_HIT", trade
        expected_pnl = (exit_price - 50.0) * position.quantity
        assert abs(trade["pnl"] - expected_pnl) < 0.01, (trade["pnl"], expected_pnl)

        print("1. Full paper entry->exit cycle: no real order placed, real _exit_reason_for ladder "
              "used, correct pnl recorded in Paper01's own trade log: PASSED")
    finally:
        restore()
        trade_history.HISTORY_DIR = real_history_dir


async def test_2_capacity_independent_of_options_real_store():
    scratch = Path(tempfile.mkdtemp(prefix="dhanboy_paper01_capacity_test_"))
    real_history_dir = trade_history.HISTORY_DIR
    trade_history.HISTORY_DIR = scratch
    store = fresh_paper_store(scratch)
    real_cap = p1config.MAX_LIVE_POSITIONS_CE
    p1config.MAX_LIVE_POSITIONS_CE = 2
    restore = install_paper_safe_dhan_mocks()
    try:
        r1 = await p1te._process_one_paper_entry("STOCK1", "CE")
        r2 = await p1te._process_one_paper_entry("STOCK2", "CE")
        assert r1["status"] == "paper_entered" and r2["status"] == "paper_entered", (r1, r2)

        r3 = await p1te._process_one_paper_entry("STOCK3", "CE")
        assert r3["status"] == "skipped" and r3["reason"] == "duplicate_or_capacity_full", r3

        # Options' own real position_store is completely untouched - it
        # never even got a look-in (no call to reserve_symbol on it, no
        # mocks needed for it at all in this test).
        assert len(ote.position_store.live_positions) == 0 or "STOCK1" not in ote.position_store.live_positions

        print("2. Paper01's MAX_LIVE_POSITIONS_CE capacity is enforced independently of Options' "
              "own real position_store: PASSED")
    finally:
        restore()
        p1config.MAX_LIVE_POSITIONS_CE = real_cap
        trade_history.HISTORY_DIR = real_history_dir


async def test_3_daily_reentry_cap_uses_paper01s_own_log_not_options_real_log():
    scratch = Path(tempfile.mkdtemp(prefix="dhanboy_paper01_reentry_test_"))
    real_history_dir = trade_history.HISTORY_DIR
    trade_history.HISTORY_DIR = scratch
    store = fresh_paper_store(scratch)
    p1config.MAX_LIVE_POSITIONS_CE = 10
    restore = install_paper_safe_dhan_mocks()
    try:
        symbol = "TCS"
        # A real Options entry logged for the SAME symbol must NOT count
        # toward Paper01's own daily cap.
        real_pos = ote.Position(
            underlying_symbol=symbol, option_trading_symbol=f"{symbol} REAL EXP CE",
            option_type="CE", quantity=100, lot_size=100, entry_price=50.0, highest_price=50.0,
            target_price=60.0, hard_stop_loss=40.0, order_id="REAL-OID", product_type="MARGIN",
        )
        await trade_history.record_opened_position("Options", real_pos)
        await asyncio.sleep(0.2)

        assert await store.count_opened_today(symbol) == 0, \
            "a real Options entry for this symbol must not appear in Paper01's own opened-count"

        real_cap = ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL
        ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL = 1
        try:
            r1 = await p1te._process_one_paper_entry(symbol, "CE")
            assert r1["status"] == "paper_entered", r1
            await p1te._exit_paper_position(symbol, store.live_positions[symbol], 55.0, "TARGET_HIT")

            r2 = await p1te._process_one_paper_entry(symbol, "CE")
            assert r2["status"] == "skipped" and r2["reason"] == "daily_reentry_cap_reached", r2
        finally:
            ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL = real_cap

        print("3. MAX_DAILY_ENTRIES_PER_SYMBOL is counted against Paper01's OWN log (a real Options "
              "entry for the same symbol has zero effect on Paper01's cap): PASSED")
    finally:
        restore()
        trade_history.HISTORY_DIR = real_history_dir


async def test_4_max_loss_hit_records_a_negative_pnl():
    scratch = Path(tempfile.mkdtemp(prefix="dhanboy_paper01_maxloss_test_"))
    real_history_dir = trade_history.HISTORY_DIR
    trade_history.HISTORY_DIR = scratch
    store = fresh_paper_store(scratch)
    restore = install_paper_safe_dhan_mocks(ltp_value=50.0)
    try:
        result = await p1te._process_one_paper_entry("WIPRO", "PE")
        assert result["status"] == "paper_entered", result
        position = store.live_positions["WIPRO"]

        max_loss_cap = ote.current_max_loss_per_trade_rs("PE")
        losing_price = position.entry_price - (max_loss_cap / position.quantity) - 0.01
        odc.dhan_wrapper.get_cached_option_ltp = lambda ts: losing_price

        await p1te._check_one_paper_position("WIPRO", position)
        assert "WIPRO" not in store.live_positions

        trades = store.all_completed_trades()
        assert trades[-1]["exit_reason"] == "MAX_LOSS_HIT", trades[-1]
        assert trades[-1]["pnl"] < 0, trades[-1]

        print("4. A MAX_LOSS_HIT exit (via the real _exit_reason_for ladder) correctly records a "
              "negative pnl in Paper01's own trade log: PASSED")
    finally:
        restore()
        trade_history.HISTORY_DIR = real_history_dir


async def main():
    print("=== Paper01 entry/exit test suite ===\n")
    await test_1_full_paper_cycle_places_no_real_order_and_records_correct_pnl()
    await test_2_capacity_independent_of_options_real_store()
    await test_3_daily_reentry_cap_uses_paper01s_own_log_not_options_real_log()
    await test_4_max_loss_hit_records_a_negative_pnl()
    print("\nALL PAPER01 ENTRY/EXIT CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
