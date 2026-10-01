"""
Ratcheted broker stop (SuperBollinger/stop_ratchet.py, 1 Oct 2026): the broker's
stop-loss order of a REAL hedge PUT follows the 30% trail up; a REAL call's goes
to the entry price once it has been +1,500; at least Rs 250 at a time, at most one
change per 5 s per order, never at/above the live price; a failed move changes
nothing else. Fully offline - Dhan's modify call is faked.

HOW TO RUN:
    uv run python -m pytest tests/test_super_bollinger_stop_ratchet.py -q
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import SuperBollinger.stop_ratchet as sr  # noqa: E402
import SuperBollinger.supervisor as sup  # noqa: E402
import SuperBollinger.trading_engine as te  # noqa: E402
from Options.dhan_client import dhan_wrapper  # noqa: E402
from SuperBollinger import settings  # noqa: E402


def pos(entry=53.10, best=None, qty=350, sl="SL-1", sym="APLAPOLLO", ts="APLAPOLLO 27 OCT 2200 PUT"):
    return NS(underlying_symbol=sym, trading_symbol=ts, entry_price=entry, best_price=best or entry, quantity=qty,
              pnl_multiplier=qty, stop_loss_order_id=sl, pending_exit_order_id=None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "STATE_FILE", tmp_path / "ratchet.json")
    monkeypatch.setattr(sr, "_state", None)
    sr._inflight.clear(); sr._last_attempt.clear()
    monkeypatch.setattr(settings, "_overrides_loaded", True)
    for k, v in (("stop_ratchet_mode", "on"), ("stop_ratchet_min_step_rs", 250.0), ("stop_ratchet_min_interval_seconds", 5.0),
                 ("hedge_trail_arm_rs", 1000.0), ("hedge_trail_giveback", 0.30), ("hedge_stop_rs", 1500.0),
                 ("max_loss_rs", 4500.0), ("breakeven_after_rs", 1500.0)):
        monkeypatch.setitem(settings._overrides, k, v)
    calls, events = [], []

    def modify(order_id, trading_symbol, qty, trigger, limit):
        if calls_cfg["fail"]:
            raise RuntimeError("DH-906 rejected")
        calls.append((order_id, round(trigger, 2), round(limit, 2), qty))
        return {"order_id": order_id, "trigger_price": round(trigger, 2), "limit_price": round(limit, 2)}
    calls_cfg = {"fail": False}
    monkeypatch.setattr(dhan_wrapper, "modify_stop_loss_limit_order", modify, raising=False)

    async def ev(event, symbol, detail, log_name="x"):
        events.append((event, symbol, detail))
    monkeypatch.setattr(sr.engine, "_record_bollinger_event", ev)
    yield NS(calls=calls, events=events, cfg=calls_cfg)
    sr._inflight.clear(); sr._last_attempt.clear()


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


def run(kind, p, ltp):
    async def go():
        await sr.maybe_ratchet(kind, p.underlying_symbol, p, ltp)
        await _drain()
    asyncio.run(go())


def test_1_target_levels(env):
    assert sr.desired_trigger("hedge", 53.10, 55.80, 350) is None                       # peak +945: trail not armed
    assert sr.desired_trigger("hedge", 53.10, 61.70, 350) == pytest.approx(53.10 + 0.7 * 8.6)   # peak +3,010 -> keep 70%
    assert sr.desired_trigger("ce", 77.05, 81.30, 350) is None                          # +1,487: not yet
    assert sr.desired_trigger("ce", 77.05, 81.35, 350) == 77.05                         # +1,505: breakeven


def test_2_hedge_follows_the_trail_up_in_steps(env, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(sr.time, "monotonic", lambda: clock["t"])
    p = pos(best=61.70)
    run("hedge", p, 61.70)
    lvl = 53.10 + 0.7 * 8.6 - sr.BOT_FIRST_GAP              # two ticks under the bot's own 30%-giveback level
    assert env.calls == [("SL-1", round(lvl, 2), round(lvl - 75 / 350, 2), 350)]
    assert env.events[-1][0] == "STOP_RATCHET_MOVED" and env.events[-1][2]["locks_rs"] == round((lvl - 53.10) * 350)
    p.best_price = 62.20                                       # level up by only +245 on 350 -> under the Rs 250 step
    clock["t"] += 1
    run("hedge", p, 62.20)
    assert len(env.calls) == 1
    p.best_price = 63.00                                       # +3,465 peak -> level +2,425: +318 over the order
    clock["t"] += 2
    run("hedge", p, 63.00)
    assert len(env.calls) == 1                                 # 3 s after the last change: within the 5 s limit
    clock["t"] += 2
    run("hedge", p, 63.00)
    assert len(env.calls) == 2 and env.calls[-1][1] == round(53.10 + 0.7 * 9.9 - sr.BOT_FIRST_GAP, 2)
    state = json.loads(sr.STATE_FILE.read_text())
    assert list(state.values())[0]["trigger"] == round(53.10 + 0.7 * 9.9 - sr.BOT_FIRST_GAP, 2)   # kept for a restart


def test_3_never_at_or_above_the_live_price_and_never_down(env):
    p = pos(best=61.70)
    run("hedge", p, 59.05)                                      # trigger 59.02 not a tick under live 59.05 - the bot acts
    assert env.calls == []
    run("hedge", p, 60.00)
    assert len(env.calls) == 1
    sr._last_attempt.clear()
    p.best_price = 61.70
    run("hedge", p, 60.00)                                      # same level again -> nothing
    assert len(env.calls) == 1


def test_4_call_goes_to_entry_once(env, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(sr.time, "monotonic", lambda: clock["t"])
    ce = pos(entry=77.05, best=80.00, sl="SL-CE", ts="APLAPOLLO 27 OCT 2220 CALL")
    run("ce", ce, 80.00)
    assert env.calls == []                                      # +1,032: breakeven rule not armed
    ce.best_price = 81.40
    run("ce", ce, 81.00)
    assert env.calls[0][:2] == ("SL-CE", round(77.05 - sr.BOT_FIRST_GAP, 2))       # at entry (two ticks under)
    ce.best_price = 95.00
    clock["t"] += 60
    run("ce", ce, 94.00)
    assert len(env.calls) == 1                                  # no profit trail on calls


def test_5_failures_change_nothing_and_stop_after_three(env, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(sr.time, "monotonic", lambda: clock["t"])
    env.cfg["fail"] = True
    p = pos(best=61.70)
    for _ in range(4):
        run("hedge", p, 61.70)
        clock["t"] += 10
    names = [e[0] for e in env.events]
    assert names.count("STOP_RATCHET_FAILED") == 3 and names.count("STOP_RATCHET_GAVE_UP") == 1
    assert "trigger" not in list(json.loads(sr.STATE_FILE.read_text()).values())[0]    # the order stays where it was


def test_6_off_shadow_and_missing_order(env, monkeypatch):
    monkeypatch.setitem(settings._overrides, "stop_ratchet_mode", "off")
    run("hedge", pos(best=61.70), 61.70)
    assert env.calls == [] and env.events == []
    monkeypatch.setitem(settings._overrides, "stop_ratchet_mode", "on")
    run("hedge", pos(best=61.70, sl=None), 61.70)               # no broker order on record -> nothing to move
    assert env.calls == []
    monkeypatch.setitem(settings._overrides, "stop_ratchet_mode", "shadow")
    run("hedge", pos(best=61.70), 61.70)
    assert env.calls == [] and env.events[-1][0] == "STOP_RATCHET_WOULD_MOVE"
    _parsed, errors = settings.validate({"stop_ratchet_mode": "maybe"})
    assert errors


def test_7_wired_into_the_hedge_and_call_price_paths(env, monkeypatch):
    seen = []

    async def spy(kind, symbol, p, ltp):
        seen.append((kind, symbol))
    monkeypatch.setattr(sr, "maybe_ratchet", spy)

    async def no_scale(*a, **k):
        return None
    monkeypatch.setattr(sup.scale, "on_hedge_price", no_scale)
    hedge = pos(best=55.0)
    monkeypatch.setattr(sup.hedge_store, "live_positions", {"APLAPOLLO": hedge})
    monkeypatch.setattr(sup.engine, "_exit_on_cooldown", lambda p: False)

    async def best(symbol, price):
        hedge.best_price = max(hedge.best_price, price)
    monkeypatch.setattr(sup.hedge_store, "update_best_price", best)
    asyncio.run(sup._apply_hedge_price("APLAPOLLO", 56.0, True))
    ce = pos(entry=77.05, best=80.0, ts="X CALL")
    monkeypatch.setattr(te.position_store, "update_best_price", lambda s, p: asyncio.sleep(0))
    asyncio.run(te._apply_price_real("APLAPOLLO", ce, 80.0))
    assert seen == [("hedge", "APLAPOLLO"), ("ce", "APLAPOLLO")]


def test_8_dhan_modify_call(monkeypatch):
    sent = {}

    class FakeDhan:
        def modify_order(self, **kw):
            sent.update(kw)
            return sent.pop("_resp", {"status": "success"})
    import Options.dhan_client as dc
    monkeypatch.setattr(dhan_wrapper, "_client", NS(Dhan=FakeDhan()), raising=False)
    monkeypatch.setattr(type(dhan_wrapper), "client", property(lambda self: NS(Dhan=FakeDhan())), raising=False)
    monkeypatch.setattr(dhan_wrapper, "_instrument_meta", lambda ts, expected_exchange=None: {"tick_size": 0.05})
    out = dc.DhanWrapper.modify_stop_loss_limit_order(dhan_wrapper, "OID9", "X CALL", 350, 59.123, 58.917)
    assert out == {"order_id": "OID9", "trigger_price": 59.1, "limit_price": 58.9}
    assert sent["order_type"] == "STOP_LOSS" and sent["trigger_price"] == 59.1 and sent["price"] == 58.9 and sent["quantity"] == 350

    class Reject(FakeDhan):
        def modify_order(self, **kw):
            return {"status": "failure", "remarks": {"error_message": "trigger above LTP"}}
    monkeypatch.setattr(type(dhan_wrapper), "client", property(lambda self: NS(Dhan=Reject())), raising=False)
    with pytest.raises(RuntimeError):
        dc.DhanWrapper.modify_stop_loss_limit_order(dhan_wrapper, "OID9", "X CALL", 350, 59.1, 58.9)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
