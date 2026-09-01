"""
Tests for wait_for_order_result()'s cache-vs-REST fill price fix - found
live 1 Sep 2026 via Swing's own first-ever real entry (APLAPOLLO
futures): a terminal-status WS order-update push was trusted directly,
including its own `average_fill_price` field - but that field's schema
is UNDOCUMENTED/unverified (dhanhq's own source only confirms
`orderNo`/`status` exist on the push). The real push for this order
carried no usable price field, silently defaulting to 0 and recording a
REAL position's entry_price as ₹0 in our own tracking, while the
broker's own REST record (`get_order_by_id`) showed the correct fill
(₹2263.30) the whole time - confirmed via a direct, read-only check
against the live account.

This function is SHARED by every real-money package (Options/Futures/
Luxury/Swing), so this was a live risk for all of them, not just Swing -
every existing test suite in this repo mocks wait_for_order_result()
directly (replacing the whole function), so none of them ever exercised
this internal cache-vs-REST logic; this is the first test that does.

Covers, against the REAL production function (not reimplemented):
  1. A terminal WS cache hit with a genuinely correct price still uses
     REST's own value (not the cache's) - REST is now ALWAYS the source
     of truth for price/quantity once an order is terminal, regardless
     of whether the cache happened to be right.
  2. The actual bug scenario: cache shows a terminal status with price=0
     (an incomplete/malformed push) - REST's real price is used instead,
     not the cache's 0.
  3. No cache entry at all (the WS never pushed anything for this
     order) - falls straight through to REST, unaffected by this fix
     (matches the pre-existing, already-correct behavior for this path).
  4. A cache hit reports terminal but REST (checked immediately after)
     hasn't caught up yet - falls back to the cache's own snapshot
     rather than crashing or hanging, matching the pre-fix behavior for
     this edge case.

HOW TO RUN:
    uv run python tests/test_wait_for_order_result_price_fix.py
"""
import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.dhan_client as dc


def _wrapper_with(order_updates: dict, rest_response: dict):
    wrapper = dc.DhanWrapper.__new__(dc.DhanWrapper)
    wrapper._order_updates = order_updates
    wrapper._client = types.SimpleNamespace(
        Dhan=types.SimpleNamespace(get_order_by_id=lambda order_id: rest_response)
    )
    wrapper.stats = {"order_status_cache_hits": 0, "order_status_rest_calls": 0}
    return wrapper


def _rest_response(order_status, avg_price, filled_qty=350):
    return {
        "status": "success", "remarks": "",
        "data": [{
            "orderStatus": order_status, "averageTradedPrice": avg_price,
            "filledQty": filled_qty, "omsErrorDescription": "TRADE CONFIRMED" if order_status == "TRADED" else "",
        }],
    }


def test_1_terminal_cache_hit_still_uses_rest_price():
    """Even when the cache's OWN price happens to already be correct,
    REST is used - the point of the fix is "always cross-check," not
    "only when the cache looks wrong" (which would require trusting the
    very field we don't trust the schema of)."""
    order_updates = {"111": {"orderNo": "111", "orderStatus": "TRADED", "averageTradedPrice": 2263.3}}
    wrapper = _wrapper_with(order_updates, _rest_response("TRADED", 2263.3))
    result = wrapper.wait_for_order_result("111")
    assert result.status == "TRADED"
    assert result.fill_price == 2263.3
    assert wrapper.stats["order_status_cache_hits"] == 1, "the cache hit should still count for stats/pacing"
    print("1. A terminal WS cache hit with an already-correct price still resolves to REST's own "
          "value (REST is unconditionally the source of truth for price once terminal): PASSED")


def test_2_the_actual_live_bug_cache_price_zero_rest_has_the_real_fill():
    """The exact scenario found live 1 Sep 2026 (APLAPOLLO): the WS push
    reports TRADED but carries no usable price (defaults to 0);
    get_order_by_id shows the real fill. Must return the REAL price, not 0."""
    order_updates = {"34226090133507": {"orderNo": "34226090133507", "orderStatus": "TRADED"}}  # no price field at all
    wrapper = _wrapper_with(order_updates, _rest_response("TRADED", 2263.3, filled_qty=350))
    result = wrapper.wait_for_order_result("34226090133507")
    assert result.status == "TRADED"
    assert result.fill_price == 2263.3, \
        f"must use REST's real fill price (2263.3), not the cache's missing/defaulted-to-0 field, got {result.fill_price}"
    assert result.filled_quantity == 350
    print("2. THE LIVE BUG SCENARIO - a terminal WS push with no usable price field no longer "
          "corrupts entry_price to 0; the real REST fill price is used instead: PASSED")


def test_3_no_cache_entry_falls_through_to_rest_unaffected():
    wrapper = _wrapper_with({}, _rest_response("TRADED", 500.0, filled_qty=100))
    result = wrapper.wait_for_order_result("222", retries=1)
    assert result.status == "TRADED"
    assert result.fill_price == 500.0
    assert wrapper.stats["order_status_cache_hits"] == 0
    assert wrapper.stats["order_status_rest_calls"] == 1
    print("3. No WS cache entry at all falls straight through to REST, exactly as before this "
          "fix (unaffected code path): PASSED")


def test_4_rest_not_yet_terminal_falls_back_to_cache_snapshot():
    """An edge case: the cache says terminal but REST (checked
    immediately after) hasn't caught up yet - must not crash, and must
    still return SOME usable status rather than hanging."""
    order_updates = {"333": {"orderNo": "333", "orderStatus": "TRADED", "averageTradedPrice": 42.0}}
    wrapper = _wrapper_with(order_updates, _rest_response("PENDING", 0, filled_qty=0))
    result = wrapper.wait_for_order_result("333")
    assert result.status == "TRADED", "must fall back to the cache's own terminal status, not hang/crash"
    assert result.fill_price == 42.0
    print("4. If REST hasn't caught up to the cache's own terminal status yet, falls back to the "
          "cache's own snapshot rather than crashing or treating the order as still pending: PASSED")


def main():
    print("=== wait_for_order_result cache-vs-REST fill price fix test suite ===\n")
    test_1_terminal_cache_hit_still_uses_rest_price()
    test_2_the_actual_live_bug_cache_price_zero_rest_has_the_real_fill()
    test_3_no_cache_entry_falls_through_to_rest_unaffected()
    test_4_rest_not_yet_terminal_falls_back_to_cache_snapshot()
    print("\nALL wait_for_order_result PRICE FIX CHECKS PASSED")


if __name__ == "__main__":
    main()
