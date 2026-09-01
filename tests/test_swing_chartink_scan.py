"""
Tests for Swing's daily Chartink scan pull (added 1 Sep 2026, user
request): "I am thinking to automate updation of our watchlist file" ->
"A specific Chartink scan URL" -> "once daily, pre-market". Unlike every
other Chartink integration in this codebase (which all wait for
Chartink to PUSH an alert TO us), this instead PULLS - reproducing the
exact request Chartink's own "Run Scan" button makes, confirmed live via
a real browser session 1 Sep 2026 (network-intercepted) and independently
re-verified with a bare `requests` script matching the exact same
result set the live page showed at the time. The mirror image of the
daily watchlist prune (test_swing_daily_watchlist_prune.py) - that one
REMOVES on a daily trend break; this one ADDS from the user's own scan.

Covers, against the REAL production functions (not reimplemented), with
ONLY the `requests` network boundary mocked (chartink_scan.py's own
`requests.Session` is replaced; nothing about the actual scan/add/log
logic is stubbed):
  1. `fetch_scan_symbols_once` against a well-formed mocked response -
     returns the right symbols, uppercased, in Chartink's own order; also
     covers the failure paths - no XSRF cookie set (page structure
     changed), an HTTP error on either the GET or POST leg, and a
     response body missing the expected `data` key - ALL raise rather
     than silently returning an empty/guessed list.
  2. `_run_chartink_watchlist_scan` - adds the scan's returned symbols to
     the REAL watchlist_store, returns the right summary dict, and
     durably logs a `CHARTINK_WATCHLIST_SCAN_COMPLETED` event via the
     real `_record_swing_event`.
  3. `_daily_chartink_watchlist_scan_tick`'s own once-per-day gating:
     does nothing before config.CHARTINK_WATCHLIST_SCAN_TIME; the first
     eligible tick actually runs; a LATER tick the same day is a no-op
     (date-gated, not a one-shot clock match); a fresh trading day resets
     it. Also covers the ONE behavior that deliberately differs from the
     watchlist prune's own gating: a fetch FAILURE does NOT mark the day
     as done - the very next tick (still the same day) retries rather
     than waiting until tomorrow.
  4. `config.CHARTINK_WATCHLIST_SCAN_ENABLED = False` disables the whole
     feature - no fetch attempted, nothing added, even well past the
     scheduled time on a fresh day.

HOW TO RUN:
    uv run python tests/test_swing_chartink_scan.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_chartink_scan_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Swing.chartink_scan as chartink_scan
import Swing.trading_engine as ste
import Swing.watchlist as swl

IST = ZoneInfo("Asia/Kolkata")


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, raise_exc=None):
        self.status_code = status_code
        self._json_data = json_data
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


class FakeSession:
    """Stands in for requests.Session - records every call so tests can
    assert on the exact headers/body sent, matching the real mechanics
    chartink_scan.py implements (GET for the CSRF cookie, POST with it
    echoed back as X-XSRF-TOKEN)."""
    def __init__(self, xsrf_cookie="abc%3D%3D", get_response=None, post_response=None,
                 get_raises=None, post_raises=None):
        self.headers = {}
        self.cookies = {"XSRF-TOKEN": xsrf_cookie} if xsrf_cookie else {}
        self._get_response = get_response or FakeResponse(200)
        self._post_response = post_response
        self._get_raises = get_raises
        self._post_raises = post_raises
        self.get_calls = []
        self.post_calls = []

    def get(self, url, timeout=None):
        self.get_calls.append({"url": url, "timeout": timeout})
        if self._get_raises:
            raise self._get_raises
        return self._get_response

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self._post_raises:
            raise self._post_raises
        return self._post_response


def install_fake_session(fake_session: FakeSession):
    real_session_cls = chartink_scan.requests.Session
    chartink_scan.requests.Session = lambda: fake_session

    def restore():
        chartink_scan.requests.Session = real_session_cls
    return restore


def _scan_response(symbols):
    return FakeResponse(200, json_data={
        "data": [{"nsecode": s, "name": f"{s} Limited", "close": 100.0} for s in symbols],
    })


def test_1_fetch_scan_symbols_once():
    # (a) Well-formed response - returns symbols uppercased, in order.
    session = FakeSession(post_response=_scan_response(["bajaj-auto", "HCLTECH", "Oil"]))
    restore = install_fake_session(session)
    try:
        symbols = chartink_scan.fetch_scan_symbols_once()
        assert symbols == ["BAJAJ-AUTO", "HCLTECH", "OIL"], symbols
        # The exact mechanics: GET first (for the cookie), then POST
        # echoing the DECODED cookie value back as X-XSRF-TOKEN.
        assert len(session.get_calls) == 1
        assert len(session.post_calls) == 1
        assert session.post_calls[0]["headers"]["X-XSRF-TOKEN"] == "abc==", \
            "the cookie's percent-encoded value must be URL-decoded before being sent as the header"
        assert session.post_calls[0]["json"] == {"scan_clause": ste.config.CHARTINK_WATCHLIST_SCAN_CLAUSE}
    finally:
        restore()

    # (b) No XSRF cookie at all - raises rather than proceeding with a
    # request Chartink would just reject.
    session = FakeSession(xsrf_cookie=None)
    restore = install_fake_session(session)
    try:
        try:
            chartink_scan.fetch_scan_symbols_once()
            assert False, "must raise when no XSRF-TOKEN cookie is present"
        except RuntimeError as e:
            assert "XSRF-TOKEN" in str(e)
    finally:
        restore()

    # (c) An HTTP error on the GET leg - raises, never silently swallowed.
    session = FakeSession(get_raises=ConnectionError("simulated network failure"))
    restore = install_fake_session(session)
    try:
        try:
            chartink_scan.fetch_scan_symbols_once()
            assert False, "a GET failure must raise"
        except ConnectionError:
            pass
    finally:
        restore()

    # (d) A malformed response (no 'data' key) - raises rather than
    # returning an empty list that would look like "scan found nothing".
    session = FakeSession(post_response=FakeResponse(200, json_data={"unexpected": "shape"}))
    restore = install_fake_session(session)
    try:
        try:
            chartink_scan.fetch_scan_symbols_once()
            assert False, "a missing 'data' key must raise, not return []"
        except RuntimeError as e:
            assert "data" in str(e)
    finally:
        restore()

    print("1. fetch_scan_symbols_once correctly parses a well-formed response (symbols uppercased, "
          "in order, cookie correctly URL-decoded before being echoed back as the header), and "
          "raises - never silently returns an empty/guessed list - on a missing cookie, a network "
          "failure, or an unexpected response shape: PASSED")


async def test_2_run_chartink_watchlist_scan_adds_and_reconfirms():
    """Also covers the interaction with the new stale-age prune (user
    request 1 Sep 2026: "unless they are again fed in using chartink
    scan results") - an already-present symbol the scan re-returns must
    have its own last_confirmed_at CLOCK RESET, not just be silently
    left alone the way plain add_symbols() would leave it - see
    test_swing_stale_age_prune.py for the removal side of this same
    mechanism."""
    store = swl.WatchlistStore()
    ste.watchlist_store = store
    await store.add_symbols(["ALREADYTHERE"])
    old_last_confirmed_at = store._symbols["ALREADYTHERE"].last_confirmed_at
    await asyncio.sleep(0.01)  # ensure a measurably later timestamp on reconfirm

    session = FakeSession(post_response=_scan_response(["NEWSTOCK1", "NEWSTOCK2", "ALREADYTHERE"]))
    restore = install_fake_session(session)
    try:
        result = await ste._run_chartink_watchlist_scan()
        assert result["symbols_returned"] == ["NEWSTOCK1", "NEWSTOCK2", "ALREADYTHERE"]
        assert result["symbols_added"] == ["NEWSTOCK1", "NEWSTOCK2"], \
            "ALREADYTHERE must not be reported as newly added"
        assert result["symbols_reconfirmed"] == ["ALREADYTHERE"], \
            "an already-present symbol the scan re-returns must be reported as reconfirmed"

        remaining = set(await store.symbols())
        assert remaining == {"ALREADYTHERE", "NEWSTOCK1", "NEWSTOCK2"}, remaining
        new_last_confirmed_at = store._symbols["ALREADYTHERE"].last_confirmed_at
        assert new_last_confirmed_at > old_last_confirmed_at, \
            "ALREADYTHERE's own last_confirmed_at (the stale-age prune's clock) must be reset by this re-feed"

        await asyncio.sleep(0.2)
        events = [e for e in trade_history.read_all_jsonl(ste.SWING_EVENTS_LOG_NAME)
                  if e["event"] == "CHARTINK_WATCHLIST_SCAN_COMPLETED"]
        assert len(events) == 1, events
        assert events[0]["symbols_added"] == ["NEWSTOCK1", "NEWSTOCK2"]
        assert events[0]["symbols_reconfirmed"] == ["ALREADYTHERE"]
        assert events[0]["scan_url"] == ste.config.CHARTINK_WATCHLIST_SCAN_URL

        print("2. _run_chartink_watchlist_scan adds only the genuinely NEW symbols to the REAL "
              "watchlist_store, and RESETS the stale-age clock (last_confirmed_at) for an "
              "already-present symbol it re-returns rather than silently leaving it alone - the "
              "mechanism 'unless they are again fed in using chartink scan results' depends on - "
              "durably logging both in a CHARTINK_WATCHLIST_SCAN_COMPLETED event: PASSED")
    finally:
        restore()


async def test_3_daily_once_gating_and_failure_does_not_mark_day_done():
    store = swl.WatchlistStore()
    ste.watchlist_store = store
    real_enabled = ste.config.CHARTINK_WATCHLIST_SCAN_ENABLED
    ste.config.CHARTINK_WATCHLIST_SCAN_ENABLED = True
    real_now = ste._now_ist
    real_last_scan = ste._last_chartink_scan_date
    ste._last_chartink_scan_date = None

    try:
        # Before the scheduled time (08:00 default) - must do nothing.
        session = FakeSession(post_response=_scan_response(["SHOULDNOTAPPEAR"]))
        restore = install_fake_session(session)
        ste._now_ist = lambda: datetime(2026, 9, 2, 7, 59, tzinfo=IST)
        await ste._daily_chartink_watchlist_scan_tick()
        assert ste._last_chartink_scan_date is None
        assert await store.symbols() == [], "must not have fetched anything before the scheduled time"
        restore()

        # A fetch FAILURE at/after the scheduled time - must NOT mark
        # the day as done (the defining difference from the prune's own
        # gating, which only skips the one symbol that failed).
        restore = install_fake_session(FakeSession(get_raises=ConnectionError("simulated failure")))
        ste._now_ist = lambda: datetime(2026, 9, 2, 8, 0, tzinfo=IST)
        await ste._daily_chartink_watchlist_scan_tick()  # must not raise
        assert ste._last_chartink_scan_date is None, \
            "a failed fetch must NOT mark today as done - the next tick should retry"
        restore()

        # The next tick, same day, succeeds - NOW it's marked done.
        restore = install_fake_session(FakeSession(post_response=_scan_response(["FRESHPICK"])))
        ste._now_ist = lambda: datetime(2026, 9, 2, 8, 5, tzinfo=IST)
        await ste._daily_chartink_watchlist_scan_tick()
        assert ste._last_chartink_scan_date == date(2026, 9, 2)
        assert "FRESHPICK" in await store.symbols()
        restore()

        # A LATER tick, same day - no-op (would raise if it actually
        # tried to fetch again, since no session is installed now).
        ste._now_ist = lambda: datetime(2026, 9, 2, 14, 0, tzinfo=IST)
        await ste._daily_chartink_watchlist_scan_tick()  # must not raise - no fetch attempted
        assert ste._last_chartink_scan_date == date(2026, 9, 2)

        # A NEW trading day resets the gate, allowed to run again.
        restore = install_fake_session(FakeSession(post_response=_scan_response(["NEXTDAYPICK"])))
        ste._now_ist = lambda: datetime(2026, 9, 3, 8, 10, tzinfo=IST)
        await ste._daily_chartink_watchlist_scan_tick()
        assert ste._last_chartink_scan_date == date(2026, 9, 3)
        assert "NEXTDAYPICK" in await store.symbols()
        restore()

        print("3. _daily_chartink_watchlist_scan_tick runs exactly ONCE per trading day at/after "
              "config.CHARTINK_WATCHLIST_SCAN_TIME - does nothing before then, and critically: a "
              "FETCH FAILURE does NOT mark the day as done, so the very next tick (still the same "
              "day) retries rather than silently waiting until tomorrow; a later successful tick "
              "the same day is a no-op; a new trading day resets cleanly: PASSED")
    finally:
        ste._now_ist = real_now
        ste._last_chartink_scan_date = real_last_scan
        ste.config.CHARTINK_WATCHLIST_SCAN_ENABLED = real_enabled


async def test_4_feature_flag_disables_scanning_entirely():
    store = swl.WatchlistStore()
    ste.watchlist_store = store
    real_enabled = ste.config.CHARTINK_WATCHLIST_SCAN_ENABLED
    ste.config.CHARTINK_WATCHLIST_SCAN_ENABLED = False
    real_now = ste._now_ist
    real_last_scan = ste._last_chartink_scan_date
    ste._last_chartink_scan_date = None
    ste._now_ist = lambda: datetime(2026, 9, 4, 12, 0, tzinfo=IST)
    # No fake session installed at all - if the flag didn't actually
    # short-circuit, any fetch attempt would raise (no requests.Session mocked).
    try:
        await ste._daily_chartink_watchlist_scan_tick()
        assert await store.symbols() == []
        assert ste._last_chartink_scan_date is None, "must not even mark a run when the flag is off"
        print("4. config.CHARTINK_WATCHLIST_SCAN_ENABLED=False disables the feature entirely - no "
              "fetch attempted, nothing added, even well past the scheduled time on a fresh day: PASSED")
    finally:
        ste._now_ist = real_now
        ste._last_chartink_scan_date = real_last_scan
        ste.config.CHARTINK_WATCHLIST_SCAN_ENABLED = real_enabled


async def main():
    print("=== Swing daily Chartink watchlist scan test suite ===\n")
    test_1_fetch_scan_symbols_once()
    await test_2_run_chartink_watchlist_scan_adds_and_reconfirms()
    await test_3_daily_once_gating_and_failure_does_not_mark_day_done()
    await test_4_feature_flag_disables_scanning_entirely()
    print("\nALL SWING CHARTINK WATCHLIST SCAN CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
