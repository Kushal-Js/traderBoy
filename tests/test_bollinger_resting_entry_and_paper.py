"""
Tests for the 28 Sep 2026 Bollinger changes: resting-order entry, the 5%
minimum stop, the minimum-ATM-premium gate, the shared trailing ratchet, and
the paper book. Fully offline - Dhan calls, the tick feed and the event log
are mocked.

HOW TO RUN:
    uv run python tests/test_bollinger_resting_entry_and_paper.py
"""
import asyncio
import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Bollinger.paper_book as paper_book_mod  # noqa: E402
import Bollinger.trading_engine as trading_engine  # noqa: E402
from Bollinger import config  # noqa: E402
from Bollinger.position_store import Position, apply_price_to_trailing  # noqa: E402
from Bollinger.signals import BollingerSignalState, resting_trigger_hit  # noqa: E402
from Options.dhan_client import IST  # noqa: E402

BAR = timedelta(minutes=config.SIGNAL_INTERVAL_MINUTES)
TODAY = date(2026, 9, 29)
BAR_START = datetime(2026, 9, 29, 10, 0, tzinfo=IST)


def _state(side="BULLISH", trigger=100.0, stop=98.0, candle_start=BAR_START, fired=None):
    return BollingerSignalState(
        valid_bullish=side == "BULLISH", valid_bearish=side == "BEARISH",
        pending_side=side, pending_trigger_price=trigger if side else None,
        pending_stop_price=stop if side else None, fired=fired, fired_trigger_price=None,
        fired_stop_price=None, last_close=99.0, candle_start=candle_start,
    )


def _forming(high, low, start=BAR_START + BAR):
    return {"candle_start": start, "high": high, "low": low, "last": (high + low) / 2,
            "last_tick_at": start + timedelta(seconds=30)}


def _position(entry=20.0, stop_pct=0.05, qty=100):
    trail = entry * stop_pct * config.TRAILING_STOP_FRACTION
    return Position(
        underlying_symbol="TEST", trading_symbol="TEST 29 SEP 100 CALL", resolved_option_type="CE",
        instrument_side="LONG", exchange_segment="NSE_FNO", product_type="MARGIN", quantity=qty, lot_size=qty,
        entry_price=entry, best_price=entry, stop_pct=stop_pct, hard_stop_loss=entry * (1 - stop_pct),
        trailing_stop_dist=trail, trailing_step=trail * config.TRAILING_STEP_FRACTION, pnl_multiplier=qty,
        order_id="PAPER",
    )


def test_1_resting_trigger_hit_bullish_and_bearish():
    assert resting_trigger_hit(_state("BULLISH"), _forming(100.2, 99.0), 5, TODAY) == ("BULLISH", 100.0, 98.0, 100.0)
    assert resting_trigger_hit(_state("BULLISH"), _forming(99.9, 99.0), 5, TODAY) is None, "no touch -> no entry"
    bear = _state("BEARISH", trigger=100.0, stop=102.0)
    assert resting_trigger_hit(bear, _forming(101.0, 99.95), 5, TODAY) == ("BEARISH", 100.0, 102.0, 100.0)
    assert resting_trigger_hit(bear, _forming(101.0, 100.05), 5, TODAY) is None
    print("1. resting trigger fires on a touch of the trigger (bullish high / bearish low), not before: PASSED")


def test_2_resting_trigger_guards():
    touching = _forming(101.0, 99.0)
    assert resting_trigger_hit(None, touching, 5, TODAY) is None, "no signal state"
    assert resting_trigger_hit(_state(side=None), touching, 5, TODAY) is None, "no armed pending order"
    yesterday = BAR_START - timedelta(days=1)
    assert resting_trigger_hit(_state(candle_start=yesterday), _forming(101.0, 99.0, yesterday + BAR), 5, TODAY) is None, \
        "a pending order from a previous day must never fire"
    assert resting_trigger_hit(_state(), None, 5, TODAY) is None, "no live forming bar (tick feed stale)"
    assert resting_trigger_hit(_state(), _forming(101.0, 99.0, BAR_START + 2 * BAR), 5, TODAY) is None, \
        "forming bar must be exactly the bar after the pending order's bar"
    print("2. resting trigger ignores missing/stale/previous-day/out-of-step state: PASSED")


