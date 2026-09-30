"""
position_memory.py (30 Sep / 1 Oct 2026): real Swing, Bollinger, Options and
Luxury positions keep what the broker cannot know across a restart - and only
when the broker's position is the SAME position (contract, quantity, side,
entry within 1%). Fully offline; files go to a temp directory.

HOW TO RUN:
    uv run python -m pytest tests/test_position_memory.py -q
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import position_memory as pm  # noqa: E402
from Bollinger.position_store import Position as BPos  # noqa: E402
from Luxury.position_store import Position as LPos  # noqa: E402
from Options.position_store import Position as OPos  # noqa: E402
from Swing.position_store import Position as SPos, _now_ist  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "DATA_DIR", tmp_path)
    pm._last_written.clear(); pm._ready.clear(); pm._last_report.clear()
    yield tmp_path
    pm._last_written.clear(); pm._ready.clear(); pm._last_report.clear()


def restart():
    """A new process: nothing in memory, only the file."""
    pm._last_written.clear(); pm._ready.clear(); pm._last_report.clear()


def bpos(entry=50.0, qty=350, **kw):
    d = dict(underlying_symbol="APLAPOLLO", trading_symbol="APLAPOLLO 27 OCT 2200 CALL", resolved_option_type="CE",
             instrument_side="LONG", exchange_segment="NSE_FNO", product_type="MARGIN", quantity=qty, lot_size=350,
             entry_price=entry, best_price=entry, stop_pct=0.05, hard_stop_loss=entry * 0.95, trailing_stop_dist=1.25,
             trailing_step=0.6, pnl_multiplier=qty)
    d.update(kw)
    return BPos(**d)


def spos(side="LONG", entry=100.0, **kw):
    d = dict(underlying_symbol="TITAN", trading_symbol="TITAN 27 OCT 3500 CALL", basket_type="OPTIONS", regime="UNKNOWN",
             instrument_side=side, exchange_segment="NSE_FNO", product_type="MARGIN", quantity=175, lot_size=175,
             entry_price=entry, best_price=entry, target_price=entry * 1.5, hard_stop_loss=entry * 0.8, order_id="",
             pnl_multiplier=175)
    d.update(kw)
    return SPos(**d)


def opos(cls, entry=10.0, **kw):
    d = dict(underlying_symbol="SBIN", option_trading_symbol="SBIN 27 OCT 800 CALL", option_type="CE", quantity=750,
             lot_size=750, entry_price=entry, highest_price=entry, target_price=entry * 1.5, hard_stop_loss=entry * 0.7,
             order_id="", product_type="MARGIN")
    d.update(kw)
    return cls(**d)


def test_1_bollinger_trailing_state_comes_back_after_a_restart(fresh):
    t_open = _now_ist() - timedelta(hours=3)
    live = bpos(best_price=61.7, stop_pct=0.12, hard_stop_loss=44.0, trailing_stop_dist=3.0, trailing_step=1.5,
                trailing_armed=True, trailing_stop_price=58.7, opened_at=t_open, entry_candle_start=t_open)
    pm.record("Bollinger", [live], pm.BOLLINGER_FIELDS)
    assert (fresh / "bollinger_position_memory.json").exists()
    restart()
    rec = bpos(entry=50.05, reconciled=True)          # what the broker reconciliation builds (flat fallback)
    rep = pm.restore("Bollinger", [rec], pm.BOLLINGER_FIELDS)
    assert (rec.best_price, rec.trailing_armed, rec.trailing_stop_price, rec.stop_pct, rec.hard_stop_loss,
            rec.trailing_stop_dist, rec.opened_at, rec.entry_candle_start) == (61.7, True, 58.7, 0.12, 44.0, 3.0, t_open, t_open)
    assert rec.entry_price == 50.05 and rec.quantity == 350          # the broker stays the truth for these
    assert len(rep["restored"]) == 1 and not rep["not_restored"]
    assert pm.last_report("Bollinger") is rep


@pytest.mark.parametrize("broker_pos, why", [
    (dict(entry=54.0), "entry"), (dict(qty=700), "quantity"),
])
def test_2_a_different_position_in_the_same_contract_inherits_nothing(broker_pos, why):
    pm.record("Bollinger", [bpos(best_price=61.7, trailing_armed=True, trailing_stop_price=58.7)], pm.BOLLINGER_FIELDS)
    restart()
    rec = bpos(reconciled=True, **broker_pos)
    rep = pm.restore("Bollinger", [rec], pm.BOLLINGER_FIELDS)
    assert not rec.trailing_armed and rec.best_price == rec.entry_price
    assert why in rep["not_restored"][0]["why"]


def test_3_closed_while_down_is_reported_then_forgotten_but_kept_if_reconciliation_never_ran():
    pm.record("Bollinger", [bpos()], pm.BOLLINGER_FIELDS)
    restart()
    pm.record("Bollinger", [], pm.BOLLINGER_FIELDS)    # monitor tick before any successful restore: keep the memory
    restart()
    assert "APLAPOLLO 27 OCT 2200 CALL" in pm.remembered("Bollinger")
    rep = pm.restore("Bollinger", [], pm.BOLLINGER_FIELDS)
    assert rep["remembered_but_not_at_broker"] == ["APLAPOLLO 27 OCT 2200 CALL"]
    pm.record("Bollinger", [], pm.BOLLINGER_FIELDS)
    assert pm.remembered("Bollinger") == {}


def test_4_best_price_only_moves_in_the_positions_favour():
    pm.record("Bollinger", [bpos(best_price=61.7)], pm.BOLLINGER_FIELDS)
    restart()
    rec = bpos(best_price=63.0, reconciled=True)
    pm.restore("Bollinger", [rec], pm.BOLLINGER_FIELDS)
    assert rec.best_price == 63.0
    restart()
    pm.record("Swing", [spos(side="SHORT", best_price=92.0)], pm.SWING_FIELDS)
    restart()
    short = spos(side="SHORT", reconciled=True)
    pm.restore("Swing", [short], pm.SWING_FIELDS)
    assert short.best_price == 92.0                     # lower is better for a SHORT


def test_5_swing_regime_opened_at_target_and_stop_come_back():
    t2 = _now_ist() - timedelta(days=2)
    pm.record("Swing", [spos(regime="BULLISH", best_price=131.0, target_price=160.0, hard_stop_loss=85.0, opened_at=t2,
                             supertrend_entry_candle_start=t2)], pm.SWING_FIELDS)
    restart()
    r = spos(reconciled=True)
    pm.restore("Swing", [r], pm.SWING_FIELDS)
    assert (r.best_price, r.regime, r.opened_at, r.target_price, r.hard_stop_loss, r.supertrend_entry_candle_start) == \
        (131.0, "BULLISH", t2, 160.0, 85.0, t2)
    restart()
    other_side = spos(side="SHORT", reconciled=True)
    rep = pm.restore("Swing", [other_side], pm.SWING_FIELDS)
    assert other_side.best_price == 100.0 and "side" in rep["not_restored"][0]["why"]


@pytest.mark.parametrize("strategy, cls", [("Options", OPos), ("Luxury", LPos)])
def test_6_options_and_luxury_keep_highest_price_and_entry_context(strategy, cls):
    t0 = datetime.now() - timedelta(hours=2)
    pm.record(strategy, [opos(cls, highest_price=14.2, target_price=13.0, hard_stop_loss=8.5, opened_at=t0,
                              supertrend_entry_candle_start=t0, entry_underlying_price=801.5)], pm.OPTIONS_FIELDS)
    assert "SBIN 27 OCT 800 CALL" in pm.remembered(strategy)          # keyed by the contract
    restart()
    rec = opos(cls, entry=10.05, target_price=15.0, hard_stop_loss=7.0, reconciled=True)
    rep = pm.restore(strategy, [rec], pm.OPTIONS_FIELDS)
    assert (rec.highest_price, rec.opened_at, rec.supertrend_entry_candle_start, rec.entry_underlying_price,
            rec.target_price, rec.hard_stop_loss, rec.entry_price) == (14.2, t0, t0, 801.5, 13.0, 8.5, 10.05)
    assert len(rep["restored"]) == 1


def test_7_writes_only_on_change_and_survives_a_corrupt_file(fresh):
    p = spos(regime="BULLISH")
    pm.record("Swing", [p], pm.SWING_FIELDS)
    f = fresh / "swing_position_memory.json"
    m1 = f.stat().st_mtime_ns
    pm.record("Swing", [p], pm.SWING_FIELDS)
    assert f.stat().st_mtime_ns == m1
    restart()
    f.write_text("{not json")
    pm.restore("Swing", [spos(reconciled=True)], pm.SWING_FIELDS)
    pm.record("Swing", [p], pm.SWING_FIELDS)
    assert "TITAN 27 OCT 3500 CALL" in json.loads(f.read_text())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
