"""
Tests for entry_backlog.py (added 26 Sep 2026, user request): "if more
than five signals are generated at a single point of time, then only five
should be placed... after a slot gets free, whoever was the signal which
couldn't get placed, including any new signals generated at that point,
should be placed - but the freshness of the signal should always take
precedence." Exercises the REAL EntryBacklog class and dispatch()
function directly - no reimplementation, only fake capacity/placement
callables standing in for position_store/enter_position_for_stock.

HOW TO RUN:
    uv run python tests/test_entry_backlog.py
"""
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import entry_backlog  # noqa: E402


class FakeCapacity:
    """Stands in for SwingPositionStore/BollingerPositionStore's own
    remaining_capacity() + the fact that a real placement actually
    consumes a slot (via reserve_symbol inside enter_position_for_stock) -
    place_real below decrements this on every call, exactly like a real
    entry would."""

    def __init__(self, capacity: int):
        self.capacity = capacity

    async def remaining(self) -> int:
        return self.capacity

    async def consume(self) -> None:
        self.capacity -= 1


def _make_recorders(cap: FakeCapacity):
    placed_real: list[str] = []
    placed_paper: list[str] = []

    async def place_real(symbol: str, payload) -> None:
        placed_real.append(symbol)
        await cap.consume()

    async def place_paper(symbol: str, payload) -> None:
        placed_paper.append(symbol)

    return placed_real, placed_paper, place_real, place_paper


def test_1_more_signals_than_capacity_only_capacity_worth_placed():
    async def run():
        backlog = entry_backlog.EntryBacklog()
        cap = FakeCapacity(5)
        placed_real, placed_paper, place_real, place_paper = _make_recorders(cap)
        candidates = [(f"SYM{i}", {"side": "LONG"}) for i in range(7)]

        await entry_backlog.dispatch(
            backlog, candidates,
            is_paper_trade=lambda s: False,
            remaining_capacity=cap.remaining,
            place_paper=place_paper, place_real=place_real,
        )

        assert len(placed_real) == 5, f"expected exactly 5 placed against capacity=5, got {len(placed_real)}"
        pending = await backlog.snapshot()
        assert len(pending) == 2, f"expected exactly 2 left in backlog, got {len(pending)}"
    asyncio.run(run())
    print("1. Exactly `capacity` signals placed when more fire at once, rest kept in backlog: PASSED")


def test_2_freshness_precedence_new_signals_jump_ahead_of_old_backlog():
    """The core behavior the user explicitly asked for: once a slot frees,
    a FRESH signal takes it before an OLDER one still waiting - even
    though the older one arrived first."""
    async def run():
        backlog = entry_backlog.EntryBacklog()
        cap = FakeCapacity(5)
        placed_real, placed_paper, place_real, place_paper = _make_recorders(cap)

        # Tick 1: 7 signals fire in the same tick (merged into the backlog
        # in list order, each getting a strictly later time.monotonic()
        # timestamp than the one before it - within a single tick this is
        # an arbitrary but deterministic tiebreak, since the user's real
        # requirement is about freshness ACROSS ticks, not intra-tick
        # ordering). Only 5 get placed - the 5 most-recently-inserted
        # (OLD2..OLD6); the 2 inserted FIRST (OLD0, OLD1 - "least fresh" of
        # this batch) miss out and stay in the backlog.
        tick1 = [(f"OLD{i}", {}) for i in range(7)]
        await entry_backlog.dispatch(
            backlog, tick1, is_paper_trade=lambda s: False,
            remaining_capacity=cap.remaining, place_paper=place_paper, place_real=place_real,
        )
        assert len(placed_real) == 5
        pending_after_tick1 = {p["symbol"] for p in await backlog.snapshot()}
        assert pending_after_tick1 == {"OLD0", "OLD1"}, pending_after_tick1

        # Tick 2: 2 slots free up (2 positions exited) AND 3 brand-new
        # fresher signals fire. 5 total candidates (2 old + 3 new) compete
        # for 2 slots - freshness must mean the 3 NEW ones outrank the 2
        # OLD ones, so the 2 OLD ones stay stuck in backlog (starved),
        # exactly the tradeoff the user explicitly accepted.
        cap.capacity = 2
        placed_real.clear()
        tick2_fresh = [(f"NEW{i}", {}) for i in range(3)]
        await entry_backlog.dispatch(
            backlog, tick2_fresh, is_paper_trade=lambda s: False,
            remaining_capacity=cap.remaining, place_paper=place_paper, place_real=place_real,
        )
        assert set(placed_real) == {"NEW0", "NEW1"} or set(placed_real).issubset({"NEW0", "NEW1", "NEW2"}), placed_real
        assert len(placed_real) == 2, f"expected exactly 2 placed (capacity), got {placed_real}"
        assert all(s.startswith("NEW") for s in placed_real), \
            f"freshness must mean the NEW signals win the freed slots over the OLD backlog, got {placed_real}"

        still_pending = {p["symbol"] for p in await backlog.snapshot()}
        assert "OLD0" in still_pending and "OLD1" in still_pending, \
            "the two OLD backlogged signals must still be waiting - fresher ones jumped ahead of them"
        assert len(still_pending) == 3, still_pending  # 2 old + 1 leftover new
    asyncio.run(run())
    print("2. A fresher signal takes a freed slot before an older backlogged one (freshness precedence): PASSED")


