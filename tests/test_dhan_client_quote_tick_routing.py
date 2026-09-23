"""
Tests for Options/dhan_client.py's _on_market_tick Quote/Full-packet
routing - specifically the exchange-segment-aware dispatch added 23 Sep
2026 alongside Swing/candle_feed.py's MCX support (subscribe_mcx_quote/
_mcx_security_id_to_symbol).

WHY THIS MATTERS: Dhan's security_id is NOT globally unique across
exchange segments, and a real collision between an NSE and an MCX
instrument has already happened in this exact codebase (trading-skills'
incidents/2026-09-17-copper-mcx-security-id-collision-and-adoption.md).
Before this change, only one quote-subscription dict existed
(_equity_security_id_to_symbol, NSE-only); adding a second one for MCX
without segment-aware routing would reintroduce that exact class of bug
the moment the SAME numeric security_id happened to be subscribed on
both segments at once - not a hypothetical, since Dhan's own IDs are
small-ish integers reused across segments.

Covers:
  1. An NSE equity Quote tick routes to the equity subscriber - unchanged
     behavior, proving the refactor didn't regress the existing
     Luxury/Futures/Options underlying_candle_feed.py path.
  2. An MCX Quote tick routes to the MCX subscriber - the new path.
  3. THE real regression test: the SAME numeric security_id subscribed on
     BOTH segments at once must route each tick to the CORRECT symbol for
     its own segment, never the other one's.
  4. A Ticker-mode packet (no "volume" key, options) never fires a quote
     subscriber at all - unchanged behavior.

HOW TO RUN:
    uv run python tests/test_dhan_client_quote_tick_routing.py
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")

from dhanhq import MarketFeed  # noqa: E402
import Options.dhan_client as odc  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
W = odc.dhan_wrapper


def _quote_tick(security_id: str, exchange_segment: int, ltp: float, volume: float) -> dict:
    return {"security_id": security_id, "exchange_segment": exchange_segment, "LTP": ltp, "volume": volume}


def test_1_nse_equity_quote_tick_routes_to_equity_subscriber():
    received = []
    saved_equity, saved_mcx, saved_subs = (
        dict(W._equity_security_id_to_symbol), dict(W._mcx_security_id_to_symbol), list(W._on_quote_tick_subscribers),
    )
    try:
        W._equity_security_id_to_symbol.clear()
        W._mcx_security_id_to_symbol.clear()
        W._on_quote_tick_subscribers.clear()
        W._equity_security_id_to_symbol["212"] = "ASHOKLEY"
        W.add_quote_tick_subscriber(lambda sym, ltp, vol, t: received.append((sym, ltp, vol)))

        W._on_market_tick(None, _quote_tick("212", MarketFeed.NSE, 150.5, 1000.0))
        assert received == [("ASHOKLEY", 150.5, 1000.0)]
        print("1. NSE equity Quote tick routes to the equity subscriber (unchanged behavior): PASSED")
    finally:
        W._equity_security_id_to_symbol.clear(); W._equity_security_id_to_symbol.update(saved_equity)
        W._mcx_security_id_to_symbol.clear(); W._mcx_security_id_to_symbol.update(saved_mcx)
        W._on_quote_tick_subscribers[:] = saved_subs


def test_2_mcx_quote_tick_routes_to_mcx_subscriber():
    received = []
    saved_equity, saved_mcx, saved_subs = (
        dict(W._equity_security_id_to_symbol), dict(W._mcx_security_id_to_symbol), list(W._on_quote_tick_subscribers),
    )
    try:
        W._equity_security_id_to_symbol.clear()
        W._mcx_security_id_to_symbol.clear()
        W._on_quote_tick_subscribers.clear()
        W._mcx_security_id_to_symbol["570750"] = "NATURALGAS"
        W.add_quote_tick_subscriber(lambda sym, ltp, vol, t: received.append((sym, ltp, vol)))

        W._on_market_tick(None, _quote_tick("570750", MarketFeed.MCX, 245.3, 50.0))
        assert received == [("NATURALGAS", 245.3, 50.0)]
        print("2. MCX Quote tick routes to the MCX subscriber (new path): PASSED")
    finally:
        W._equity_security_id_to_symbol.clear(); W._equity_security_id_to_symbol.update(saved_equity)
        W._mcx_security_id_to_symbol.clear(); W._mcx_security_id_to_symbol.update(saved_mcx)
        W._on_quote_tick_subscribers[:] = saved_subs


def test_3_colliding_security_id_across_segments_never_cross_contaminates():
    """THE regression test: the exact numeric security_id "999" subscribed
    as BOTH an NSE equity AND an MCX contract at once (a real, confirmed
    collision shape per the 17 Sep 2026 incident) - each tick must route
    to the symbol registered for ITS OWN segment, never the other one's."""
    received = []
    saved_equity, saved_mcx, saved_subs = (
        dict(W._equity_security_id_to_symbol), dict(W._mcx_security_id_to_symbol), list(W._on_quote_tick_subscribers),
    )
    try:
        W._equity_security_id_to_symbol.clear()
        W._mcx_security_id_to_symbol.clear()
        W._on_quote_tick_subscribers.clear()
        W._equity_security_id_to_symbol["999"] = "SOME_NSE_STOCK"
        W._mcx_security_id_to_symbol["999"] = "SOME_MCX_CONTRACT"
        W.add_quote_tick_subscriber(lambda sym, ltp, vol, t: received.append((sym, ltp, vol)))

        W._on_market_tick(None, _quote_tick("999", MarketFeed.NSE, 100.0, 10.0))
        W._on_market_tick(None, _quote_tick("999", MarketFeed.MCX, 200.0, 20.0))

        assert received == [("SOME_NSE_STOCK", 100.0, 10.0), ("SOME_MCX_CONTRACT", 200.0, 20.0)], (
            f"a colliding security_id across segments must never cross-contaminate the two symbols, got {received}"
        )
        print("3. A security_id colliding across NSE/MCX never cross-contaminates either symbol: PASSED")
    finally:
        W._equity_security_id_to_symbol.clear(); W._equity_security_id_to_symbol.update(saved_equity)
        W._mcx_security_id_to_symbol.clear(); W._mcx_security_id_to_symbol.update(saved_mcx)
        W._on_quote_tick_subscribers[:] = saved_subs


