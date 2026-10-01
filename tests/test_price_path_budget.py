"""
Tests for the 1 Oct 2026 price-path fixes (trading-skills learnings/price-path-
cost-rest-budget-and-memory.md):
  1. instrument-master lookups are memoized (they were a ~69 ms pandas scan on
     every WS-cache price read);
  2. ONE ~1/s budget for Dhan's /marketfeed/ltp, /ohlc and /quote calls, the
     wait happens outside any lock (on the event loop for async callers), REST
     LTP goes straight to Dhan by security id (no Tradehull df copy + 0.4 s
     sleep), and a quiet contract's cached tick is trusted while the feed is
     alive;
  3. Super Bollinger reads each position's price once per cycle, shared by the
     monitor loop, the supervisor and the disaster brake.
Fully offline - every Dhan call is a fake.

HOW TO RUN:
    uv run python -m pytest tests/test_price_path_budget.py -q
"""
import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Bollinger.trading_engine as boll  # noqa: E402
import Options.config as dcfg  # noqa: E402
import Options.dhan_client as dc  # noqa: E402
from Bollinger.position_store import Position  # noqa: E402
from Options.dhan_client import DhanWrapper, IST, dhan_wrapper  # noqa: E402
from SuperBollinger import pricing  # noqa: E402


def _master() -> pd.DataFrame:
    return pd.DataFrame([
        {"SEM_TRADING_SYMBOL": "APLAPOLLO-Oct2026-2240-CE", "SEM_CUSTOM_SYMBOL": "APLAPOLLO 27 OCT 2240 CALL",
         "SEM_EXM_EXCH_ID": "NSE", "SEM_SMST_SECURITY_ID": 111, "SEM_LOT_UNITS": 350.0,
         "SEM_EXPIRY_DATE": "2026-10-27 14:30:00", "SEM_TICK_SIZE": 5.0, "SEM_INSTRUMENT_NAME": "OPTSTK",
         "SEM_SERIES": "NA"},
        {"SEM_TRADING_SYMBOL": "APLAPOLLO", "SEM_CUSTOM_SYMBOL": "APL Apollo Tubes", "SEM_EXM_EXCH_ID": "NSE",
         "SEM_SMST_SECURITY_ID": 25780, "SEM_LOT_UNITS": 1.0, "SEM_EXPIRY_DATE": None, "SEM_TICK_SIZE": 10.0,
         "SEM_INSTRUMENT_NAME": "EQUITY", "SEM_SERIES": "EQ"},
    ])


def _wrapper(df=None) -> DhanWrapper:
    w = DhanWrapper()
    w._client = SimpleNamespace(instrument_df=df if df is not None else _master())
    return w


def _pos(ts="APLAPOLLO 27 OCT 2240 CALL", order_id="X", qty=350, entry=50.0):
    return Position(underlying_symbol="APLAPOLLO", trading_symbol=ts, resolved_option_type="CE",
                    instrument_side="LONG", exchange_segment="NSE_FNO", product_type="MARGIN", quantity=qty,
                    lot_size=qty, entry_price=entry, best_price=entry, stop_pct=0.0, hard_stop_loss=0.05,
                    trailing_stop_dist=1e12, trailing_step=1e12, pnl_multiplier=qty, order_id=order_id)


@pytest.fixture
def budget(monkeypatch):
    """A fresh quote budget at the real 1.1 s spacing."""
    monkeypatch.setattr(DhanWrapper, "QUOTE_MIN_GAP_SECONDS", 1.1)
    monkeypatch.setitem(DhanWrapper._quote_budget, "next_at", 0.0)
    yield
    DhanWrapper._quote_budget["next_at"] = 0.0


@pytest.fixture
def shared_prices():
    for d in (pricing._shared, pricing._inflight, pricing._quotes, pricing._quote_inflight):
        d.clear()
    yield
    for d in (pricing._shared, pricing._inflight, pricing._quotes, pricing._quote_inflight):
        d.clear()


