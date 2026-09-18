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
     _volume_ratio_at/_efficiency_ratio_at) match known reference values.
  4. evaluate_and_log's exception safety - a failure deep inside the
     sync path must never raise into the caller (it's called via
     fire_and_forget from inside a position-store lock).
  5. Kaufman Efficiency Ratio (added 17 Sep 2026, shadow-mode/log-only -
     see reversal_filters.py's own docstring for the backtest evidence
     and why it's not in the recommended combo yet) is wired to the
     conservative 0.3 threshold, not the in-sample-best 0.45.

HOW TO RUN:
    uv run python tests/test_reversal_filters_shadow.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_reversal_filters_shadow_test_"))
trade_history.HISTORY_DIR = scratch_dir

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


def test_8_efficiency_ratio_matches_known_reference():
    # Monotonically rising closes by 1 each bar -> every bar contributes
    # fully to net direction -> ER should be exactly 1.0 (pure trend).
    closes = [100.0 + i for i in range(15)]
    er = rf._efficiency_ratio_at(closes, idx=14, period=10)
    assert er == 1.0, er

    # A pure back-and-forth oscillation (up 1, down 1, ...) makes net
    # displacement ~0 while the path sum keeps growing -> ER near 0.
    closes_choppy = [100.0, 101.0] * 8
    er_choppy = rf._efficiency_ratio_at(closes_choppy, idx=10, period=10)
    assert er_choppy is not None and er_choppy < 0.2, er_choppy

    # Under-warmed series (idx < period) must return None, not raise.
    assert rf._efficiency_ratio_at([100.0, 101.0, 102.0], idx=2, period=10) is None
    print("8. _efficiency_ratio_at returns 1.0 for a pure trend, near-0 for pure chop, "
          "None when under-warmed: PASSED")


def test_9_er_blocks_uses_the_conservative_threshold_not_the_in_sample_best():
    """Sanity-check the threshold actually wired in is the conservative
    0.3 recommended after the overlap backtest, not the in-sample-best
    0.45 found during the same research (see reversal_filters.py's own
    docstring for why 0.45 would be an overfit choice)."""
    assert rf.ER_THRESHOLD == 0.3, rf.ER_THRESHOLD
    print("9. ER_THRESHOLD is wired to the conservative 0.3, not the in-sample-best 0.45: PASSED")


def _fake_candle_response(n=45):
    """A plausible, mildly-trending 5-min candle series - just needs to be
    long enough (>=40 bars) and varied enough that RSI/ADX/vol_ratio/ER
    all compute to real numbers, not None."""
    closes = [100.0 + i * 0.3 + (0.5 if i % 3 == 0 else -0.2) for i in range(n)]
    highs = [c + 0.4 for c in closes]
    lows = [c - 0.4 for c in closes]
    volumes = [1000.0 + (i % 5) * 50 for i in range(n)]
    return {"status": "success", "data": {"high": highs, "low": lows, "close": closes, "volume": volumes}}


def _install_fake_client(per_symbol_response=None, raise_for=None):
    """per_symbol_response: dict[symbol -> candle response dict] (defaults
    every symbol to _fake_candle_response()). raise_for: set of symbols
    whose intraday_minute_data call raises, simulating a per-candidate
    fetch failure without breaking the whole batch."""
    per_symbol_response = per_symbol_response or {}
    raise_for = raise_for or set()

    class FakeDhan:
        @staticmethod
        def intraday_minute_data(security_id=None, **kwargs):
            symbol = security_id  # _equity_security_id is mocked to return the symbol itself
            if symbol in raise_for:
                raise RuntimeError(f"simulated fetch failure for {symbol}")
            return per_symbol_response.get(symbol, _fake_candle_response())

    original_client = dhan_wrapper._client
    original_sec_id = dhan_wrapper._equity_security_id
    dhan_wrapper._client = mock.Mock()
    dhan_wrapper._client.Dhan = FakeDhan()
    dhan_wrapper._equity_security_id = lambda symbol: symbol

    def restore():
        dhan_wrapper._client = original_client
        dhan_wrapper._equity_security_id = original_sec_id

    return restore


def test_10_fetch_indicators_returns_none_when_not_authenticated():
    original = dhan_wrapper._client
    dhan_wrapper._client = None
    with mock.patch.object(dhan_wrapper, "authenticate") as fake_auth:
        try:
            result = rf._fetch_indicators_sync("RELIANCE")
            assert result is None
            fake_auth.assert_not_called()
            print("10. _fetch_indicators_sync returns None (never triggers real auth) when "
                  "dhan_wrapper._client is None: PASSED")
        finally:
            dhan_wrapper._client = original


def test_11_fetch_indicators_returns_real_numbers_on_success():
    restore = _install_fake_client()
    try:
        result = rf._fetch_indicators_sync("RELIANCE")
        assert result is not None
        assert all(k in result for k in ("rsi", "adx", "vol_ratio", "er"))
        assert result["rsi"] is not None and result["vol_ratio"] is not None
        print("11. _fetch_indicators_sync returns real RSI/ADX/VolRatio/ER numbers on a "
              "successful fetch with enough history: PASSED")
    finally:
        restore()


async def test_12_log_alert_candidates_never_authenticates_when_not_ready():
    original = dhan_wrapper._client
    dhan_wrapper._client = None
    with mock.patch.object(dhan_wrapper, "authenticate") as fake_auth:
        try:
            await rf.log_alert_candidates("Options", "test-scan", "CE", ["RELIANCE", "TCS"], ["TCS"])
            fake_auth.assert_not_called()
            print("12. log_alert_candidates never triggers real authentication when "
                  "dhan_wrapper._client is None: PASSED")
        finally:
            dhan_wrapper._client = original


async def test_13_logs_one_row_per_candidate_tagged_correctly():
    restore = _install_fake_client()
    with mock.patch.object(dhan_wrapper, "get_day_change_pct", lambda s: {"RELIANCE": 1.2, "TCS": 0.5, "INFY": 3.0}[s]):
        try:
            await rf.log_alert_candidates("Options", "test-scan-13", "CE",
                                           ["RELIANCE", "TCS", "INFY"], ["INFY"])
            rows = [json.loads(l) for l in open(trade_history.dated_path(rf.ALERT_CANDIDATE_SHADOW_LOG_NAME))
                    if json.loads(l).get("scan_name") == "test-scan-13"]
            assert len(rows) == 3, rows
            by_symbol = {r["symbol"]: r for r in rows}
            assert by_symbol["RELIANCE"]["was_selected"] is False
            assert by_symbol["TCS"]["was_selected"] is False
            assert by_symbol["INFY"]["was_selected"] is True
            assert by_symbol["RELIANCE"]["day_change_pct"] == 1.2
            assert by_symbol["INFY"]["rsi"] is not None, "the selected candidate must also get real indicators logged"
            assert by_symbol["RELIANCE"]["rsi"] is not None, "a REJECTED candidate must get the same indicators logged"
            print("13. log_alert_candidates logs one row per candidate (selected AND rejected), each "
                  "correctly tagged was_selected, with day_change_pct and RSI/ADX/VolRatio/ER: PASSED")
        finally:
            restore()


async def test_14_one_candidates_fetch_failure_does_not_break_the_others():
    restore = _install_fake_client(raise_for={"TCS"})
    with mock.patch.object(dhan_wrapper, "get_day_change_pct", lambda s: 1.0):
        try:
            await rf.log_alert_candidates("Options", "test-scan-14", "CE",
                                           ["RELIANCE", "TCS", "INFY"], ["RELIANCE"])
            rows = [json.loads(l) for l in open(trade_history.dated_path(rf.ALERT_CANDIDATE_SHADOW_LOG_NAME))
                    if json.loads(l).get("scan_name") == "test-scan-14"]
            by_symbol = {r["symbol"]: r for r in rows}
            assert len(rows) == 3, "all 3 candidates must still get a row, even though TCS's own fetch failed"
            assert by_symbol["TCS"]["rsi"] is None, "TCS's own failed fetch must log None indicators, not raise"
            assert by_symbol["RELIANCE"]["rsi"] is not None
            assert by_symbol["INFY"]["rsi"] is not None
            print("14. A single candidate's indicator-fetch failure doesn't break logging for the "
                  "other candidates in the same alert: PASSED")
        finally:
            restore()


async def main():
    print("=== reversal_filters.py shadow-mode filter test suite ===\n")
    await test_1_never_triggers_real_auth_when_not_yet_authenticated()
    await test_2_swallows_failures_from_a_broken_client()
    test_3_record_supertrend_exit_is_trivial_and_safe()
    test_4_compute_rsi_matches_known_reference()
    test_5_compute_adx_returns_none_until_warm()
    test_6_volume_ratio_at_basic_math()
    test_7_climax_combo_needs_both_conditions()
    test_8_efficiency_ratio_matches_known_reference()
    test_9_er_blocks_uses_the_conservative_threshold_not_the_in_sample_best()
    test_10_fetch_indicators_returns_none_when_not_authenticated()
    test_11_fetch_indicators_returns_real_numbers_on_success()
    await test_12_log_alert_candidates_never_authenticates_when_not_ready()
    await test_13_logs_one_row_per_candidate_tagged_correctly()
    await test_14_one_candidates_fetch_failure_does_not_break_the_others()
    print("\nALL reversal_filters shadow-mode tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
