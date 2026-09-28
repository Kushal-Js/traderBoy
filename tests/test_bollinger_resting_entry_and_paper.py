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


def test_10_sides_long_drops_bearish_entries():
    trading_engine._resting_consumed.clear()

    async def bear_state(symbol):
        return _state("BEARISH", trigger=100.0, stop=102.0)

    async def bull_state(symbol):
        return _state("BULLISH")

    common = dict(is_fresh=True)
    for sides, state_fn, forming, expected in (
        ("long", bear_state, _forming(101.0, 99.9), None),
        ("both", bear_state, _forming(101.0, 99.9), ("BEARISH", 100.0, 102.0, 100.0)),
        ("long", bull_state, _forming(100.5, 99.0), ("BULLISH", 100.0, 98.0, 100.0)),
    ):
        trading_engine._resting_consumed.clear()
        with mock.patch.object(config, "ENTRY_MODE", "resting"), mock.patch.object(config, "SIDES", sides), \
             mock.patch.object(trading_engine.signals, "get_signal_state", side_effect=state_fn), \
             mock.patch.object(trading_engine.candle_feed, "is_fresh", return_value=common["is_fresh"]), \
             mock.patch.object(trading_engine.candle_feed, "forming_bar", return_value=forming), \
             mock.patch.object(trading_engine, "_now_ist", return_value=BAR_START + BAR + timedelta(minutes=1)):
            got = asyncio.run(trading_engine._evaluate_entry_signal("TEST"))
        assert got == expected, (sides, got)
    print("10. SIDES=long drops BEARISH (buy-PE) entries, keeps BULLISH; SIDES=both keeps both: PASSED")


def test_11_hold_to_close_exits_only_on_max_loss():
    pos = _position(entry=20.0, stop_pct=0.05, qty=100)
    apply_price_to_trailing(pos, 21.0)  # trailing armed
    with mock.patch.object(config, "EXIT_MODE", "trailing"):
        assert trading_engine._exit_reason_for(pos, 18.5) == "TRAILING_STOP_HIT"
    with mock.patch.object(config, "EXIT_MODE", "hold_to_close"):
        assert trading_engine._exit_reason_for(pos, 18.5) is None, "no %/trailing stop in hold_to_close"
        assert trading_engine._exit_reason_for(pos, 10.0) is None, "Rs 1,000 loss < MAX_LOSS"
        big = _position(entry=20.0, qty=1000)
        assert trading_engine._exit_reason_for(big, 15.0) == "MAX_LOSS_HIT", "Rs 5,000 loss >= MAX_LOSS"
        assert trading_engine._broker_stop_pct(0.05) == 0.95, "broker SL is only a disaster backstop"
    with mock.patch.object(config, "EXIT_MODE", "trailing"):
        assert trading_engine._broker_stop_pct(0.05) == 0.05
    print("11. hold_to_close: no % or trailing stop, MAX_LOSS still exits, broker SL = 95% backstop: PASSED")


def test_12_daily_square_off_predicate():
    tue_1514 = datetime(2026, 9, 29, 15, 14, tzinfo=IST)
    tue_1515 = datetime(2026, 9, 29, 15, 15, tzinfo=IST)
    sat_1600 = datetime(2026, 10, 3, 16, 0, tzinfo=IST)
    with mock.patch.object(config, "EXIT_MODE", "hold_to_close"):
        with mock.patch.object(trading_engine, "_now_ist", return_value=tue_1514):
            assert trading_engine._is_daily_square_off_time() is False
        with mock.patch.object(trading_engine, "_now_ist", return_value=tue_1515):
            assert trading_engine._is_daily_square_off_time() is True
        with mock.patch.object(trading_engine, "_now_ist", return_value=sat_1600):
            assert trading_engine._is_daily_square_off_time() is False
    with mock.patch.object(config, "EXIT_MODE", "trailing"), \
         mock.patch.object(trading_engine, "_now_ist", return_value=tue_1515):
        assert trading_engine._is_daily_square_off_time() is False, "trailing mode never squares off daily"
    print("12. daily square-off fires from 15:15 on weekdays, only in hold_to_close mode: PASSED")


def test_13_paper_daily_square_off_closes_at_market_price():
    records = []
    with tempfile.TemporaryDirectory() as tmp:
        book = paper_book_mod.PaperBook(str(Path(tmp) / "paper.json"))
        asyncio.run(book.open(_position(entry=20.0, qty=100)))

        async def fake_ltp(position):
            return 22.0

        async def fake_event(event, symbol, detail):
            records.append((event, detail))

        with mock.patch.object(trading_engine, "paper_book", book), \
             mock.patch.object(paper_book_mod, "append_jsonl"), \
             mock.patch.object(trading_engine, "_get_ltp", side_effect=fake_ltp), \
             mock.patch.object(trading_engine, "_record_bollinger_event", side_effect=fake_event), \
             mock.patch.object(trading_engine.dhan_wrapper, "unsubscribe_option_price"):
            asyncio.run(trading_engine._check_paper_positions({"TEST"}, "DAILY_SQUARE_OFF"))
        assert "TEST" not in book.positions
    closed = [d for e, d in records if e == "PAPER_POSITION_CLOSED"][0]
    assert closed["exit_reason"] == "DAILY_SQUARE_OFF" and closed["pnl_raw"] == 200.0
    print("13. paper positions are squared off at the live price with reason DAILY_SQUARE_OFF: PASSED")


