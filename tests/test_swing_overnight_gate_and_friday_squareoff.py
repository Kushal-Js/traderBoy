"""
Tests for the 22/23 Sep 2026 fix: Swing's entry-evaluation path and its
structure-break refresh loop polled Dhan for regime/Supertrend/structure-
break data unconditionally, 24/7 - including nights and weekends, when no
new candle can form and no order could fill anyway. Confirmed live: 39
DH-904 rate-limit hits in under an hour, overnight, entirely from this.
Two independent pieces, both covered here:

  1. Swing/signals.py's _symbol_market_open - a per-symbol (MCX vs NSE)
     market-hours gate, used by Swing/trading_engine.py's _monitor_tick
     (entry-evaluation only, never the exit-check) and by this module's
     own structure_break_refresh_loop.
  2. Swing/trading_engine.py's weekly Friday square-off (config.FRIDAY_
     SQUARE_OFF_ENABLED/_TIME) - every open Swing position (NSE and MCX
     alike) force-closed by 15:25 IST every Friday, no new entries taken
     for the rest of the week, to avoid weekend gap risk.

HOW TO RUN:
    uv run python tests/test_swing_overnight_gate_and_friday_squareoff.py
"""
import asyncio
import contextlib
import os
import sys
import tempfile
from datetime import datetime, time as dtime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_overnight_gate_test_"))
trade_history.HISTORY_DIR = scratch_dir

from Options.dhan_client import IST
import Swing.config as sc
import Swing.signals as ssig
import Swing.trading_engine as ste

from test_swing_v2_entry_exit import install_mocks, _set  # noqa: E402 - reuses the proven entry-flow mock harness


def _at(year, month, day, hh, mm) -> datetime:
    return datetime(year, month, day, hh, mm, tzinfo=IST)


@contextlib.contextmanager
def _frozen_now(dt: datetime):
    """Three separate modules each read their OWN datetime.now(IST) and all
    need freezing together for a single simulated 'now' to take effect
    end-to-end: Swing.signals (_symbol_market_open's weekday check),
    Options.dhan_client (is_market_open's actual time-of-day check - same
    reason tests/test_mcx_market_hours.py patches it on its own), and
    Swing.trading_engine (_is_friday_square_off_time/_parse_hhmm_today)."""
    with mock.patch("Swing.signals.datetime") as fake_signals_dt, \
         mock.patch("Options.dhan_client.datetime") as fake_dhan_dt, \
         mock.patch("Swing.trading_engine.datetime") as fake_ste_dt:
        fake_signals_dt.now.return_value = dt
        fake_dhan_dt.now.return_value = dt
        fake_dhan_dt.strptime = datetime.strptime
        fake_ste_dt.now.return_value = dt
        yield


# --------------------------------------------------------------------------- #
# 1. Per-symbol market-hours gate
# --------------------------------------------------------------------------- #
def test_1_nse_symbol_gated_to_nse_hours():
    with _frozen_now(_at(2026, 9, 23, 20, 10)):  # Wed, well past NSE close, still MCX hours
        assert ssig._symbol_market_open("ASHOKLEY") is False, \
            "an NSE-only symbol must report closed at 20:10 IST even though MCX is still open"
    with _frozen_now(_at(2026, 9, 23, 11, 0)):
        assert ssig._symbol_market_open("ASHOKLEY") is True, "11:00 IST is within NSE's 09:15-15:30 window"
    print("1. An NSE watchlist symbol is gated to NSE hours specifically, not MCX's longer session: PASSED")


def test_2_mcx_symbol_open_in_its_own_longer_evening_session():
    assert "NATURALGAS" in sc.MCX_SYMBOLS, "test assumes NATURALGAS is configured as an MCX symbol"
    with _frozen_now(_at(2026, 9, 23, 20, 10)):  # Wed - closed for NSE, still open for MCX
        assert ssig._symbol_market_open("NATURALGAS") is True, \
            "an MCX symbol must report open at 20:10 IST - this is exactly the real incident's own gap"
    with _frozen_now(_at(2026, 9, 24, 0, 30)):  # past MCX's own close too
        assert ssig._symbol_market_open("NATURALGAS") is False
    print("2. An MCX watchlist symbol stays open through its own materially longer evening session, "
          "and reports closed once past MCX's own close: PASSED")


