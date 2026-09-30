"""
Tests for SuperBollinger/live_state.py (30 Sep 2026): the live-state file,
write-ahead order intents and the three-way restart reconcile, plus the
supervisor keeping data alive for held symbols. Fully offline - a fake broker
stands in for Dhan, and every file is written under a temp directory.

HOW TO RUN:
    uv run python -m pytest tests/test_super_bollinger_live_state.py -q
"""
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Bollinger.position_store as bps  # noqa: E402
import SuperBollinger.supervisor as sup  # noqa: E402
import SuperBollinger.trading_engine as te  # noqa: E402
from Bollinger import signals, trading_engine as engine  # noqa: E402
from Options.dhan_client import OrderResult, OrderStatus, dhan_wrapper  # noqa: E402
from SuperBollinger import best_price_memory, live_state as ls  # noqa: E402
from SuperBollinger.state import HEDGE_STRATEGY, STRATEGY, halted, hedge_store, position_store  # noqa: E402


class FakeBroker:
    def __init__(self):
        self.positions, self.down = [], False
        self.orders, self.pending = {}, {}
        self.placed_sl, self.cancelled, self.attrib, self.loaded = [], [], {}, []

    def get_open_fno_positions(self):
        if self.down:
            raise RuntimeError("broker down")
        return list(self.positions)

    def refresh_order_status(self, oid, is_amo=False):
        st, px, q = self.orders.get(oid, (OrderStatus.CANCELLED, 0.0, 0))
        return OrderResult(order_id=oid, status=st, remark="", fill_price=px, filled_quantity=q, is_amo=False)

    def wait_for_order_result(self, oid, is_amo=False, retries=6, delay=1.0):
        return self.refresh_order_status(oid)

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        _st, px, q = self.orders.get(oid, (OrderStatus.PENDING, 0.0, 0))
        self.orders[oid] = (OrderStatus.CANCELLED, px, q)

    def place_sl(self, ts, qty, side, trig, limit, tag, product):
        self.placed_sl.append((ts, qty, trig))
        return {"order_id": f"SL-{len(self.placed_sl)}"}

    def series(self, symbol, *_a):
        self.loaded.append(symbol)
        signals._rest_series_cache[symbol] = {"close": [1.0] * 40, "high": [1.0] * 40, "low": [1.0] * 40}
        return signals._rest_series_cache[symbol]


def _bp(sym, ts, ot, qty, avg):
    return {"underlying_symbol": sym, "trading_symbol": ts, "option_type": ot, "quantity": qty, "avg_price": avg,
            "lot_size": qty, "product_type": "MARGIN"}


def _restart():
    """What a process restart wipes."""
    for store in (position_store, hedge_store):
        store.live_positions.clear()
        store.reserved_symbols.clear()
        store.closed_positions_today.clear()
    sup._track.clear()
    te.PROFILE.consumed.clear()
    halted["day"], halted["reason"] = None, None
    ls._intents.clear()
    ls._last_digest = None
    best_price_memory._mem = None
    signals._rest_series_cache.clear()
    sup._series_checked.clear()


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b = FakeBroker()
    monkeypatch.setattr(dhan_wrapper, "get_open_fno_positions", b.get_open_fno_positions)
    monkeypatch.setattr(dhan_wrapper, "subscribe_option_price", lambda ts: None)
    monkeypatch.setattr(dhan_wrapper, "get_pending_order_id", lambda ts, side, exch=None: b.pending.get((ts, side)))
    monkeypatch.setattr(dhan_wrapper, "refresh_order_status", b.refresh_order_status)
    monkeypatch.setattr(dhan_wrapper, "wait_for_order_result", b.wait_for_order_result)
    monkeypatch.setattr(dhan_wrapper, "cancel_order", b.cancel_order)
    monkeypatch.setattr(dhan_wrapper, "place_stop_loss_limit_order", b.place_sl)
    monkeypatch.setattr(te, "attribute_open_broker_position", lambda ts: b.attrib.get(ts))
    monkeypatch.setattr(sup, "attribute_open_broker_position", lambda ts: b.attrib.get(ts))
    monkeypatch.setattr(signals, "_underlying_reference", lambda s: ("1", "NSE_EQ", "EQUITY"))
    monkeypatch.setattr(signals, "_get_intraday_series", b.series)

    async def _no_filters(*_a, **_k):
        return None
    monkeypatch.setattr(bps.reversal_filters, "evaluate_and_log", _no_filters)
    _restart()
    yield b
    _restart()


