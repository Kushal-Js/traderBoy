"""
Three pieces added 30 Sep / 1 Oct 2026, fully offline:
  - stock_selection.py: the 60-minute candles and the 1-hour-filtered fit used by
    the SHADOW list, run_shadow's record + weekly score, shadow_status;
  - SuperBollinger/entry_filters.last_closed_candle (the live 1-hour-green filter);
  - order_safety.cancel_unfilled / outcome_event (never leave an unfilled order resting).

HOW TO RUN:
    uv run python -m pytest tests/test_stock_selection_shadow_filters_orders.py -q
"""
import asyncio
import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import order_safety  # noqa: E402
import stock_selection as S  # noqa: E402
from Options.dhan_client import IST, OrderResult, OrderStatus  # noqa: E402
from SuperBollinger.entry_filters import last_closed_candle  # noqa: E402

DAY0 = date(2026, 9, 21)


def bars(days, price_fn):
    """5-min bars 09:15-15:25 for `days` sessions; price_fn(k) -> (open, high, low, close)."""
    ts, o, h, l, c, k = [], [], [], [], [], 0
    d = DAY0
    for _ in range(days):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        t = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
        for _i in range(75):
            oo, hh, ll, cc = price_fn(k)
            ts.append(int(t.timestamp())); o.append(oo); h.append(hh); l.append(ll); c.append(cc)
            t += timedelta(minutes=5); k += 1
        d += timedelta(days=1)
    return {"timestamps": ts, "opens": o, "highs": h, "lows": l, "closes": c}


# --------------------------------------------------------------------------- #
# stock_selection: candles, filtered fit, shadow record
# --------------------------------------------------------------------------- #
def test_1_hourly_candles_are_anchored_at_0915_with_a_1515_stub():
    fast = bars(1, lambda k: (100 + k, 100 + k + 0.5, 100 + k - 0.5, 100 + k + 0.2 if k < 12 else 100 + k - 0.2))
    ends, green = S.hourly_candles(fast)
    assert len(ends) == 7                                    # 09:15, 10:15, ... 14:15 and the 15:15-15:30 stub
    first_end = datetime.fromtimestamp(ends[0], IST)
    assert (first_end.hour, first_end.minute) == (10, 15)
    assert green[0] is True                                  # 09:15-10:15: opened 100, closed 111.2


def test_2_the_hour_filter_only_ever_removes_trades():
    rng = random.Random(3)
    px = [100.0]

    def walk(_k):
        o = px[-1]
        c = o * (1 + rng.gauss(0, 0.004))
        px.append(c)
        return o, max(o, c) * 1.001, min(o, c) * 0.999, c
    fast = bars(12, walk)
    plain, filt = S.fit_by_day(fast, False), S.fit_by_day(fast, True)
    assert sum(v[1] for v in filt.values()) <= sum(v[1] for v in plain.values())
    assert set(filt) <= set(plain)


def test_3_run_shadow_records_both_lists_then_scores_them_a_week_later(tmp_path, monkeypatch):
    rec, scores = tmp_path / "rec.json", tmp_path / "scores.jsonl"
    week1 = [date(2026, 9, 21) + timedelta(days=i) for i in range(5)]
    week2 = week1 + [date(2026, 9, 28), date(2026, 9, 29)]
    state = {"sessions": week1}

    def fetcher(days, seen, deadline=None):
        seen.update(state["sessions"])
        return lambda sym: {"sym": sym}
    monkeypatch.setattr(S, "_fast_fetcher", fetcher)
    picks = [{"symbol": f"S{i}", "source": "strategy_fit", "fit_pct": 1.0, "fit_trades": 3, "ath_score": 50.0} for i in range(12)]
    monkeypatch.setattr(S, "hybrid_select", lambda *a, **k: picks)
    monkeypatch.setattr(S, "fit_by_day", lambda fast, hour_filter=False: {
        date(2026, 9, 25): (5.0, 1), date(2026, 9, 28): (1.0 if fast["sym"] == "S0" else 0.5, 2), date(2026, 9, 29): (-0.2, 1)})
    monkeypatch.setitem(S._LAST, "dailies", {"X": {"close": [1.0], "high": [1.0], "low": [1.0], "volume": [1.0]}})
    lines = []
    r1 = S.run_shadow(lines.append, ["S0", "L1"], record_path=rec, scores_path=scores)
    assert r1["as_of"] == "2026-09-25" and r1["live"]["symbols"] == ["S0", "L1"] and len(r1["shadow"]["symbols"]) == 12
    assert r1["only_live"] == ["L1"] and not scores.exists()
    state["sessions"] = week2
    S.run_shadow(lines.append, ["S0", "L1"], record_path=rec, scores_path=scores)
    score = json.loads(scores.read_text().splitlines()[-1])
    assert score["as_of"] == "2026-09-25" and score["sessions"] == ["2026-09-28", "2026-09-29"]
    assert score["live"]["pct"] == round((1.0 - 0.2) + (0.5 - 0.2), 2)          # S0 and L1, only days after the pick
    assert score["shadow"]["trades"] == 12 * 3 and score["in_both"] == ["S0"]
    monkeypatch.setattr(S, "SHADOW_FILE", rec)
    monkeypatch.setattr(S, "SHADOW_SCORES_FILE", scores)
    st = S.shadow_status()
    assert st["traded"] is False and st["weeks_scored"] == 1 and "per_symbol" not in st["scores"][0]["live"]
    assert st["current"]["as_of"] == "2026-09-29"


