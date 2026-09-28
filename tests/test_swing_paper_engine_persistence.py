"""
Tests for the 28 Sep 2026 Swing paper engine fixes: open paper positions are
persisted across restarts, paper positions get the same Friday square-off as
real ones, closes are logged once, and GET /swing/paper-trades reports them.
Fully offline; every file write goes to a temp directory (never the real
data/ or history/ folders).

HOW TO RUN:
    uv run python tests/test_swing_paper_engine_persistence.py
"""
import asyncio
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from Options.dhan_client import IST  # noqa: E402
from Swing import swing_main  # noqa: E402
from Swing import swing_paper_engine as spe  # noqa: E402
from Swing.position_store import Position  # noqa: E402


def _pos(symbol="APOLLOHOSP", entry=29.85):
    return Position(
        underlying_symbol=symbol, trading_symbol=f"{symbol} 29 SEP 8800 PUT", basket_type="OPTIONS",
        regime="BEARISH", instrument_side="LONG", exchange_segment="NSE_FNO", product_type="PAPER",
        quantity=125, lot_size=125, entry_price=entry, best_price=entry, target_price=entry * 1.5,
        hard_stop_loss=entry * 0.8, order_id="", pnl_multiplier=125, resolved_option_type="PE",
        opened_at=datetime(2026, 9, 28, 9, 20, 24, tzinfo=IST),
        supertrend_entry_candle_start=datetime(2026, 9, 28, 9, 15, tzinfo=IST),
    )


def _with_temp_file(tmp):
    return mock.patch.object(spe, "PAPER_POSITIONS_FILE", Path(tmp) / "swing_paper_positions.json")


def test_1_persist_and_restore_round_trip():
    with tempfile.TemporaryDirectory() as tmp, _with_temp_file(tmp):
        spe._positions.clear()
        pos = _pos()
        spe._positions[pos.underlying_symbol] = pos
        spe._positions["PENDING"] = None  # an in-progress entry is never persisted
        spe._save_locked()
        spe._positions.clear()
        restored = spe.load_positions()
        assert [p.underlying_symbol for p in restored] == ["APOLLOHOSP"]
        r = spe._positions["APOLLOHOSP"]
        assert r.opened_at == pos.opened_at and r.supertrend_entry_candle_start == pos.supertrend_entry_candle_start
        assert r.entry_price == 29.85 and r.hard_stop_loss == pos.hard_stop_loss and r.product_type == "PAPER"
    spe._positions.clear()
    print("1. Swing paper positions round-trip through the persisted file (datetimes included): PASSED")


def test_2_corrupt_file_does_not_block_startup():
    with tempfile.TemporaryDirectory() as tmp, _with_temp_file(tmp):
        spe.PAPER_POSITIONS_FILE.write_text("{not json")
        assert spe.load_positions() == []
    print("2. a corrupt positions file is ignored, never blocks startup: PASSED")


def _run_check(pos, ltp, friday=False, mcx_friday=False, exit_reason=None, signal_reason=None):
    logged = []

    async def fake_ltp(position):
        return ltp

    async def fake_signal(symbol, position):
        return signal_reason

    patches = [
        mock.patch.object(spe.swing_te, "_get_ltp", side_effect=fake_ltp),
        mock.patch.object(spe.swing_te, "_is_friday_square_off_time", return_value=friday),
        mock.patch.object(spe.swing_te, "_is_mcx_friday_square_off_time", return_value=mcx_friday),
        mock.patch.object(spe.swing_te, "_is_index_square_off_time", return_value=False),
        mock.patch.object(spe.swing_te, "_exit_reason_for", return_value=exit_reason),
        mock.patch.object(spe.swing_te, "_evaluate_exit_signal", side_effect=fake_signal),
        mock.patch.object(spe.dhan_wrapper, "unsubscribe_option_price"),
        mock.patch.object(spe.trade_history, "append_jsonl", side_effect=lambda name, rec: logged.append((name, rec))),
    ]
    for p in patches:
        p.start()
    try:
        asyncio.run(spe._check_one(pos.underlying_symbol, pos))
    finally:
        for p in reversed(patches):
            p.stop()
    return logged


