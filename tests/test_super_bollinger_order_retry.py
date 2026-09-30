"""
Tests for the unfilled-order handling added after the 30 Sep 2026 SONACOMS
incident (a PENDING market BUY was abandoned and left resting at Dhan):
  - settle_unfilled_order: cancel + re-read the final status;
  - retry_unfilled_buy: re-price at the live ask and retry while the setup
    holds, never above the chase cap;
  - the real entry path end to end (market order pending -> retry fills);
  - the supervisor's orphan sweep.
Fully offline - Dhan is a scripted fake; files go to a temp directory.

HOW TO RUN:
    uv run python -m pytest tests/test_super_bollinger_order_retry.py -q
"""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Bollinger.position_store as bps  # noqa: E402
import SuperBollinger.supervisor as sup  # noqa: E402
import SuperBollinger.trading_engine as te  # noqa: E402
from Bollinger import trading_engine as engine  # noqa: E402
from Options.dhan_client import OrderResult, OrderStatus, dhan_wrapper  # noqa: E402
from SuperBollinger import live_state as ls, settings  # noqa: E402
from SuperBollinger.state import STRATEGY, hedge_store, position_store  # noqa: E402

LEG = {"trading_symbol": "SONA 830 CE", "product_type": "MARGIN", "lot_size": 1225, "quantity": 1225,
       "security_id": "128040", "pnl_multiplier": 1225}


def _res(oid, status, price=0.0, qty=0):
    return OrderResult(order_id=oid, status=status, remark="x", fill_price=price, filled_quantity=qty, is_amo=False)


class FakeDhan:
    """Orders fill only if their limit price reaches `ask` at the time they are placed."""
    def __init__(self):
        self.ask, self.bid, self.ltp = 28.6, 28.0, 28.1
        self.orders, self.limits, self.cancelled, self.sl = {}, [], [], []
        self.market_fills = False
        self.n = 0

    def _new(self, status, price=0.0, qty=0):
        self.n += 1
        oid = f"ORD{self.n}"
        self.orders[oid] = (status, price, qty)
        return oid

    def place_market_order(self, ts, qty, side, tag=None, product=None):
        oid = self._new(OrderStatus.TRADED if self.market_fills else OrderStatus.PENDING, self.ask if self.market_fills else 0.0, qty)
        return {"order_id": oid, "is_amo": False}

    def place_limit_order(self, ts, qty, side, price, tag=None, product=None):
        filled = self.ask is not None and price >= self.ask
        oid = self._new(OrderStatus.TRADED if filled else OrderStatus.PENDING, price if filled else 0.0, qty)
        self.limits.append((oid, price))
        return {"order_id": oid, "is_amo": False, "price": price}

    def status(self, oid, is_amo=False, retries=6, delay=1.0):
        return _res(oid, *self.orders[oid])

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        st, px, q = self.orders[oid]
        if st in OrderStatus.OPEN_STATUSES:
            self.orders[oid] = (OrderStatus.CANCELLED, px, q)

    def quote(self, ts, attempts=3):
        return {"ltp": self.ltp, "bid": self.bid, "ask": self.ask, "bid_qty": 100, "ask_qty": 100}

    def place_sl(self, ts, qty, side, trig, limit, tag, product):
        self.sl.append((ts, qty, trig))
        return {"order_id": f"SL{len(self.sl)}"}


@pytest.fixture
def dhan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = FakeDhan()
    for name, fn in (("place_market_order", d.place_market_order), ("place_limit_order", d.place_limit_order),
                     ("wait_for_order_result", d.status), ("refresh_order_status", d.status),
                     ("cancel_order", d.cancel_order), ("get_option_quote", d.quote),
                     ("place_stop_loss_limit_order", d.place_sl), ("subscribe_option_price", lambda ts: None),
                     ("unsubscribe_option_price", lambda ts: None), ("get_pending_order_id", lambda ts, side, exch=None: None)):
        monkeypatch.setattr(dhan_wrapper, name, fn)
    events = []

    async def fake_event(event, symbol, detail):
        events.append((event, symbol, detail))
    monkeypatch.setattr(te, "_event", fake_event)

    async def _no_filters(*_a, **_k):
        return None
    monkeypatch.setattr(bps.reversal_filters, "evaluate_and_log", _no_filters)
    for k, v in (("entry_retry_max", 2), ("entry_retry_wait_seconds", 1.0), ("entry_chase_max_pct", 5.0),
                 ("entry_retry_limit_buffer_pct", 1.0), ("funds_check_enabled", False)):
        monkeypatch.setitem(settings._overrides, k, v)
    monkeypatch.setattr(settings, "_overrides_loaded", True)
    d.events = events
    for store in (position_store, hedge_store):
        store.live_positions.clear(); store.reserved_symbols.clear(); store.orders_today.clear()
    ls._intents.clear()
    yield d
    for store in (position_store, hedge_store):
        store.live_positions.clear(); store.reserved_symbols.clear(); store.orders_today.clear()
    ls._intents.clear()


