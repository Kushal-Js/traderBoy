"""
Tests for Options/dhan_client.py's _instrument_meta expected_exchange
parameter, added 14 Sep 2026 after a REAL live bug (not theoretical) was
found while investigating Copper's margin requirements: Dhan's own
instrument master had two rows sharing the exact same SEM_TRADING_SYMBOL
/ SEM_CUSTOM_SYMBOL string ("COPPER-23Sep2026-1360-CE" / "COPPER 23 SEP
1360 CALL") - one a genuine MCX OPTFUT row, the other a bogus row tagged
SEM_EXM_EXCH_ID="NSE" with an invalid expiry time that looks like
corrupted/duplicate data in Dhan's own scrip master. Without a hint,
_instrument_meta's `row.iloc[-1]` tiebreak picked the bogus NSE row -
get_atm_option("COPPER", "CE") would have handed a live entry a
security_id that doesn't correspond to any real instrument.

Coverage:
  1. expected_exchange="MCX" correctly picks the real MCX row over the
     colliding bogus NSE one (the exact real-world incident, reproduced).
  2. expected_exchange="NSE" correctly picks an NSE row when one
     genuinely exists at that exchange for the same symbol string.
  3. expected_exchange filtering to an exchange with NO match raises a
     clear ValueError rather than silently falling back to the wrong
     exchange's row.
  4. No expected_exchange (existing callers/behavior) still resolves an
     UNAMBIGUOUS symbol correctly - the new parameter is additive, not a
     behavior change for the common single-match case.
  5. _is_mcx_commodity correctly distinguishes a real MCX commodity from
     an NSE stock, purely from instrument-master data (no hardcoded
     symbol list).

HOW TO RUN:
    uv run python tests/test_instrument_meta_exchange_disambiguation.py
"""
import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import pandas as pd

import Options.dhan_client as odc

W = odc.dhan_wrapper


def _row(exch, instrument_name, trading_symbol, custom_symbol, security_id, strike, expiry, lot_units=1.0, tick_size=5.0):
    return {
        "SEM_EXM_EXCH_ID": exch, "SEM_INSTRUMENT_NAME": instrument_name,
        "SEM_TRADING_SYMBOL": trading_symbol, "SEM_CUSTOM_SYMBOL": custom_symbol,
        "SEM_SMST_SECURITY_ID": security_id, "SEM_STRIKE_PRICE": strike,
        "SEM_EXPIRY_DATE": expiry, "SEM_LOT_UNITS": lot_units, "SEM_TICK_SIZE": tick_size,
    }


def _install_fake_instruments(df: pd.DataFrame):
    saved_client = W._client
    W._client = types.SimpleNamespace(instrument_df=df)
    return saved_client


def test_1_expected_exchange_mcx_picks_the_real_row_over_the_colliding_nse_one():
    """Reproduces the actual 14 Sep 2026 incident: a genuine MCX OPTFUT
    row and a bogus NSE-tagged row share the identical symbol strings."""
    saved = _install_fake_instruments(pd.DataFrame([
        _row("MCX", "OPTFUT", "COPPER-23Sep2026-1360-CE", "COPPER 23 SEP 1360 CALL",
             574852, 1360.0, "2026-09-23 23:30:00"),
        _row("NSE", "OPTFUT", "COPPER-23Sep2026-1360-CE", "COPPER 23 SEP 1360 CALL",
             123250, 1360.0, "2026-09-23 20:00:00"),
    ]))
    try:
        meta = W._instrument_meta("COPPER 23 SEP 1360 CALL", expected_exchange="MCX")
        assert meta["security_id"] == "574852", \
            f"expected the real MCX row (574852), got {meta['security_id']!r} - the bogus NSE row won again"
        print("1. expected_exchange='MCX' correctly picks the real MCX row over the colliding bogus NSE row: PASSED")
    finally:
        W._client = saved


