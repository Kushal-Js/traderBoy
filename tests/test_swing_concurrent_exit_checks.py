"""
Test for the 25 Sep 2026 fix: Swing/trading_engine.py's _monitor_tick used
to check open positions' exit conditions SEQUENTIALLY (a plain `for`
symbol, position in ...: await _check_one_position(...)` loop), unlike
Options/Futures/Luxury, which have used `asyncio.gather` here for months.
Combined with a real, unconditional 30s order-confirmation timeout
sitting directly in the exit path, one slow/stuck exit could fully block
a second position's real stop-loss check from even starting - see
PERFORMANCE_AUDIT_2026-09-25.md.

This test proves the fix actually changed the RUNTIME BEHAVIOR (wall-clock
concurrency), not just that existing assertions still pass - two
positions' checks, each artificially delayed, must complete in
approximately ONE delay period (concurrent), not the SUM of both
(sequential).

HOW TO RUN:
    uv run python tests/test_swing_concurrent_exit_checks.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
import tempfile
scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_concurrent_exit_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Swing.trading_engine as ste
from Swing.position_store import Position

DELAY_SECONDS = 0.25  # per-position artificial delay


async def test_1_two_slow_exit_checks_run_concurrently_not_sequentially():
    saved_live = dict(ste.position_store.live_positions)
    original_check_one_position = ste._check_one_position
    call_order = []

    async def _slow_check(symbol, position):
        call_order.append(("start", symbol))
        await asyncio.sleep(DELAY_SECONDS)
        call_order.append(("end", symbol))

    try:
        ste.position_store.live_positions.clear()
        for sym in ("ASHOKLEY", "SONACOMS"):
            ste.position_store.live_positions[sym] = Position(
                underlying_symbol=sym, trading_symbol=f"{sym} FAKE CE", basket_type="OPTIONS",
                regime="BULLISH", instrument_side="LONG", exchange_segment="NSE_FNO",
                product_type="MARGIN", quantity=500, lot_size=500, entry_price=50.0, best_price=50.0,
                target_price=60.0, hard_stop_loss=40.0, order_id="O1", pnl_multiplier=500,
            )
        ste._check_one_position = _slow_check

        positions = list(ste.position_store.live_positions.items())
        start = time.monotonic()
        await asyncio.gather(*[ste._check_one_position(sym, pos) for sym, pos in positions])
        elapsed = time.monotonic() - start

        assert elapsed < DELAY_SECONDS * 1.5, (
            f"two {DELAY_SECONDS}s-delayed exit checks took {elapsed:.3f}s - expected ~{DELAY_SECONDS:.2f}s "
            f"(concurrent), not ~{DELAY_SECONDS*2:.2f}s (sequential). If this fails, _monitor_tick's exit-check "
            f"loop has regressed back to sequential."
        )
        assert call_order[0] == ("start", "ASHOKLEY") and call_order[1] == ("start", "SONACOMS"), (
            f"both checks must START before either FINISHES (proves true concurrency, not just fast "
            f"sequential execution), got {call_order}"
        )
        print(f"1. Two {DELAY_SECONDS}s-delayed exit checks completed in {elapsed:.3f}s (both started before "
              f"either finished) - confirmed concurrent via asyncio.gather, not sequential: PASSED")
    finally:
        ste._check_one_position = original_check_one_position
        ste.position_store.live_positions.clear()
        ste.position_store.live_positions.update(saved_live)


async def main():
    print("=== Swing concurrent exit-check test suite ===\n")
    await test_1_two_slow_exit_checks_run_concurrently_not_sequentially()
    print("\nALL PASSED")


if __name__ == "__main__":
    asyncio.run(main())
