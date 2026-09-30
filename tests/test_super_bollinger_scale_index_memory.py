"""
Tests for three Super Bollinger additions of 30 Sep 2026:
  - SuperBollinger/scale.py - the scale-in PAPER variant (CE add at +1,500 sold
    on a Supertrend flip; PE add on Supertrend-bearish with the pair-recovery
    exit);
  - SuperBollinger/best_price_memory.py - best price kept across a restart;
  - the permanent index symbols (NIFTY/BANKNIFTY) with their own on/off flag
    and paper/real toggle "SuperBollingerIndex".
Fully offline; files go to a temp directory.

HOW TO RUN:
    uv run python -m pytest tests/test_super_bollinger_scale_index_memory.py -q
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import paper_mode_control as pmc  # noqa: E402
import SuperBollinger.scale as sc  # noqa: E402
import SuperBollinger.trading_engine as te  # noqa: E402
import trade_history  # noqa: E402
from Bollinger import trading_engine as engine  # noqa: E402
from Bollinger.position_store import Position  # noqa: E402
from Options.dhan_client import IST, dhan_wrapper  # noqa: E402
from SuperBollinger import best_price_memory as bpm, settings  # noqa: E402
from SuperBollinger.state import position_store  # noqa: E402

NOW = datetime(2026, 9, 30, 11, 0, tzinfo=IST)


def _pos(sym, ts, ot, entry, qty):
    p = Position(underlying_symbol=sym, trading_symbol=ts, resolved_option_type=ot, instrument_side="LONG",
                 exchange_segment="NSE_FNO", product_type="MARGIN", quantity=qty, lot_size=qty, entry_price=entry,
                 best_price=entry, stop_pct=0.0, hard_stop_loss=0.05, trailing_stop_dist=1e12, trailing_step=1e12,
                 pnl_multiplier=qty, order_id="X")
    p.opened_at = NOW
    return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engine, "_now_ist", lambda: NOW)
    monkeypatch.setattr(dhan_wrapper, "subscribe_option_price", lambda *_a, **_k: None)
    st = {"d": 1, "close": None}

    async def fake_st(_symbol):
        return st["d"], st["close"]
    monkeypatch.setattr(sc, "supertrend_now", fake_st)
    monkeypatch.setattr(settings, "_overrides_loaded", True)
    for k, v in (("scale_mode", "paper"), ("hedge_stop_rs", 1500.0), ("hedge_trail_arm_rs", 1000.0),
                 ("hedge_trail_giveback", 0.4), ("scale_pe_add_stop_rs", 750.0), ("scale_ce_add_at_rs", 1500.0),
                 ("scale_ce_add_st_exit", True), ("scale_ce_add_cutoff_time", "14:00"),
                 ("scale_pe_add_cutoff_time", "14:30"), ("square_off_time", "15:15")):
        monkeypatch.setitem(settings._overrides, k, v)
    for book in sc.BOOKS:
        book.positions.clear()
    sc._state = None
    bpm._mem = None
    position_store.live_positions.clear()
    position_store.closed_positions_today.clear()
    yield st
    for book in sc.BOOKS:
        book.positions.clear()
    sc._state = None
    bpm._mem = None
    position_store.live_positions.clear()
    position_store.closed_positions_today.clear()


def _scale_trades():
    f = sorted(Path(trade_history.HISTORY_DIR).glob("*_super_bollinger_scale_paper_trades.log"))
    return [json.loads(line) for line in f[-1].read_text().splitlines()] if f else []


# --------------------------------------------------------------------------- #
# Scale-in variant: pure rules
# --------------------------------------------------------------------------- #
def test_1_pe_pair_decision_order_of_exits():
    D = sc.pe_pair_decision
    args = (1500, 750, 1000, 0.4)
    assert D(48.8, 350, 53.10, 61.70, 58.0, 0, -4585, *args) == ("ALL", "HEDGE_STOP")
    assert D(55.80, 350, 53.10, 61.70, 58.0, 0, -4585, *args) == ("B", "ADD_OWN_STOP")
    # CE -4,585 is recovered at 62.10: A +3,150, B +1,435
    assert D(62.10, 350, 53.10, 62.10, 58.0, 0, -4585, *args) == ("ALL", "PAIR_RECOVERED")
    assert D(62.00, 350, 53.10, 62.00, 58.0, 0, -4585, *args) == (None, None)
    # B already stopped (-750): A's trail (best 61.70 -> exit at/below 58.26) closes what is left
    assert D(58.20, 350, 53.10, 61.70, None, -750, -4585, *args) == ("ALL", "HEDGE_TRAIL")
    # no CE PnL known -> never "recovered", only the hedge's own rules
    assert D(70.0, 350, 53.10, 70.0, 58.0, 0, None, *args) == (None, None)


def test_2_ce_add_rules():
    assert sc.ce_add_due(77.05, 81.35, 350, 1500) and not sc.ce_add_due(77.05, 81.0, 350, 1500)
    assert sc.ce_add_st_exit_due(-1, 1000.0, 999.0)
    assert not sc.ce_add_st_exit_due(-1, 999.0, 999.0)       # the bar closed before the add
    assert not sc.ce_add_st_exit_due(1, 1000.0, 999.0)


# --------------------------------------------------------------------------- #
# Scale-in variant: flows
# --------------------------------------------------------------------------- #
def test_3_ce_add_once_at_1500_profit_then_sold_on_a_supertrend_flip_after_the_add(env):
    async def run():
        ce = _pos("AAA", "AAA CE", "CE", 77.05, 350)
        position_store.live_positions["AAA"] = ce
        await sc.on_ce_price("AAA", ce, 80.0, True)
        assert "AAA" not in sc.ce_add_book.positions
        await sc.on_ce_price("AAA", ce, 81.40, True)
        assert sc.ce_add_book.positions["AAA"].entry_price == 81.40
        await sc.on_ce_price("AAA", ce, 83.0, True)
        assert len([k for k in sc._st()["ce_done"] if k.startswith("AAA|")]) == 1      # never a second add
        added_at = sc.ce_add_book.positions["AAA"].opened_at.timestamp()
        env.update(d=-1, close=added_at - 10)
        await sc._manage_ce_add("AAA", 82.0)
        assert "AAA" in sc.ce_add_book.positions                                         # that bar closed before the add
        env.update(d=-1, close=added_at + 60)
        await sc._manage_ce_add("AAA", 82.5)
        assert "AAA" not in sc.ce_add_book.positions
        assert _scale_trades()[-1]["exit_reason"] == "ADD_SUPERTREND_BEARISH"
    asyncio.run(run())


def test_4_ce_add_exits_with_the_original_at_its_exit_price(env):
    async def run():
        ce = _pos("BBB", "BBB CE", "CE", 100.0, 100)
        position_store.live_positions["BBB"] = ce
        await sc.on_ce_price("BBB", ce, 116.0, True)
        del position_store.live_positions["BBB"]
        ce.exit_price, ce.exit_reason = 100.0, "BREAKEVEN_STOP_HIT"
        position_store.closed_positions_today.append(ce)
        await sc._manage_ce_add("BBB", 99.0)
        t = [x for x in _scale_trades() if x["underlying_symbol"] == "BBB"][-1]
        assert (t["exit_price"], t["exit_reason"], t["pnl_raw"]) == (100.0, "WITH_ORIGINAL_BREAKEVEN_STOP_HIT", -1600.0)
    asyncio.run(run())


def test_5_pe_add_on_supertrend_bearish_and_both_lots_out_when_the_pair_loss_is_recovered(env):
    async def run():
        ce = _pos("APL", "APL CE", "CE", 77.05, 350)
        ce.exit_price, ce.exit_reason = 63.95, "STOP_LOSS_HIT"                         # CE already closed: -4,585
        position_store.closed_positions_today.append(ce)
        hedge = _pos("APL", "APL PE", "PE", 53.10, 350)
        hedge.best_price = 61.70
        await sc.on_hedge_price("APL", hedge, 58.0, True)
        assert "APL" not in sc.pe_add_book.positions                                   # Supertrend still bullish
        env.update(d=-1, close=NOW.timestamp())
        await sc.on_hedge_price("APL", hedge, 58.0, True)
        a, b = sc.pe_copy_book.positions["APL"], sc.pe_add_book.positions["APL"]
        assert (a.entry_price, a.best_price, b.entry_price) == (53.10, 61.70, 58.0)
        assert round(sc._st()["pairs"]["APL"]["ce_realized"]) == -4585
        await sc.on_hedge_price("APL", hedge, 58.5, True)
        assert len([k for k in sc._st()["pe_done"] if k.startswith("APL|")]) == 1
        await sc._manage_pe("APL", 60.0)
        assert "APL" in sc.pe_copy_book.positions
        await sc._manage_pe("APL", 62.15)
        assert "APL" not in sc.pe_copy_book.positions and "APL" not in sc.pe_add_book.positions
        assert {t["exit_reason"] for t in _scale_trades() if t["underlying_symbol"] == "APL"} == {"PAIR_RECOVERED"}
        sc._state = None                                                                # a restart keeps the bookkeeping
        assert "APL" in sc._st()["pairs"] and sc._st()["pe_done"]
    asyncio.run(run())


def test_6_shadow_and_off_modes_open_nothing(env, monkeypatch):
    async def run():
        ce = _pos("CCC", "CCC CE", "CE", 100.0, 100)
        monkeypatch.setitem(settings._overrides, "scale_mode", "off")
        await sc.on_ce_price("CCC", ce, 120.0, True)
        assert not sc.ce_add_book.positions and not sc._st()["ce_done"]
        monkeypatch.setitem(settings._overrides, "scale_mode", "shadow")
        await sc.on_ce_price("CCC", ce, 120.0, True)
        assert not sc.ce_add_book.positions and sc._st()["ce_done"]                    # decided + logged, no position
        with pytest.raises(ValueError):
            settings._parse_scale_mode("real")                                          # real is not built
    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Best-price memory
# --------------------------------------------------------------------------- #
def test_7_best_price_survives_a_restart_only_for_the_same_trade(env):
    h = _pos("APL", "APL PE", "PE", 53.10, 350)
    h.best_price = 61.70
    bpm.record("SuperBollingerHedge", [h])
    bpm._mem = None                                                                     # new process
    same = _pos("APL", "APL PE", "PE", 53.10, 350)
    assert bpm.restore("SuperBollingerHedge", [same]) and same.best_price == 61.70
    other_entry = _pos("APL", "APL PE", "PE", 55.0, 350)
    bpm.restore("SuperBollingerHedge", [other_entry])
    assert other_entry.best_price == 55.0                                               # a different trade in that contract
    other_book = _pos("APL", "APL PE", "PE", 53.10, 350)
    bpm.restore("SuperBollinger", [other_book])
    assert other_book.best_price == 53.10
    lower = _pos("APL", "APL PE", "PE", 53.10, 350)
    lower.best_price = 58.85
    bpm.record("SuperBollingerHedge", [lower])                                          # never lowers what it holds
    assert json.loads(bpm.FILE.read_text())["SuperBollingerHedge|APL PE"]["best"] == 61.70


# --------------------------------------------------------------------------- #
# Permanent index symbols
# --------------------------------------------------------------------------- #
def test_8_index_symbols_sit_outside_the_weekly_watchlist_and_follow_their_own_flags(env, monkeypatch):
    async def run():
        monkeypatch.setattr(dhan_wrapper, "is_mcx_commodity", lambda s: s in ("COPPER", "NATURALGAS"))

        async def weekly():
            return ["SAIL", "TITAN", "NIFTY", "COPPER"], "test"       # a stray index in the file is not how it is traded
        monkeypatch.setattr(te.super_watchlist, "symbols", weekly)
        monkeypatch.setitem(settings._overrides, "excluded_symbols", [])
        monkeypatch.setitem(settings._overrides, "index_symbols", ["BANKNIFTY", "NIFTY"])
        monkeypatch.setitem(settings._overrides, "index_enabled", False)
        assert await te.eligible_symbols() == ["SAIL", "TITAN"] and not te.is_eligible_symbol("NIFTY")
        monkeypatch.setitem(settings._overrides, "index_enabled", True)
        assert await te.eligible_symbols() == ["SAIL", "TITAN", "BANKNIFTY", "NIFTY"]
        monkeypatch.setitem(settings._overrides, "index_symbols", ["NIFTY"])
        assert await te.eligible_symbols() == ["SAIL", "TITAN", "NIFTY"]
        monkeypatch.setitem(settings._overrides, "excluded_symbols", ["NIFTY"])
        assert await te.eligible_symbols() == ["SAIL", "TITAN"]
        _parsed, errors = settings.validate({"index_symbols": "NIFTY,SAIL"})
        assert errors and "not an index symbol" in errors[0]
    asyncio.run(run())


def test_9_index_paper_toggle_is_independent_of_the_stocks(env, monkeypatch):
    monkeypatch.setattr(pmc, "_overrides_loaded", True)
    monkeypatch.setattr(pmc, "_overrides", {"SuperBollinger": False, "SuperBollingerIndex": True})
    assert te.is_paper_symbol("NIFTY") and te.is_paper_symbol("BANKNIFTY") and not te.is_paper_symbol("SAIL")
    monkeypatch.setattr(pmc, "_overrides", {"SuperBollinger": True, "SuperBollingerIndex": False})
    assert not te.is_paper_symbol("NIFTY") and te.is_paper_symbol("SAIL")
    assert "SuperBollingerIndex" in pmc.STRATEGIES
    assert pmc._ENV_VAR_NAMES["SuperBollingerIndex"] == "SUPER_BOLLINGER_INDEX_PAPER_MODE_ENABLED"
    monkeypatch.setattr(pmc, "_overrides", {})
    assert pmc.is_paper_mode_enabled("SuperBollingerIndex") == settings.INDEX_PAPER_MODE_ENABLED_DEFAULT


def test_10_hedge_defaults_are_the_live_decisions_of_30_sep():
    d = settings._FIELDS
    assert d["hedge_trigger_rs"][2] == "1800" and d["hedge_trail_giveback"][2] == "0.30"
    assert d["hedge_stop_rs"][2] == "1500" and d["hedge_cutoff_time"][2] == "15:15"     # no time cutoff
    assert d["entry_retry_max"][2] == "2" and d["entry_chase_max_pct"][2] == "5"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
