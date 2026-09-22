"""
Tests for the new global liquid-contract-resolution gate
(dhan_client.get_liquid_atm_option) - added 18 Sep 2026 after a real
incident: ATHERENERG 29 SEP 1540 PUT's broker-side stop-loss was
REJECTED with "EXCH:17181: Contract not traded. Market order not
allowed" because the natural ATM strike had never printed a single
trade before the position was already opened. User request: a single
shared function all 4 live-trading packages (Options/Futures/Luxury/
Swing) route their contract resolution through, that (1) refuses a
currently-illiquid or never-recently-traded strike, and (2) substitutes
a nearby, actively-traded strike instead of just blocking the entry.

Covers, against the REAL production functions (not reimplemented):
  1. get_daily_volume_sum sums a successful daily-history fetch's volume
     column, and returns None (not zero, not an exception) on failure.
  2. _nearby_option_candidates filters/sorts a realistic instrument-
     master DataFrame correctly: same underlying/expiry/option_type
     only, sorted by distance from the ATM strike, ATM itself first.
  3. get_liquid_atm_option: the ATM strike passing both checks returns
     it unchanged, with zero candidate-search overhead.
  4. get_liquid_atm_option: the ATM strike failing the CURRENT-session
     liquidity check correctly substitutes the nearest passing strike.
  5. get_liquid_atm_option: the ATM strike failing the PRIOR-session
     volume check (the actual ATHERENERG shape) correctly substitutes
     the nearest passing strike.
  6. get_liquid_atm_option: no candidate within the search window
     passing returns None - never silently falls back to the raw ATM.
  7. config.LIQUID_CONTRACT_GATE_ENABLED=False bypasses everything,
     returning the plain ATM pick with zero liquidity/volume calls.
  8. An MCX underlying (Swing's Copper) bypasses the two checks
     entirely (deliberately out of scope - see the function's own
     docstring) - plain ATM passthrough, zero liquidity/volume calls.
  9. The "not authenticated" bypass (self._client is None) - the same
     safety net used everywhere else in this test suite - returns the
     plain ATM pick without attempting a real fetch, so this new gate
     never breaks a test that never authenticates.
  10-13. Full integration, one per live-trading package (Futures,
      Options, Luxury, Swing): the package's own real entry function
      returns {"status": "skipped", "reason": "no_liquid_contract_
      available"} and places ZERO orders when get_liquid_atm_option
      finds nothing tradeable - proving the new skip path is correctly
      wired end to end in EVERY package's own copy of the entry flow,
      not just present in isolation (each package keeps its own
      trading_engine.py, so a wiring mistake in one wouldn't be caught
      by another's tests).
  14-16. Added 22 Sep 2026, real incident: get_atm_option RAISING
      MissingOptionLegError (Tradehull's ATM_Strike_Selection computed a
      strike but had no trading_symbol for that specific option_type
      there - happened live for COPPER CE at strike 1415) used to
      propagate straight out of get_liquid_atm_option, aborting the
      WHOLE liquid-contract search before it ever got a chance to try a
      nearby strike - defeating this function's entire purpose for
      exactly the failure shape it was built to route around. 14: falls
      back to _nearest_listed_expiry (read directly from the instrument
      master, independent of Tradehull) + the same nearby-strike search
      every other failure mode already gets, seeded from the strike
      Tradehull DID successfully compute. 15: no listed expiry at all
      (a genuinely dead option chain) returns None cleanly. 16: gate
      disabled preserves the OLD behavior exactly - the exception still
      propagates, never silently swallowed.

HOW TO RUN:
    uv run python tests/test_liquid_contract_resolution.py
"""
import asyncio
import os
import sys
import tempfile
import types
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_liquid_contract_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
from Options.dhan_client import AtmOption, DhanWrapper, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def make_atm(strike: float, trading_symbol: str = None) -> AtmOption:
    ts = trading_symbol or f"TESTSTOCK 29 SEP {strike:g} CALL"
    return AtmOption(trading_symbol=ts, strike=strike, option_type="CE", lot_size=500,
                      security_id=f"SECID-{strike:g}", expiry_date=FUTURE_EXPIRY)