def test_3_weekend_closes_both_segments_regardless_of_time_of_day():
    with _frozen_now(_at(2026, 9, 26, 11, 0)):  # Saturday, 26 Sep 2026
        assert ssig._symbol_market_open("ASHOKLEY") is False
        assert ssig._symbol_market_open("NATURALGAS") is False, \
            "is_market_open() itself only checks time-of-day - the weekday guard must catch this"
    print("3. Both NSE and MCX symbols report closed on a Saturday even during what would "
          "otherwise be live trading hours: PASSED")


async def test_4_monitor_tick_skips_entry_evaluation_outside_market_hours():
    """The actual regression: without the gate, _monitor_tick's watchlist
    scan calls _evaluate_entry_signal (which fetches live regime/
    Supertrend data) unconditionally, every 5s, even at 2 AM."""
    _set("options", max_concurrent=2)
    restore, placed, _ = install_mocks(entry_fill_status="TRADED")
    try:
        from Swing.watchlist import watchlist_store
        original_symbols = watchlist_store.symbols
        watchlist_store.symbols = lambda: asyncio.sleep(0, result=["ASHOKLEY"])

        eval_calls = []

        async def _fake_evaluate(symbol):
            eval_calls.append(symbol)
            return "BULLISH"

        original_evaluate = ste._evaluate_entry_signal
        ste._evaluate_entry_signal = _fake_evaluate
        try:
            with _frozen_now(_at(2026, 9, 23, 2, 0)):  # Wed 2 AM - well outside NSE hours
                await ste._monitor_tick()
                assert eval_calls == [], \
                    f"_evaluate_entry_signal must not be called for an NSE symbol at 2 AM, got {eval_calls}"

            with _frozen_now(_at(2026, 9, 23, 11, 0)):  # Wed, within NSE hours
                await ste._monitor_tick()
                assert eval_calls == ["ASHOKLEY"], \
                    f"_evaluate_entry_signal must be called once real trading hours arrive, got {eval_calls}"
        finally:
            ste._evaluate_entry_signal = original_evaluate
            watchlist_store.symbols = original_symbols
    finally:
        restore()
    print("4. _monitor_tick's entry-evaluation is skipped entirely outside market hours, "
          "and resumes once hours arrive: PASSED")


# --------------------------------------------------------------------------- #
# 2. Friday square-off
# --------------------------------------------------------------------------- #
def test_5_is_friday_square_off_time():
    with mock.patch("Swing.trading_engine.datetime") as fake_dt:
        fake_dt.now.return_value = _at(2026, 9, 25, 15, 24)  # Friday, 1 min before cutoff
        # _parse_hhmm_today builds off _now_ist()'s own return value (a real
        # datetime instance) via .replace(), so mocking datetime.now above is
        # sufficient - no further patching needed for it here.
        assert ste._is_friday_square_off_time() is False, "15:24 IST Friday is still 1 minute before cutoff"
        fake_dt.now.return_value = _at(2026, 9, 25, 15, 25)
        assert ste._is_friday_square_off_time() is True, "15:25 IST Friday is exactly the configured cutoff"
        fake_dt.now.return_value = _at(2026, 9, 25, 20, 0)
        assert ste._is_friday_square_off_time() is True, "stays True for the rest of Friday once crossed"
        fake_dt.now.return_value = _at(2026, 9, 24, 20, 0)  # Thursday, same time-of-day
        assert ste._is_friday_square_off_time() is False, "only Friday - Thursday evening must not trigger it"
    print("5. _is_friday_square_off_time fires only at/after 15:25 IST on a Friday, not other weekdays: PASSED")