def test_4_ticker_mode_packet_never_fires_quote_subscriber():
    received = []
    saved_equity, saved_mcx, saved_subs = (
        dict(W._equity_security_id_to_symbol), dict(W._mcx_security_id_to_symbol), list(W._on_quote_tick_subscribers),
    )
    try:
        W._equity_security_id_to_symbol.clear()
        W._mcx_security_id_to_symbol.clear()
        W._on_quote_tick_subscribers.clear()
        W._equity_security_id_to_symbol["212"] = "ASHOKLEY"
        W.add_quote_tick_subscriber(lambda sym, ltp, vol, t: received.append((sym, ltp, vol)))

        # No "volume" key - a Ticker-mode option packet, must never dispatch to a quote subscriber.
        W._on_market_tick(None, {"security_id": "212", "exchange_segment": MarketFeed.NSE, "LTP": 99.0})
        assert received == [], "a Ticker-mode packet (no volume key) must never fire a quote subscriber"
        print("4. A Ticker-mode packet (no 'volume' key) never fires a quote subscriber: PASSED")
    finally:
        W._equity_security_id_to_symbol.clear(); W._equity_security_id_to_symbol.update(saved_equity)
        W._mcx_security_id_to_symbol.clear(); W._mcx_security_id_to_symbol.update(saved_mcx)
        W._on_quote_tick_subscribers[:] = saved_subs


def main():
    print("=== Options/dhan_client.py Quote-tick exchange-segment routing test suite ===\n")
    test_1_nse_equity_quote_tick_routes_to_equity_subscriber()
    test_2_mcx_quote_tick_routes_to_mcx_subscriber()
    test_3_colliding_security_id_across_segments_never_cross_contaminates()
    test_4_ticker_mode_packet_never_fires_quote_subscriber()
    print("\nALL DHAN_CLIENT QUOTE-TICK ROUTING TESTS PASSED")


if __name__ == "__main__":
    main()