def test_2_expected_exchange_nse_picks_a_genuine_nse_row():
    saved = _install_fake_instruments(pd.DataFrame([
        _row("MCX", "OPTFUT", "SAMESTR-Sep2026-100-CE", "SAMESTR SEP 100 CALL", 111111, 100.0, "2026-09-23 23:30:00"),
        _row("NSE", "OPTSTK", "SAMESTR-Sep2026-100-CE", "SAMESTR SEP 100 CALL", 222222, 100.0, "2026-09-25 15:30:00"),
    ]))
    try:
        meta = W._instrument_meta("SAMESTR SEP 100 CALL", expected_exchange="NSE")
        assert meta["security_id"] == "222222", f"expected the NSE row (222222), got {meta['security_id']!r}"
        print("2. expected_exchange='NSE' correctly picks the genuine NSE row: PASSED")
    finally:
        W._client = saved


def test_3_expected_exchange_with_no_match_raises_instead_of_falling_back():
    saved = _install_fake_instruments(pd.DataFrame([
        _row("MCX", "OPTFUT", "ONLYMCX-Sep2026-100-CE", "ONLYMCX SEP 100 CALL", 333333, 100.0, "2026-09-23 23:30:00"),
    ]))
    try:
        raised = False
        try:
            W._instrument_meta("ONLYMCX SEP 100 CALL", expected_exchange="NSE")
        except ValueError:
            raised = True
        assert raised, "expected a ValueError when the requested exchange has no matching row, not a silent fallback"
        print("3. expected_exchange with zero matches raises ValueError rather than silently using another exchange's row: PASSED")
    finally:
        W._client = saved


def test_4_no_expected_exchange_still_resolves_an_unambiguous_symbol():
    """Regression guard: every pre-14-Sep call site that never passed
    expected_exchange must still resolve correctly for the common case -
    a symbol that only exists on one exchange."""
    saved = _install_fake_instruments(pd.DataFrame([
        _row("NSE", "OPTSTK", "RELIANCE-Sep2026-3000-CE", "RELIANCE SEP 3000 CALL", 444444, 3000.0, "2026-09-25 15:30:00"),
    ]))
    try:
        meta = W._instrument_meta("RELIANCE SEP 3000 CALL")
        assert meta["security_id"] == "444444", f"expected 444444, got {meta['security_id']!r}"
        print("4. No expected_exchange still resolves an unambiguous symbol correctly (zero behavior change): PASSED")
    finally:
        W._client = saved


def test_5_is_mcx_commodity_distinguishes_real_mcx_from_nse():
    saved = _install_fake_instruments(pd.DataFrame([
        _row("MCX", "FUTCOM", "COPPER-30Sep2026-FUT", "COPPER SEP FUT", 571298, None, "2026-09-30 23:30:00"),
        _row("NSE", "FUTSTK", "RELIANCE-Sep2026-FUT", "RELIANCE SEP FUT", 555555, None, "2026-09-25 15:30:00"),
    ]))
    try:
        assert W._is_mcx_commodity("COPPER") is True, "COPPER has a real MCX FUTCOM row - should be detected as an MCX commodity"
        assert W._is_mcx_commodity("RELIANCE") is False, "RELIANCE has no MCX FUTCOM row - must not be misdetected as one"
        print("5. _is_mcx_commodity correctly distinguishes a real MCX commodity from an NSE stock: PASSED")
    finally:
        W._client = saved


if __name__ == "__main__":
    test_1_expected_exchange_mcx_picks_the_real_row_over_the_colliding_nse_one()
    test_2_expected_exchange_nse_picks_a_genuine_nse_row()
    test_3_expected_exchange_with_no_match_raises_instead_of_falling_back()
    test_4_no_expected_exchange_still_resolves_an_unambiguous_symbol()
    test_5_is_mcx_commodity_distinguishes_real_mcx_from_nse()
    print("\nAll tests passed.")