async def _ok():
    return None


def test_1_settle_leaves_a_terminal_order_alone_and_cancels_an_open_one(dhan):
    async def run():
        oid = dhan._new(OrderStatus.REJECTED)
        r = await te.settle_unfilled_order("S", "S CE", oid, _res(oid, OrderStatus.REJECTED), False, "entry")
        assert r.status == OrderStatus.REJECTED and dhan.cancelled == []
        oid = dhan._new(OrderStatus.PENDING)
        r = await te.settle_unfilled_order("S", "S CE", oid, _res(oid, OrderStatus.PENDING), False, "entry")
        assert r.status == OrderStatus.CANCELLED and dhan.cancelled == [oid]
        assert dhan.events[-1][0] == "ORDER_UNFILLED_CANCELLED"
    asyncio.run(run())


def test_2_settle_adopts_a_fill_that_raced_the_cancel_and_flags_a_stuck_order(dhan, monkeypatch):
    async def run():
        oid = dhan._new(OrderStatus.TRADED, 28.4, 1225)                     # filled before the cancel arrived
        r = await te.settle_unfilled_order("S", "S CE", oid, _res(oid, OrderStatus.PENDING), False, "entry")
        assert r.status == OrderStatus.TRADED and r.fill_price == 28.4
        assert dhan.events[-1][0] == "ORDER_FILLED_DURING_CANCEL"

        def boom(_oid):
            raise RuntimeError("cancel rejected")
        monkeypatch.setattr(dhan_wrapper, "cancel_order", boom)
        oid = dhan._new(OrderStatus.PENDING)
        r = await te.settle_unfilled_order("S", "S CE", oid, _res(oid, OrderStatus.TRANSIT), False, "hedge")
        assert r.status == OrderStatus.PENDING and dhan.events[-1][0] == "ORDER_STILL_RESTING_AT_BROKER"
    asyncio.run(run())


def test_3_retry_buys_at_the_live_ask_and_leaves_its_intent_open_for_the_caller(dhan):
    async def run():
        result, oid, intent, reason = await te.retry_unfilled_buy("SONA", LEG, 1225, 28.1, _ok, "entry", position_store, "SBol")
        assert reason == "filled" and result.status == OrderStatus.TRADED and result.fill_price == 28.6
        assert dhan.limits == [(oid, 28.6)]                                  # priced at the ask, one attempt
        assert intent in ls._intents and ls._intents[intent]["order_id"] == oid   # still on file until recorded
        assert [e[0] for e in dhan.events] == ["ORDER_RETRY"]
        await ls.intent_finish(intent, True)
        assert not ls._intents
    asyncio.run(run())


def test_4_retry_never_pays_more_than_the_chase_cap(dhan):
    async def run():
        dhan.ask = 29.6                                                      # 5.3% above the 28.10 reference
        result, oid, intent, reason = await te.retry_unfilled_buy("SONA", LEG, 1225, 28.1, _ok, "entry", position_store, "SBol")
        assert (result, oid, intent, reason) == (None, None, None, "price_ran_away")
        assert dhan.limits == [] and [e[0] for e in dhan.events] == ["ORDER_RETRY_SKIPPED", "ENTRY_ABANDONED"]
        assert not ls._intents
    asyncio.run(run())