def test_1_get_daily_volume_sum_sums_success_and_returns_none_on_failure():
    wrapper = DhanWrapper.__new__(DhanWrapper)

    class FakeDhan:
        def historical_daily_data(self, **kwargs):
            return {"data": {"volume": [100.0, 250.0, 0.0, 375.0]}}

    wrapper._client = types.SimpleNamespace(Dhan=FakeDhan())
    result = wrapper.get_daily_volume_sum("SECID-1", "NSE_FNO", "OPTSTK", lookback_days=7)
    assert result == 725.0, f"expected the summed volume 725.0, got {result}"
    print("1a. get_daily_volume_sum correctly sums a successful daily-history fetch's volume column: PASSED")

    class FailingDhan:
        def historical_daily_data(self, **kwargs):
            raise RuntimeError("simulated Dhan API failure")

    wrapper._client = types.SimpleNamespace(Dhan=FailingDhan())
    result = wrapper.get_daily_volume_sum("SECID-1", "NSE_FNO", "OPTSTK", lookback_days=7)
    assert result is None, f"expected None (not zero, not an exception) on fetch failure, got {result}"
    print("1b. get_daily_volume_sum returns None (never zero, never raises) on a fetch failure: PASSED")


def test_2_nearby_option_candidates_filters_and_sorts_correctly():
    wrapper = DhanWrapper.__new__(DhanWrapper)
    import pandas as pd

    rows = []
    # Same underlying, CE, correct expiry - the real candidate pool.
    for strike in [1400, 1450, 1500, 1540, 1580, 1620, 1660]:
        rows.append({
            "SEM_EXM_EXCH_ID": "NSE", "SEM_CUSTOM_SYMBOL": f"TESTSTOCK 29 SEP {strike} CALL",
            "SEM_EXPIRY_DATE": FUTURE_EXPIRY.isoformat(), "SEM_OPTION_TYPE": "CE",
            "SEM_STRIKE_PRICE": strike, "SEM_LOT_UNITS": "500", "SEM_SMST_SECURITY_ID": 10000 + strike,
        })
    # A PE row at the same strike - must NOT be picked up for a CE search.
    rows.append({
        "SEM_EXM_EXCH_ID": "NSE", "SEM_CUSTOM_SYMBOL": "TESTSTOCK 29 SEP 1540 PUT",
        "SEM_EXPIRY_DATE": FUTURE_EXPIRY.isoformat(), "SEM_OPTION_TYPE": "PE",
        "SEM_STRIKE_PRICE": 1540, "SEM_LOT_UNITS": "500", "SEM_SMST_SECURITY_ID": 99999,
    })
    # A different underlying sharing a prefix - must NOT collide (the
    # trailing-space match this function uses over Tradehull's own bare
    # .startswith is exactly what guards against this).
    rows.append({
        "SEM_EXM_EXCH_ID": "NSE", "SEM_CUSTOM_SYMBOL": "TESTSTOCKPLUS 29 SEP 1540 CALL",
        "SEM_EXPIRY_DATE": FUTURE_EXPIRY.isoformat(), "SEM_OPTION_TYPE": "CE",
        "SEM_STRIKE_PRICE": 1540, "SEM_LOT_UNITS": "500", "SEM_SMST_SECURITY_ID": 88888,
    })
    # Same underlying/type but a DIFFERENT (wrong) expiry - must be excluded.
    rows.append({
        "SEM_EXM_EXCH_ID": "NSE", "SEM_CUSTOM_SYMBOL": "TESTSTOCK 27 OCT 1540 CALL",
        "SEM_EXPIRY_DATE": (FUTURE_EXPIRY + timedelta(days=28)).isoformat(), "SEM_OPTION_TYPE": "CE",
        "SEM_STRIKE_PRICE": 1540, "SEM_LOT_UNITS": "500", "SEM_SMST_SECURITY_ID": 77777,
    })
    wrapper.instruments = lambda: pd.DataFrame(rows)

    atm = make_atm(1540.0, "TESTSTOCK 29 SEP 1540 CALL")
    candidates = wrapper._nearby_option_candidates("TESTSTOCK", "CE", atm, max_search=3)

    symbols = [c.trading_symbol for c in candidates]
    assert "TESTSTOCK 29 SEP 1540 PUT" not in symbols, "a PE row must never appear in a CE search"
    assert "TESTSTOCKPLUS 29 SEP 1540 CALL" not in symbols, \
        "a different underlying sharing a name prefix must never collide"
    assert "TESTSTOCK 27 OCT 1540 CALL" not in symbols, "a different expiry must never appear"
    assert candidates[0].strike == 1540.0, f"the ATM strike itself must sort first, got {candidates[0].strike}"
    strikes = [c.strike for c in candidates]
    assert strikes == sorted(strikes, key=lambda s: abs(s - 1540.0)), \
        f"candidates must be sorted by distance from ATM, got {strikes}"
    print(f"2. _nearby_option_candidates filters out wrong option_type/underlying/expiry rows and sorts "
          f"the remaining {len(candidates)} candidates by distance from ATM (ATM first): PASSED")