# --------------------------------------------------------------------------- #
# 1. Memoized instrument lookups
# --------------------------------------------------------------------------- #
def test_1_instrument_meta_scans_the_master_once_per_symbol(monkeypatch):
    w = _wrapper()
    scans = {"n": 0}
    real = DhanWrapper._instrument_meta_uncached

    def counting(self, df, ts, ex):
        scans["n"] += 1
        return real(self, df, ts, ex)
    monkeypatch.setattr(DhanWrapper, "_instrument_meta_uncached", counting)

    first = w._instrument_meta("APLAPOLLO 27 OCT 2240 CALL", expected_exchange="NSE")
    second = w._instrument_meta("APLAPOLLO 27 OCT 2240 CALL", expected_exchange="NSE")
    assert first == second and first["security_id"] == "111" and first["lot_size"] == 350
    assert scans["n"] == 1, "the second lookup must come from the cache"
    assert w.stats["instrument_meta_cache_hits"] == 1

    first["security_id"] = "corrupted"            # callers get a copy - the cache cannot be poisoned
    assert w._instrument_meta("APLAPOLLO 27 OCT 2240 CALL", expected_exchange="NSE")["security_id"] == "111"

    # a different exchange hint is a different question
    with pytest.raises(ValueError):
        w._instrument_meta("APLAPOLLO 27 OCT 2240 CALL", expected_exchange="MCX")
    with pytest.raises(ValueError):                 # misses are not cached - looked up in full again
        w._instrument_meta("APLAPOLLO 27 OCT 2240 CALL", expected_exchange="MCX")
    assert scans["n"] == 3

    w._client.instrument_df = _master()           # a re-downloaded master starts a fresh cache
    w._instrument_meta("APLAPOLLO 27 OCT 2240 CALL", expected_exchange="NSE")
    assert scans["n"] == 4


def test_2_security_id_and_equity_lookups_are_memoized():
    w = _wrapper()
    assert w._instrument_meta_by_security_id("111")["trading_symbol"] == "APLAPOLLO 27 OCT 2240 CALL"
    assert w._equity_security_id("APLAPOLLO") == "25780"
    df = w._client.instrument_df
    df.drop(df.index, inplace=True)                # same frame, now empty: only a cache can answer
    assert w._instrument_meta_by_security_id("111")["lot_size"] == 350
    assert w._equity_security_id("APLAPOLLO") == "25780"
    with pytest.raises(ValueError):
        w._equity_security_id("NOTLISTED")


# --------------------------------------------------------------------------- #
# 2. One quote budget for LTP / OHLC / quote
# --------------------------------------------------------------------------- #
def test_3_quote_slots_are_spaced_and_a_full_queue_is_refused(budget):
    w = _wrapper()
    waits = [w._reserve_quote_slot() for _ in range(3)]
    assert waits[0] == pytest.approx(0.0, abs=0.01)
    assert waits[1] == pytest.approx(1.1, abs=0.02)
    assert waits[2] == pytest.approx(2.2, abs=0.02)
    before = DhanWrapper._quote_budget["next_at"]
    assert w._reserve_quote_slot(max_wait=1.0) is None, "a 3.3 s queue is longer than max_wait"
    assert DhanWrapper._quote_budget["next_at"] == before, "a refused reservation must not take a slot"
    assert w.stats["quote_budget_busy"] == 1


def test_4_a_delayed_call_is_slotted_where_its_request_lands(budget):
    """Tradehull's OHLC call sleeps 2 s before its request: the slot is placed
    at the landing time, and a later call is spaced from THAT."""
    w = _wrapper()
    t0 = time.monotonic()
    assert w._reserve_quote_slot(delay=2.0) == pytest.approx(0.0, abs=0.01)
    assert DhanWrapper._quote_budget["next_at"] - t0 == pytest.approx(3.1, abs=0.05)
    assert w._reserve_quote_slot() == pytest.approx(3.1, abs=0.05)


