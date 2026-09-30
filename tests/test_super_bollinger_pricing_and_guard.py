"""
Tests for two 30 Sep 2026 after-hours fixes:
  - SuperBollinger/pricing.py - the order book as a second price source (no
    entry lost to a failed price call; an open loss near the hedge trigger is
    also judged by the bid/ask mid on thin options);
  - the same-contract guard between Swing and the Bollinger family
    (cross_strategy_registry.same_contract_key).
Fully offline.

HOW TO RUN:
    uv run python -m pytest tests/test_super_bollinger_pricing_and_guard.py -q
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import cross_strategy_registry as registry  # noqa: E402
import Bollinger.trading_engine as boll  # noqa: E402
import SuperBollinger.supervisor as sup  # noqa: E402
import SuperBollinger.trading_engine as te  # noqa: E402
import Swing.trading_engine as swing  # noqa: E402
from Bollinger.position_store import Position, position_store as bollinger_store  # noqa: E402
from Options.dhan_client import dhan_wrapper  # noqa: E402
from SuperBollinger import pricing, settings  # noqa: E402
from SuperBollinger.state import hedge_store, position_store  # noqa: E402
from Swing.position_store import position_store as swing_store  # noqa: E402


def _pos(sym="APL", ts="APL 2240 CE", entry=55.65, qty=350, order_id="X"):
    return Position(underlying_symbol=sym, trading_symbol=ts, resolved_option_type="CE", instrument_side="LONG",
                    exchange_segment="NSE_FNO", product_type="MARGIN", quantity=qty, lot_size=qty, entry_price=entry,
                    best_price=entry, stop_pct=0.0, hard_stop_loss=0.05, trailing_stop_dist=1e12, trailing_step=1e12,
                    pnl_multiplier=qty, order_id=order_id)


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b = {"ltp": None, "ltp_fails": False, "quote": None, "quote_calls": 0}

    async def ltp_async(ts, **_k):
        if b["ltp_fails"]:
            raise ValueError("No LTP returned")
        return b["ltp"]

    def get_quote(ts, attempts=3):
        b["quote_calls"] += 1
        if b["quote"] is None:
            raise ValueError("No quote returned")
        return dict(b["quote"])
    monkeypatch.setattr(dhan_wrapper, "get_option_ltp_async", ltp_async)
    monkeypatch.setattr(dhan_wrapper, "get_option_quote", get_quote)
    monkeypatch.setattr(settings, "_overrides_loaded", True)
    pricing._quotes.clear()
    for store in (position_store, hedge_store, bollinger_store, swing_store):
        store.live_positions.clear()
        store.reserved_symbols.clear()
    registry._claimed.clear()
    yield b
    pricing._quotes.clear()
    for store in (position_store, hedge_store, bollinger_store, swing_store):
        store.live_positions.clear()
        store.reserved_symbols.clear()
    registry._claimed.clear()


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
def test_1_entry_price_falls_back_to_the_order_book_when_the_price_call_fails(book):
    async def run():
        book["ltp"] = 28.1
        assert await pricing.price_for_entry("X CE") == (28.1, "ltp") and book["quote_calls"] == 0
        book["ltp_fails"], book["quote"] = True, {"ltp": 28.1, "bid": 28.0, "ask": 28.6}
        assert await pricing.price_for_entry("X CE") == (28.6, "quote_ask")           # the side we would buy at
        book["quote"] = {"ltp": 28.1, "bid": None, "ask": None}
        assert await pricing.price_for_entry("X CE") == (28.1, "quote_ltp")
        book["quote"] = None
        assert await pricing.price_for_entry("X CE") == (None, "none")
    asyncio.run(run())


def test_2_premium_gate_a_missing_price_is_not_a_low_premium(book, monkeypatch):
    async def run():
        events = []

        async def fake_event(event, symbol, detail):
            events.append((event, detail))

        async def leg(_symbol, _side, _profile):
            return {"trading_symbol": "PNB 1120 CE", "lot_size": 650, "product_type": "MARGIN", "security_id": "1"}
        monkeypatch.setattr(te, "_event", fake_event)
        monkeypatch.setattr(te.engine, "_resolve_option_leg", leg)
        monkeypatch.setitem(settings._overrides, "min_premium_rs", 5.0)
        monkeypatch.setitem(settings._overrides, "quantity_lots", 1)
        # the 30 Sep case: the price call returns nothing, but the book has a price -> the entry goes ahead
        book["ltp_fails"], book["quote"] = True, {"ltp": 39.0, "bid": 38.8, "ask": 39.4}
        out_leg, price = await te._resolve_leg("PNB")
        assert price == 39.4 and out_leg["quantity"] == 650 and events == []
        # nothing anywhere -> its own event, not "low premium"
        book["quote"] = None
        with pytest.raises(te.engine._SkipEntry) as skip:
            await te._resolve_leg("PNB")
        assert skip.value.result["reason"] == "no_price" and events[-1][0] == "ENTRY_SKIPPED_NO_PRICE"
        # a real low premium is still refused as before
        book["ltp_fails"], book["ltp"] = False, 3.2
        with pytest.raises(te.engine._SkipEntry) as skip:
            await te._resolve_leg("PNB")
        assert skip.value.result["reason"] == "premium_below_minimum" and events[-1][0] == "ENTRY_SKIPPED_LOW_PREMIUM"
    asyncio.run(run())


def test_3_mid_needs_both_sides_and_a_sane_spread_and_quotes_are_reused_briefly(book):
    async def run():
        assert pricing.mid_of({"bid": 49.0, "ask": 50.0}) == 49.5
        assert pricing.mid_of({"bid": 40.0, "ask": 50.0}) is None                      # 22% wide: says nothing
        assert pricing.mid_of({"bid": None, "ask": 50.0}) is None and pricing.mid_of(None) is None
        pos = _pos()
        book["quote"] = {"ltp": 51.0, "bid": 49.0, "ask": 50.0}
        assert await pricing.mark_for_loss(pos, 51.0) == (49.5, "mid")                 # stale last trade, lower mid
        assert await pricing.mark_for_loss(pos, 49.0) == (49.0, "ltp")                 # never marks a loss UP
        assert book["quote_calls"] == 1                                                # second read came from cache
    asyncio.run(run())


def test_4_live_price_uses_the_book_for_a_real_position_only(book, monkeypatch):
    async def run():
        async def failing(_pos):
            raise ValueError("No LTP returned")
        monkeypatch.setattr(pricing.engine, "_get_ltp", failing)
        book["quote"] = {"ltp": 51.0, "bid": 49.0, "ask": 50.0}
        assert await pricing.live_price(_pos()) == 49.5
        with pytest.raises(ValueError):
            await pricing.live_price(_pos(order_id="PAPER"))                           # paper keeps its cheap path
        book["quote"] = None
        pricing._quotes.clear()
        with pytest.raises(ValueError):
            await pricing.live_price(_pos())
    asyncio.run(run())


def test_5_hedge_trigger_is_seen_on_the_mid_when_the_last_trade_is_stale(book, monkeypatch):
    async def run():
        opened = []

        async def fake_open(symbol, ce, ce_ltp, loss, spot, atr_v, drop, mode):
            opened.append((symbol, round(ce_ltp, 2), round(loss), mode))

        async def fake_log(*_a, **_k):
            return None

        async def no_scale(*_a, **_k):
            return None
        monkeypatch.setattr(sup, "_open_hedge", fake_open)
        monkeypatch.setattr(sup, "_log", fake_log)
        monkeypatch.setattr(sup.scale, "on_ce_price", no_scale)
        monkeypatch.setattr(sup, "_spot", lambda s: 2192.0)
        monkeypatch.setattr(sup, "_atr", lambda s: 5.0)
        for k, v in (("hedge_mode", "real"), ("hedge_trigger_rs", 1800.0), ("hedge_atr_mult", 1.0),
                     ("hedge_cutoff_time", "15:15"), ("shadow_stop_reenter_rs", 0.0)):
            monkeypatch.setitem(settings._overrides, k, v)
        monkeypatch.setattr(sup, "_now", lambda: sup.engine._now_ist().replace(hour=14, minute=56))
        sup._track.clear()
        pos = _pos(entry=55.65, qty=350)
        sup._track[("APL", pos.opened_at.isoformat())] = {"entry_spot": 2206.1, "hedged": False, "waiting_logged": False,
                                                          "shadow_stopped": False, "shadow_reentered": False}
        # last trade 51.60 -> loss 1,417 (79% of the trigger); the book is at 49.8/50.2 -> mid 50.0 -> loss 1,977
        book["quote"] = {"ltp": 51.6, "bid": 49.8, "ask": 50.2}
        await sup.check_ce("APL", pos, 51.60, True)
        assert opened == [("APL", 50.0, 1978, "real")] or opened == [("APL", 50.0, 1977, "real")]
        # a paper CE, or a loss far from the trigger, never asks the book
        calls = book["quote_calls"]
        sup._track.clear()
        await sup.check_ce("APL", _pos(entry=55.65), 54.0, True)                        # loss 577 < 60% of 1,800
        assert book["quote_calls"] == calls and len(opened) == 1
    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Same-contract guard
# --------------------------------------------------------------------------- #
def _patch_swing(monkeypatch, basket="OPTIONS"):
    entered = []

    async def inner(symbol, regime):
        entered.append(symbol)
        return {"symbol": symbol, "status": "entered"}

    async def not_mcx(_symbol):
        return False

    async def no_event(*_a, **_k):
        return None
    monkeypatch.setattr(swing, "_enter_position_for_stock", inner)
    monkeypatch.setattr(swing.mcx_registry, "options_only", not_mcx)
    monkeypatch.setattr(swing, "_record_swing_event", no_event)
    monkeypatch.setattr(swing.config, "BASKET_TYPE", basket)
    return entered


def test_6_swing_refuses_an_underlying_the_bollinger_family_holds_for_real(book, monkeypatch):
    async def run():
        entered = _patch_swing(monkeypatch)
        position_store.live_positions["NIFTY"] = _pos("NIFTY", "NIFTY 22750 CE", 163.7, 65)
        out = await swing.enter_position_for_stock("NIFTY", "BULLISH")
        assert out["reason"] == "held_by_SuperBollinger" and entered == []
        position_store.live_positions.clear()
        hedge_store.reserved_symbols.add("NIFTY")                                      # a hedge being opened counts too
        assert (await swing.enter_position_for_stock("NIFTY", "BEARISH"))["reason"] == "held_by_SuperBollingerHedge"
        hedge_store.reserved_symbols.clear()
        bollinger_store.live_positions["NIFTY"] = _pos("NIFTY", "NIFTY 22750 CE", 163.7, 65)
        assert (await swing.enter_position_for_stock("NIFTY", "BULLISH"))["reason"] == "held_by_Bollinger"
        bollinger_store.live_positions.clear()
        assert (await swing.enter_position_for_stock("NIFTY", "BULLISH"))["status"] == "entered" and entered == ["NIFTY"]
        assert registry.snapshot() == {}                                               # claim released every time
    asyncio.run(run())


def test_7_swing_futures_baskets_are_not_blocked_and_swing_stays_independent_of_options(book, monkeypatch):
    async def run():
        entered = _patch_swing(monkeypatch, basket="FUTURES")
        position_store.live_positions["SAIL"] = _pos("SAIL", "SAIL 185 CE", 5.0, 4700)
        assert (await swing.enter_position_for_stock("SAIL", "BULLISH"))["status"] == "entered" and entered == ["SAIL"]
        # the same-contract key is its own namespace: a Swing claim never blocks Options/Luxury on the plain key
        assert await registry.try_claim(registry.same_contract_key("TCS"), "Swing")
        assert await registry.try_claim("TCS", "Options")
        assert not await registry.try_claim(registry.same_contract_key("TCS"), "SuperBollinger")
    asyncio.run(run())


def test_8_super_bollinger_and_bollinger_refuse_an_underlying_swing_holds_in_options(book, monkeypatch):
    async def run():
        events = []

        async def fake_event(event, symbol, detail):
            events.append(event)

        async def should_not_run(*_a, **_k):
            raise AssertionError("the entry must not be attempted")

        async def boll_event(*_a, **_k):
            return None
        monkeypatch.setattr(te, "_event", fake_event)
        monkeypatch.setattr(te, "_enter_real_reserved", should_not_run)
        monkeypatch.setattr(boll, "_enter_position_for_stock", should_not_run)
        monkeypatch.setattr(boll, "_record_bollinger_event", boll_event)
        swing_store.live_positions["NIFTY"] = SimpleNamespace(basket_type="OPTIONS")
        assert (await te.enter_real("NIFTY", 22736.65, 22686.7))["reason"] == "held_by_swing"
        assert events == ["ENTRY_SKIPPED_HELD_BY_SWING"]
        assert (await boll.enter_position_for_stock("NIFTY", "BULLISH", 1.0, 1.0, 1.0))["reason"] == "held_by_swing"
        assert registry.snapshot() == {}
        # a Swing FUTURES position shares no option contract -> not a reason to refuse
        swing_store.live_positions["NIFTY"] = SimpleNamespace(basket_type="FUTURES")
        assert boll.swing_real_holds("NIFTY") is False
        swing_store.live_positions.clear()
        swing_store.reserved_symbols.add("NIFTY")                                      # Swing mid-entry: unknown, refuse
        assert boll.swing_real_holds("NIFTY") is True
    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