def test_3_evaluate_entry_acts_on_a_pending_order_only_once():
    trading_engine._resting_consumed.clear()

    async def fake_state(symbol):
        return _state()

    with mock.patch.object(config, "ENTRY_MODE", "resting"), \
         mock.patch.object(trading_engine.signals, "get_signal_state", side_effect=fake_state), \
         mock.patch.object(trading_engine.candle_feed, "is_fresh", return_value=True), \
         mock.patch.object(trading_engine.candle_feed, "forming_bar", return_value=_forming(100.5, 99.0)), \
         mock.patch.object(trading_engine, "_now_ist", return_value=BAR_START + BAR + timedelta(minutes=1)):
        first = asyncio.run(trading_engine._evaluate_entry_signal("TEST"))
        second = asyncio.run(trading_engine._evaluate_entry_signal("TEST"))
    assert first == ("BULLISH", 100.0, 98.0, 100.0), first
    assert second is None, "the same pending order must not open a second trade after a quick stop-out"

    with mock.patch.object(config, "ENTRY_MODE", "resting"), \
         mock.patch.object(trading_engine.signals, "get_signal_state", side_effect=fake_state), \
         mock.patch.object(trading_engine.candle_feed, "is_fresh", return_value=False), \
         mock.patch.object(trading_engine.candle_feed, "forming_bar", return_value=_forming(100.5, 99.0)), \
         mock.patch.object(trading_engine, "_now_ist", return_value=BAR_START + BAR + timedelta(minutes=1)):
        trading_engine._resting_consumed.clear()
        assert asyncio.run(trading_engine._evaluate_entry_signal("TEST")) is None, "stale tick feed -> no entry"
    print("3. a pending order is acted on at most once, and never on a stale tick feed: PASSED")


def test_4_bar_close_mode_still_works():
    async def fake_state(symbol):
        return BollingerSignalState(True, False, None, None, None, "BULLISH", 100.0, 98.0, 100.4, BAR_START)

    with mock.patch.object(config, "ENTRY_MODE", "bar_close"), \
         mock.patch.object(trading_engine.signals, "get_signal_state", side_effect=fake_state):
        assert asyncio.run(trading_engine._evaluate_entry_signal("TEST")) == ("BULLISH", 100.0, 98.0, 100.4)
    print("4. ENTRY_MODE=bar_close keeps the original fire-on-closed-bar behaviour: PASSED")


def test_5_stop_params_use_the_5pct_floor():
    assert config.MIN_STOP_PCT == 0.05, f"default MIN_STOP_PCT should be 0.05, got {config.MIN_STOP_PCT}"
    stop_pct, hard, trail, step = trading_engine._stop_params(20.0, 100.0, 99.5, 100.0)  # 0.5% swing -> floored
    assert stop_pct == 0.05 and abs(hard - 19.0) < 1e-9
    assert abs(trail - 20.0 * 0.05 / 3) < 1e-9 and abs(step - trail / 5) < 1e-9
    stop_pct, *_ = trading_engine._stop_params(20.0, 100.0, 92.0, 100.0)  # 8% swing -> used as-is
    assert abs(stop_pct - 0.08) < 1e-9
    print("5. stop sizing floors at 5% of premium; trailing = 1/3 of stop, step = 1/5 of trailing: PASSED")


def test_6_premium_gate():
    assert trading_engine.premium_too_low(1.10, is_mcx=False), "SUZLON-style Rs 1 option must be blocked"
    assert trading_engine.premium_too_low(4.95, is_mcx=False)
    assert not trading_engine.premium_too_low(5.0, is_mcx=False)
    assert not trading_engine.premium_too_low(38.0, is_mcx=False)
    assert trading_engine.premium_too_low(None, is_mcx=False), "no price -> never enter"
    assert not trading_engine.premium_too_low(1.10, is_mcx=True), "MCX is exempt"
    print("6. premium gate blocks NSE options under Rs 5 (and unpriced ones), exempts MCX: PASSED")


def test_7_trailing_ratchet_unchanged():
    pos = _position(entry=20.0, stop_pct=0.05)  # trail dist 0.3333, step 0.0667
    apply_price_to_trailing(pos, 20.3)
    assert not pos.trailing_armed, "not armed before +stop/3"
    apply_price_to_trailing(pos, 20.40)
    assert pos.trailing_armed and abs(pos.trailing_stop_price - (20.40 - pos.trailing_stop_dist)) < 1e-9
    armed_at = pos.trailing_stop_price
    apply_price_to_trailing(pos, 20.43)
    assert pos.trailing_stop_price == armed_at, "moves only in steps of trailing_step"
    apply_price_to_trailing(pos, 20.60)
    assert pos.trailing_stop_price > armed_at
    high = pos.trailing_stop_price
    apply_price_to_trailing(pos, 19.0)
    assert pos.trailing_stop_price == high and pos.best_price == 20.60, "never loosens on a drop"
    print("7. shared trailing ratchet arms at +stop/3, steps up only, never loosens: PASSED")