def test_3_paper_mode_bypasses_capacity_entirely():
    async def run():
        backlog = entry_backlog.EntryBacklog()
        cap = FakeCapacity(0)  # zero real capacity
        placed_real, placed_paper, place_real, place_paper = _make_recorders(cap)
        candidates = [("PAPERSYM", {}), ("REALSYM", {})]

        await entry_backlog.dispatch(
            backlog, candidates,
            is_paper_trade=lambda s: s == "PAPERSYM",
            remaining_capacity=cap.remaining, place_paper=place_paper, place_real=place_real,
        )

        assert placed_paper == ["PAPERSYM"], placed_paper
        assert placed_real == [], "zero real capacity must mean REALSYM does not get placed"
        pending = {p["symbol"] for p in await backlog.snapshot()}
        assert pending == {"REALSYM"}
    asyncio.run(run())
    print("3. A paper-mode symbol is dispatched immediately, never competing for real capacity: PASSED")


def test_4_fresh_signal_for_same_symbol_replaces_and_refreshes_pending_one():
    async def run():
        backlog = entry_backlog.EntryBacklog()
        await backlog.upsert_fresh("DLF", {"side": "LONG"})
        await asyncio.sleep(0.01)
        await backlog.upsert_fresh("DLF", {"side": "SHORT"})  # a fresh, opposite signal supersedes the old one

        pending = await backlog.pop_all_freshest_first()
        assert len(pending) == 1, "must be exactly one pending entry per symbol, not a duplicate"
        symbol, _generated_at, payload = pending[0]
        assert symbol == "DLF" and payload == {"side": "SHORT"}, \
            "the newer signal must have replaced the older one, not queued alongside it"
    asyncio.run(run())
    print("4. A fresh signal for an already-pending symbol replaces it (dedup by symbol): PASSED")


def test_5_reinsert_preserves_original_timestamp_not_a_refresh():
    async def run():
        backlog = entry_backlog.EntryBacklog()
        await backlog.upsert_fresh("OLD", {})
        old_entry = (await backlog.pop_all_freshest_first())[0]
        _symbol, old_generated_at, old_payload = old_entry

        # Simulate time passing before OLD gets reinserted (lost out to
        # something fresher this tick).
        await asyncio.sleep(0.02)
        await backlog.reinsert_unplaced("OLD", old_generated_at, old_payload)

        # A genuinely new signal now arrives - it must still be considered
        # FRESHER than OLD's reinserted (original) timestamp.
        await backlog.upsert_fresh("NEW", {})

        ordered = await backlog.pop_all_freshest_first()
        symbols_in_order = [s for s, _, _ in ordered]
        assert symbols_in_order == ["NEW", "OLD"], \
            f"OLD must keep ranking behind NEW even after reinsertion - got {symbols_in_order}"
    asyncio.run(run())
    print("5. reinsert_unplaced preserves the original timestamp (no unfair freshness refresh): PASSED")


def test_6_clear_empties_the_backlog():
    async def run():
        backlog = entry_backlog.EntryBacklog()
        await backlog.upsert_fresh("A", {})
        await backlog.upsert_fresh("B", {})
        await backlog.clear()
        assert await backlog.snapshot() == []
    asyncio.run(run())
    print("6. clear() empties the backlog (day-boundary reset): PASSED")


def test_7_snapshot_is_read_only_freshest_first():
    async def run():
        backlog = entry_backlog.EntryBacklog()
        await backlog.upsert_fresh("A", {})
        await asyncio.sleep(0.01)
        await backlog.upsert_fresh("B", {})

        snap1 = await backlog.snapshot()
        snap2 = await backlog.snapshot()
        assert [p["symbol"] for p in snap1] == ["B", "A"], "snapshot must be freshest-first"
        assert snap1 == snap2 or all(
            abs(a["age_seconds"] - b["age_seconds"]) < 0.5 for a, b in zip(snap1, snap2)
        ), "snapshot must not consume/mutate the backlog"
        pending_still_there = await backlog.snapshot()
        assert len(pending_still_there) == 2, "snapshot must not remove anything"
    asyncio.run(run())
    print("7. snapshot() is freshest-first and non-destructive: PASSED")


if __name__ == "__main__":
    test_1_more_signals_than_capacity_only_capacity_worth_placed()
    test_2_freshness_precedence_new_signals_jump_ahead_of_old_backlog()
    test_3_paper_mode_bypasses_capacity_entirely()
    test_4_fresh_signal_for_same_symbol_replaces_and_refreshes_pending_one()
    test_5_reinsert_preserves_original_timestamp_not_a_refresh()
    test_6_clear_empties_the_backlog()
    test_7_snapshot_is_read_only_freshest_first()
    print("\nAll tests passed.")