def _wrapper_with_gate_active():
    """A DhanWrapper configured to exercise the REAL gate logic (not the
    not-authenticated bypass) - _client set to a non-None sentinel, same
    trick used by this suite wherever a check's "is this even running"
    branch needs to be proven active rather than silently bypassed."""
    wrapper = DhanWrapper.__new__(DhanWrapper)
    wrapper._client = object()
    wrapper._liquidity_cache = {}
    return wrapper


def test_3_atm_passing_both_checks_returns_it_unchanged():
    wrapper = _wrapper_with_gate_active()
    atm = make_atm(1540.0)
    wrapper.get_atm_option = lambda sym, ot: atm
    wrapper._is_mcx_commodity = lambda sym: False
    calls = {"n": 0}

    def fake_is_liquid_and_active(candidate, is_mcx=False):
        calls["n"] += 1
        return True  # ATM passes immediately

    wrapper._is_contract_liquid_and_active = fake_is_liquid_and_active
    wrapper._nearby_option_candidates = lambda *a, **k: [atm, make_atm(1580.0)]

    odc.config.LIQUID_CONTRACT_GATE_ENABLED = True
    result = wrapper.get_liquid_atm_option("TESTSTOCK", "CE")
    assert result is atm, "the ATM candidate must be returned as-is when it passes"
    assert calls["n"] == 1, f"only the ATM candidate should ever be checked once it passes, got {calls['n']} calls"
    print("3. The ATM strike passing both liquidity checks is returned unchanged with no further "
          "candidate search: PASSED")


def test_4_atm_fails_current_session_liquidity_substitutes_nearby():
    wrapper = _wrapper_with_gate_active()
    atm = make_atm(1540.0)
    nearby = make_atm(1580.0)
    wrapper.get_atm_option = lambda sym, ot: atm
    wrapper._is_mcx_commodity = lambda sym: False
    wrapper._nearby_option_candidates = lambda *a, **k: [atm, nearby]

    def fake_check(candidate, is_mcx=False):
        return candidate.strike != 1540.0  # ATM (1540) fails, 1580 passes

    wrapper._is_contract_liquid_and_active = fake_check
    odc.config.LIQUID_CONTRACT_GATE_ENABLED = True
    result = wrapper.get_liquid_atm_option("TESTSTOCK", "CE")
    assert result is nearby, f"expected the substitute 1580 strike, got {result}"
    print("4. The ATM strike failing the current-session liquidity check correctly substitutes the "
          "nearest passing strike instead of blocking the entry: PASSED")