def test_8_paper_book_open_update_close_persist():
    records = []
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(paper_book_mod, "append_jsonl", side_effect=lambda name, rec: records.append((name, rec))):
        path = Path(tmp) / "paper.json"
        book = paper_book_mod.PaperBook(str(path))
        pos = _position(entry=20.0, qty=100)
        assert asyncio.run(book.open(pos))
        assert not asyncio.run(book.open(_position())), "one paper position per symbol"
        asyncio.run(book.update("TEST", 20.60))
        assert json.loads(path.read_text())[0]["trailing_armed"] is True, "trailing state persisted"

        restored = paper_book_mod.PaperBook(str(path))
        restored.load()
        assert restored.positions["TEST"].trailing_armed and restored.positions["TEST"].best_price == 20.60
        assert restored.positions["TEST"].opened_at == pos.opened_at, "datetimes round-trip"

        rec = asyncio.run(restored.close("TEST", 21.0, "TRAILING_STOP_HIT"))
        assert asyncio.run(restored.close("TEST", 21.0, "TRAILING_STOP_HIT")) is None, "double close is a no-op"
        assert json.loads(path.read_text()) == []
    assert rec["pnl_raw"] == 100.0
    expected = (21.0 * (1 - 0.005) - 20.0 * (1 + 0.005)) * 100
    assert abs(rec["pnl_modeled"] - round(expected, 2)) < 1e-6, rec
    assert records and records[0][0] == paper_book_mod.PAPER_TRADES_LOG_NAME
    print("8. paper book persists open positions + trailing state, logs raw and slippage-modeled P&L: PASSED")


def test_9_enter_paper_end_to_end():
    fake_atm = SimpleNamespace(trading_symbol="TEST 29 SEP 100 CALL", security_id="1", lot_size=100,
                               expiry_date=date(2026, 10, 27))
    events = []

    async def fake_event(event, symbol, detail):
        events.append(event)

    with tempfile.TemporaryDirectory() as tmp:
        book = paper_book_mod.PaperBook(str(Path(tmp) / "paper.json"))
        common = [
            mock.patch.object(trading_engine, "paper_book", book),
            mock.patch.object(paper_book_mod, "append_jsonl"),
            mock.patch.object(trading_engine, "_record_bollinger_event", side_effect=fake_event),
            mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=False),
            mock.patch.object(trading_engine.dhan_wrapper, "get_liquid_atm_option", return_value=fake_atm),
            mock.patch.object(trading_engine.dhan_wrapper, "subscribe_option_price"),
            mock.patch.object(trading_engine.dhan_wrapper, "unsubscribe_option_price"),
            mock.patch.object(trading_engine.dhan_wrapper, "place_market_order",
                              side_effect=AssertionError("paper mode must never place an order")),
        ]
        for c in common:
            c.start()
        try:
            with mock.patch.object(trading_engine.dhan_wrapper, "_get_option_ltp_once", return_value=3.0):
                r = asyncio.run(trading_engine._enter_paper("TEST", "BULLISH", 100.0, 99.5, 100.0))
            assert r["reason"] == "premium_below_minimum" and "TEST" not in book.positions
            assert "ENTRY_SKIPPED_LOW_PREMIUM" in events

            with mock.patch.object(trading_engine.dhan_wrapper, "_get_option_ltp_once", return_value=20.0):
                r = asyncio.run(trading_engine._enter_paper("TEST", "BULLISH", 100.0, 99.5, 100.0))
            assert r["status"] == "paper_entered" and book.positions["TEST"].hard_stop_loss == 19.0
            assert "PAPER_POSITION_OPENED" in events

            asyncio.run(trading_engine._check_paper_position("TEST", 19.5))
            assert "TEST" in book.positions, "above the 5% stop -> still open"
            asyncio.run(trading_engine._check_paper_position("TEST", 18.9))
            assert "TEST" not in book.positions and "PAPER_POSITION_CLOSED" in events, "hit the 5% stop -> closed"
        finally:
            for c in common:
                c.stop()
    print("9. paper entry: skips cheap options, fills at live price, exits on the shared stop, never orders: PASSED")


if __name__ == "__main__":
    test_1_resting_trigger_hit_bullish_and_bearish()
    test_2_resting_trigger_guards()
    test_3_evaluate_entry_acts_on_a_pending_order_only_once()
    test_4_bar_close_mode_still_works()
    test_5_stop_params_use_the_5pct_floor()
    test_6_premium_gate()
    test_7_trailing_ratchet_unchanged()
    test_8_paper_book_open_update_close_persist()
    test_9_enter_paper_end_to_end()
    print("\nAll tests passed.")
