"""
Tests for Swing/position_store.py's pure, direction-aware helper
functions - the LONG vs SHORT math every entry/exit decision in
Swing/trading_engine.py routes through. No mocking needed at all: every
function under test is a plain synchronous function with no I/O.

Highest-value target: broker_stop_trigger_and_limit's SHORT-side sign.
A SHORT position's protective order is a BUY, and a BUY stop-loss must
sit ABOVE the current price with its limit ABOVE the trigger - inverted
from a LONG's SELL stop, which sits BELOW price with its limit BELOW
the trigger. Getting this backwards produces a stop that can never fire.

HOW TO RUN:
    uv run python tests/test_swing_v2_position_model.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from Swing.position_store import (
    broker_stop_trigger_and_limit, entry_transaction_type, exit_transaction_type,
    giveback_floor, hard_stop_for, is_more_favorable, price_past_giveback_floor,
    price_past_hard_stop, price_past_target, resolve_instrument_side,
    resolved_option_type_for, target_price_for, unrealized_pnl_rs,
)


def test_1_resolve_instrument_side_lookup_table():
    assert resolve_instrument_side("futures", "bullish") == "LONG"
    assert resolve_instrument_side("futures", "bearish") == "SHORT"
    assert resolve_instrument_side("options", "bullish") == "LONG"
    assert resolve_instrument_side("options", "bearish") == "LONG", \
        "a bearish OPTIONS entry buys a PE - itself always a LONG position"
    assert resolve_instrument_side("equity", "bullish") == "LONG"
    assert resolve_instrument_side("equity", "bearish") is None, \
        "equity is long-only - a bearish regime must skip entry entirely, not resolve to SHORT"
    print("1. resolve_instrument_side's lookup table matches basket_type x regime -> side/None exactly: PASSED")


def test_2_resolved_option_type_for():
    assert resolved_option_type_for("options", "bullish") == "CE"
    assert resolved_option_type_for("options", "bearish") == "PE"
    assert resolved_option_type_for("futures", "bullish") is None
    assert resolved_option_type_for("equity", "bearish") is None
    print("2. resolved_option_type_for returns CE/PE only for the OPTIONS basket-type: PASSED")


def test_3_transaction_types():
    assert entry_transaction_type("LONG") == "BUY"
    assert entry_transaction_type("SHORT") == "SELL"
    assert exit_transaction_type("LONG") == "SELL"
    assert exit_transaction_type("SHORT") == "BUY", \
        "a SHORT position's exit (and its resting broker-side stop) is a BUY, not a SELL"
    print("3. entry/exit_transaction_type are correct and mirror each other for both sides: PASSED")


def test_4_unrealized_pnl_and_favorability():
    # LONG: profit when price rises above entry.
    assert unrealized_pnl_rs("LONG", entry_price=100.0, ltp=110.0, quantity=10) == 100.0
    assert unrealized_pnl_rs("LONG", entry_price=100.0, ltp=90.0, quantity=10) == -100.0
    # SHORT: profit when price FALLS below entry - the mirror image.
    assert unrealized_pnl_rs("SHORT", entry_price=100.0, ltp=90.0, quantity=10) == 100.0, \
        "a SHORT position profits when price falls, not rises"
    assert unrealized_pnl_rs("SHORT", entry_price=100.0, ltp=110.0, quantity=10) == -100.0

    # best_price tracks the MOST FAVORABLE price seen: highest for LONG, lowest for SHORT.
    assert is_more_favorable("LONG", candidate_price=105.0, current_best=100.0) is True
    assert is_more_favorable("LONG", candidate_price=95.0, current_best=100.0) is False
    assert is_more_favorable("SHORT", candidate_price=95.0, current_best=100.0) is True, \
        "for a SHORT, a LOWER price is more favorable (closer to profit), not a higher one"
    assert is_more_favorable("SHORT", candidate_price=105.0, current_best=100.0) is False
    print("4. unrealized_pnl_rs and is_more_favorable are correctly mirrored for LONG vs SHORT: PASSED")


def test_5_target_and_hard_stop_prices():
    # LONG: target above entry, hard stop below.
    assert target_price_for("LONG", 100.0, 0.20) == 120.0
    assert hard_stop_for("LONG", 100.0, 0.20) == 80.0
    assert price_past_target("LONG", ltp=121.0, target_price=120.0) is True
    assert price_past_hard_stop("LONG", ltp=79.0, hard_stop=80.0) is True

    # SHORT: target BELOW entry (price falling further is the win), hard stop ABOVE.
    assert target_price_for("SHORT", 100.0, 0.20) == 80.0, \
        "a SHORT's target is BELOW entry - price needs to fall, not rise, to hit target"
    assert hard_stop_for("SHORT", 100.0, 0.20) == 120.0, \
        "a SHORT's hard stop is ABOVE entry - price rising against the position triggers it"
    assert price_past_target("SHORT", ltp=79.0, target_price=80.0) is True
    assert price_past_target("SHORT", ltp=81.0, target_price=80.0) is False
    assert price_past_hard_stop("SHORT", ltp=121.0, hard_stop=120.0) is True
    assert price_past_hard_stop("SHORT", ltp=119.0, hard_stop=120.0) is False
    print("5. target_price_for/hard_stop_for and their price-past checks are correctly mirrored: PASSED")


def test_6_giveback_floor():
    # LONG: floor is BELOW best_price (a retrace DOWN from the peak arms the exit).
    floor = giveback_floor("LONG", best_price=120.0, giveback_pct=0.0)
    assert floor == 120.0
    assert price_past_giveback_floor("LONG", ltp=119.99, floor=floor) is True
    assert price_past_giveback_floor("LONG", ltp=120.0, floor=floor) is False

    # SHORT: floor is ABOVE best_price (a retrace UP from the trough arms the exit) - the mirror.
    floor_short = giveback_floor("SHORT", best_price=80.0, giveback_pct=0.0)
    assert floor_short == 80.0
    assert price_past_giveback_floor("SHORT", ltp=80.01, floor=floor_short) is True, \
        "for a SHORT, price RISING past the floor (not falling) is what arms profit-protection"
    assert price_past_giveback_floor("SHORT", ltp=80.0, floor=floor_short) is False

    # A nonzero giveback tolerates a small retrace before arming, for both sides.
    floor_tol = giveback_floor("LONG", best_price=100.0, giveback_pct=0.05)
    assert floor_tol == 95.0
    floor_tol_short = giveback_floor("SHORT", best_price=100.0, giveback_pct=0.05)
    assert floor_tol_short == 105.0
    print("6. giveback_floor and its price-past check are correctly mirrored for LONG vs SHORT: PASSED")


def test_7_broker_stop_trigger_and_limit_long():
    """LONG: trigger BELOW entry (loss-side), limit further BELOW trigger -
    matches Options/trading_engine.py's own formula exactly."""
    entry, qty, cap, gap_mult = 100.0, 50, 1000.0, 0.05
    trigger, limit = broker_stop_trigger_and_limit("LONG", entry, qty, cap, gap_mult)
    assert trigger == entry - (cap / qty) == 80.0
    assert limit == trigger - (cap * gap_mult / qty) == 79.0
    assert limit < trigger < entry, "for a LONG: limit < trigger < entry"
    print("7. broker_stop_trigger_and_limit (LONG): trigger/limit sit below entry, limit below trigger: PASSED")


