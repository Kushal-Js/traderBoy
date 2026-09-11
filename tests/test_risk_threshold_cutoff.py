"""
Tests for the before/after-11:30 split of MAX_LOSS_PER_TRADE_RS and
PROFIT_PROTECTION_THRESHOLD_RS - user request 31 Aug 2026 ("1500/1000
before/after 11:30 for profit protection, 1200/1000 before/after 11:30
for max loss, for both Options and Futures") - AND for config.ENABLE_
MAX_LOSS_HIT_BEFORE_CUTOFF (added 11 Sep 2026, user request: "disable
MAX_LOSS_HIT for before 11:30 and add exit conditions as below: EMA 9
of 5 min close crossed below EMA 12 of 5 min close or 5 min close
crossed below 5 min supertrend" - the trend-reversal exits already
existed and are covered elsewhere; this file covers the new disable).

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
     each side of the cutoff, for Options/Futures/Luxury.
  4. With ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF at its default (False),
     MAX_LOSS_HIT never fires before the cutoff REGARDLESS of loss size
     (not just "under the old 1200/1300 cap" - a much bigger loss too),
     for Options/Futures/Luxury, but fires normally the instant the
     clock crosses the cutoff.
  5. ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF=True restores the original
     always-on behavior (fires before the cutoff at the BEFORE_CUTOFF
     threshold, same as it always used to).
  6. The disable is scoped to MAX_LOSS_HIT ONLY - TRAILING_SL_HIT/
     STOP_LOSS_HIT (the percentage stop) still fires normally before the
     cutoff even with MAX_LOSS_HIT disabled, for all 3 packages.

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
import Luxury.trading_engine as lte
from Options.position_store import Position as OptionsPosition
from Futures.position_store import Position as FuturesPosition
from Luxury.position_store import Position as LuxuryPosition

IST = ZoneInfo("Asia/Kolkata")
BEFORE_CUTOFF = datetime(2026, 8, 31, 11, 0, tzinfo=IST)   # 11:00 AM
AT_CUTOFF = datetime(2026, 8, 31, 11, 30, tzinfo=IST)      # exactly 11:30
AFTER_CUTOFF = datetime(2026, 8, 31, 11, 45, tzinfo=IST)   # 11:45 AM

ALL_PACKAGES = (("Options", ote), ("Futures", fte), ("Luxury", lte))


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
    """1200->1500 (user request 11 Sep 2026, alongside re-enabling
    ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF after the disable-before-11:30
    experiment - see that flag's own docstring) - pinned to the current
    deployed value, same convention as every other "pinned" test in this
    suite when a live constant changes."""
    for label, module in (("Options", ote), ("Futures", fte)):
        restore = _freeze_time(module, BEFORE_CUTOFF)
        try:
            assert module.current_max_loss_per_trade_rs() == 1500, label
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
          "return 1500/1500 before 11:30 and 1000/1000 after, for both Options and Futures: PASSED")


def test_1b_luxury_max_loss_pinned_to_its_own_wider_cap():
    """Luxury's MAX_LOSS_HIT cap was raised to 4500/2100 (11 Sep 2026, user
    request - Luxury only, Options/Futures unchanged at 1500/1000) - a
    separate pinned check since Luxury now genuinely diverges from the
    other two packages, not just its own independently-configured copy of
    the same number. PROFIT_PROTECTION_THRESHOLD_RS is untouched by this
    change, still 1500/1000 same as the others."""
    restore = _freeze_time(lte, BEFORE_CUTOFF)
    try:
        assert lte.current_max_loss_per_trade_rs() == 4500, "Luxury"
        assert lte.current_profit_protection_threshold_rs() == 1500, "Luxury"
    finally:
        restore()

    restore = _freeze_time(lte, AFTER_CUTOFF)
    try:
        assert lte.current_max_loss_per_trade_rs() == 2100, "Luxury"
        assert lte.current_profit_protection_threshold_rs() == 1000, "Luxury"
    finally:
        restore()
    print("1b. Luxury's own current_max_loss_per_trade_rs() returns 4500 before 11:30 and 2100 "
          "after (Options/Futures unaffected, still 1500/1000): PASSED")


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


def _make_luxury_position(**overrides) -> LuxuryPosition:
    defaults = dict(
        underlying_symbol="TESTSTOCK", option_trading_symbol="TESTSTOCK 25 SEP 100 CALL",
        option_type="CE", quantity=1, lot_size=1, entry_price=2000.0, highest_price=2000.0,
        target_price=1_000_000.0, hard_stop_loss=-1_000_000.0,
        order_id="OID", product_type="MARGIN", opened_at=datetime.now(),
    )
    defaults.update(overrides)
    return LuxuryPosition(**defaults)


ALL_MAKE_POSITION = (
    ("Options", ote, _make_options_position),
    ("Futures", fte, _make_futures_position),
    ("Luxury", lte, _make_luxury_position),
)


def test_3_exit_reason_for_uses_the_correct_cap_on_each_side():
    """quantity=1 so 1 rupee of LTP movement = Rs 1 of P&L. The MAX_LOSS_HIT
    straddle loss is computed from each module's OWN before/after cap
    (rather than a single hardcoded number) since Luxury now runs a
    genuinely different pair (4500/2100) from Options/Futures (1500/1000,
    11 Sep 2026) - this keeps the test meaningful regardless of any one
    package's own independently-tuned values, present or future.
    hard_stop_loss/target_price are pushed far away on every position
    here so only the MAX_LOSS_HIT/PROFIT_PROTECTION_HIT checks under test
    can possibly fire - isolating them from TARGET_HIT/TRAILING_SL_HIT/
    STOP_LOSS_HIT, which have their own dedicated coverage in
    test_deep_integration.py.

    ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF is forced True here - this test is
    specifically about the BEFORE/AFTER threshold VALUES, which only
    matters when MAX_LOSS_HIT is actually allowed to fire before the
    cutoff at all; the NEW default (False, disabled entirely before the
    cutoff) has its own dedicated tests 4-6 below."""
    for label, module, make_position in ALL_MAKE_POSITION:
        real_enabled = module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF
        module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF = True
        # MAX_LOSS_HIT: a loss strictly between this module's own AFTER cap
        # (tighter) and BEFORE cap (looser) - must NOT trip before 11:30,
        # MUST trip after. Options/Futures: (1000, 1500) -> 1250. Luxury:
        # (2100, 4500) -> 3300.
        before_cap = module.config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF
        after_cap = module.config.MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF
        straddle_loss = after_cap + (before_cap - after_cap) / 2
        pos = make_position()
        ltp = pos.entry_price - straddle_loss

        restore = _freeze_time(module, BEFORE_CUTOFF)
        try:
            assert module._exit_reason_for(pos, ltp) is None, \
                f"{label}: a Rs {straddle_loss:.0f} loss must NOT trip any exit before 11:30 (max-loss cap is {before_cap:.0f})"
        finally:
            restore()

        restore = _freeze_time(module, AFTER_CUTOFF)
        try:
            assert module._exit_reason_for(pos, ltp) == "MAX_LOSS_HIT", \
                f"{label}: a Rs {straddle_loss:.0f} loss MUST trip MAX_LOSS_HIT after 11:30 (cap is {after_cap:.0f})"
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
            module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF = real_enabled

    print("3. _exit_reason_for() itself fires MAX_LOSS_HIT/PROFIT_PROTECTION_HIT at the "
          "correct before/after-11:30 threshold, for Options/Futures/Luxury "
          "(with ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF forced on): PASSED")


# --------------------------------------------------------------------- #
# config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF (added 11 Sep 2026, default
# False) - see its own docstring in Options/config.py for the full
# rationale: rely on trend-reversal exits during the more volatile first
# part of the session instead of a hard rupee cap.
# --------------------------------------------------------------------- #

def test_4_max_loss_hit_disabled_before_cutoff_regardless_of_loss_size():
    """The code default ships False (see Options/config.py's own
    docstring) - deployed live as True as of 11 Sep 2026 (re-enabled at
    a looser Rs 1500 before-cutoff cap, after the fully-disabled
    experiment cost more than it saved against real trades - see
    trading-skills' capacity-and-ranking.md). This test explicitly
    forces the flag OFF (save/restore) rather than assuming it's the
    ambient value, so it stays meaningful regardless of what's currently
    deployed: unlike test_3 above, this isn't about which threshold
    value applies - a HUGE loss (Rs 50,000, dwarfing even the loosest
    before-cutoff cap ever used) must still produce no exit before the
    cutoff when the flag is off, and the moment the clock crosses into
    the afternoon, the exact same position fires MAX_LOSS_HIT normally."""
    for label, module, make_position in ALL_MAKE_POSITION:
        real_enabled = module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF
        module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF = False
        pos = make_position()
        ltp = pos.entry_price - 50_000.0  # a loss no morning cap has ever been anywhere close to

        restore = _freeze_time(module, BEFORE_CUTOFF)
        try:
            assert module._exit_reason_for(pos, ltp) is None, \
                f"{label}: MAX_LOSS_HIT must not fire before the cutoff AT ALL, any loss size, when the flag is off"
        finally:
            restore()

        restore = _freeze_time(module, AFTER_CUTOFF)
        try:
            assert module._exit_reason_for(pos, ltp) == "MAX_LOSS_HIT", \
                f"{label}: the SAME position must trip MAX_LOSS_HIT normally once past the cutoff"
        finally:
            restore()
            module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF = real_enabled
    print("4. ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF=False suppresses MAX_LOSS_HIT before "
          "the cutoff regardless of loss size, for Options/Futures/Luxury, while firing normally "
          "the instant the clock crosses into the afternoon: PASSED")


def test_5_flag_on_restores_the_original_always_on_behavior():
    for label, module, make_position in ALL_MAKE_POSITION:
        real_enabled = module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF
        module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF = True
        pos = make_position()
        ltp = pos.entry_price - 50_000.0
        restore = _freeze_time(module, BEFORE_CUTOFF)
        try:
            assert module._exit_reason_for(pos, ltp) == "MAX_LOSS_HIT", \
                f"{label}: with the flag explicitly re-enabled, a real loss must trip MAX_LOSS_HIT before the cutoff too"
        finally:
            restore()
            module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF = real_enabled
    print("5. ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF=True cleanly restores the original always-on "
          "behavior, for Options/Futures/Luxury: PASSED")


def test_6_disable_is_scoped_to_max_loss_hit_only():
    """The percentage stop-loss (STOP_LOSS_PCT/dynamic-SL) must still
    catch a large enough loss before the cutoff even with MAX_LOSS_HIT
    disabled - this feature narrows WHICH exit fires, it doesn't remove
    downside protection altogether. Flag forced off explicitly (see
    test_4's own docstring for why - not assumed from whatever's
    currently deployed)."""
    for label, module, make_position in ALL_MAKE_POSITION:
        real_enabled = module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF
        module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF = False
        # hard_stop_loss set close to entry (unlike the other tests' -1e6
        # placeholder) so a modest drop trips the percentage stop - but
        # kept small enough in RUPEE terms (qty=1) that it would NEVER
        # have tripped even the loosest MAX_LOSS_HIT cap ever used on its
        # own, isolating this as a TRAILING_SL_HIT/STOP_LOSS_HIT, not a
        # coincidental MAX_LOSS_HIT.
        pos = make_position(hard_stop_loss=1990.0)  # 10 rupees below entry (2000)
        ltp = 1985.0  # loss_rs = 15 - far below any MAX_LOSS_HIT cap, but below hard_stop_loss
        restore = _freeze_time(module, BEFORE_CUTOFF)
        try:
            reason = module._exit_reason_for(pos, ltp)
            assert reason in ("TRAILING_SL_HIT", "STOP_LOSS_HIT"), \
                f"{label}: the percentage stop-loss must still fire before the cutoff, got {reason}"
        finally:
            restore()
            module.config.ENABLE_MAX_LOSS_HIT_BEFORE_CUTOFF = real_enabled
    print("6. The MAX_LOSS_HIT-before-cutoff disable is scoped to that ONE exit - the percentage "
          "stop-loss still fires normally before the cutoff, for Options/Futures/Luxury: PASSED")


def main():
    print("=== Risk-threshold time-of-day cutoff test suite ===\n")
    test_1_lookup_functions_switch_at_the_cutoff()
    test_1b_luxury_max_loss_pinned_to_its_own_wider_cap()
    test_2_exact_boundary_instant_counts_as_after()
    test_3_exit_reason_for_uses_the_correct_cap_on_each_side()
    test_4_max_loss_hit_disabled_before_cutoff_regardless_of_loss_size()
    test_5_flag_on_restores_the_original_always_on_behavior()
    test_6_disable_is_scoped_to_max_loss_hit_only()
    print("\nALL RISK-THRESHOLD-CUTOFF CHECKS PASSED")


if __name__ == "__main__":
    main()