def test_5_retry_stops_when_the_setup_no_longer_holds(dhan):
    async def run():
        async def gone():
            return "momentum_gone"
        result, _oid, _intent, reason = await te.retry_unfilled_buy("SONA", LEG, 1225, 28.1, gone, "entry", position_store, "SBol")
        assert result is None and reason == "momentum_gone" and dhan.limits == []
        assert dhan.events[-1][0] == "ENTRY_ABANDONED" and dhan.events[-1][2]["reason"] == "momentum_gone"
    asyncio.run(run())


def test_6_unfilled_retries_are_cancelled_and_then_abandoned(dhan, monkeypatch):
    async def run():
        real_limit = dhan.place_limit_order

        def never_fills(ts, qty, side, price, tag=None, product=None):
            out = real_limit(ts, qty, side, price, tag, product)
            dhan.orders[out["order_id"]] = (OrderStatus.PENDING, 0.0, 0)     # the ask moved away each time
            return out
        monkeypatch.setattr(dhan_wrapper, "place_limit_order", never_fills)
        result, _oid, _intent, reason = await te.retry_unfilled_buy("SONA", LEG, 1225, 28.1, _ok, "hedge", hedge_store, "SBH")
        assert result is None and reason == "retries_exhausted"
        assert len(dhan.limits) == 2 and dhan.cancelled == [o for o, _p in dhan.limits]   # both retries cancelled
        assert dhan.events[-1][0] == "HEDGE_ABANDONED" and not ls._intents
    asyncio.run(run())


def test_7_no_ask_in_the_book_uses_last_price_plus_the_buffer_and_zero_retries_means_off(dhan, monkeypatch):
    async def run():
        dhan.ask = None
        await te.retry_unfilled_buy("SONA", LEG, 1225, 28.1, _ok, "entry", position_store, "SBol")
        assert round(dhan.limits[0][1], 3) == round(28.1 * 1.01, 3)
        monkeypatch.setitem(settings._overrides, "entry_retry_max", 0)
        n = len(dhan.limits)
        result, _o, _i, reason = await te.retry_unfilled_buy("SONA", LEG, 1225, 28.1, _ok, "entry", position_store, "SBol")
        assert result is None and reason == "retries_off" and len(dhan.limits) == n
    asyncio.run(run())


def _patch_entry(monkeypatch, spot):
    async def leg(_symbol):
        return dict(LEG), 28.1
    monkeypatch.setattr(te, "_resolve_leg", leg)
    monkeypatch.setattr(te, "_entries_open_now", lambda: True)
    monkeypatch.setattr(te, "_square_off_now", lambda: False)
    monkeypatch.setattr(te.candle_feed, "is_fresh", lambda *_a: True)
    monkeypatch.setattr(te.candle_feed, "forming_bar", lambda _s: {"last": spot})


def test_8_real_entry_market_order_pending_then_the_retry_fills_and_the_position_is_managed(dhan, monkeypatch):
    async def run():
        _patch_entry(monkeypatch, spot=826.4)                                # still above the 825.95 trigger
        out = await te._enter_real_reserved("SONA", 825.95, 824.6, "tick")
        assert out["status"] == "entered" and out["entry_price"] == 28.6
        pos = position_store.live_positions["SONA"]
        assert pos.quantity == 1225 and pos.stop_loss_order_id == "SL1" and pos.order_id == dhan.limits[0][0]
        assert dhan.cancelled == ["ORD1"]                                    # the pending market order was cancelled
        assert not ls._intents                                               # nothing left in flight
        assert [e[0] for e in dhan.events] == ["ORDER_UNFILLED_CANCELLED", "ORDER_RETRY", "POSITION_OPENED"]
    asyncio.run(run())


def test_9_real_entry_is_abandoned_cleanly_when_the_breakout_has_failed(dhan, monkeypatch):
    async def run():
        _patch_entry(monkeypatch, spot=824.0)                                # back below the trigger
        out = await te._enter_real_reserved("SONA", 825.95, 824.6, "tick")
        assert out["status"] == "failed" and "SONA" not in position_store.live_positions
        assert dhan.cancelled == ["ORD1"] and dhan.limits == [] and dhan.sl == [] and not ls._intents
        assert dhan.events[-1][0] == "ENTRY_ABANDONED"
    asyncio.run(run())