def test_5_ltp_and_quote_requests_never_reach_dhan_closer_than_the_gap(budget, monkeypatch):
    w = _wrapper()
    hits: list[float] = []
    lock = threading.Lock()

    def record():
        with lock:
            hits.append(time.monotonic())

    def ticker_data(securities):
        record()
        seg, ids = next(iter(securities.items()))
        return {"status": "success", "data": {"data": {seg: {str(ids[0]): {"last_price": 51.5}}}}}

    def quote_data(securities):
        record()
        sid = str(securities["NSE_FNO"][0])
        return {"status": "success", "data": {"data": {"NSE_FNO": {sid: {
            "last_price": 51.5, "depth": {"buy": [{"price": 51.4, "quantity": 350}],
                                          "sell": [{"price": 51.6, "quantity": 700}]}}}}}}
    w._client.Dhan = SimpleNamespace(ticker_data=ticker_data, quote_data=quote_data)

    async def run():
        loop = asyncio.get_running_loop()
        return await asyncio.gather(
            w.get_option_ltp_async("APLAPOLLO 27 OCT 2240 CALL"),
            loop.run_in_executor(None, w.get_option_quote, "APLAPOLLO 27 OCT 2240 CALL"),
            w.get_option_ltp_async("APLAPOLLO 27 OCT 2240 CALL"),
            loop.run_in_executor(None, w.get_option_quote, "APLAPOLLO 27 OCT 2240 CALL"),
        )
    results = asyncio.run(run())
    assert results[0] == 51.5 and results[2] == 51.5
    assert results[1]["bid"] == 51.4 and results[3]["ask"] == 51.6
    hits.sort()
    gaps = [b - a for a, b in zip(hits, hits[1:])]
    assert len(hits) == 4 and min(gaps) >= 1.05, f"requests too close together: {gaps}"


def test_6_waiting_for_a_slot_does_not_hold_a_worker_thread(budget):
    w = _wrapper()
    w._client.Dhan = SimpleNamespace(ticker_data=lambda s: {"status": "success", "data": {"data": {
        "NSE_FNO": {"111": {"last_price": 10.0}}}}})
    DhanWrapper._quote_budget["next_at"] = time.monotonic() + 1.0     # the next slot is 1 s away

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        ltp_task = asyncio.create_task(w.get_option_ltp_async("APLAPOLLO 27 OCT 2240 CALL"))
        await asyncio.sleep(0.1)
        t = time.monotonic()
        await loop.run_in_executor(None, lambda: None)                # needs the ONLY worker
        other_job_waited = time.monotonic() - t
        return await ltp_task, other_job_waited
    price, waited = asyncio.run(run())
    assert price == 10.0
    assert waited < 0.3, f"the LTP call held the only worker while waiting for its slot ({waited:.2f}s)"


def test_7_rest_ltp_goes_direct_by_security_id_and_tradehull_only_for_names(budget):
    w = _wrapper()
    calls = []

    def ticker_data(securities):
        calls.append(securities)
        return {"status": "success", "data": {"data": {"NSE_FNO": {"111": {"last_price": 48.25}}}}}

    def tradehull_ltp(names):
        calls.append(("tradehull", names))
        return {names[0]: 2400.0}
    w._client.Dhan = SimpleNamespace(ticker_data=ticker_data)
    w._client.get_ltp_data = tradehull_ltp

    assert w._get_option_ltp_once("APLAPOLLO 27 OCT 2240 CALL") == 48.25
    assert calls[-1] == {"NSE_FNO": [111]}
    assert w._get_option_ltp_once("APLAPOLLO") == 2400.0, "an equity name keeps Tradehull's name lookup"
    assert calls[-1] == ("tradehull", ["APLAPOLLO"])

    # Dhan's rate-limit / failure envelope -> the same error the retry paths already expect
    w._client.Dhan = SimpleNamespace(ticker_data=lambda s: {
        "status": "failure", "remarks": {"error_code": None, "error_type": None, "error_message": None}, "data": ""})
    with pytest.raises(ValueError, match="No LTP returned"):
        w._get_option_ltp_once("APLAPOLLO 27 OCT 2240 CALL")


