"""
Super Bollinger, 1 Oct 2026:
  - paper positions (NIFTY/BANKNIFTY on paper) no longer take REAL slots; paper
    has its own, equal limit;
  - SuperBollinger/shadow_list.py trades the Friday shadow list on PAPER with the
    same rules, its own signal bookkeeping, book and logs - never the real book.
Fully offline: Dhan calls, signals and prices are faked.

HOW TO RUN:
    uv run python -m pytest tests/test_super_bollinger_shadow_list_and_slots.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import capacity_control  # noqa: E402
import paper_mode_control  # noqa: E402
import stock_selection  # noqa: E402
import trade_history  # noqa: E402
import SuperBollinger.shadow_list as sl  # noqa: E402
import SuperBollinger.trading_engine as te  # noqa: E402
from Bollinger import trading_engine as engine  # noqa: E402
from SuperBollinger import settings  # noqa: E402
from SuperBollinger.state import paper_book, position_store  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity_control, "get_max_concurrent_trades", lambda strategy: 2)
    monkeypatch.setattr(paper_mode_control, "is_paper_mode_enabled", lambda s: s == "SuperBollingerIndex")
    monkeypatch.setattr(settings, "_overrides_loaded", True)
    for k, v in (("shadow_list_mode", "paper"), ("entry_filter_1h", "on"), ("max_loss_rs", 4500.0),
                 ("breakeven_after_rs", 1500.0), ("quantity_lots", 1), ("min_premium_rs", 5.0), ("excluded_symbols", [])):
        monkeypatch.setitem(settings._overrides, k, v)
    for book in (paper_book, sl.shadow_book):
        book.positions.clear()
    position_store.live_positions.clear(); position_store.reserved_symbols.clear()
    monkeypatch.setattr(stock_selection, "SHADOW_FILE", tmp_path / "shadow.json")
    monkeypatch.setattr(sl.shadow_book, "_path", tmp_path / "shadow_book.json")
    sl._list_cache.update(mtime=None, symbols=[], as_of=None)
    sl._running["on"] = False
    events = []

    async def ev(event, symbol, detail):
        events.append((event, symbol))
    monkeypatch.setattr(sl, "_event", ev)
    monkeypatch.setattr(sl.signals, "_symbol_market_open", lambda s: True)
    monkeypatch.setattr(sl.signals, "is_symbol_ws_fresh", lambda s: True)
    monkeypatch.setattr(sl.dhan_wrapper, "subscribe_option_price", lambda ts: None)
    yield events
    for book in (paper_book, sl.shadow_book):
        book.positions.clear()
    position_store.live_positions.clear(); position_store.reserved_symbols.clear()


def test_1_paper_index_positions_do_not_take_real_slots(env):
    paper_book.positions.update({"NIFTY": object(), "BANKNIFTY": object()})
    assert te.open_count() == 0 and te.paper_open_count() == 2
    assert te._slot_free_for("TITAN")                   # a real stock entry still has both real slots
    assert not te._slot_free_for("NIFTY")               # a 3rd paper position waits for the paper limit
    position_store.live_positions.update({"TITAN": object(), "SAIL": object()})
    assert not te._slot_free_for("GLENMARK")            # 2 real positions = the real limit
    paper_book.positions.clear()
    assert te._slot_free_for("NIFTY")                   # a full real book does not block paper


def _write_list(symbols, as_of="2026-10-01"):
    stock_selection.SHADOW_FILE.write_text(json.dumps({"as_of": as_of, "shadow": {"symbols": symbols}}))


def test_2_shadow_list_is_read_from_the_friday_record(env):
    assert sl.shadow_symbols() == ([], None)
    _write_list(["AAA", "BBB", "NIFTY"])
    assert sl.shadow_symbols() == (["AAA", "BBB"], "2026-10-01")       # indices never traded from this list


def test_3_entries_follow_the_live_rules_on_a_separate_paper_book(env, monkeypatch):
    _write_list(["AAA", "BBB", "CCC", "DDD"])

    async def fake_eval(symbol, profile):
        assert profile is sl.PROFILE                                    # its own signal bookkeeping
        return ("BULLISH", 100.0, 95.0, 100.0) if symbol != "DDD" else None
    monkeypatch.setattr(engine, "_evaluate_entry_signal", fake_eval)

    async def leg(symbol, sig, profile):
        return {"trading_symbol": f"{symbol} 27 OCT 100 CALL", "lot_size": 100, "product_type": "MARGIN"}
    monkeypatch.setattr(engine, "_resolve_option_leg", leg)

    async def ltp(ts):
        return 20.0
    monkeypatch.setattr(sl.dhan_wrapper, "get_option_ltp_async", ltp)

    async def green(symbol):
        return symbol != "BBB", {"candle_open": 1.0, "candle_close": 2.0}
    monkeypatch.setattr(sl.entry_filters, "last_hour_green", green)
    asyncio.run(sl._entries())
    assert sorted(sl.shadow_book.positions) == ["AAA", "CCC"]          # BBB: red last hour; then the limit of 2
    assert ("SHADOW_ENTRY_SKIPPED_1H_RED", "BBB") in env and ("SHADOW_PAPER_OPENED", "AAA") in env
    pos = sl.shadow_book.positions["AAA"]
    assert (pos.entry_price, pos.quantity, pos.order_id) == (20.0, 100, "SHADOW")
    assert not position_store.live_positions and not paper_book.positions      # real book untouched
    assert sl.PROFILE is not te.PROFILE and sl.PROFILE.consumed is not te.PROFILE.consumed


def test_4_exits_max_loss_breakeven_and_square_off(env, monkeypatch):
    for sym in ("AAA", "CCC", "EEE"):
        leg = {"trading_symbol": f"{sym} CE", "lot_size": 100, "product_type": "MARGIN", "quantity": 100}
        asyncio.run(sl.shadow_book.open(te._new_position(sym, leg, 20.0, "SHADOW")))
    prices = {"AAA": -25.0, "CCC": 25.0, "EEE": 36.0}

    async def get_ltp(p):
        return prices[p.underlying_symbol]
    monkeypatch.setattr(engine, "_get_ltp", get_ltp)
    asyncio.run(sl._exits(False))
    assert "AAA" not in sl.shadow_book.positions                       # -4,500 on 100 qty
    prices["EEE"] = 20.0
    asyncio.run(sl._exits(False))
    assert "EEE" not in sl.shadow_book.positions                       # was +1,600, back to cost -> breakeven stop
    asyncio.run(sl._exits(True))
    assert not sl.shadow_book.positions                                # 15:15 square-off
    recs = [json.loads(line) for f in Path(trade_history.HISTORY_DIR).glob("*_super_bollinger_shadow_paper_trades.log")
            for line in f.read_text().splitlines()]
    assert {r["exit_reason"] for r in recs} >= {"MAX_LOSS_HIT", "BREAKEVEN_STOP_HIT", "DAILY_SQUARE_OFF"}
    assert all(r["strategy"] == "SuperBollingerShadow" and "pnl_modeled" in r for r in recs)


def test_5_mode_switch_and_one_pass_at_a_time(env, monkeypatch):
    started = []
    monkeypatch.setattr(sl.asyncio, "create_task", lambda c: started.append(c) or c.close())
    monkeypatch.setitem(settings._overrides, "shadow_list_mode", "off")
    sl.kick(False, True)
    assert not started
    monkeypatch.setitem(settings._overrides, "shadow_list_mode", "paper")
    sl.kick(False, True)
    assert len(started) == 1
    sl._running["on"] = True
    sl.kick(False, True)
    assert len(started) == 1
    sl._running["on"] = False
    sl.kick(False, False)                                              # nothing open and entries closed -> no pass
    assert len(started) == 1
    _parsed, errors = settings.validate({"shadow_list_mode": "real"})
    assert errors                                                      # never traded for real


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