def test_14_trading_days_to_expiry_and_roll_decision():
    tue = date(2026, 9, 29)
    assert trading_engine.trading_days_to_expiry(tue, date(2026, 9, 28)) == 1   # Mon -> Tue
    assert trading_engine.trading_days_to_expiry(tue, date(2026, 9, 25)) == 2   # Fri -> Tue (weekend skipped)
    assert trading_engine.trading_days_to_expiry(tue, date(2026, 9, 24)) == 3   # Thu -> Tue
    with mock.patch.object(config, "ROLL_EXPIRY_WITHIN_TRADING_DAYS", 2):
        assert trading_engine._needs_expiry_roll(tue, date(2026, 9, 28))
        assert trading_engine._needs_expiry_roll(tue, date(2026, 9, 25))
        assert not trading_engine._needs_expiry_roll(tue, date(2026, 9, 24))
        assert not trading_engine._needs_expiry_roll(None, date(2026, 9, 28))
    with mock.patch.object(config, "ROLL_EXPIRY_WITHIN_TRADING_DAYS", 0):
        assert not trading_engine._needs_expiry_roll(tue, date(2026, 9, 28)), "0 disables the rule"
    print("14. trading-days-to-expiry counts weekdays; roll within 2 trading days, 0 disables: PASSED")


def test_15_resolve_leg_rolls_to_next_expiry_or_skips():
    near = SimpleNamespace(trading_symbol="TEST 29 SEP 100 CALL", security_id="1", lot_size=100,
                           expiry_date=date(2026, 9, 29), strike=100.0)
    nxt = SimpleNamespace(trading_symbol="TEST 27 OCT 100 CALL", security_id="2", lot_size=100,
                          expiry_date=date(2026, 10, 27), strike=100.0)
    mon = datetime(2026, 9, 28, 10, 0, tzinfo=IST)
    events = []

    async def fake_event(event, symbol, detail):
        events.append(event)

    def run(liquid, is_mcx=False, now=mon):
        with mock.patch.object(config, "ROLL_EXPIRY_WITHIN_TRADING_DAYS", 2), \
             mock.patch.object(trading_engine.dhan_config, "LIQUID_CONTRACT_GATE_ENABLED", True), \
             mock.patch.object(trading_engine, "_now_ist", return_value=now), \
             mock.patch.object(trading_engine, "_record_bollinger_event", side_effect=fake_event), \
             mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=is_mcx), \
             mock.patch.object(trading_engine.dhan_wrapper, "get_liquid_atm_option", return_value=near), \
             mock.patch.object(trading_engine.dhan_wrapper, "_get_atm_option_once", return_value=nxt), \
             mock.patch.object(trading_engine.dhan_wrapper, "_is_index_underlying", return_value=False), \
             mock.patch.object(trading_engine.dhan_wrapper, "_nearby_option_candidates", return_value=[nxt]), \
             mock.patch.object(trading_engine.dhan_wrapper, "_is_contract_liquid_and_active", return_value=liquid), \
             mock.patch.object(trading_engine.mcx_registry, "pnl_multiplier", side_effect=_fake_multiplier):
            return asyncio.run(trading_engine._resolve_option_leg("TEST", "BULLISH"))

    leg = run(liquid=True)
    assert leg["trading_symbol"] == "TEST 27 OCT 100 CALL", "1 trading day to expiry -> next month"
    try:
        run(liquid=False)
        raise AssertionError("expected a skip when no liquid next-expiry contract exists")
    except trading_engine._SkipEntry as skip:
        assert skip.result["reason"] == "expiry_roll_no_liquid_contract"
    assert "ENTRY_SKIPPED_ROLL_FAILED" in events
    leg = run(liquid=True, is_mcx=True)
    assert leg["trading_symbol"] == "TEST 29 SEP 100 CALL", "MCX is exempt from the roll"
    far = datetime(2026, 9, 21, 10, 0, tzinfo=IST)  # 6 trading days to expiry
    assert run(liquid=True, now=far)["trading_symbol"] == "TEST 29 SEP 100 CALL", "no roll when far from expiry"
    print("15. near-expiry entries roll to a liquid next-month contract, else skip; MCX/far-dated unchanged: PASSED")


async def _fake_multiplier(symbol):
    return 2500


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
    test_10_sides_long_drops_bearish_entries()
    test_11_hold_to_close_exits_only_on_max_loss()
    test_12_daily_square_off_predicate()
    test_13_paper_daily_square_off_closes_at_market_price()
    test_14_trading_days_to_expiry_and_roll_decision()
    test_15_resolve_leg_rolls_to_next_expiry_or_skips()
    print("\nAll tests passed.")
