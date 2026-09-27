"""
Regression test for the 27 Sep 2026 fix: IndexScalping/paper_engine.py's
_poll_one_index had NO day-of-week check at all - only a time-of-day
window (market_open_dt/square_off_dt) computed off *today's* date
regardless of what day of the week today actually is. Confirmed live via
journalctl: this polled NIFTY/BANKNIFTY (security_id 13/25, config.
INDEX_SECURITY_ID) every POLL_INTERVAL_SECONDS (15s default), 24/7,
including weekends - 73 DH-904 rate-limit hits logged over one weekend,
for a PAPER-ONLY strategy that was never going to place a real order
regardless. Same bug class Swing already found and fixed 22 Sep 2026 for
its own entry-evaluation loop (see tests/test_swing_overnight_gate_and_
friday_squareoff.py and Swing/signals.py's _symbol_market_open docstring)
- just never ported to this file.

Coverage:
  1. On a Saturday, during what would otherwise be live market hours,
     _poll_one_index returns immediately WITHOUT calling _fetch_index_daily
     or dhan_wrapper.get_option_ltp (the two real Dhan call sites this
     function can reach) - the actual regression.
  2. On a weekday during market hours, the same setup DOES proceed past
     the weekday gate (reaches the daily-fetch call) - proves the fix
     didn't accidentally also block legitimate weekday polling.
  3. The stale-carryover safety net (force-closing a leftover open
     position when the date rolls over) still fires correctly on the
     first WEEKDAY poll after a weekend gap - proves returning early on
     Sat/Sun doesn't silently lose that behavior, it's just deferred to
     Monday's first poll, which sees gate_date != today exactly as before.

HOW TO RUN:
    uv run python tests/test_index_scalping_weekend_gate.py
"""
import asyncio
import os
import sys
from datetime import datetime, date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import IndexScalping.paper_engine as pe
from IndexScalping.paper_engine import IndexState, IST


def _at(year, month, day, hh, mm) -> datetime:
    return datetime(year, month, day, hh, mm, tzinfo=IST)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_1_saturday_skips_the_poll_entirely_no_dhan_calls():
    state = IndexState(underlying="NIFTY", security_id="13")
    with mock.patch("IndexScalping.paper_engine.datetime") as fake_dt, \
         mock.patch("IndexScalping.paper_engine._fetch_index_daily") as fake_daily, \
         mock.patch.object(pe.dhan_wrapper, "get_option_ltp") as fake_ltp:
        fake_dt.now.return_value = _at(2026, 9, 26, 11, 0)  # Saturday, well within 09:15-15:15
        fake_dt.combine = datetime.combine
        loop = asyncio.get_event_loop()
        _run(pe._poll_one_index(loop, state))
        assert not fake_daily.called, "Saturday poll must not fetch daily index candles at all"
        assert not fake_ltp.called, "Saturday poll must not fetch option LTP at all"
        assert state.bullish_gate is None and state.bearish_gate is None, \
            "gates must stay unset - nothing should have been evaluated"
    print("1. Saturday, during would-be market hours: _poll_one_index returns immediately, "
          "zero Dhan calls made: PASSED")


def test_2_weekday_during_market_hours_still_proceeds_past_the_gate():
    state = IndexState(underlying="NIFTY", security_id="13")
    with mock.patch("IndexScalping.paper_engine.datetime") as fake_dt, \
         mock.patch("IndexScalping.paper_engine._fetch_index_daily") as fake_daily:
        fake_dt.now.return_value = _at(2026, 9, 25, 11, 0)  # Friday, within market hours
        fake_dt.combine = datetime.combine
        fake_daily.return_value = {"open": [], "close": []}  # too short - function returns after this
        loop = asyncio.get_event_loop()
        _run(pe._poll_one_index(loop, state))
        assert fake_daily.called, "a weekday poll during market hours must still reach the daily-fetch call"
    print("2. A weekday during market hours still proceeds past the new weekday gate "
          "(the fix doesn't block legitimate polling): PASSED")


def test_3_stale_carryover_still_fires_on_mondays_first_poll_after_a_weekend():
    from IndexScalping.paper_engine import PaperPosition
    stale = PaperPosition(underlying="NIFTY", option_type="CE", trading_symbol="NIFTY-STALE-CE",
                           quantity=1, entry_time=_at(2026, 9, 25, 14, 0), entry_price=100.0)
    state = IndexState(underlying="NIFTY", security_id="13", gate_date=date(2026, 9, 25), open_position=stale)
    with mock.patch("IndexScalping.paper_engine.datetime") as fake_dt, \
         mock.patch("IndexScalping.paper_engine._fetch_index_daily") as fake_daily, \
         mock.patch("IndexScalping.paper_engine._record_exit") as fake_record_exit:
        fake_dt.now.return_value = _at(2026, 9, 28, 9, 20)  # Monday, just after market open
        fake_dt.combine = datetime.combine
        fake_daily.return_value = {"open": [], "close": []}
        loop = asyncio.get_event_loop()
        _run(pe._poll_one_index(loop, state))
        assert fake_record_exit.called, \
            "Monday's first poll must still force-close a stale Friday position via the date-rollover check"
        args = fake_record_exit.call_args[0]
        assert args[2] == "STALE_CARRYOVER"
    print("3. A stale position left open over the weekend is still correctly force-closed on "
          "Monday's first poll - the weekend gate doesn't lose this safety net, just defers it: PASSED")


if __name__ == "__main__":
    test_1_saturday_skips_the_poll_entirely_no_dhan_calls()
    test_2_weekday_during_market_hours_still_proceeds_past_the_gate()
    test_3_stale_carryover_still_fires_on_mondays_first_poll_after_a_weekend()
    print("\nAll tests passed.")
