"""
Tests for the multi-window trading schedule - user request 10 Sep 2026:
"create trading time zones, zone1 for 9:15 upto 11 AM, zone2 from 2 PM
upto 3:28 PM for Options, luxury and Futures and trading should only be
allowed within these 2 zones, update and deploy also using configs".

Built as config.ENABLE_TRADING_WINDOWS / config.TRADING_WINDOWS +
trading_engine.is_within_trading_windows(), checked in each package's
webhook handler right before the pre-existing single-cutoff / square-off
checks. The single-cutoff ENABLE_TRADING_TIME_LIMIT is SUPERSEDED when
windows are on (is_past_allowed_trading_time() short-circuits to False)
so the two can't fight - the end-to-end rejection path for the single
cutoff itself is already covered by test_luxury_allowed_trading_time.py.

Covers, for all three packages (Options / Luxury / Futures), against the
REAL production functions:
  1. Feature OFF -> is_within_trading_windows() always True (no gate).
  2. Feature ON, "09:15-11:00,14:00-15:28":
       - inside either zone  -> True
       - before 09:15, in the 11:00-14:00 gap, at/after 15:28 -> False
       - boundaries: start inclusive, end exclusive
  3. _parse_trading_windows(): skips malformed chunks, "" -> [] (which
     makes the gate fail CLOSED - no window matches - the safe direction).
  4. is_past_allowed_trading_time() short-circuits to False whenever
     ENABLE_TRADING_WINDOWS is on, even with ENABLE_TRADING_TIME_LIMIT on
     and the clock well past ALLOWED_TRADING_TIME.

HOW TO RUN:
    uv run python tests/test_trading_windows.py
"""
import os
import sys
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.trading_engine as ote
import Luxury.trading_engine as lte
import Futures.trading_engine as fte

IST = ZoneInfo("Asia/Kolkata")
SPEC = "09:15-11:00,14:00-15:28"
PACKAGES = (("Options", ote), ("Luxury", lte), ("Futures", fte))


def _at(h, m):
    return datetime.now(IST).replace(hour=h, minute=m, second=0, microsecond=0)


def _set(mod, **kw):
    """Set config attrs, return a restore fn."""
    saved = {k: getattr(mod.config, k) for k in kw}
    for k, v in kw.items():
        setattr(mod.config, k, v)
    return lambda: [setattr(mod.config, k, v) for k, v in saved.items()]


def test_1_feature_off_never_gates():
    for name, mod in PACKAGES:
        restore = _set(mod, ENABLE_TRADING_WINDOWS=False)
        try:
            for h, m in [(6, 0), (9, 14), (12, 30), (15, 45), (23, 0)]:
                assert mod.is_within_trading_windows(_at(h, m)) is True, f"{name} {h}:{m}"
        finally:
            restore()
    print("1. ENABLE_TRADING_WINDOWS=False -> is_within_trading_windows() is always True (no gate), "
          "all three packages: PASSED")


def test_2_windows_gate_the_right_times():
    inside = [(9, 15), (9, 16), (10, 30), (10, 59), (14, 0), (14, 1), (15, 27)]
    outside = [(9, 0), (9, 14), (11, 0), (11, 1), (12, 30), (13, 59), (15, 28), (15, 45), (16, 30)]
    for name, mod in PACKAGES:
        restore = _set(mod, ENABLE_TRADING_WINDOWS=True, TRADING_WINDOWS=SPEC)
        try:
            for h, m in inside:
                assert mod.is_within_trading_windows(_at(h, m)) is True, f"{name}: {h:02d}:{m:02d} should be INSIDE a window"
            for h, m in outside:
                assert mod.is_within_trading_windows(_at(h, m)) is False, f"{name}: {h:02d}:{m:02d} should be OUTSIDE every window"
        finally:
            restore()
    print("2. Windows '09:15-11:00,14:00-15:28': inside either zone -> True; before 09:15, the 11:00-14:00 "
          "gap, and >=15:28 -> False; start inclusive / end exclusive, all three packages: PASSED")


def test_3_parse_is_fail_closed_on_bad_input():
    for name, mod in PACKAGES:
        assert mod._parse_trading_windows("09:15-11:00,14:00-15:28") == \
            [(dt_time(9, 15), dt_time(11, 0)), (dt_time(14, 0), dt_time(15, 28))], name
        assert mod._parse_trading_windows("") == [], name
        assert mod._parse_trading_windows("garbage,09:15-11:00,also-bad,25:99-xx") == \
            [(dt_time(9, 15), dt_time(11, 0))], name
        # a fully-unparseable spec -> [] -> gate fails CLOSED (no entries)
        restore = _set(mod, ENABLE_TRADING_WINDOWS=True, TRADING_WINDOWS="nonsense")
        try:
            assert mod.is_within_trading_windows(_at(10, 0)) is False, f"{name}: unparseable spec must block all entries"
        finally:
            restore()
    print("3. _parse_trading_windows() skips malformed chunks, '' -> []; an unparseable spec makes the "
          "gate fail CLOSED (no new entries), all three packages: PASSED")


def test_4_windows_supersede_the_single_cutoff():
    for name, mod in PACKAGES:
        # single cutoff ON at 11:00, clock at 14:30 (past it) -> would normally block,
        # but windows ON must make is_past_allowed_trading_time() short-circuit to False.
        restore_now = None
        real_now = mod._now_ist
        mod._now_ist = lambda: _at(14, 30)
        restore = _set(mod, ENABLE_TRADING_TIME_LIMIT=True, ALLOWED_TRADING_TIME="11:00",
                       ENABLE_TRADING_WINDOWS=True, TRADING_WINDOWS=SPEC)
        try:
            assert mod.is_past_allowed_trading_time() is False, \
                f"{name}: with windows ON, the single-cutoff check must short-circuit to False"
            # and the window check itself allows 14:30 (inside zone 2)
            assert mod.is_within_trading_windows() is True, f"{name}: 14:30 is inside zone 2"
        finally:
            restore()
            mod._now_ist = real_now
    print("4. ENABLE_TRADING_WINDOWS ON makes is_past_allowed_trading_time() short-circuit to False even "
          "with the single cutoff enabled and the clock past it, all three packages: PASSED")


def main():
    print("=== Multi-window trading schedule test suite ===\n")
    test_1_feature_off_never_gates()
    test_2_windows_gate_the_right_times()
    test_3_parse_is_fail_closed_on_bad_input()
    test_4_windows_supersede_the_single_cutoff()
    print("\nALL TRADING-WINDOW CHECKS PASSED")


if __name__ == "__main__":
    main()