def test_8_broker_stop_trigger_and_limit_short():
    """SHORT: the mirror image - trigger ABOVE entry, limit further ABOVE
    trigger. This is the single easiest place to get a sign wrong (a
    stop that can never fire, or fires on the wrong side) - the whole
    reason this function exists as one tested place rather than an
    inline expression at the call site."""
    entry, qty, cap, gap_mult = 100.0, 50, 1000.0, 0.05
    trigger, limit = broker_stop_trigger_and_limit("SHORT", entry, qty, cap, gap_mult)
    assert trigger == entry + (cap / qty) == 120.0, "a SHORT's trigger must sit ABOVE entry"
    assert limit == trigger + (cap * gap_mult / qty) == 121.0, "a SHORT's limit must sit ABOVE its trigger"
    assert entry < trigger < limit, "for a SHORT: entry < trigger < limit - fully inverted from LONG"
    print("8. broker_stop_trigger_and_limit (SHORT): trigger/limit sit above entry, limit above trigger "
          "(the exact mirror of the LONG case): PASSED")


def test_9_broker_stop_gap_is_independent_of_quantity():
    """The RUPEE gap between trigger and limit is cap*gap_multiple
    regardless of quantity - the property this whole design (adopted for
    Options/Futures/Luxury this session) depends on to keep the SL-L's
    worst-case extra loss predictable across wildly different position
    sizes."""
    for qty in (10, 500, 6950):
        trigger, limit = broker_stop_trigger_and_limit("LONG", 50.0, qty, 4500.0, 0.05)
        gap_rs = (trigger - limit) * qty
        assert abs(gap_rs - 4500.0 * 0.05) < 1e-9, f"gap_rs should be Rs 225 regardless of qty={qty}, got {gap_rs}"
    print("9. broker_stop_trigger_and_limit's rupee gap is independent of quantity, as designed: PASSED")


def main():
    print("=== Swing v2 position-model (direction-aware math) test suite ===\n")
    test_1_resolve_instrument_side_lookup_table()
    test_2_resolved_option_type_for()
    test_3_transaction_types()
    test_4_unrealized_pnl_and_favorability()
    test_5_target_and_hard_stop_prices()
    test_6_giveback_floor()
    test_7_broker_stop_trigger_and_limit_long()
    test_8_broker_stop_trigger_and_limit_short()
    test_9_broker_stop_gap_is_independent_of_quantity()
    print("\nALL SWING V2 POSITION-MODEL CHECKS PASSED")


if __name__ == "__main__":
    main()