def test_10_orphan_sweep_cancels_only_this_strategys_stale_unowned_buy_orders(dhan, monkeypatch):
    async def run():
        old = (engine._now_ist() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        young = engine._now_ist().strftime("%Y-%m-%d %H:%M:%S")
        a = dhan._new(OrderStatus.PENDING); b = dhan._new(OrderStatus.PENDING); c = dhan._new(OrderStatus.PENDING)
        e = dhan._new(OrderStatus.PENDING); f = dhan._new(OrderStatus.PENDING)

        def row(oid, tag, created):
            return {"order_id": oid, "trading_symbol": "SONACOMS-Oct2026-830-CE", "security_id": "128040", "tag": tag,
                    "transaction_type": "BUY", "status": "PENDING", "quantity": 1225, "order_type": "LIMIT",
                    "price": 28.05, "created": created}
        book = [row(a, "SBol-SONACO-590814", old),        # the SONACOMS case -> cancel
                row(b, "Bol-SONACO-111111", old),         # another strategy's order -> leave
                row(c, "SBol-TITAN-222222", young),       # seconds old -> leave this time
                row(e, "SBH-GLENMA-333333", old),         # a hedge order in flight (has an intent) -> leave
                row(f, "SBH-APLAPO-444444", old)]         # stale hedge order -> cancel
        monkeypatch.setattr(dhan_wrapper, "list_open_orders", lambda side=None: book)
        monkeypatch.setattr(dhan_wrapper, "get_open_fno_positions", lambda: [])
        logged = []

        async def fake_log(event, symbol, **detail):
            logged.append((event, detail.get("order_id")))
        monkeypatch.setattr(sup, "_log", fake_log)
        iid = ls.intent_begin("hedge", "GLEN", {"trading_symbol": "GLEN 2380 PE", "product_type": "MARGIN", "lot_size": 375}, 375)
        ls.intent_order(iid, e)
        out = await sup.sweep_orphans()
        assert sorted(out["cancelled"]) == sorted([a, f]) and sorted(dhan.cancelled) == sorted([a, f])
        assert sorted(logged) == sorted([("ORPHAN_ORDER_CANCELLED", a), ("ORPHAN_ORDER_CANCELLED", f)])
    asyncio.run(run())


def test_11_orphan_sweep_adopts_a_fill_and_flags_an_untracked_position(dhan, monkeypatch):
    async def run():
        old = (engine._now_ist() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        oid = dhan._new(OrderStatus.TRADED, 28.4, 1225)                      # it filled just before the sweep's cancel
        book = [{"order_id": oid, "trading_symbol": "SONACOMS-Oct2026-830-CE", "security_id": "128040",
                 "tag": "SBol-SONACO-590814", "transaction_type": "BUY", "status": "PENDING", "quantity": 1225,
                 "order_type": "LIMIT", "price": 28.4, "created": old}]
        positions = [{"underlying_symbol": "SONA", "trading_symbol": "SONA 830 CE", "option_type": "CE", "quantity": 1225,
                      "avg_price": 28.4, "lot_size": 1225, "product_type": "MARGIN"},
                     {"underlying_symbol": "TITAN", "trading_symbol": "TITAN 4600 CE", "option_type": "CE", "quantity": 175,
                      "avg_price": 90.0, "lot_size": 175, "product_type": "MARGIN"}]
        monkeypatch.setattr(dhan_wrapper, "list_open_orders", lambda side=None: book)
        monkeypatch.setattr(dhan_wrapper, "get_open_fno_positions", lambda: positions)
        monkeypatch.setattr(dhan_wrapper, "_instrument_meta",
                            lambda ts, expected_exchange=None: {"security_id": "128040" if ts.startswith("SONA") else "999"})
        monkeypatch.setattr(sup, "attribute_open_broker_position", lambda ts: STRATEGY if ts.startswith("TITAN") else None)
        logged = []

        async def fake_log(event, symbol, **detail):
            logged.append(event)
        monkeypatch.setattr(sup, "_log", fake_log)
        sup._untracked_logged.clear()
        out = await sup.sweep_orphans()
        pos = position_store.live_positions.get("SONA")
        assert pos is not None and pos.entry_price == 28.4 and pos.stop_loss_order_id == "SL1"
        assert out["adopted"][0]["outcome"] == "filled_adopted"
        assert out["untracked"] == ["TITAN 4600 CE"] and "UNTRACKED_POSITION_AT_BROKER" in logged
    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