def test_5_atm_fails_prior_session_volume_substitutes_nearby():
    """Same mechanism as test 4, but isolates _is_contract_liquid_and_active
    itself so the prior-session-volume branch specifically is exercised -
    the exact ATHERENERG shape (current-session check alone would have
    said nothing, since the contract genuinely hadn't traded yet)."""
    wrapper = _wrapper_with_gate_active()
    atm = make_atm(1540.0)
    nearby = make_atm(1580.0)
    wrapper.refresh_liquidity_signal = lambda ts: None
    wrapper.get_cached_illiquid = lambda ts: False  # never confirmed illiquid this session

    def fake_volume(security_id, seg, itype, lookback_days):
        return 0.0 if security_id == atm.security_id else 5000.0

    wrapper.get_daily_volume_sum = fake_volume
    odc.config.LIQUID_CONTRACT_MIN_PRIOR_SESSION_VOLUME = 500.0
    odc.config.LIQUID_CONTRACT_LOOKBACK_DAYS = 7

    assert wrapper._is_contract_liquid_and_active(atm) is False, \
        "zero prior-session volume must fail the check even with a clean current-session signal"
    assert wrapper._is_contract_liquid_and_active(nearby) is True

    wrapper.get_atm_option = lambda sym, ot: atm
    wrapper._is_mcx_commodity = lambda sym: False
    wrapper._nearby_option_candidates = lambda *a, **k: [atm, nearby]
    odc.config.LIQUID_CONTRACT_GATE_ENABLED = True
    result = wrapper.get_liquid_atm_option("TESTSTOCK", "CE")
    assert result is nearby, f"expected the substitute strike with real prior-session volume, got {result}"
    print("5. The ATM strike with zero prior-session volume (the real ATHERENERG shape) correctly "
          "substitutes a nearby strike with genuine trading history: PASSED")


def test_6_no_candidate_passes_returns_none():
    wrapper = _wrapper_with_gate_active()
    atm = make_atm(1540.0)
    wrapper.get_atm_option = lambda sym, ot: atm
    wrapper._is_mcx_commodity = lambda sym: False
    wrapper._nearby_option_candidates = lambda *a, **k: [atm, make_atm(1580.0), make_atm(1500.0)]
    wrapper._is_contract_liquid_and_active = lambda candidate, is_mcx=False: False
    odc.config.LIQUID_CONTRACT_GATE_ENABLED = True
    result = wrapper.get_liquid_atm_option("TESTSTOCK", "CE")
    assert result is None, f"expected None when no candidate in the search window passes, got {result}"
    print("6. No candidate within the search window passing both checks returns None - never silently "
          "falls back to the raw, unchecked ATM pick: PASSED")


def test_7_gate_disabled_bypasses_everything():
    wrapper = _wrapper_with_gate_active()
    atm = make_atm(1540.0)
    wrapper.get_atm_option = lambda sym, ot: atm
    called = {"n": 0}
    wrapper._nearby_option_candidates = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or []
    odc.config.LIQUID_CONTRACT_GATE_ENABLED = False
    result = wrapper.get_liquid_atm_option("TESTSTOCK", "CE")
    odc.config.LIQUID_CONTRACT_GATE_ENABLED = True
    assert result is atm, "disabled flag must return the plain ATM pick"
    assert called["n"] == 0, "disabled flag must skip the candidate search entirely, zero extra calls"
    print("7. LIQUID_CONTRACT_GATE_ENABLED=False bypasses the whole gate, returning the plain ATM pick "
          "with zero liquidity/volume calls: PASSED")


