"""
Tests for get_margin_required()/get_fund_limits() (Options/dhan_client.py,
added 1 Sep 2026) - the two real, read-only Dhan REST wrappers built for
Swing's paper-trading margin/funds logging (user request: "logging real
margin and funds required during paper trading so that we can do
analysis also"). See tests/test_swing_paper_engine.py for how these are
actually USED inside paper_engine.py - this file covers just the two
wrappers themselves, against a fake `self.client.Dhan`, no live Dhan
session needed.

Covers, against the REAL production functions (not reimplemented):
  1. get_margin_required returns the `data` dict on a genuine
     `status: "success"` response.
  2. get_margin_required raises (not a silent `0`/`None`) on a
     `status: "failure"` response - the whole reason this wraps the RAW
     dhanhq call instead of reusing Tradehull's own margin_calculator(),
     which swallows every failure into an indistinguishable `0`.
  3. get_margin_required retries on a transient failure and recovers -
     same _retry() helper every other REST call site here uses.
  4. get_fund_limits returns the `data` dict on success.
  5. get_fund_limits raises on a `status: "failure"` response.

HOW TO RUN:
    uv run python tests/test_margin_and_funds.py
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

import Options.dhan_client as dc


def _wrapper_with_fake_dhan(dhan_obj):
    wrapper = dc.DhanWrapper.__new__(dc.DhanWrapper)
    wrapper._client = types.SimpleNamespace(Dhan=dhan_obj)
    return wrapper


def test_1_get_margin_required_returns_data_on_success():
    fake_dhan = types.SimpleNamespace(
        margin_calculator=lambda **kwargs: {"status": "success", "data": {"totalMargin": 12500.0, "leverage": "1.00X"}}
    )
    wrapper = _wrapper_with_fake_dhan(fake_dhan)
    result = wrapper.get_margin_required("12345", "NSE_FNO", "BUY", 250, "MARGIN", 2500.0)
    assert result == {"totalMargin": 12500.0, "leverage": "1.00X"}
    print("1. get_margin_required returns the real `data` dict on a success response: PASSED")


def test_2_get_margin_required_raises_on_failure_status():
    calls = {"n": 0}

    def always_fails(**kwargs):
        calls["n"] += 1
        return {"status": "failure", "remarks": "DH-905 Invalid Security ID"}

    wrapper = _wrapper_with_fake_dhan(types.SimpleNamespace(margin_calculator=always_fails))
    try:
        wrapper.get_margin_required("bad-id", "NSE_FNO", "BUY", 250, "MARGIN", 2500.0)
        assert False, "expected a ValueError, not a silent failure"
    except ValueError as e:
        assert "margin_calculator failed" in str(e), str(e)
        assert calls["n"] == 3, f"expected all 3 attempts exhausted (persistent failure), got {calls['n']}"
        print("2. get_margin_required RAISES on a failure status (never a silent 0/None the way "
              "Tradehull's own wrapper would) - after exhausting retries: PASSED")


def test_3_get_margin_required_retries_on_transient_failure_and_recovers():
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "failure", "remarks": "transient"}
        return {"status": "success", "data": {"totalMargin": 999.0}}

    wrapper = _wrapper_with_fake_dhan(types.SimpleNamespace(margin_calculator=flaky))
    result = wrapper.get_margin_required("12345", "NSE_FNO", "BUY", 250, "MARGIN", 2500.0)
    assert result == {"totalMargin": 999.0}
    assert calls["n"] == 2, f"expected exactly 2 calls (1 fail + 1 success), got {calls['n']}"
    print("3. get_margin_required retries a transient failure and recovers on the next attempt "
          "(same shared _retry() helper as every other REST call site here): PASSED")


def test_4_get_fund_limits_returns_data_on_success():
    fake_dhan = types.SimpleNamespace(
        get_fund_limits=lambda: {"status": "success", "data": {"availabelBalance": 100000.0, "sodLimit": 150000.0}}
    )
    wrapper = _wrapper_with_fake_dhan(fake_dhan)
    result = wrapper.get_fund_limits()
    assert result == {"availabelBalance": 100000.0, "sodLimit": 150000.0}
    print("4. get_fund_limits returns the real `data` dict (Dhan's own field names/casing "
          "untouched, including its own 'availabelBalance' typo) on success: PASSED")


def test_5_get_fund_limits_raises_on_failure_status():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        return {"status": "failure", "remarks": "some error"}

    wrapper = _wrapper_with_fake_dhan(types.SimpleNamespace(get_fund_limits=always_fails))
    try:
        wrapper.get_fund_limits()
        assert False, "expected a ValueError, not a silent failure"
    except ValueError as e:
        assert "get_fund_limits failed" in str(e), str(e)
        assert calls["n"] == 3, f"expected all 3 attempts exhausted, got {calls['n']}"
        print("5. get_fund_limits RAISES on a failure status after exhausting retries: PASSED")


def main():
    print("=== get_margin_required / get_fund_limits test suite ===\n")
    test_1_get_margin_required_returns_data_on_success()
    test_2_get_margin_required_raises_on_failure_status()
    test_3_get_margin_required_retries_on_transient_failure_and_recovers()
    test_4_get_fund_limits_returns_data_on_success()
    test_5_get_fund_limits_raises_on_failure_status()
    print("\nALL MARGIN/FUNDS WRAPPER CHECKS PASSED")


if __name__ == "__main__":
    main()