def test_8_mcx_contracts_are_read_on_the_mcx_segment(budget, monkeypatch):
    w = _wrapper()
    monkeypatch.setattr(w, "_expected_exchange_for", lambda ts: "MCX")
    monkeypatch.setattr(w, "_instrument_meta", lambda ts, expected_exchange=None: {"security_id": "472780"})
    seen = []

    def ticker_data(securities):
        seen.append(securities)
        return {"status": "success", "data": {"data": {"MCX_COMM": {"472780": {"last_price": 812.0}}}}}
    w._client.Dhan = SimpleNamespace(ticker_data=ticker_data)
    assert w._get_option_ltp_once("COPPER 28 NOV 810 CALL") == 812.0
    assert seen == [{"MCX_COMM": [472780]}]


def test_9_quiet_contract_is_trusted_only_while_the_feed_is_alive_and_it_has_ticked(monkeypatch):
    w = _wrapper()
    monkeypatch.setattr(dcfg, "ENABLE_WS_FEED", True)
    monkeypatch.setattr(dcfg, "LTP_STALE_AFTER_SECONDS", 5.0)
    monkeypatch.setattr(dcfg, "LTP_FEED_HEALTHY_MAX_AGE_SECONDS", 15.0)
    monkeypatch.setattr(dcfg, "FEED_ALIVE_MAX_SILENCE_SECONDS", 5.0)
    ts = "APLAPOLLO 27 OCT 2240 CALL"

    def age(seconds):
        w._ltp_cache_ts["111"] = datetime.now(IST) - timedelta(seconds=seconds)

    w._on_market_tick(None, {"security_id": 111, "LTP": 50.0})       # the contract's own subscription works
    w._on_market_tick(None, {"security_id": 25780, "LTP": 2400.0})   # feed alive
    age(10)
    assert w.get_cached_option_ltp(ts) == 50.0, "quiet contract, live feed: the last trade is still the price"
    assert w.stats["ltp_quiet_contract_trusted"] == 1
    age(20)
    assert w.get_cached_option_ltp(ts) is None, "older than LTP_FEED_HEALTHY_MAX_AGE_SECONDS -> one REST check"

    age(10)
    w._last_feed_tick_at = time.monotonic() - 10                     # feed silent
    assert w.get_cached_option_ltp(ts) is None, "dead feed -> the old 5 s rule"

    w._on_market_tick(None, {"security_id": 25780, "LTP": 2401.0})   # feed back
    w._on_market_connect(None)                                       # ...but after a reconnect
    age(10)
    assert w.get_cached_option_ltp(ts) is None, "after a reconnect the contract must tick again first"

    w.note_rest_ltp(ts, 49.0)                                        # a REST re-prime is not a WS tick
    age(10)
    assert w.get_cached_option_ltp(ts) is None

    w._on_market_tick(None, {"security_id": 111, "LTP": 49.5})
    age(3)
    assert w.get_cached_option_ltp(ts) == 49.5, "a fresh tick is always fine"
    monkeypatch.setattr(dcfg, "LTP_FEED_HEALTHY_MAX_AGE_SECONDS", 0.0)
    age(10)
    assert w.get_cached_option_ltp(ts) is None, "0 = off: the old 5 s rule"


def test_10_unsubscribe_forgets_the_contract(monkeypatch):
    w = _wrapper()
    monkeypatch.setattr(dcfg, "ENABLE_WS_FEED", True)
    w._on_market_tick(None, {"security_id": 111, "LTP": 50.0})
    assert "111" in w._ws_ticked
    w.unsubscribe_option_price("APLAPOLLO 27 OCT 2240 CALL")
    assert "111" not in w._ws_ticked


# --------------------------------------------------------------------------- #
# 3. One price read per position per cycle (Super Bollinger)
# --------------------------------------------------------------------------- #
def test_11_concurrent_and_back_to_back_callers_share_one_read(shared_prices, monkeypatch):
    reads = {"n": 0}

    async def fake_get_ltp(position):
        reads["n"] += 1
        await asyncio.sleep(0.05)
        return 50.0 + reads["n"]
    monkeypatch.setattr(boll, "_get_ltp", fake_get_ltp)

    async def run():
        real = _pos()
        prices = await asyncio.gather(*[pricing.position_price(real) for _ in range(3)])
        again = await pricing.position_price(real)           # within SHARED_PRICE_MAX_AGE_SECONDS
        paper = await pricing.position_price(_pos(order_id="PAPER"))   # paper is never shared with real
        return prices, again, paper
    prices, again, paper = asyncio.run(run())
    assert prices == [51.0, 51.0, 51.0] and again == 51.0
    assert paper == 52.0 and reads["n"] == 2


