"""
Expiry-day square-off (expiry_square_off.py, 30 Sep / 1 Oct 2026): a position
whose own contract expires today is closed from 15:25 (MCX 23:25) whatever the
weekday, in Options, Luxury, Swing, Bollinger and the Options/Luxury paper
engine. Fully offline: the instrument master is replaced by a small fake.

HOW TO RUN:
    uv run python -m pytest tests/test_expiry_square_off.py -q
"""
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import expiry_square_off as ex  # noqa: E402
from Options.dhan_client import IST, dhan_wrapper  # noqa: E402

EXPIRY = date(2026, 10, 27)                         # a Tuesday monthly expiry
APL = "APLAPOLLO 27 OCT 2200 PUT"
LATER = "APLAPOLLO 23 NOV 2760 CALL"
MASTER = {APL: EXPIRY, LATER: date(2026, 11, 23)}


def at(d, h, m):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


@pytest.fixture(autouse=True)
def fake_master(monkeypatch):
    ex._expiry.clear(); ex._unknown_on.clear()

    def meta(trading_symbol, exchange=None):
        if trading_symbol == "TITAN":
            return {"expiry_date": float("nan")}
        if trading_symbol not in MASTER:
            raise ValueError(f"No instrument found for trading_symbol {trading_symbol}")
        return {"expiry_date": MASTER[trading_symbol]}
    monkeypatch.setattr(dhan_wrapper, "_instrument_meta", meta)
    yield
    ex._expiry.clear(); ex._unknown_on.clear()


def due(sym, when, mcx=False):
    return asyncio.run(ex.due_today(sym, mcx, when, "15:25", "23:25"))


def test_1_expiry_date_comes_from_the_instrument_master_and_is_cached(monkeypatch):
    assert ex.contract_expiry(APL, "NSE") == EXPIRY
    monkeypatch.setattr(dhan_wrapper, "_instrument_meta", lambda *a, **k: 1 / 0)
    assert ex.contract_expiry(APL, "NSE") == EXPIRY                 # cached - no second lookup
    assert ex.contract_expiry("NOPE 99 XYZ 1 CALL", "NSE") is None  # unknown -> None, never raises


def test_2_equity_has_no_expiry():
    assert ex.contract_expiry("TITAN", "NSE") is None


@pytest.mark.parametrize("when, expected", [
    (at(EXPIRY, 15, 24), False), (at(EXPIRY, 15, 25), True), (at(EXPIRY, 15, 29), True),
    (at(EXPIRY, 15, 31), False),                     # market closed - no order after the close
    (at(date(2026, 10, 26), 15, 26), False),         # the day before
    (at(date(2026, 10, 23), 15, 26), False),         # the Friday before: the Friday rule, not this one
    (at(date(2026, 10, 28), 15, 26), True),          # a contract already past its expiry
])
def test_3_window(when, expected):
    assert due(APL, when) is expected


def test_4_later_contract_and_mcx_times():
    assert not due(LATER, at(EXPIRY, 15, 26))
    assert not ex.in_window(at(EXPIRY, 15, 26), "23:25", True) and ex.in_window(at(EXPIRY, 23, 26), "23:25", True)


@pytest.mark.parametrize("pkg", ["Options", "Luxury"])
def test_5_options_and_luxury_close_only_the_expiring_contract(pkg, monkeypatch):
    te = __import__(f"{pkg}.trading_engine", fromlist=["x"])
    calls = []

    async def fake(reason, symbols=None):
        calls.append((reason, symbols))
    monkeypatch.setattr(te, "_square_off_all", fake)
    monkeypatch.setattr(te.position_store, "live_positions",
                        {"APLAPOLLO": NS(option_trading_symbol=APL), "OTHER": NS(option_trading_symbol=LATER)})
    monkeypatch.setattr(te, "_now_ist", lambda: at(EXPIRY, 15, 26))
    asyncio.run(te._expiry_day_square_off())
    assert calls == [("EXPIRY_DAY_SQUARE_OFF", {"APLAPOLLO"})]
    calls.clear()
    monkeypatch.setattr(te, "_now_ist", lambda: at(EXPIRY, 14, 0))
    asyncio.run(te._expiry_day_square_off())
    assert calls == []
    monkeypatch.setattr(te.config, "ENABLE_EXPIRY_DAY_SQUARE_OFF", False)
    monkeypatch.setattr(te, "_now_ist", lambda: at(EXPIRY, 15, 26))
    asyncio.run(te._expiry_day_square_off())
    assert calls == []


def test_6_swing_and_bollinger_hooks(monkeypatch):
    import Bollinger.trading_engine as bte
    import Swing.trading_engine as ste
    monkeypatch.setattr(ste, "_now_ist", lambda: at(EXPIRY, 15, 26))
    assert asyncio.run(ste._expires_today_and_due(NS(trading_symbol=APL, exchange_segment="NSE_FNO")))
    assert not asyncio.run(ste._expires_today_and_due(NS(trading_symbol=LATER, exchange_segment="NSE_FNO")))
    assert not asyncio.run(ste._expires_today_and_due(NS(trading_symbol="TITAN", exchange_segment="NSE_EQ")))
    monkeypatch.setattr(bte, "_now_ist", lambda: at(EXPIRY, 15, 26))
    book = {"APLAPOLLO": NS(trading_symbol=APL, exchange_segment="NSE_FNO"),
            "OTHER": NS(trading_symbol=LATER, exchange_segment="NSE_FNO")}
    assert asyncio.run(bte._expiring_today_and_due(book)) == {"APLAPOLLO"}
    monkeypatch.setattr(bte.config, "EXPIRY_DAY_SQUARE_OFF_ENABLED", False)
    assert asyncio.run(bte._expiring_today_and_due(book)) == set()


def test_7_options_luxury_paper_engine(monkeypatch):
    import breakout_paper_engine as bpe
    exits = []

    async def fake_exit(strategy, symbol, position, price, reason):
        exits.append(reason)

    async def ltp(_ts):
        return 12.0
    cfg = NS(ENABLE_EXPIRY_DAY_SQUARE_OFF=True, EXPIRY_DAY_SQUARE_OFF_TIME="15:25", ENABLE_SUPERTREND_EXIT=False,
             ENABLE_EMA_CROSS_EXIT=False, LIQUIDITY_GUARD_ENABLED=False)
    monkeypatch.setattr(bpe, "_exit_one", fake_exit)
    monkeypatch.setitem(bpe._HOOKS, "Options", NS(cfg=cfg, get_ltp=ltp, exit_reason_for=lambda *a: None))
    fixed = {"t": at(EXPIRY, 15, 26)}

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed["t"]
    monkeypatch.setattr(bpe, "datetime", FakeDT)
    paper = NS(option_trading_symbol=APL, highest_price=12.0, underlying_symbol="APLAPOLLO")
    asyncio.run(bpe._check_one("Options", "APLAPOLLO", paper))
    assert exits == ["EXPIRY_DAY_SQUARE_OFF"]
    exits.clear(); fixed["t"] = at(date(2026, 10, 26), 15, 26)
    asyncio.run(bpe._check_one("Options", "APLAPOLLO", paper))
    assert exits == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