async def _open_book(b):
    """One CE with an armed best price + its hedge, one closed CE, supervisor memory, a used signal, a halt."""
    now = engine._now_ist()
    ce = te._new_position("MOTI", {"trading_symbol": "MOTI 1000 CE", "product_type": "MARGIN", "quantity": 775,
                                   "lot_size": 775}, 40.75, "O1", "SLCE")
    ce.best_price, ce.opened_at = 45.0, now - timedelta(hours=1)
    await position_store.add_position(ce)
    hd = sup._hedge_position("MOTI", {"trading_symbol": "MOTI 1000 PE", "product_type": "MARGIN", "lot_size": 775},
                             775, 20.0, "O2", "SLPE")
    hd.best_price = 23.0
    await hedge_store.add_position(hd)
    old = te._new_position("APL", {"trading_symbol": "APL 2220 CE", "product_type": "MARGIN", "quantity": 350,
                                   "lot_size": 350}, 77.05, "O0")
    await position_store.add_position(old)
    await position_store.close_position("APL", 63.95, "STOP_LOSS_HIT")
    sup._track[("MOTI", ce.opened_at.isoformat())] = {"entry_spot": 1008.0, "hedged": True, "waiting_logged": True,
                                                      "shadow_stopped": False, "shadow_reentered": False}
    te.PROFILE.consumed["MOTI"] = now.replace(second=0, microsecond=0)
    halted["day"], halted["reason"] = now.date(), "test halt"
    b.positions = [_bp("MOTI", "MOTI 1000 CE", "CE", 775, 40.75), _bp("MOTI", "MOTI 1000 PE", "PE", 775, 20.0)]
    b.attrib.update({"MOTI 1000 CE": STRATEGY, "MOTI 1000 PE": HEDGE_STRATEGY})
    b.pending = {("MOTI 1000 CE", "SELL"): "SLCE", ("MOTI 1000 PE", "SELL"): "SLPE"}
    return ce


def test_1_state_file_is_written_only_when_something_changed(broker):
    async def run():
        await _open_book(broker)
        assert ls.save() is True and ls.FILE.exists()
        assert ls.save() is False
        position_store.live_positions["MOTI"].best_price = 46.0
        assert ls.save() is True
        data = json.loads(ls.FILE.read_text())
        assert data["positions"][0]["best_price"] == 46.0 and data["halted"]["reason"] == "test halt"
    asyncio.run(run())


def test_2_restart_restores_positions_and_everything_the_broker_does_not_know(broker):
    async def run():
        ce = await _open_book(broker)
        opened, used = ce.opened_at, te.PROFILE.consumed["MOTI"]
        ls.save()
        _restart()
        report = await ls.restore()
        p, h = position_store.live_positions["MOTI"], hedge_store.live_positions["MOTI"]
        assert (p.opened_at, p.best_price, p.order_id, p.stop_loss_order_id) == (opened, 45.0, "O1", "SLCE")
        assert h.best_price == 23.0 and h.stop_loss_order_id == "SLPE"
        # the day's closed loss is back, so the disaster brake counts it again
        assert [c.exit_price for c in position_store.closed_positions_today] == [63.95]
        assert round(sum((c.exit_price - c.entry_price) * c.pnl_multiplier
                         for c in position_store.closed_positions_today)) == -4585
        assert sup._track[("MOTI", opened.isoformat())]["hedged"] is True      # no second hedge on this CE
        assert te.PROFILE.consumed["MOTI"] == used                              # trigger cannot fire twice
        assert sup.halted_today() and halted["reason"] == "test halt"
        assert broker.loaded == ["MOTI"] and report["warm_up"] == [{"symbol": "MOTI", "series_loaded": True}]
        assert report["needs_review"] is False and report["state_file"] is True
        assert ls.last_report() is report
    asyncio.run(run())


