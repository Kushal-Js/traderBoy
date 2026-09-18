"""
Tests for the opening-burst extra CE capacity slot (added 19 Sep 2026,
user request following the opening-burst-capacity backtest - see
trading-skills' designs/opening-burst-slot-and-sl-target-sensitivity.md
for the full rationale/numbers). Deployed flag-on by default per that
request, across all 3 CE-issuing packages: Options, Futures, Luxury.

Coverage:
  1. Outside the burst window, _cap_for("CE") returns the ordinary base
     cap (MAX_LIVE_POSITIONS_CE) - unaffected.
  2. Inside the window, _cap_for("CE") returns base + BURST_EXTRA_SLOTS_CE.
  3. _cap_for("PE") is NEVER affected by the burst window, regardless of
     time - the backtest only ever modeled CE (shadow_evaluator itself is
     CE-only), so PE keeps its ordinary cap unconditionally.
  4. Setting BURST_CAPACITY_ENABLED=False disables the extra slot even
     during the window - proves this is a genuine, independently
     flippable kill switch, not baked in unconditionally.
  5. Window boundaries (BURST_WINDOW_START/END) are inclusive on both ends.
  6. End-to-end through reserve_symbol(): with base capacity already full,
     a 3rd CE reservation is accepted during the burst window and
     rejected outside it - proves the mechanism is wired through the
     actual capacity-enforcement path, not just the pure helper function.
  7. Same coverage repeated (lighter) for Futures and Luxury, to catch a
     copy-paste mistake in either package's own env-var prefix wiring.

Pure in-memory logic - no Dhan calls, no auth risk. Uses pytest's own
monkeypatch fixture to flip config values per test without leaking
mutations across tests (each test gets a fresh patch/undo).

HOW TO RUN (full suite, not standalone - see tests/README.md):
    uv run --with pytest --with pytest-asyncio pytest tests/test_burst_capacity.py -q --asyncio-mode=auto
"""
import asyncio
import os
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")

import Options.config as ocfg
import Options.position_store as ops
import Futures.config as fcfg
import Futures.position_store as fps
import Luxury.config as lcfg
import Luxury.position_store as lps

IST = ZoneInfo("Asia/Kolkata")
IN_WINDOW = datetime(2026, 9, 19, 9, 30, tzinfo=IST)          # inside 09:15-10:00
BEFORE_WINDOW = datetime(2026, 9, 19, 9, 10, tzinfo=IST)      # before 09:15
AFTER_WINDOW = datetime(2026, 9, 19, 10, 5, tzinfo=IST)       # after 10:00
AT_START = datetime(2026, 9, 19, 9, 15, tzinfo=IST)
AT_END = datetime(2026, 9, 19, 10, 0, tzinfo=IST)

PACKAGES = [
    ("Options", ocfg, ops),
    ("Futures", fcfg, fps),
    ("Luxury", lcfg, lps),
]


@pytest.mark.parametrize("name,cfg,mod", PACKAGES)
def test_1_outside_window_ce_cap_is_unaffected(name, cfg, mod, monkeypatch):
    monkeypatch.setattr(cfg, "BURST_CAPACITY_ENABLED", True)
    monkeypatch.setattr(cfg, "MAX_LIVE_POSITIONS_CE", 2)
    monkeypatch.setattr(cfg, "BURST_EXTRA_SLOTS_CE", 1)
    monkeypatch.setattr(mod, "_in_burst_window", lambda now=None: False)
    assert mod._cap_for("CE") == 2
    assert mod._in_burst_window(BEFORE_WINDOW) is False
    assert mod._in_burst_window(AFTER_WINDOW) is False


@pytest.mark.parametrize("name,cfg,mod", PACKAGES)
def test_2_inside_window_ce_cap_gets_extra_slot(name, cfg, mod, monkeypatch):
    monkeypatch.setattr(cfg, "BURST_CAPACITY_ENABLED", True)
    monkeypatch.setattr(cfg, "MAX_LIVE_POSITIONS_CE", 2)
    monkeypatch.setattr(cfg, "BURST_EXTRA_SLOTS_CE", 1)
    monkeypatch.setattr(mod, "_in_burst_window", lambda now=None: True)
    assert mod._cap_for("CE") == 3
    assert mod._in_burst_window(IN_WINDOW) is True


@pytest.mark.parametrize("name,cfg,mod", PACKAGES)
def test_3_pe_cap_never_affected_by_burst_window(name, cfg, mod, monkeypatch):
    monkeypatch.setattr(cfg, "BURST_CAPACITY_ENABLED", True)
    monkeypatch.setattr(cfg, "MAX_LIVE_POSITIONS_PE", 2)
    monkeypatch.setattr(cfg, "BURST_EXTRA_SLOTS_CE", 5)  # even a large extra must never leak into PE
    assert mod._cap_for("PE") == 2


@pytest.mark.parametrize("name,cfg,mod", PACKAGES)
def test_4_disabled_flag_keeps_base_cap_even_in_window(name, cfg, mod, monkeypatch):
    monkeypatch.setattr(cfg, "BURST_CAPACITY_ENABLED", False)
    monkeypatch.setattr(cfg, "MAX_LIVE_POSITIONS_CE", 2)
    monkeypatch.setattr(cfg, "BURST_EXTRA_SLOTS_CE", 1)

    def fake_in_window(now=None):
        return True  # pretend we're always inside the window
    monkeypatch.setattr(mod, "_in_burst_window", fake_in_window)
    assert mod._cap_for("CE") == 2


@pytest.mark.parametrize("name,cfg,mod", PACKAGES)
def test_5_window_boundaries_are_inclusive(name, cfg, mod, monkeypatch):
    monkeypatch.setattr(cfg, "BURST_WINDOW_START", "09:15")
    monkeypatch.setattr(cfg, "BURST_WINDOW_END", "10:00")
    assert mod._in_burst_window(AT_START) is True
    assert mod._in_burst_window(AT_END) is True
    assert mod._in_burst_window(datetime(2026, 9, 19, 9, 14, 59, tzinfo=IST)) is False
    assert mod._in_burst_window(datetime(2026, 9, 19, 10, 0, 1, tzinfo=IST)) is False


@pytest.mark.parametrize("name,cfg,mod", PACKAGES)
def test_6_reserve_symbol_accepts_third_ce_only_inside_window(name, cfg, mod, monkeypatch):
    monkeypatch.setattr(cfg, "BURST_CAPACITY_ENABLED", True)
    monkeypatch.setattr(cfg, "MAX_LIVE_POSITIONS_CE", 2)
    monkeypatch.setattr(cfg, "BURST_EXTRA_SLOTS_CE", 1)

    store = mod.PositionStore()
    asyncio.run(store.reserve_symbol("AAA", "CE"))
    asyncio.run(store.reserve_symbol("BBB", "CE"))  # base capacity (2) now full

    # Outside the window: the 3rd reservation must be rejected.
    monkeypatch.setattr(mod, "_in_burst_window", lambda now=None: False)
    accepted_outside = asyncio.run(store.reserve_symbol("CCC", "CE"))
    assert accepted_outside is False

    # Inside the window: the 3rd reservation (the extra slot) must succeed.
    monkeypatch.setattr(mod, "_in_burst_window", lambda now=None: True)
    accepted_inside = asyncio.run(store.reserve_symbol("CCC", "CE"))
    assert accepted_inside is True

    # A 4th is still rejected even inside the window (only +1 slot, not unlimited).
    accepted_fourth = asyncio.run(store.reserve_symbol("DDD", "CE"))
    assert accepted_fourth is False