def test_4_run_shadow_records_nothing_without_data_or_with_too_few_picks(tmp_path, monkeypatch):
    rec = tmp_path / "rec.json"
    monkeypatch.setattr(S, "_fast_fetcher", lambda days, seen, deadline=None: (lambda s: None))
    monkeypatch.setitem(S._LAST, "dailies", {"X": {"close": [1.0], "high": [1.0], "low": [1.0], "volume": [1.0]}})
    assert S.run_shadow(lambda m: None, ["A"], record_path=rec, scores_path=tmp_path / "s") is None and not rec.exists()

    def fetcher(days, seen, deadline=None):
        seen.add(date(2026, 9, 25))
        return lambda s: {}
    monkeypatch.setattr(S, "_fast_fetcher", fetcher)
    monkeypatch.setattr(S, "hybrid_select", lambda *a, **k: [{"symbol": "A", "fit_pct": 0, "fit_trades": 0, "ath_score": 1,
                                                                "source": "ath_fill"}])
    assert S.run_shadow(lambda m: None, ["A"], record_path=rec, scores_path=tmp_path / "s") is None and not rec.exists()


# --------------------------------------------------------------------------- #
# The live 1-hour-green filter's candle
# --------------------------------------------------------------------------- #
def two_sessions():
    d1, d2 = datetime(2026, 9, 29, 9, 15, tzinfo=IST), datetime(2026, 9, 30, 9, 15, tzinfo=IST)
    ts, o, c = [], [], []
    for start in (d1, d2):
        for i in range(75):
            t = start + timedelta(minutes=5 * i)
            ts.append(int(t.timestamp())); o.append(100.0 + i); c.append(100.0 + i + (1 if start == d2 else -1))
    return ts, o, c


def test_5_last_closed_candle():
    ts, o, c = two_sessions()
    at = lambda h, m: datetime(2026, 9, 30, h, m, tzinfo=IST)            # noqa: E731
    before_first_hour = last_closed_candle(ts, o, c, at(10, 14))
    assert before_first_hour["start"] == datetime(2026, 9, 29, 15, 15, tzinfo=IST)       # the previous session's stub
    first = last_closed_candle(ts, o, c, at(10, 20))
    assert first["start"] == at(9, 15) and (first["open"], first["close"]) == (100.0, 112.0)
    assert last_closed_candle(ts, o, c, at(15, 31))["start"] == at(15, 15)               # the stub counts after 15:30
    assert last_closed_candle(ts, o, c, at(15, 29))["start"] == at(14, 15)
    assert last_closed_candle([], [], [], at(10, 20)) is None


# --------------------------------------------------------------------------- #
# order_safety
# --------------------------------------------------------------------------- #
def res(status, filled=0):
    return OrderResult(order_id="1", status=status, remark="", fill_price=10.0, filled_quantity=filled)


@pytest.fixture
def broker(monkeypatch):
    calls = {"cancel": 0, "final": None, "cancel_raises": False}

    def cancel(order_id):
        calls["cancel"] += 1
        if calls["cancel_raises"]:
            raise RuntimeError("DH-906")

    def wait(order_id, is_amo, polls, interval):
        return calls["final"]
    monkeypatch.setattr(order_safety.dhan_wrapper, "cancel_order", cancel)
    monkeypatch.setattr(order_safety.dhan_wrapper, "wait_for_order_result", wait)
    return calls


def test_6_a_filled_order_is_left_alone(broker):
    final, err = asyncio.run(order_safety.cancel_unfilled("1", res(OrderStatus.TRADED, 100)))
    assert final.status == OrderStatus.TRADED and err is None and broker["cancel"] == 0


@pytest.mark.parametrize("after, event", [
    (OrderStatus.CANCELLED, "ORDER_UNFILLED_CANCELLED"),
    (OrderStatus.TRADED, "ORDER_FILLED_DURING_CANCEL"),
    (OrderStatus.PENDING, "ORDER_STILL_RESTING_AT_BROKER"),
])
def test_7_an_open_order_is_cancelled_and_its_final_status_read_again(broker, after, event):
    broker["final"] = res(after, 100 if after == OrderStatus.TRADED else 0)
    final, err = asyncio.run(order_safety.cancel_unfilled("1", res(OrderStatus.PENDING)))
    assert broker["cancel"] == 1 and final.status == after and order_safety.outcome_event(final) == event


def test_8_a_failed_cancel_is_reported_and_the_status_still_re_read(broker):
    broker.update(cancel_raises=True, final=res(OrderStatus.TRADED, 100))
    final, err = asyncio.run(order_safety.cancel_unfilled("1", res(OrderStatus.PENDING)))
    assert "DH-906" in err and final.status == OrderStatus.TRADED


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
