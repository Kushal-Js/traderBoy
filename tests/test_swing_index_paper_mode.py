"""
Tests for config.INDEX_PAPER_MODE_ENABLED (added 24 Sep 2026, user request:
"add a flag to turn off real trading and start paper trading" for the
newly-added NIFTY/BANKNIFTY v3 Day Range logic + its brand-new WS index
feed, both deployed the same day with no live track record yet).

Independent of the existing global config.PAPER_MODE_ENABLED (which
replaces ALL of Swing's real trading) - this one only reroutes NIFTY/
BANKNIFTY candidates to swing_paper_engine.process_paper_entry, leaving
every other symbol on the real path, unaffected.

Covers:
  1. _monitor_tick routes an index candidate to the paper engine when
     INDEX_PAPER_MODE_ENABLED is on (global PAPER_MODE_ENABLED off) -
     a non-index candidate in the SAME tick still enters for real.
  2. process_paper_entry refuses a fresh NIFTY/BANKNIFTY entry once
     today's index daily square-off window has passed (mirrors the real
     path's index_square_off_now check) - zero instrument resolution
     calls, proving it's an early return, not a late-stage skip.
  3. The paper engine's own _check_one force-closes an index paper
     position at the daily square-off cutoff with reason
     INDEX_DAILY_SQUARE_OFF, same as the real book.

HOW TO RUN:
    uv run python tests/test_swing_index_paper_mode.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_index_paper_mode_test_"))
trade_history.HISTORY_DIR = scratch_dir

from Options.dhan_client import IST
import Swing.config as sc
import Swing.swing_paper_engine as spe
import Swing.trading_engine as ste
from Swing.position_store import Position

from test_swing_v2_entry_exit import install_mocks, _set  # noqa: E402 - reuses the proven entry-flow mock harness


def _at(year, month, day, hh, mm) -> datetime:
    return datetime(year, month, day, hh, mm, tzinfo=IST)


async def test_1_monitor_tick_routes_only_index_candidates_to_paper_when_flag_on():
    """ASHOKLEY must still enter for real; NIFTY must go to the paper
    engine instead - proves the flag is scoped, not a global replacement,
    the way config.PAPER_MODE_ENABLED already is."""
    _set("options", max_concurrent=5)
    restore, placed, _ = install_mocks(entry_fill_status="TRADED")
    saved_paper_flag, saved_global_paper_flag = sc.INDEX_PAPER_MODE_ENABLED, sc.PAPER_MODE_ENABLED
    try:
        sc.INDEX_PAPER_MODE_ENABLED = True
        sc.PAPER_MODE_ENABLED = False

        from Swing.watchlist import watchlist_store
        original_symbols = watchlist_store.symbols
        watchlist_store.symbols = lambda: asyncio.sleep(0, result=["ASHOKLEY", "NIFTY"])

        async def _fake_evaluate(symbol):
            return "BULLISH"

        paper_calls = []

        async def _fake_paper_entry(symbol, regime):
            paper_calls.append(symbol)
            return {"symbol": symbol, "status": "paper_entered", "mode": "paper"}

        original_evaluate, original_paper_entry = ste._evaluate_entry_signal, spe.process_paper_entry
        ste._evaluate_entry_signal = _fake_evaluate
        spe.process_paper_entry = _fake_paper_entry
        try:
            with mock.patch("Swing.trading_engine.datetime") as fake_dt:
                fake_dt.now.return_value = _at(2026, 9, 21, 11, 0)  # plain Monday, well within market hours
                await ste._monitor_tick()
            assert paper_calls == ["NIFTY"], f"expected only NIFTY routed to the paper engine, got {paper_calls}"
            assert any(o["trading_symbol"].startswith("ASHOKLEY") for o in placed), (
                f"ASHOKLEY must still place a REAL order - INDEX_PAPER_MODE_ENABLED must not affect it, got {placed}"
            )
        finally:
            ste._evaluate_entry_signal = original_evaluate
            spe.process_paper_entry = original_paper_entry
            watchlist_store.symbols = original_symbols
    finally:
        sc.INDEX_PAPER_MODE_ENABLED, sc.PAPER_MODE_ENABLED = saved_paper_flag, saved_global_paper_flag
        restore()
    print("1. _monitor_tick routes NIFTY to the paper engine while ASHOKLEY still enters for real, "
          "with only INDEX_PAPER_MODE_ENABLED on: PASSED")


async def test_2_process_paper_entry_refuses_fresh_index_entry_after_square_off_window():
    saved_enabled = sc.INDEX_DAILY_SQUARE_OFF_ENABLED
    sc.INDEX_DAILY_SQUARE_OFF_ENABLED = True

    def _raise_if_called(*a, **k):
        raise AssertionError("instrument resolution must never be reached - this must be an early return")

    from Options.dhan_client import dhan_wrapper
    saved_atm = dhan_wrapper.get_liquid_atm_option
    dhan_wrapper.get_liquid_atm_option = _raise_if_called
    try:
        with mock.patch("Swing.trading_engine.datetime") as fake_dt:
            fake_dt.now.return_value = _at(2026, 9, 21, 15, 30)  # plain Monday, past the 15:25 cutoff
            result = await spe.process_paper_entry("NIFTY", "BULLISH")
        assert result["status"] == "skipped" and result["reason"] == "index_daily_square_off_window", result
    finally:
        dhan_wrapper.get_liquid_atm_option = saved_atm
        sc.INDEX_DAILY_SQUARE_OFF_ENABLED = saved_enabled
    print("2. process_paper_entry refuses a fresh NIFTY entry past today's square-off cutoff, "
          "with zero instrument-resolution calls: PASSED")


async def test_3_check_one_force_closes_index_paper_position_at_cutoff():
    saved_enabled = sc.INDEX_DAILY_SQUARE_OFF_ENABLED
    sc.INDEX_DAILY_SQUARE_OFF_ENABLED = True
    position = Position(
        underlying_symbol="BANKNIFTY", trading_symbol="BANKNIFTY FAKE PE", basket_type="OPTIONS", regime="BEARISH",
        instrument_side="LONG", exchange_segment="NSE_FNO", product_type="PAPER", quantity=30, lot_size=30,
        entry_price=100.0, best_price=105.0, target_price=120.0, hard_stop_loss=80.0, order_id="",
        pnl_multiplier=30,
    )
    async with spe._lock:
        spe._positions["BANKNIFTY"] = position

    async def _fake_get_ltp(pos):
        return 106.0

    # _exit_one unsubscribes the option's WS price feed on close - never let
    # that touch the real dhan_wrapper.client (real Dhan auth), same
    # defensive-mocking convention as install_mocks() uses elsewhere (see
    # test_swing_v2_entry_exit.py's own comment on this exact risk class).
    from Options.dhan_client import dhan_wrapper
    saved_unsub = dhan_wrapper.unsubscribe_option_price
    dhan_wrapper.unsubscribe_option_price = lambda trading_symbol: None
    saved_get_ltp = ste._get_ltp
    ste._get_ltp = _fake_get_ltp
    try:
        with mock.patch("Swing.trading_engine.datetime") as fake_dt:
            fake_dt.now.return_value = _at(2026, 9, 21, 15, 26)  # just past cutoff
            await spe._check_one("BANKNIFTY", position)
        assert position.status == "CLOSED" and position.exit_reason == "INDEX_DAILY_SQUARE_OFF", (
            position.status, position.exit_reason,
        )
        assert "BANKNIFTY" not in spe._positions, "the closed paper position must be removed from the live paper book"
    finally:
        ste._get_ltp = saved_get_ltp
        dhan_wrapper.unsubscribe_option_price = saved_unsub
        sc.INDEX_DAILY_SQUARE_OFF_ENABLED = saved_enabled
        async with spe._lock:
            spe._positions.pop("BANKNIFTY", None)
    print("3. swing_paper_engine._check_one force-closes a NIFTY/BANKNIFTY paper position at the daily "
          "square-off cutoff with reason INDEX_DAILY_SQUARE_OFF, matching the real book: PASSED")


async def main():
    print("=== Swing INDEX_PAPER_MODE_ENABLED test suite ===\n")
    await test_1_monitor_tick_routes_only_index_candidates_to_paper_when_flag_on()
    await test_2_process_paper_entry_refuses_fresh_index_entry_after_square_off_window()
    await test_3_check_one_force_closes_index_paper_position_at_cutoff()
    print("\nALL SWING INDEX PAPER MODE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