def test_3_friday_square_off_closes_and_logs_once():
    with tempfile.TemporaryDirectory() as tmp, _with_temp_file(tmp):
        spe._positions.clear()
        pos = _pos()
        spe._positions[pos.underlying_symbol] = pos
        logged = _run_check(pos, 31.0, friday=True)
        assert "APOLLOHOSP" not in spe._positions
        assert json.loads(spe.PAPER_POSITIONS_FILE.read_text()) == [], "closed position removed from the file"
        assert len(logged) == 1 and logged[0][0] == "swing_paper_trades"
        rec = logged[0][1]
        assert rec["exit_reason"] == "FRIDAY_SQUARE_OFF" and rec["mode"] == "paper"
        assert abs(rec["pnl"] - (31.0 - 29.85) * 125) < 1e-6
        # A second close attempt (e.g. a concurrent check) is a no-op - never logged twice.
        asyncio.run(spe._exit_one(pos.underlying_symbol, pos, 31.0, "FRIDAY_SQUARE_OFF"))
        assert len(logged) == 1
    spe._positions.clear()
    print("3. paper positions get the Friday square-off, removed from the file, logged exactly once: PASSED")


def test_4_normal_tick_keeps_position_and_persists_best_price():
    with tempfile.TemporaryDirectory() as tmp, _with_temp_file(tmp):
        spe._positions.clear()
        pos = _pos()
        spe._positions[pos.underlying_symbol] = pos
        logged = _run_check(pos, 33.0)
        assert "APOLLOHOSP" in spe._positions and not logged
        assert json.loads(spe.PAPER_POSITIONS_FILE.read_text())[0]["best_price"] == 33.0, "best price persisted"
        logged = _run_check(pos, 23.0, exit_reason="STOP_LOSS_HIT")
        assert "APOLLOHOSP" not in spe._positions and logged[0][1]["exit_reason"] == "STOP_LOSS_HIT"
    spe._positions.clear()
    print("4. a normal tick keeps the position and persists its best price; the real exit ladder still closes it: PASSED")


def test_5_mcx_paper_uses_mcx_friday_time():
    with tempfile.TemporaryDirectory() as tmp, _with_temp_file(tmp):
        spe._positions.clear()
        pos = _pos(symbol="COPPER")
        pos.exchange_segment = "MCX_COMM"
        spe._positions["COPPER"] = pos
        assert not _run_check(pos, 30.0, friday=True), "MCX isn't closed at the NSE Friday time"
        logged = _run_check(pos, 30.0, mcx_friday=True)
        assert logged and logged[0][1]["exit_reason"] == "MCX_FRIDAY_SQUARE_OFF"
    spe._positions.clear()
    print("5. MCX paper positions square off at the MCX Friday time, not the NSE one: PASSED")


def test_6_paper_trades_endpoint():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "log.log"
        log.write_text(json.dumps({"underlying_symbol": "NIFTY", "pnl": 500.0}) + "\n"
                       + json.dumps({"underlying_symbol": "SONACOMS", "pnl": -200.0}) + "\n")
        spe._positions.clear()
        spe._positions["APOLLOHOSP"] = _pos()
        with mock.patch("trade_history.dated_path", return_value=log):
            out = asyncio.run(swing_main.get_paper_trades("2026-09-28"))
    spe._positions.clear()
    assert out["closed_count"] == 2 and out["wins"] == 1 and out["total_pnl"] == 300.0
    assert list(out["open_positions"]) == ["APOLLOHOSP"] and out["day"] == "2026-09-28"
    print("6. GET /swing/paper-trades returns open positions plus the day's closed trades and totals: PASSED")


if __name__ == "__main__":
    test_1_persist_and_restore_round_trip()
    test_2_corrupt_file_does_not_block_startup()
    test_3_friday_square_off_closes_and_logs_once()
    test_4_normal_tick_keeps_position_and_persists_best_price()
    test_5_mcx_paper_uses_mcx_friday_time()
    test_6_paper_trades_endpoint()
    print("\nAll tests passed.")