def test_3_position_closed_by_its_broker_stop_while_the_bot_was_down(broker):
    async def run():
        await _open_book(broker)
        ls.save()
        broker.positions = [_bp("MOTI", "MOTI 1000 PE", "PE", 775, 20.0)]      # the CE is gone at the broker
        broker.orders["SLCE"] = (OrderStatus.TRADED, 34.9, 775)
        _restart()
        report = await ls.restore()
        assert "MOTI" not in position_store.live_positions and "MOTI" in hedge_store.live_positions
        closed = [c for c in position_store.closed_positions_today if c.trading_symbol == "MOTI 1000 CE"]
        assert closed and (closed[0].exit_price, closed[0].exit_reason) == (34.9, "STOP_LOSS_HIT")
        assert report["closed_while_down"][0]["exit_price"] == 34.9 and report["needs_review"] is False
    asyncio.run(run())


def test_4_closed_while_down_with_no_known_exit_price_is_flagged(broker):
    async def run():
        await _open_book(broker)
        ls.save()
        broker.positions = [_bp("MOTI", "MOTI 1000 PE", "PE", 775, 20.0)]
        broker.orders["SLCE"] = (OrderStatus.CANCELLED, 0.0, 0)                 # e.g. closed by hand
        _restart()
        report = await ls.restore()
        closed = [c for c in position_store.closed_positions_today if c.trading_symbol == "MOTI 1000 CE"]
        assert closed[0].exit_price is None and closed[0].exit_reason == "CLOSED_WHILE_BOT_WAS_DOWN"
        assert report["needs_review"] is True
    asyncio.run(run())


def test_5_broker_unreachable_at_startup_keeps_managing_what_the_file_holds(broker):
    async def run():
        await _open_book(broker)
        ls.save()
        broker.down = True
        _restart()
        report = await ls.restore()
        p = position_store.live_positions.get("MOTI")
        assert p is not None and p.best_price == 45.0 and p.stop_loss_order_id == "SLCE"
        assert "MOTI" in hedge_store.live_positions
        assert report["broker_reachable"] is False and report["needs_review"] is True
    asyncio.run(run())


def test_6_order_that_filled_but_was_never_recorded_is_adopted_with_a_stop(broker):
    async def run():
        iid = ls.intent_begin("entry", "SONA", {"trading_symbol": "SONA 830 CE", "product_type": "MARGIN",
                                                "lot_size": 1225}, 1225)
        ls.intent_order(iid, "B9")
        assert json.loads(ls.FILE.read_text())["intents"][iid]["order_id"] == "B9"   # on disk before anything else
        broker.orders["B9"] = (OrderStatus.TRADED, 28.4, 1225)
        broker.positions = [_bp("SONA", "SONA 830 CE", "CE", 1225, 28.4)]        # no open record in trade history
        _restart()
        report = await ls.restore()
        p = position_store.live_positions.get("SONA")
        assert p is not None and p.entry_price == 28.4 and p.quantity == 1225 and p.stop_loss_order_id == "SL-1"
        assert broker.placed_sl[0][:2] == ("SONA 830 CE", 1225)
        assert report["intents"][0]["outcome"] == "filled_adopted" and not ls._intents
    asyncio.run(run())


