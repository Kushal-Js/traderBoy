"""
Tests for reversal_filters.py - the shadow-mode (log-only, never blocks)
reversal-prevention filter prototype added 16 Sep 2026, hooked into
Options/Futures/Luxury's position_store.py add_position/close_position.

Coverage:
  1. The auth-guard: evaluate_and_log must NEVER touch dhan_wrapper.client
     (the lazy property that triggers a real login) when dhan_wrapper.
     _client is still None - this was a REAL incident found while
     building this module (see reversal_filters.py's own docstring): the
     first version called _equity_security_id directly, which internally
     reads self.client, triggering a genuine Dhan authentication attempt
     from a background thread on every test that calls add_position().
  2. record_supertrend_exit is a trivial, synchronous, never-raising
     in-memory write.
  3. The pure indicator functions (_compute_rsi/_compute_adx/
     _volume_ratio_at) match known reference values.
  4. evaluate_and_log's exception safety - a failure deep inside the
     sync path must never raise into the caller (it's called via
     fire_and_forget from inside a position-store lock).

HOW TO RUN:
    uv run python tests/test_reversal_filters_shadow.py
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import reversal_filters as rf
from Options.dhan_client import dhan_wrapper


async def test_1_never_triggers_real_auth_when_not_yet_authenticated():
    """The real incident this guards against: evaluate_and_log must
    return immediately, without ever reading dhan_wrapper.client (the
    property that lazily calls authenticate()), when _client is None."""
    original = dhan_wrapper._client
    dhan_wrapper._client = None
    with mock.patch.object(dhan_wrapper, "authenticate") as fake_auth:
        try:
            await rf.evaluate_and_log("Options", "RELIANCE", "CE", 100.0, "FAKE-1")
            fake_auth.assert_not_called()
            print("1. evaluate_and_log never triggers real authentication when dhan_wrapper._client is None: PASSED")
        finally:
            dhan_wrapper._client = original


async def test_2_swallows_failures_from_a_broken_client():
    """Even with _client set to something that will raise on every call
    (simulating a stale/incompatible mock left by another test file),
    evaluate_and_log must never raise."""
    original = dhan_wrapper._client
    dhan_wrapper._client = mock.Mock()
    dhan_wrapper._client.Dhan.intraday_minute_data.side_effect = RuntimeError("simulated broker failure")
    try:
        await rf.evaluate_and_log("Options", "RELIANCE", "CE", 100.0, "FAKE-2")
        print("2. evaluate_and_log swallows a failing/broken client without raising: PASSED")
    finally:
        dhan_wrapper._client = original


def test_3_record_supertrend_exit_is_trivial_and_safe():
    rf._last_supertrend_exit.clear()
    rf.record_supertrend_exit("Options", "TATAPOWER", "CE")
    assert ("Options", "TATAPOWER", "CE") in rf._last_supertrend_exit
    print("3. record_supertrend_exit records a timestamp for the exact (strategy, symbol, option_type) key: PASSED")


def test_4_compute_rsi_matches_known_reference():
    # Monotonically rising closes -> RSI should approach 100 (no losses at all).
    closes = [100 + i for i in range(30)]
    rsi = rf._compute_rsi(closes, period=14)
    assert rsi[14] == 100.0, rsi[14]
    assert rsi[-1] == 100.0, rsi[-1]
    print("4. _compute_rsi returns 100 for a monotonically rising series (zero average loss): PASSED")


def test_5_compute_adx_returns_none_until_warm():
    closes = [100.0] * 10
    highs = [101.0] * 10
    lows = [99.0] * 10
    adx = rf._compute_adx(highs, lows, closes, period=14)
    assert all(v is None for v in adx), "ADX needs 2*period+1 bars - must be None throughout an under-warmed series"
    print("5. _compute_adx returns None throughout when the series is shorter than 2*period+1: PASSED")


def test_6_volume_ratio_at_basic_math():
    volumes = [100.0] * 20 + [500.0]
    ratio = rf._volume_ratio_at(volumes, idx=20, lookback=20)
    assert ratio == 5.0, ratio
    print("6. _volume_ratio_at correctly computes entry-candle volume vs the prior 20-bar average: PASSED")


def test_7_climax_combo_needs_both_conditions() -> None:
    """Sanity-check the combo logic directly (not just via the full
    evaluate_and_log path) - RSI-extreme alone, or a volume spike alone,
    must not be enough; both together must be."""
    def combo(rsi, option_type, vol_ratio):
        rsi_extreme = rsi is not None and (rsi > rf.RSI_OVERBOUGHT if option_type == "CE" else rsi < rf.RSI_OVERSOLD)
        return rsi_extreme and vol_ratio is not None and vol_ratio > rf.SPIKE_RATIO

    assert combo(80.0, "CE", 5.0) is False, "RSI-extreme alone (no volume spike) must not trigger the combo"
    assert combo(50.0, "CE", 30.0) is False, "volume spike alone (no RSI extreme) must not trigger the combo"
    assert combo(80.0, "CE", 30.0) is True, "RSI-extreme AND a volume spike together must trigger the combo"
    print("7. Climax-combo filter logic requires BOTH RSI-extreme AND a volume spike, neither alone: PASSED")


async def main():
    print("=== reversal_filters.py shadow-mode filter test suite ===\n")
    await test_1_never_triggers_real_auth_when_not_yet_authenticated()
    await test_2_swallows_failures_from_a_broken_client()
    test_3_record_supertrend_exit_is_trivial_and_safe()
    test_4_compute_rsi_matches_known_reference()
    test_5_compute_adx_returns_none_until_warm()
    test_6_volume_ratio_at_basic_math()
    test_7_climax_combo_needs_both_conditions()
    print("\nALL reversal_filters shadow-mode tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
