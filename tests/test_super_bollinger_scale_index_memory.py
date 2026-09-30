"""
Tests for three Super Bollinger additions of 30 Sep 2026:
  - SuperBollinger/scale.py - the scale-in PAPER variant; tests 1-6 rewritten
    1 Oct 2026 for the rules live since 30 Sep 18:54: S1 = one more call when
    the stock is back at its trigger after the dip (own 4,500, exits with the
    original), S2 = a second PUT lot on Supertrend-bearish, one sold when both
    together reach +4,000, the other kept above its cost on the hedge trail;
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
                 ("hedge_trail_giveback", 0.30), ("scale_pe_add_stop_rs", 750.0), ("scale_pe_target_rs", 4000.0),
                 ("scale_ce_readd_confirm", True), ("entry_cutoff_time", "14:00"), ("max_loss_rs", 4500.0),
                 ("square_off_time", "15:15")):
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
# Scale-in variant (S1 + S2, rewritten 1 Oct 2026 for the rules live since 30 Sep 18:54): pure rules
# --------------------------------------------------------------------------- #
def test_1_s2_pe_wave_decision_order_of_exits():
    """S2: lot A = the hedge copy (53.10), lot B added at 58.00, 350 each. Order: kept-lot floor, hedge stop,
    B's own stop, the combined target (sells B), then the hedge trail."""
    def D(ltp, a_best, b_entry=58.0, booked=False):
        return sc.pe_wave_decision(ltp, 350, 53.10, a_best, b_entry, 350 if b_entry else 0, booked,
                                   1500, 750, 4000, 1000, 0.30)
    assert D(48.8, 55.0) == ("ALL", "HEDGE_STOP")                     # A -1,505
    assert D(55.8, 56.0) == ("B", "ADD_OWN_STOP")                     # B -770, A +945
    assert D(61.3, 61.3) == ("B", "TARGET_BOOKED")                    # A +2,870 + B +1,155 = +4,025
    assert D(61.2, 61.2) == (None, None)
    assert D(53.0, 65.0, b_entry=None, booked=True) == ("ALL", "KEPT_LOT_FLOOR")   # the kept lot never goes below cost
    assert D(61.4, 65.0, b_entry=None, booked=True) == ("ALL", "HEDGE_TRAIL")      # peak +4,165, 30% given back
    assert D(61.5, 65.0, b_entry=None, booked=True) == (None, None)
    assert D(54.0, 55.0, b_entry=None) == (None, None)               # B already stopped: only the hedge rules


def test_1b_s2_target_level_is_where_both_lots_together_make_the_target():
    """The combined +4,000 on two 350-lots from 53.10 and 58.00 is reached at 61.264..."""
    level = (4000 / 350 + 53.10 + 58.0) / 2
    assert sc.pe_wave_decision(level + 0.001, 350, 53.10, level, 58.0, 350, False, 1500, 750, 4000, 1000, 0.3) == ("B", "TARGET_BOOKED")
    assert sc.pe_wave_decision(level - 0.01, 350, 53.10, level, 58.0, 350, False, 1500, 750, 4000, 1000, 0.3) == (None, None)


def test_2_s1_readd_rule():
    """S1: only after the dip (hedge trigger seen) and only once the STOCK is back at its entry trigger."""
    assert sc.ce_readd_due(True, 2200.5, 2200.0) and sc.ce_readd_due(True, 2200.0, 2200.0)
    assert not sc.ce_readd_due(True, 2199.9, 2200.0)
    assert not sc.ce_readd_due(False, 2210.0, 2200.0)                 # no dip, no re-add
    assert not sc.ce_readd_due(True, None, 2200.0) and not sc.ce_readd_due(True, 2210.0, None)


# --------------------------------------------------------------------------- #
# Scale-in variant: flows
# --------------------------------------------------------------------------- #
def test_3_s1_one_more_call_when_the_stock_is_back_at_its_trigger_after_the_dip(env, monkeypatch):
    async def run():
        ce = _pos("AAA", "AAA CE", "CE", 77.05, 350)
        position_store.live_positions["AAA"] = ce
        track = {"hedged": False, "entry_spot": 2200.0}
        await sc.on_ce_price("AAA", ce, 72.0, True, track, 2201.0)
        assert "AAA" not in sc.ce_add_book.positions                   # never dipped to the hedge trigger
        track["hedged"] = True
        await sc.on_ce_price("AAA", ce, 70.0, True, track, 2199.0)
        assert "AAA" not in sc.ce_add_book.positions                   # stock still below its trigger
        env.update(d=-1)
        await sc.on_ce_price("AAA", ce, 71.0, True, track, 2200.5)
        assert "AAA" not in sc.ce_add_book.positions                   # Supertrend not bullish (confirmation on)
        env.update(d=1)
        await sc.on_ce_price("AAA", ce, 71.5, True, track, 2200.5)
        add = sc.ce_add_book.positions["AAA"]
        assert (add.entry_price, add.quantity) == (71.5, 350)
        await sc.on_ce_price("AAA", ce, 73.0, True, track, 2203.0)
        assert len([k for k in sc._st()["ce_done"] if k.startswith("AAA|")]) == 1      # never a second re-add
        assert sc._st()["ce_parent"]["AAA"] == {"ts": "AAA CE", "entry": 77.05, "real": True}
        late = _pos("LLL", "LLL CE", "CE", 50.0, 100)
        monkeypatch.setattr(engine, "_now_ist", lambda: NOW.replace(hour=14, minute=5))
        await sc.on_ce_price("LLL", late, 49.0, True, {"hedged": True, "entry_spot": 100.0}, 101.0)
        assert "LLL" not in sc.ce_add_book.positions                   # no re-add from the 14:00 entry cutoff
    asyncio.run(run())


