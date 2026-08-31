"""
Tests for the before/after-11:30 split of MAX_LOSS_PER_TRADE_RS and
PROFIT_PROTECTION_THRESHOLD_RS - user request 31 Aug 2026 ("1500/1000
before/after 11:30 for profit protection, 1200/1000 before/after 11:30
for max loss, for both Options and Futures").

Covers, against the REAL production functions (not reimplemented):
  1. current_max_loss_per_trade_rs()/current_profit_protection_threshold_rs()
     return the correct value on each side of config.RISK_THRESHOLD_CUTOFF_TIME,
     for both Options and Futures independently.
  2. The exact boundary instant (11:30:00) already counts as "after" -
     consistent with every other time-of-day gate in this codebase
     (is_past_square_off_time, is_past_allowed_trading_time all use the
     same >= semantics).
  3. _exit_reason_for itself - not just the lookup functions - actually
     fires MAX_LOSS_HIT/PROFIT_PROTECTION_HIT at the correct threshold on
     each side of the cutoff, for both Options and Futures.

HOW TO RUN:
    uv run python tests/test_risk_threshold_cutoff.py
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.trading_engine as ote
import Futures.trading_engine as fte
from Options.position_store import Position as OptionsPosition
from Futures.position_store import Position as FuturesPosition

IST = ZoneInfo("Asia/Kolkata")
BEFORE_CUTOFF = datetime(2026, 8, 31, 11, 0, tzinfo=IST)   # 11:00 AM
AT_CUTOFF = datetime(2026, 8, 31, 11, 30, tzinfo=IST)      # exactly 11:30
AFTER_CUTOFF = datetime(2026, 8, 31, 11, 45, tzinfo=IST)   # 11:45 AM


def _freeze_time(module, dt: datetime):
    """Monkeypatches module._now_ist() to a fixed instant, returns a
    restore() closure. Both trading_engine modules define their own
    _now_ist() (not shared), so this must be applied per-module."""
    real = module._now_ist
    module._now_ist = lambda: dt

    def restore():
        module._now_ist = real
    return restore


def test_1_lookup_functions_switch_at_the_cutoff():
    for label, module in (("Options", ote), ("Futures", fte)):
        restore = _freeze_time(module, BEFORE_CUTOFF)
        try:
            assert module.current_max_loss_per_trade_rs() == 1200, label
            assert module.current_profit_protection_threshold_rs() == 1500, label
        finally:
            restore()

        restore = _freeze_time(module, AFTER_CUTOFF)
        try:
            assert module.current_max_loss_per_trade_rs() == 1000, label
            assert module.current_profit_protection_threshold_rs() == 1000, label
        finally:
            restore()
    print("1. current_max_loss_per_trade_rs()/current_profit_protection_threshold_rs() "
          "return 1200/1500 before 11:30 and 1000/1000 after, for both Options and Futures: PASSED")


def test_2_exact_boundary_instant_counts_as_after():
    """Matches this codebase's own established convention elsewhere
    (is_past_square_off_time, is_past_allowed_trading_time - both use
    `_now_ist() >= cutoff`) - the boundary second itself already gets the
    tighter afternoon values, not the looser morning ones."""
    for label, module in (("Options", ote), ("Futures", fte)):
        restore = _freeze_time(module, AT_CUTOFF)
        try:
            assert module.current_max_loss_per_trade_rs() == 1000, label
            assert module.current_profit_protection_threshold_rs() == 1000, label
        finally:
            restore()
    print("2. Exactly 11:30:00 already counts as 'after' for both packages, "
          "consistent with every other time-of-day gate in this codebase: PASSED")


def _make_options_position(**overrides) -> OptionsPosition:
    defaults = dict(
        underlying_symbol="TESTSTOCK", option_trading_symbol="TESTSTOCK 25 SEP 100 CALL",
        option_type="CE", quantity=1, lot_size=1, entry_price=2000.0, highest_price=2000.0,
        target_price=1_000_000.0,   # far away - never hit in these tests
        hard_stop_loss=-1_000_000.0,  # far away - never hit via trailing/hard SL in these tests
        order_id="OID", product_type="MARGIN", opened_at=datetime.now(),
    )
    defaults.update(overrides)
    return OptionsPosition(**defaults)


def _make_futures_position(**overrides) -> FuturesPosition:
    defaults = dict(
        underlying_symbol="TESTSTOCK", option_trading_symbol="TESTSTOCK 25 SEP 100 CALL",
        option_type="CE", quantity=1, lot_size=1, entry_price=2000.0, highest_price=2000.0,
        target_price=1_000_000.0, hard_stop_loss=-1_000_000.0,
        order_id="OID", product_type="MARGIN", opened_at=datetime.now(),
    )
    defaults.update(overrides)
    return FuturesPosition(**defaults)


def test_3_exit_reason_for_uses_the_correct_cap_on_each_side():
    """quantity=1 so 1 rupee of LTP movement = Rs 1 of P&L, making the
    1200/1000/1500/1000 boundaries easy to straddle exactly. hard_stop_loss/
    target_price are pushed far away on every position here so only the
    MAX_LOSS_HIT/PROFIT_PROTECTION_HIT checks under test can possibly fire -
    isolating them from TARGET_HIT/TRAILING_SL_HIT/STOP_LOSS_HIT, which
    have their own dedicated coverage in test_deep_integration.py."""
    for label, module, make_position in (
        ("Options", ote, _make_options_position),
        ("Futures", fte, _make_futures_position),
    ):
        # MAX_LOSS_HIT: loss of Rs 1100 (entry 2000, ltp 900) -> under the
        # 1200 morning cap (no trip) but over the 1000 afternoon cap (trips).
        pos = make_position()
        ltp = pos.entry_price - 1100.0  # loss_rs = (2000 - 900) * 1 = 1100

        restore = _freeze_time(module, BEFORE_CUTOFF)
        try:
            assert module._exit_reason_for(pos, ltp) is None, \
                f"{label}: a Rs 1100 loss must NOT trip any exit before 11:30 (max-loss cap is 1200)"
        finally:
            restore()

        restore = _freeze_time(module, AFTER_CUTOFF)
        try:
            assert module._exit_reason_for(pos, ltp) == "MAX_LOSS_HIT", \
                f"{label}: a Rs 1100 loss MUST trip MAX_LOSS_HIT after 11:30 (cap is 1000)"
        finally:
            restore()

        # PROFIT_PROTECTION_HIT: peak profit of Rs 1200 (highest_price set
        # 1200 above entry), current ltp one rupee below that peak -> not
        # armed before 11:30 (threshold 1500) but armed after 11:30
        # (threshold 1000).
        pos2 = make_position(highest_price=2000.0 + 1200.0)  # peak_profit_rs = 1200
        ltp2 = pos2.highest_price - 1  # one rupee off the peak, still a large net profit

        restore = _freeze_time(module, BEFORE_CUTOFF)
        try:
            assert module._exit_reason_for(pos2, ltp2) is None, \
                f"{label}: Rs 1200 peak profit must NOT arm protection before 11:30 (threshold is 1500)"
        finally:
            restore()

        restore = _freeze_time(module, AFTER_CUTOFF)
        try:
            assert module._exit_reason_for(pos2, ltp2) == "PROFIT_PROTECTION_HIT", \
                f"{label}: Rs 1200 peak profit MUST arm protection after 11:30 (threshold is 1000)"
        finally:
            restore()

    print("3. _exit_reason_for() itself fires MAX_LOSS_HIT/PROFIT_PROTECTION_HIT at the "
          "correct before/after-11:30 threshold, for both Options and Futures: PASSED")


def main():
    print("=== Risk-threshold time-of-day cutoff test suite ===\n")
    test_1_lookup_functions_switch_at_the_cutoff()
    test_2_exact_boundary_instant_counts_as_after()
    test_3_exit_reason_for_uses_the_correct_cap_on_each_side()
    print("\nALL RISK-THRESHOLD-CUTOFF CHECKS PASSED")


if __name__ == "__main__":
    main()