def test_8_mcx_underlying_runs_through_the_same_checks_with_mcx_segment_codes():
    """Extended 18 Sep 2026 (same day, user follow-up request: "add this
    for MCX all trades also") - Copper now gets the SAME liquid-contract
    protection as every NSE underlying, using the real, empirically-
    confirmed MCX segment codes (exchange_segment="MCX_COMM",
    instrument_type="OPTFUT" - confirmed against the real instrument
    master's own SEM_EXCH_INSTRUMENT_TYPE column for a real COPPER
    option row, not guessed)."""
    wrapper = _wrapper_with_gate_active()
    atm = make_atm(1540.0, "COPPER 23 SEP 1360 CALL")
    wrapper.get_atm_option = lambda sym, ot: atm
    wrapper._is_mcx_commodity = lambda sym: sym == "COPPER"
    wrapper._nearby_option_candidates = lambda *a, **k: [atm]
    calls = []

    def fake_check(candidate, is_mcx=False):
        calls.append(is_mcx)
        return True

    wrapper._is_contract_liquid_and_active = fake_check
    odc.config.LIQUID_CONTRACT_GATE_ENABLED = True
    result = wrapper.get_liquid_atm_option("COPPER", "CE")
    assert result is atm
    assert calls == [True], f"an MCX underlying must run through the SAME checks with is_mcx=True, got {calls}"
    print("8. An MCX underlying (Swing's Copper) runs through the SAME liquidity/prior-session checks "
          "as NSE, with is_mcx=True correctly threaded through: PASSED")


def test_8b_mcx_liquid_and_active_uses_the_real_mcx_segment_codes():
    wrapper = DhanWrapper.__new__(DhanWrapper)
    wrapper._liquidity_cache = {}
    liquidity_calls = []
    volume_calls = []
    wrapper.refresh_liquidity_signal = lambda ts, **kw: liquidity_calls.append(kw)
    wrapper.get_cached_illiquid = lambda ts: False

    def fake_volume(security_id, exchange_segment, instrument_type, lookback_days):
        volume_calls.append((exchange_segment, instrument_type))
        return 5000.0

    wrapper.get_daily_volume_sum = fake_volume
    odc.config.LIQUID_CONTRACT_MIN_PRIOR_SESSION_VOLUME = 500.0
    odc.config.LIQUID_CONTRACT_LOOKBACK_DAYS = 7

    candidate = make_atm(1360.0, "COPPER 23 SEP 1360 CALL")
    result = wrapper._is_contract_liquid_and_active(candidate, is_mcx=True)
    assert result is True
    assert liquidity_calls == [{
        "expected_exchange": "MCX", "exchange_segment": "MCX_COMM", "instrument_type": "OPTFUT",
    }], f"expected the real MCX segment codes passed to refresh_liquidity_signal, got {liquidity_calls}"
    assert volume_calls == [("MCX_COMM", "OPTFUT")], \
        f"expected the real MCX segment codes passed to get_daily_volume_sum, got {volume_calls}"
    print("8b. _is_contract_liquid_and_active passes the real, confirmed MCX segment codes "
          "(MCX_COMM/OPTFUT) to both checks when is_mcx=True, not NSE's: PASSED")


def test_8c_nearby_option_candidates_filters_mcx_rows_correctly():
    wrapper = DhanWrapper.__new__(DhanWrapper)
    import pandas as pd

    rows = []
    for strike in [1300, 1330, 1360, 1390, 1420]:
        rows.append({
            "SEM_EXM_EXCH_ID": "MCX", "SEM_CUSTOM_SYMBOL": f"COPPER 23 SEP {strike} CALL",
            "SEM_EXPIRY_DATE": FUTURE_EXPIRY.isoformat(), "SEM_OPTION_TYPE": "CE",
            "SEM_STRIKE_PRICE": strike, "SEM_LOT_UNITS": "1", "SEM_SMST_SECURITY_ID": 570000 + strike,
            "SM_SYMBOL_NAME": "COPPER",
        })
    # An NSE row sharing the same custom-symbol prefix - must never leak
    # into an MCX search (and vice versa - the exchange filter alone
    # already guards this, SM_SYMBOL_NAME is the extra belt-and-braces
    # check Tradehull's own MCX branch uses).
    rows.append({
        "SEM_EXM_EXCH_ID": "NSE", "SEM_CUSTOM_SYMBOL": "COPPER 23 SEP 1360 CALL",
        "SEM_EXPIRY_DATE": FUTURE_EXPIRY.isoformat(), "SEM_OPTION_TYPE": "CE",
        "SEM_STRIKE_PRICE": 1360, "SEM_LOT_UNITS": "500", "SEM_SMST_SECURITY_ID": 12345,
        "SM_SYMBOL_NAME": "COPPER",
    })
    wrapper.instruments = lambda: pd.DataFrame(rows)

    atm = make_atm(1360.0, "COPPER 23 SEP 1360 CALL")
    candidates = wrapper._nearby_option_candidates("COPPER", "CE", atm, max_search=3, is_mcx=True)
    security_ids = [c.security_id for c in candidates]
    assert "12345" not in security_ids, "the NSE row sharing the same custom-symbol text must never appear in an MCX search"
    assert candidates[0].strike == 1360.0, f"the ATM strike itself must sort first, got {candidates[0].strike}"
    print(f"8c. _nearby_option_candidates(is_mcx=True) correctly filters to MCX-only rows (excluding an "
          f"NSE row with the same custom-symbol text) and sorts the {len(candidates)} candidates by "
          f"distance from ATM: PASSED")