def test_4_s1_extra_call_exits_with_the_original_or_on_its_own_max_loss(env, monkeypatch):
    async def run():
        track = {"hedged": True, "entry_spot": 100.0}
        ce = _pos("BBB", "BBB CE", "CE", 100.0, 100)
        position_store.live_positions["BBB"] = ce
        await sc.on_ce_price("BBB", ce, 96.0, True, track, 100.5)
        del position_store.live_positions["BBB"]                        # the original is stopped at breakeven
        ce.exit_price, ce.exit_reason = 100.0, "BREAKEVEN_STOP_HIT"
        position_store.closed_positions_today.append(ce)
        await sc._manage_ce_add("BBB", 99.0)
        t = [x for x in _scale_trades() if x["underlying_symbol"] == "BBB"][-1]
        assert (t["exit_price"], t["exit_reason"], t["pnl_raw"]) == (100.0, "WITH_ORIGINAL_BREAKEVEN_STOP_HIT", 400.0)

        ce2 = _pos("CCC", "CCC CE", "CE", 80.0, 350)
        position_store.live_positions["CCC"] = ce2
        await sc.on_ce_price("CCC", ce2, 70.0, True, {"hedged": True, "entry_spot": 50.0}, 50.0)
        await sc._manage_ce_add("CCC", 57.2)
        assert "CCC" in sc.ce_add_book.positions                        # -4,480: not yet
        await sc._manage_ce_add("CCC", 57.1)
        t = [x for x in _scale_trades() if x["underlying_symbol"] == "CCC"][-1]
        assert t["exit_reason"] == "ADD_MAX_LOSS"                       # its own 4,500 while the original is still open

        ce3 = _pos("DDD", "DDD CE", "CE", 40.0, 100)
        position_store.live_positions["DDD"] = ce3
        await sc.on_ce_price("DDD", ce3, 39.0, True, {"hedged": True, "entry_spot": 10.0}, 10.0)
        monkeypatch.setattr(engine, "_now_ist", lambda: NOW.replace(hour=15, minute=15))
        await sc._manage_ce_add("DDD", 41.0)
        assert [x for x in _scale_trades() if x["underlying_symbol"] == "DDD"][-1]["exit_reason"] == "DAILY_SQUARE_OFF"
    asyncio.run(run())


def test_5_s2_second_put_lot_one_sold_at_the_combined_target_the_other_kept_above_cost(env):
    async def run():
        hedge = _pos("APL", "APL PE", "PE", 53.10, 350)
        hedge.best_price = 55.0
        await sc.on_hedge_price("APL", hedge, 58.0, True)
        assert "APL" not in sc.pe_add_book.positions                    # Supertrend still bullish
        env.update(d=-1, close=NOW.timestamp())
        await sc.on_hedge_price("APL", hedge, 58.0, True)
        a, b = sc.pe_copy_book.positions["APL"], sc.pe_add_book.positions["APL"]
        assert (a.entry_price, a.best_price, b.entry_price) == (53.10, 55.0, 58.0)
        assert sc._st()["pairs"]["APL"] == {"booked": False}
        await sc.on_hedge_price("APL", hedge, 58.5, True)
        assert len([k for k in sc._st()["pe_done"] if k.startswith("APL|")]) == 1      # one second lot per hedge
        await sc._manage_pe("APL", 61.2)
        assert "APL" in sc.pe_add_book.positions
        await sc._manage_pe("APL", 61.3)
        assert "APL" not in sc.pe_add_book.positions and "APL" in sc.pe_copy_book.positions
        assert sc._st()["pairs"]["APL"]["booked"] is True
        booked = [t for t in _scale_trades() if t["underlying_symbol"] == "APL"][-1]
        assert booked["exit_reason"] == "TARGET_BOOKED"
        events = [json.loads(line) for f in sorted(Path(trade_history.HISTORY_DIR).glob("*_super_bollinger_scale.log"))
                  for line in f.read_text().splitlines()]
        closed = [e for e in events if e["event"] == "SCALE_PE_CLOSED" and e.get("exit_reason") == "TARGET_BOOKED"][-1]
        assert closed["rule_level"] == round((4000 / 350 + 53.10 + 58.0) / 2, 2) and closed["fill_minus_level"] > 0
        sc._state = None                                                # a restart keeps the bookkeeping
        assert sc._st()["pairs"]["APL"]["booked"] is True
        await sc._manage_pe("APL", 53.0)
        assert "APL" not in sc.pe_copy_book.positions                   # the kept lot is never let below its cost
        assert [t for t in _scale_trades() if t["underlying_symbol"] == "APL"][-1]["exit_reason"] == "KEPT_LOT_FLOOR"
    asyncio.run(run())


def test_6_shadow_and_off_modes_open_nothing(env, monkeypatch):
    async def run():
        ce = _pos("CCC", "CCC CE", "CE", 100.0, 100)
        track = {"hedged": True, "entry_spot": 100.0}
        monkeypatch.setitem(settings._overrides, "scale_mode", "off")
        await sc.on_ce_price("CCC", ce, 99.0, True, track, 101.0)
        assert not sc.ce_add_book.positions and not sc._st()["ce_done"]
        monkeypatch.setitem(settings._overrides, "scale_mode", "shadow")
        await sc.on_ce_price("CCC", ce, 99.0, True, track, 101.0)
        assert not sc.ce_add_book.positions and sc._st()["ce_done"]     # decided + logged, no position
        with pytest.raises(ValueError):
            settings._parse_scale_mode("real")                          # real is not built
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