async def test_6_monitor_tick_square_offs_and_skips_normal_flow_on_friday():
    """Confirms _monitor_tick calls _square_off_all with the right reason
    and returns early - no ordinary exit-check, no new-entry evaluation -
    once the Friday cutoff has passed, so nothing carries into the
    weekend and no new position gets opened only to need the same
    treatment moments later."""
    _set("options", max_concurrent=2)
    restore, placed, _ = install_mocks(entry_fill_status="TRADED")
    try:
        from Swing.watchlist import watchlist_store
        original_symbols = watchlist_store.symbols
        watchlist_store.symbols = lambda: asyncio.sleep(0, result=["ASHOKLEY"])

        eval_calls = []

        async def _fake_evaluate(symbol):
            eval_calls.append(symbol)
            return "BULLISH"

        squareoff_calls = []
        original_square_off_all = ste._square_off_all

        async def _fake_square_off_all(reason):
            squareoff_calls.append(reason)

        original_evaluate = ste._evaluate_entry_signal
        ste._evaluate_entry_signal = _fake_evaluate
        ste._square_off_all = _fake_square_off_all
        try:
            with mock.patch("Swing.trading_engine.datetime") as fake_dt:
                fake_dt.now.return_value = _at(2026, 9, 25, 15, 30)  # Friday, past cutoff
                await ste._monitor_tick()
                assert squareoff_calls == ["FRIDAY_SQUARE_OFF"], squareoff_calls
                assert eval_calls == [], \
                    f"no new entry should be evaluated once the Friday square-off window is active, got {eval_calls}"
        finally:
            ste._evaluate_entry_signal = original_evaluate
            ste._square_off_all = original_square_off_all
            watchlist_store.symbols = original_symbols
    finally:
        restore()
    print("6. _monitor_tick calls _square_off_all('FRIDAY_SQUARE_OFF') and skips the ordinary "
          "exit-check/entry-scan entirely once the Friday cutoff has passed: PASSED")


# --------------------------------------------------------------------------- #
# 3. Daily index-only square-off (NIFTY/BANKNIFTY, added 24 Sep 2026 - user
# request: "all open positions for NIFTY and BANKNIFTY to be strictly
# squared off at 3:25 daily... as they are very volatile instruments")
# --------------------------------------------------------------------------- #
def test_7_is_index_square_off_time():
    with mock.patch("Swing.trading_engine.datetime") as fake_dt:
        fake_dt.now.return_value = _at(2026, 9, 21, 15, 24)  # plain Monday, 1 min before cutoff
        assert ste._is_index_square_off_time() is False
        fake_dt.now.return_value = _at(2026, 9, 21, 15, 25)
        assert ste._is_index_square_off_time() is True, "must fire on an ordinary Monday, not just Friday"
        fake_dt.now.return_value = _at(2026, 9, 21, 20, 0)
        assert ste._is_index_square_off_time() is True, "stays True for the rest of the day once crossed"
        fake_dt.now.return_value = _at(2026, 9, 26, 15, 30)  # Saturday
        assert ste._is_index_square_off_time() is False, "weekend must not trigger it"
    print("7. _is_index_square_off_time fires daily at/after 15:25 IST on ANY weekday, not just Friday: PASSED")


async def test_8_square_off_all_symbol_scoping():
    """_square_off_all(reason, symbols={...}) must only touch the given
    symbols' positions, leaving every other open position completely
    untouched - the core safety property the index-only daily square-off
    depends on (must never force-close a carrying-by-design ASHOKLEY/
    COPPER position just because NIFTY's own daily cutoff fired)."""
    from Swing.position_store import Position
    saved_live = dict(ste.position_store.live_positions)
    original_get_ltp, original_exit_position = ste._get_ltp, ste._exit_position
    exited = []
    try:
        ste.position_store.live_positions.clear()
        ste.position_store.live_positions["NIFTY"] = Position(
            underlying_symbol="NIFTY", trading_symbol="NIFTY FAKE CE", basket_type="OPTIONS", regime="BULLISH",
            instrument_side="LONG", exchange_segment="NSE_FNO", product_type="MARGIN", quantity=65, lot_size=65,
            entry_price=100.0, best_price=100.0, target_price=120.0, hard_stop_loss=80.0, order_id="O1",
            pnl_multiplier=65,
        )
        ste.position_store.live_positions["ASHOKLEY"] = Position(
            underlying_symbol="ASHOKLEY", trading_symbol="ASHOKLEY FAKE CE", basket_type="OPTIONS", regime="BULLISH",
            instrument_side="LONG", exchange_segment="NSE_FNO", product_type="MARGIN", quantity=500, lot_size=500,
            entry_price=50.0, best_price=50.0, target_price=60.0, hard_stop_loss=40.0, order_id="O2",
            pnl_multiplier=500,
        )

        async def _fake_get_ltp(position):
            return position.entry_price

        async def _fake_exit_position(symbol, position, ltp, reason):
            exited.append(symbol)
            await ste.position_store.close_position(symbol, ltp, reason)

        ste._get_ltp = _fake_get_ltp
        ste._exit_position = _fake_exit_position

        await ste._square_off_all("INDEX_DAILY_SQUARE_OFF", symbols={"NIFTY", "BANKNIFTY"})
        assert exited == ["NIFTY"], f"only the index symbol should be squared off, got {exited}"
        assert "ASHOKLEY" in ste.position_store.live_positions, \
            "a non-index symbol's position must be completely untouched by the index-scoped square-off"
        assert "NIFTY" not in ste.position_store.live_positions, "NIFTY itself must actually be closed"
    finally:
        ste._get_ltp, ste._exit_position = original_get_ltp, original_exit_position
        ste.position_store.live_positions.clear()
        ste.position_store.live_positions.update(saved_live)
    print("8. _square_off_all(symbols=...) only touches the given symbols, leaving every other "
          "open position completely alone: PASSED")