def test_9_not_authenticated_bypass():
    """Regression test for a real bug caught by this session's own full
    suite run: _is_mcx_commodity touches self.instruments() -> self.client,
    which LAZILY AUTHENTICATES FOR REAL if self._client is still None -
    an earlier version of get_liquid_atm_option called _is_mcx_commodity
    BEFORE checking self._client is None, so every test exercising a real
    entry path (26 of them, across 4 different test files) started
    attempting a genuine Dhan login and failed with "Dhan login failed
    (mode=pin_totp)". _is_mcx_commodity is deliberately left UNMOCKED
    here (unlike every other test in this file) so this test fails the
    same loud way if that ordering ever regresses again - a mocked
    _is_mcx_commodity would silently hide the exact bug that happened."""
    wrapper = DhanWrapper.__new__(DhanWrapper)
    wrapper._client = None
    atm = make_atm(1540.0)
    wrapper.get_atm_option = lambda sym, ot: atm
    odc.config.LIQUID_CONTRACT_GATE_ENABLED = True
    result = wrapper.get_liquid_atm_option("TESTSTOCK", "CE")
    assert result is atm, "an unauthenticated wrapper (the normal unit-test state) must get a plain passthrough"
    print("9. self._client is None (the standard unauthenticated unit-test state) bypasses the gate "
          "entirely BEFORE ever touching _is_mcx_commodity/self.client, matching every existing test's "
          "install_all_dhan_mocks() expectations - regression-tested against the real, unmocked "
          "_is_mcx_commodity: PASSED")


async def _assert_process_one_entry_skips_cleanly(package_label, position_store_module, trading_engine_module):
    store = position_store_module.PositionStore()
    trading_engine_module.position_store = store

    originals = {name: getattr(odc.dhan_wrapper, name) for name in [
        "get_liquid_atm_option", "place_market_order", "has_open_position_for_underlying",
        "get_pending_order_id", "_get_open_fno_positions_once", "is_rsi_loss_reentry_blocked",
    ]}
    placed_orders = []
    odc.dhan_wrapper.get_liquid_atm_option = lambda sym, ot: None
    odc.dhan_wrapper.place_market_order = lambda *a, **k: placed_orders.append(a) or {
        "order_id": "SHOULD-NOT-HAPPEN", "is_amo": False}
    odc.dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    odc.dhan_wrapper.get_pending_order_id = lambda ts, tt: None
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda sym: False
    try:
        result = await trading_engine_module._process_one_entry("NOLIQUIDITY", "CE")
        assert result["status"] == "skipped" and result["reason"] == "no_liquid_contract_available", result
        assert placed_orders == [], f"zero orders must ever be placed when no liquid contract exists, got {placed_orders}"
        print(f"{package_label} real _process_one_entry cleanly skips (zero orders placed, reason="
              f"'no_liquid_contract_available') when get_liquid_atm_option finds nothing tradeable: PASSED")
    finally:
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)


async def test_10_futures_process_one_entry_skips_cleanly_when_no_liquid_contract():
    import Futures.position_store as fps
    import Futures.trading_engine as fte
    print("10.", end=" ")
    await _assert_process_one_entry_skips_cleanly("Futures", fps, fte)