def test_12_a_new_cycle_reads_again(shared_prices, monkeypatch):
    monkeypatch.setattr(pricing, "SHARED_PRICE_MAX_AGE_SECONDS", 0.05)
    reads = {"n": 0}

    async def fake_get_ltp(position):
        reads["n"] += 1
        return 40.0 + reads["n"]
    monkeypatch.setattr(boll, "_get_ltp", fake_get_ltp)

    async def run():
        first = await pricing.position_price(_pos())
        await asyncio.sleep(0.1)
        return first, await pricing.position_price(_pos())
    assert asyncio.run(run()) == (41.0, 42.0)


def test_13_a_failed_or_cancelled_read_never_leaves_a_waiter_hanging(shared_prices, monkeypatch):
    gate = {"fail": True}

    async def fake_get_ltp(position):
        await asyncio.sleep(0.05)
        if gate["fail"]:
            raise ValueError("No LTP returned")
        await asyncio.sleep(10)
        return 1.0
    monkeypatch.setattr(boll, "_get_ltp", fake_get_ltp)

    async def run():
        pos = _pos()
        owner = asyncio.create_task(pricing.position_price(pos))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(pricing.position_price(pos))
        with pytest.raises(ValueError):
            await owner                                         # the reader sees its own error
        with pytest.raises(pricing.NoSharedPrice):
            await asyncio.wait_for(waiter, 1.0)                 # the waiter is told, not left hanging
        with pytest.raises(pricing.NoSharedPrice):              # the failure counts for this cycle too
            await pricing.position_price(pos)

        pricing._shared.clear()
        gate["fail"] = False
        owner = asyncio.create_task(pricing.position_price(pos))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(pricing.position_price(pos))
        await asyncio.sleep(0.1)
        owner.cancel()                                          # e.g. a caller's wait_for timed out
        with pytest.raises(pricing.NoSharedPrice):
            await asyncio.wait_for(waiter, 1.0)
        assert not pricing._inflight
        assert ("APLAPOLLO 27 OCT 2240 CALL", False) not in pricing._shared, "a cancellation is not cached"
    asyncio.run(run())


def test_14_monitor_supervisor_and_brake_cost_one_rest_call(shared_prices, monkeypatch):
    """The real _get_ltp path with a stale WS cache: the monitor loop
    (live_price), the hedge loop (position_price) and the brake (live_price)
    reading the same real position at the same moment -> ONE REST call."""
    rest = {"n": 0}

    async def rest_ltp(ts, **_k):
        rest["n"] += 1
        await asyncio.sleep(0.05)
        return 47.5
    monkeypatch.setattr(dhan_wrapper, "get_cached_option_ltp", lambda ts: None)
    monkeypatch.setattr(dhan_wrapper, "note_rest_ltp", lambda ts, ltp: None)
    monkeypatch.setattr(dhan_wrapper, "get_option_ltp_async", rest_ltp)

    async def run():
        pos = _pos()
        return await asyncio.gather(pricing.live_price(pos), pricing.position_price(pos), pricing.live_price(pos))
    assert asyncio.run(run()) == [47.5, 47.5, 47.5]
    assert rest["n"] == 1


def test_15_concurrent_order_book_fallbacks_make_one_quote_call(shared_prices, monkeypatch):
    calls = {"n": 0}

    def get_quote(ts, attempts=3):
        calls["n"] += 1
        time.sleep(0.05)
        return {"ltp": 47.0, "bid": 46.9, "ask": 47.1}
    monkeypatch.setattr(dhan_wrapper, "get_option_quote", get_quote)

    async def no_price(position):
        raise ValueError("No LTP returned")
    monkeypatch.setattr(boll, "_get_ltp", no_price)

    async def run():
        pos = _pos()
        return await asyncio.gather(pricing.live_price(pos), pricing.live_price(pos), pricing.live_price(pos))
    assert asyncio.run(run()) == [47.0, 47.0, 47.0]
    assert calls["n"] == 1