def test_7_order_still_resting_at_the_broker_after_a_restart_is_cancelled(broker):
    async def run():
        iid = ls.intent_begin("hedge", "TITAN", {"trading_symbol": "TITAN 4600 PE", "product_type": "MARGIN",
                                                 "lot_size": 175}, 175)
        ls.intent_order(iid, "B10")
        broker.orders["B10"] = (OrderStatus.PENDING, 0.0, 0)
        _restart()
        report = await ls.restore()
        assert "B10" in broker.cancelled and "TITAN" not in hedge_store.live_positions
        assert report["intents"][0]["outcome"] == "not_filled_CANCELLED"
    asyncio.run(run())


def test_8_intent_without_an_order_id_cancels_a_pending_buy_in_that_contract(broker):
    async def run():
        ls.intent_begin("entry", "SAIL", {"trading_symbol": "SAIL 185 CE", "product_type": "MARGIN", "lot_size": 4700}, 4700)
        broker.pending[("SAIL 185 CE", "BUY")] = "B12"                           # died before the id came back
        broker.orders["B12"] = (OrderStatus.PENDING, 0.0, 0)
        _restart()
        report = await ls.restore()
        assert "B12" in broker.cancelled and "SAIL" not in position_store.live_positions
        assert report["intents"][0]["order_id"] == "B12"
    asyncio.run(run())


def test_9_no_state_file_or_a_file_from_another_day_falls_back_to_the_broker(broker):
    async def run():
        broker.positions = [_bp("MOTI", "MOTI 1000 CE", "CE", 775, 40.75)]
        broker.attrib["MOTI 1000 CE"] = STRATEGY
        report = await ls.restore()
        assert "MOTI" in position_store.live_positions and report["state_file"] is False
        assert report["positions"][0]["source"] == "broker_only"
        ls.save(force=True)
        data = json.loads(ls.FILE.read_text())
        data["day"] = "2026-09-29"
        ls.FILE.write_text(json.dumps(data))
        assert ls.load() is None
        ls.FILE.write_text("{not json")
        assert ls.load() is None
    asyncio.run(run())


def test_10_error_after_sending_an_order_adopts_the_fill_at_once(broker):
    async def run():
        iid = ls.intent_begin("entry", "SAIL", {"trading_symbol": "SAIL 185 CE", "product_type": "MARGIN", "lot_size": 4700}, 4700)
        ls.intent_order(iid, "B11")
        broker.orders["B11"] = (OrderStatus.TRADED, 5.5, 4700)
        await ls.intent_finish(iid, outcome_known=False)
        assert "SAIL" in position_store.live_positions and not ls._intents
        # a handled outcome just drops the intent
        iid = ls.intent_begin("entry", "X", {"trading_symbol": "X CE", "product_type": "MARGIN", "lot_size": 1}, 1)
        await ls.intent_finish(iid, outcome_known=True)
        assert not ls._intents
    asyncio.run(run())


def test_11_supervisor_loads_data_for_a_held_symbol_and_raises_the_alarm_without_it(broker, monkeypatch):
    async def run():
        assert sup._spot("GLEN") is None and sup._atr("GLEN") is None          # the GLENMARK state after a restart
        assert await sup.ensure_series("GLEN", force=True) is True
        assert sup._atr("GLEN") is not None and broker.loaded == ["GLEN"]
        assert await sup.ensure_series("GLEN") is True and broker.loaded == ["GLEN"]   # throttled: no second load
        events = []

        async def fake_log(event, symbol, **detail):
            events.append((event, symbol))
        monkeypatch.setattr(sup, "_log", fake_log)
        clock = {"t": 1000.0}
        monkeypatch.setattr(sup.time, "monotonic", lambda: clock["t"])
        sup._no_data_since.clear()
        sup._no_data_logged.clear()
        await sup._check_held_data("NODATA", True)
        assert events == []                                                     # not yet 30s
        clock["t"] += 31
        await sup._check_held_data("NODATA", True)
        assert events == [("HELD_POSITION_NO_DATA", "NODATA")]
        await sup._check_held_data("NODATA", True)
        assert len(events) == 1                                                 # rate limited
    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