async def test_11_options_process_one_entry_skips_cleanly_when_no_liquid_contract():
    import Options.position_store as ops
    import Options.trading_engine as ote
    print("11.", end=" ")
    await _assert_process_one_entry_skips_cleanly("Options", ops, ote)


async def test_12_luxury_process_one_entry_skips_cleanly_when_no_liquid_contract():
    import Luxury.position_store as lps
    import Luxury.trading_engine as lte
    print("12.", end=" ")
    await _assert_process_one_entry_skips_cleanly("Luxury", lps, lte)


async def test_13_swing_enter_position_for_stock_skips_cleanly_when_no_liquid_contract():
    import Swing.config as sc
    import Swing.trading_engine as ste

    real_basket_type = sc.BASKET_TYPE
    sc.BASKET_TYPE = "OPTIONS"
    ste.position_store.__init__()

    async def fake_get_supertrend_state(symbol):
        return None

    real_get_supertrend_state = ste.signals.get_supertrend_state
    ste.signals.get_supertrend_state = fake_get_supertrend_state

    originals = {name: getattr(odc.dhan_wrapper, name) for name in [
        "get_liquid_atm_option", "place_market_order", "get_pending_order_id",
    ]}
    placed_orders = []
    odc.dhan_wrapper.get_liquid_atm_option = lambda sym, ot: None
    odc.dhan_wrapper.place_market_order = lambda *a, **k: placed_orders.append(a) or {
        "order_id": "SHOULD-NOT-HAPPEN", "is_amo": False}
    odc.dhan_wrapper.get_pending_order_id = lambda ts, tt, *_: None
    try:
        result = await ste.enter_position_for_stock("NOLIQUIDITY", "BULLISH")
        assert result["status"] == "skipped" and result["reason"] == "no_liquid_contract_available", result
        assert placed_orders == [], f"zero orders must ever be placed when no liquid contract exists, got {placed_orders}"
        print("13. Swing's real enter_position_for_stock cleanly skips (zero orders placed, reason="
              "'no_liquid_contract_available') when get_liquid_atm_option finds nothing tradeable: PASSED")
    finally:
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
        ste.signals.get_supertrend_state = real_get_supertrend_state
        sc.BASKET_TYPE = real_basket_type


def test_14_atm_leg_completely_missing_falls_back_to_nearby_search():
    """The actual bug fixed 22 Sep 2026: get_atm_option RAISING
    MissingOptionLegError (Tradehull computed a strike but had no
    trading_symbol for this option_type there - real incident: COPPER CE
    at strike 1415) must NOT propagate straight out of get_liquid_atm_
    option and abort the whole liquid-contract search - it must fall back
    to _nearest_listed_expiry + the same nearby-strike search every other
    failure mode already gets, seeded from the strike Tradehull DID
    compute."""
    wrapper = _wrapper_with_gate_active()
    nearby = make_atm(1420.0)

    def raise_missing_leg(sym, ot):
        raise odc.MissingOptionLegError(sym, ot, 1415.0)

    wrapper.get_atm_option = raise_missing_leg
    wrapper._is_mcx_commodity = lambda sym: True
    wrapper._nearest_listed_expiry = lambda sym, ot, is_mcx: FUTURE_EXPIRY
    captured = {}

    def fake_nearby(sym, ot, reference, max_search, is_mcx):
        captured["reference"] = reference
        captured["is_mcx"] = is_mcx
        return [reference, nearby]

    wrapper._nearby_option_candidates = fake_nearby
    wrapper._is_contract_liquid_and_active = lambda candidate, is_mcx=False: candidate.trading_symbol == nearby.trading_symbol
    odc.config.LIQUID_CONTRACT_GATE_ENABLED = True

    result = wrapper.get_liquid_atm_option("COPPER", "CE")
    assert result is nearby, f"expected the substitute strike found via the fallback search, got {result}"
    assert captured["reference"].strike == 1415.0, (
        f"fallback search must seed from the strike Tradehull DID compute, got {captured['reference'].strike}"
    )
    assert captured["reference"].expiry_date == FUTURE_EXPIRY
    assert captured["is_mcx"] is True
    print("14. get_atm_option raising MissingOptionLegError (the natural strike has no listed leg at "
          "all) correctly falls back to the nearby-strike search instead of aborting the whole lookup: PASSED")