async def test_9_monitor_tick_squares_off_index_only_and_keeps_normal_flow_for_others():
    """THE real regression test: on an ordinary (non-Friday) day past
    15:25, _monitor_tick must (a) force-close NIFTY/BANKNIFTY positions,
    (b) NOT return early - ASHOKLEY still gets its normal exit-check/
    entry-scan this same tick, and (c) refuse a FRESH same-day NIFTY
    entry even if a signal fires, while still taking one for ASHOKLEY."""
    _set("options", max_concurrent=5)
    restore, placed, _ = install_mocks(entry_fill_status="TRADED")
    try:
        from Swing.watchlist import watchlist_store
        original_symbols = watchlist_store.symbols
        watchlist_store.symbols = lambda: asyncio.sleep(0, result=["ASHOKLEY", "NIFTY"])

        eval_calls = []

        async def _fake_evaluate(symbol):
            eval_calls.append(symbol)
            return "BULLISH"

        squareoff_calls = []
        original_square_off_all = ste._square_off_all

        async def _fake_square_off_all(reason, symbols=None):
            squareoff_calls.append((reason, symbols))

        original_evaluate = ste._evaluate_entry_signal
        ste._evaluate_entry_signal = _fake_evaluate
        ste._square_off_all = _fake_square_off_all
        try:
            with mock.patch("Swing.trading_engine.datetime") as fake_dt:
                fake_dt.now.return_value = _at(2026, 9, 21, 15, 30)  # plain Monday, past 15:25 cutoff
                await ste._monitor_tick()
                assert squareoff_calls == [("INDEX_DAILY_SQUARE_OFF", sc.INDEX_SYMBOLS)], squareoff_calls
                assert eval_calls == ["ASHOKLEY"], (
                    f"ASHOKLEY must still be evaluated normally (no early return), NIFTY must be "
                    f"skipped entirely (no fresh same-day index entry), got {eval_calls}"
                )
        finally:
            ste._evaluate_entry_signal = original_evaluate
            ste._square_off_all = original_square_off_all
            watchlist_store.symbols = original_symbols
    finally:
        restore()
    print("9. _monitor_tick force-closes NIFTY/BANKNIFTY daily past 15:25 WITHOUT returning early - "
          "ASHOKLEY keeps its normal exit-check/entry-scan, and no fresh same-day index entry is taken: PASSED")


async def main():
    print("=== Swing overnight market-hours gate + Friday square-off test suite ===\n")
    test_1_nse_symbol_gated_to_nse_hours()
    test_2_mcx_symbol_open_in_its_own_longer_evening_session()
    test_3_weekend_closes_both_segments_regardless_of_time_of_day()
    await test_4_monitor_tick_skips_entry_evaluation_outside_market_hours()
    test_5_is_friday_square_off_time()
    await test_6_monitor_tick_square_offs_and_skips_normal_flow_on_friday()
    test_7_is_index_square_off_time()
    await test_8_square_off_all_symbol_scoping()
    await test_9_monitor_tick_squares_off_index_only_and_keeps_normal_flow_for_others()
    print("\nALL SWING OVERNIGHT-GATE + FRIDAY-SQUARE-OFF CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
