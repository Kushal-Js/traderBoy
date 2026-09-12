"""
Tests for Swing/trading_engine.py's _exit_reason_for (pure, flat rupee/
percent thresholds) and _evaluate_exit_signal's entry-candle guard - the
actual EXIT DECISION logic, as distinct from tests/test_swing_v2_entry_
exit.py's coverage of order placement/sync once an exit reason has
already fired.

HOW TO RUN:
    uv run python tests/test_swing_v2_exit_reason.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Swing.config as sc
import Swing.trading_engine as ste
from Swing.position_store import Position
from Swing.signals import SupertrendState


def _make_position(**overrides):
    defaults = dict(
        underlying_symbol="TESTSTOCK", trading_symbol="TESTSTOCK FUT", basket_type="FUTURES",
        regime="BULLISH", instrument_side="LONG", exchange_segment="NSE_FNO", product_type="MARGIN",
        quantity=100, lot_size=100, entry_price=100.0, best_price=100.0,
        target_price=120.0, hard_stop_loss=80.0, order_id="OID",
    )
    defaults.update(overrides)
    return Position(**defaults)


def test_1_max_loss_hit_fires_at_the_configured_cap_for_long_and_short():
    real_cap = sc.MAX_LOSS_PROTECTION_RS
    sc.MAX_LOSS_PROTECTION_RS = 1000.0
    try:
        long_pos = _make_position(instrument_side="LONG", entry_price=100.0, quantity=100,
                                   target_price=1_000_000.0, hard_stop_loss=-1_000_000.0)
        assert ste._exit_reason_for(long_pos, ltp=89.99) == "MAX_LOSS_HIT"   # loss = 1001
        assert ste._exit_reason_for(long_pos, ltp=90.01) is None            # loss = 999

        short_pos = _make_position(instrument_side="SHORT", entry_price=100.0, quantity=100,
                                    target_price=-1_000_000.0, hard_stop_loss=1_000_000.0)
        assert ste._exit_reason_for(short_pos, ltp=110.01) == "MAX_LOSS_HIT"  # loss = 1001 (price rose against short)
        assert ste._exit_reason_for(short_pos, ltp=109.99) is None
        print("1. MAX_LOSS_HIT fires at exactly the configured rupee cap, for both LONG and SHORT: PASSED")
    finally:
        sc.MAX_LOSS_PROTECTION_RS = real_cap


def test_2_target_hit_respects_enable_flag_and_direction():
    real_enabled = sc.ENABLE_TARGET_EXIT
    try:
        sc.ENABLE_TARGET_EXIT = True
        long_pos = _make_position(instrument_side="LONG", entry_price=100.0, target_price=120.0, hard_stop_loss=-1e6)
        assert ste._exit_reason_for(long_pos, ltp=120.0) == "TARGET_HIT"
        short_pos = _make_position(instrument_side="SHORT", entry_price=100.0, target_price=80.0, hard_stop_loss=1e6)
        assert ste._exit_reason_for(short_pos, ltp=80.0) == "TARGET_HIT", "a SHORT's target is below entry"

        sc.ENABLE_TARGET_EXIT = False
        assert ste._exit_reason_for(long_pos, ltp=130.0) is None, "flag off must suppress TARGET_HIT"
        print("2. TARGET_HIT respects ENABLE_TARGET_EXIT and fires correctly for both directions: PASSED")
    finally:
        sc.ENABLE_TARGET_EXIT = real_enabled


def test_3_profit_protection_needs_both_threshold_and_giveback():
    real_threshold, real_giveback = sc.PROFIT_PROTECTION_RS, sc.PROFIT_PROTECTION_GIVEBACK_PCT
    try:
        sc.PROFIT_PROTECTION_RS = 500.0
        sc.PROFIT_PROTECTION_GIVEBACK_PCT = 0.0
        # Peak profit of 600 (best_price=106 vs entry=100, qty=100) crosses the threshold;
        # any dip below best_price (0% giveback) arms it.
        pos = _make_position(instrument_side="LONG", entry_price=100.0, best_price=106.0,
                              target_price=1e6, hard_stop_loss=-1e6, quantity=100)
        assert ste._exit_reason_for(pos, ltp=105.99) == "PROFIT_PROTECTION_HIT"
        assert ste._exit_reason_for(pos, ltp=106.0) is None, "at the peak itself, not yet a dip"

        # Peak profit UNDER the threshold must never arm it, no matter how far it then dips.
        pos2 = _make_position(instrument_side="LONG", entry_price=100.0, best_price=104.0,
                               target_price=1e6, hard_stop_loss=-1e6, quantity=100)
        assert ste._exit_reason_for(pos2, ltp=90.0) != "PROFIT_PROTECTION_HIT"
        print("3. PROFIT_PROTECTION_HIT requires peak profit past the threshold AND a retrace past the "
              "giveback floor - never fires on threshold alone: PASSED")
    finally:
        sc.PROFIT_PROTECTION_RS, sc.PROFIT_PROTECTION_GIVEBACK_PCT = real_threshold, real_giveback


def test_4_stop_loss_hit_direction_aware():
    long_pos = _make_position(instrument_side="LONG", entry_price=100.0, hard_stop_loss=90.0,
                               target_price=1e6, quantity=100)
    assert ste._exit_reason_for(long_pos, ltp=89.99) == "STOP_LOSS_HIT"
    assert ste._exit_reason_for(long_pos, ltp=90.01) is None

    short_pos = _make_position(instrument_side="SHORT", entry_price=100.0, hard_stop_loss=110.0,
                                target_price=-1e6, quantity=100)
    assert ste._exit_reason_for(short_pos, ltp=110.01) == "STOP_LOSS_HIT"
    assert ste._exit_reason_for(short_pos, ltp=109.99) is None
    print("4. STOP_LOSS_HIT (the flat 20% hard stop) fires correctly for both LONG and SHORT: PASSED")


def test_5_exit_reason_precedence_max_loss_wins_over_everything():
    """A position that is simultaneously past MAX_LOSS_PROTECTION_RS and
    past its hard stop must report MAX_LOSS_HIT, not STOP_LOSS_HIT - the
    rupee cap is checked first, same relative ordering as every other
    package in this codebase."""
    real_cap = sc.MAX_LOSS_PROTECTION_RS
    sc.MAX_LOSS_PROTECTION_RS = 500.0
    try:
        pos = _make_position(instrument_side="LONG", entry_price=100.0, hard_stop_loss=95.0,
                              target_price=1e6, quantity=100)
        # loss at ltp=90 is Rs 1000 (past both the Rs500 cap and the hard stop at 95)
        assert ste._exit_reason_for(pos, ltp=90.0) == "MAX_LOSS_HIT"
        print("5. MAX_LOSS_HIT takes precedence over STOP_LOSS_HIT when both would fire: PASSED")
    finally:
        sc.MAX_LOSS_PROTECTION_RS = real_cap


async def test_6_supertrend_reversal_suppressed_on_the_entry_candle():
    real_supertrend = sc.ENABLE_SUPERTREND_EXIT
    sc.ENABLE_SUPERTREND_EXIT = True
    try:
        entry_candle = datetime(2026, 9, 1, 10, 0)
        # Same candle as entry - crossed_below is true, but must NOT count as a real reversal yet.
        same_candle_state = SupertrendState(candle_start=entry_candle, close=90.0, supertrend=95.0, is_above=False,
                                             prev_close=100.0, prev_supertrend=95.0, prev_is_above=True)
        ste.signals.get_supertrend_state = lambda symbol: _resolved(same_candle_state)
        pos = _make_position(instrument_side="LONG", supertrend_entry_candle_start=entry_candle)
        result = await ste._evaluate_exit_signal("TESTSTOCK", pos)
        assert result is None, "a reversal reading the ENTRY candle itself must not count yet"

        # A LATER candle with a genuine crossed_below - now it should fire.
        later_candle = entry_candle + timedelta(minutes=5)
        later_state = SupertrendState(candle_start=later_candle, close=90.0, supertrend=95.0, is_above=False,
                                       prev_close=100.0, prev_supertrend=95.0, prev_is_above=True)
        ste.signals.get_supertrend_state = lambda symbol: _resolved(later_state)
        result2 = await ste._evaluate_exit_signal("TESTSTOCK", pos)
        assert result2 == "SUPERTREND_REVERSAL"
        print("6. A Supertrend reversal on the position's own entry candle is suppressed; a genuinely "
              "later reversal fires SUPERTREND_REVERSAL: PASSED")
    finally:
        sc.ENABLE_SUPERTREND_EXIT = real_supertrend


async def _resolved(value):
    return value


async def main():
    print("=== Swing v2 exit-reason decision logic test suite ===\n")
    test_1_max_loss_hit_fires_at_the_configured_cap_for_long_and_short()
    test_2_target_hit_respects_enable_flag_and_direction()
    test_3_profit_protection_needs_both_threshold_and_giveback()
    test_4_stop_loss_hit_direction_aware()
    test_5_exit_reason_precedence_max_loss_wins_over_everything()
    await test_6_supertrend_reversal_suppressed_on_the_entry_candle()
    print("\nALL SWING V2 EXIT-REASON CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