def test_15_atm_leg_missing_and_no_listed_expiry_returns_none_cleanly():
    """If even the independent instrument-master expiry lookup comes back
    empty (a genuinely dead option chain), the fallback must return None,
    never raise and never fall through with a bogus reference."""
    wrapper = _wrapper_with_gate_active()

    def raise_missing_leg(sym, ot):
        raise odc.MissingOptionLegError(sym, ot, 1415.0)

    wrapper.get_atm_option = raise_missing_leg
    wrapper._is_mcx_commodity = lambda sym: True
    wrapper._nearest_listed_expiry = lambda sym, ot, is_mcx: None
    odc.config.LIQUID_CONTRACT_GATE_ENABLED = True

    result = wrapper.get_liquid_atm_option("COPPER", "CE")
    assert result is None, f"expected a clean None when no expiry is listed at all, got {result}"
    print("15. MissingOptionLegError with no listed expiry at all (dead option chain) returns None "
          "cleanly rather than raising or guessing: PASSED")


def test_16_atm_leg_missing_with_gate_disabled_still_raises():
    """Preserves the OLD behavior exactly when the gate is off - an
    exception on a missing leg must still propagate, not be silently
    swallowed into a None (a caller depending on the exception to know
    something's wrong should keep seeing it when they've explicitly
    turned the safety net off)."""
    wrapper = _wrapper_with_gate_active()

    def raise_missing_leg(sym, ot):
        raise odc.MissingOptionLegError(sym, ot, 1415.0)

    wrapper.get_atm_option = raise_missing_leg
    odc.config.LIQUID_CONTRACT_GATE_ENABLED = False
    try:
        wrapper.get_liquid_atm_option("COPPER", "CE")
        assert False, "expected MissingOptionLegError to propagate when the gate is disabled"
    except odc.MissingOptionLegError:
        pass
    finally:
        odc.config.LIQUID_CONTRACT_GATE_ENABLED = True
    print("16. Gate disabled: MissingOptionLegError still propagates unchanged (old behavior preserved): PASSED")


def main():
    print("=== Liquid-contract-resolution gate test suite ===\n")
    test_1_get_daily_volume_sum_sums_success_and_returns_none_on_failure()
    test_2_nearby_option_candidates_filters_and_sorts_correctly()
    test_3_atm_passing_both_checks_returns_it_unchanged()
    test_4_atm_fails_current_session_liquidity_substitutes_nearby()
    test_5_atm_fails_prior_session_volume_substitutes_nearby()
    test_6_no_candidate_passes_returns_none()
    test_7_gate_disabled_bypasses_everything()
    test_8_mcx_underlying_runs_through_the_same_checks_with_mcx_segment_codes()
    test_8b_mcx_liquid_and_active_uses_the_real_mcx_segment_codes()
    test_8c_nearby_option_candidates_filters_mcx_rows_correctly()
    test_9_not_authenticated_bypass()
    asyncio.run(test_10_futures_process_one_entry_skips_cleanly_when_no_liquid_contract())
    asyncio.run(test_11_options_process_one_entry_skips_cleanly_when_no_liquid_contract())
    asyncio.run(test_12_luxury_process_one_entry_skips_cleanly_when_no_liquid_contract())
    asyncio.run(test_13_swing_enter_position_for_stock_skips_cleanly_when_no_liquid_contract())
    test_14_atm_leg_completely_missing_falls_back_to_nearby_search()
    test_15_atm_leg_missing_and_no_listed_expiry_returns_none_cleanly()
    test_16_atm_leg_missing_with_gate_disabled_still_raises()
    print("\nALL LIQUID-CONTRACT-RESOLUTION CHECKS PASSED")


if __name__ == "__main__":
    main()
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
