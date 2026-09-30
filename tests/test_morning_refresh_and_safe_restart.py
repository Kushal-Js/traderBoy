"""
morning_refresh.py (the 08:00 IST restart through safe_restart.py, 1 Oct 2026)
and safe_restart.py's checks (30 Sep / 1 Oct 2026). Nothing is restarted: the
restart, the bot's HTTP endpoints and time.sleep are faked.

HOW TO RUN:
    uv run python -m pytest tests/test_morning_refresh_and_safe_restart.py -q
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import morning_refresh as mr  # noqa: E402
import safe_restart as sr  # noqa: E402
import weekly_watchlist_refresh as wwr  # noqa: E402


@pytest.fixture
def morning(monkeypatch):
    monkeypatch.setattr(mr, "log", lambda msg: None)
    monkeypatch.setattr(mr.time, "sleep", lambda s: None)

    def scenario(codes, up=True):
        seq, calls = list(codes), []

        def fake(*extra):
            calls.append(extra)
            return seq.pop(0)
        monkeypatch.setattr(mr, "safe_restart", fake)
        monkeypatch.setattr(mr, "bot_up", lambda: up)
        return mr.main(), calls
    return scenario


def test_1_morning_clean_restart(morning):
    assert morning([0]) == (0, [()])
    assert morning([1]) == (1, [()])                                   # restarted, a report needs review


def test_2_morning_retries_a_refused_check_then_forces(morning):
    assert morning([2, 2, 0]) == (0, [(), (), ()])
    code, calls = morning([2] * mr.MAX_ATTEMPTS + [0])
    assert code == 0 and calls[-1] == ("--force",) and len(calls) == mr.MAX_ATTEMPTS + 1


def test_3_morning_bot_down_goes_straight_to_force(morning):
    assert morning([0], up=False) == (0, [("--force",)])


def test_4_morning_health_not_back(morning):
    assert morning([3, 3, 0]) == (0, [(), ("--force",), ("--force",)])
    assert morning([3, 3, 3]) == (5, [(), ("--force",), ("--force",)])


# --------------------------------------------------------------------------- #
# safe_restart.py
# --------------------------------------------------------------------------- #
@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(sr, "SNAP_DIR", tmp_path / "snaps")
    monkeypatch.setattr(sr, "STATE_FILE", tmp_path / "data" / "super_bollinger_live_state.json")
    monkeypatch.setattr(sr, "MEMORY_FILES", {k: tmp_path / v for k, v in sr.MEMORY_FILES.items()})
    responses = {ep: {"live_positions": []} for ep in sr.ENDPOINTS}
    responses.update({"super-bollinger/positions": {"live_positions": [], "reserved_symbols": []},
                      "super-bollinger/supervisor": {"open_hedges_real": []}, "super-bollinger/live-state": {"intents": []},
                      "health": {"status": "ok"}, "super-bollinger/restart-report": {"needs_review": False}})
    monkeypatch.setattr(sr, "get", lambda path, timeout=10: responses[path])
    restarts = []
    monkeypatch.setattr(sr.subprocess, "run", lambda *a, **k: restarts.append(a))
    monkeypatch.setattr(sr.time, "sleep", lambda s: None)
    return responses, restarts, tmp_path


def run_sr(monkeypatch, *args):
    monkeypatch.setattr(sr.sys, "argv", ["safe_restart.py", *args])
    return sr.main()


def test_5_flat_bot_restarts(bot, monkeypatch):
    responses, restarts, _ = bot
    assert run_sr(monkeypatch) == 0 and len(restarts) == 1
    assert run_sr(monkeypatch, "--check") == 0 and len(restarts) == 1    # --check never restarts


def test_6_exit_in_flight_or_missing_broker_stop_blocks(bot, monkeypatch):
    responses, restarts, _ = bot
    responses["super-bollinger/positions"]["live_positions"] = [
        {"underlying_symbol": "SAIL", "trading_symbol": "SAIL CE", "quantity": 4700, "entry_price": 6.4, "best_price": 6.9, "stop_loss_order_id": None}]
    assert run_sr(monkeypatch) == 2 and not restarts
    assert run_sr(monkeypatch, "--force") in (0, 1) and len(restarts) == 1


def test_7_real_swing_or_options_position_must_be_in_its_memory_file(bot, monkeypatch):
    responses, restarts, tmp = bot
    responses["swing/positions"]["live_positions"] = [{"trading_symbol": "TITAN CE", "quantity": 175, "entry_price": 100,
                                                       "best_price": 120}]
    responses["positions"]["live_positions"] = [{"option_trading_symbol": "SBIN CE", "quantity": 750, "entry_price": 10,
                                                 "highest_price": 12}]
    assert run_sr(monkeypatch) == 2 and not restarts                   # neither is remembered yet
    (tmp / "data" / "swing_position_memory.json").write_text(json.dumps({"TITAN CE": {"best_price": 120}}))
    (tmp / "data" / "options_position_memory.json").write_text(json.dumps({"SBIN CE": {"highest_price": 12}}))
    for name in ("swing", "bollinger", "options", "luxury"):
        responses[f"{name}/restart-report"] = {"last_restore": {"restored": [], "not_restored": [],
                                                                "remembered_but_not_at_broker": []}}
    assert run_sr(monkeypatch) == 0 and len(restarts) == 1


def test_8_restart_report_needing_review_exits_1(bot, monkeypatch):
    responses, restarts, _ = bot
    responses["swing/restart-report"] = {"last_restore": {"restored": [], "not_restored": [{"trading_symbol": "X",
                                                                                            "why": "entry"}],
                                                          "remembered_but_not_at_broker": []}}
    assert run_sr(monkeypatch) == 1


def test_9_weekly_job_restarts_through_safe_restart(monkeypatch):
    seen, lines = [], []

    class P:
        def __init__(self, code):
            self.returncode, self.stdout, self.stderr = code, "snapshot ...\nbot is up", ""
    for code, outcome in ((0, "restarted"), (1, "restarted_needs_review"), (2, "skipped_checks_failed"),
                          (3, "restarted_health_not_back")):
        monkeypatch.setattr(wwr.subprocess, "run", lambda args, **k: seen.append(args) or P(code))
        assert wwr.restart_bot(lines.append) == outcome
    assert all(str(wwr.SAFE_RESTART) in a for a in seen)
    assert any("safe_restart: bot is up" in line for line in lines)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
