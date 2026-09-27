"""
Manual CLI wrapper around fno_ath_screener.py's get_top_n_candidates() -
the composite Minervini+ATH+Stage2 breakout/momentum score, now THE global
common function (see that module's own docstring for the full formula and
reasoning, and weekly_watchlist_refresh.py for the scheduled job that
also calls it). This script does no scoring of its own anymore - it just
authenticates (local/handoff-token pattern, since this is meant to be run
by hand, possibly off the droplet), calls the shared function, and prints
a report.

User request (26-27 Sep 2026): "Update ATH method to find top 15 stocks
for highest possibility for breakout after tightness/consolidation or
continued momentum with the combination of Minervini/ATH/Stage2/... Show
me the list" -> later: "Create and deploy this ATH method as a global
common function" (fno_ath_screener.py) "and Create a scheduler which runs
on every week on Friday at [midnight IST]" (weekly_watchlist_refresh.py).

Read-only: historical_daily_data only, no order placement.

HOW TO RUN:
    DHAN_AUTH_MODE=access_token DHAN_ACCESS_TOKEN=<handoff token> \\
        uv run python screen_fno_top15_composite.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from Options.dhan_client import dhan_wrapper
from fno_ath_screener import get_top_n_candidates, explain, DEFAULT_TOP_N


def main() -> None:
    dhan_wrapper.authenticate()

    universe = None  # let get_top_n_candidates fetch the full F&O universe itself
    print(f"Fetching daily data + scoring the full F&O universe composite "
          f"(Minervini 25pts, 52w-high-proximity 20pts, loosened-Stage2 25pts, "
          f"fresh-cross bonus 10pts, ADX bonus 10pts, momentum-consistency bonus 10pts)...\n")

    def _progress(done, total):
        print(f"  ...{done}/{total} done")

    top_n, failed = get_top_n_candidates(n=DEFAULT_TOP_N, universe=universe, progress_callback=_progress)

    print(f"\nScored (failed/skipped: {len(failed)}).\n")

    print("=" * 150)
    print(f"TOP {DEFAULT_TOP_N} - HIGHEST BREAKOUT/MOMENTUM COMPOSITE SCORE")
    print("=" * 150)
    print(f"{'#':>2s} {'Symbol':12s} {'Last':>9s} {'Score':>6s} | {'Minervini':>9s} {'HighProx':>8s} "
          f"{'Stage2':>7s} {'Cross':>6s} {'ADX':>6s} {'Mom':>4s}")
    for i, r in enumerate(top_n, 1):
        print(f"{i:2d} {r['symbol']:12s} {r['latest_close']:9.2f} {r['total_score']:6.1f} | "
              f"{r['minervini_score']:9.1f} {r['high_proximity_score']:8.1f} "
              f"{r['stage2_score']:7.1f} {r['cross_score']:6.1f} {r['adx_score']:6.1f} {r['momentum_score']:4.1f}")

    print("\n" + "=" * 150)
    print("EXPLANATIONS")
    print("=" * 150)
    for i, r in enumerate(top_n, 1):
        print(f"{i:2d}. {r['symbol']} (score {r['total_score']:.1f}/100): {explain(r)}")

    if failed:
        print(f"\n{len(failed)} symbols skipped (no equity match, insufficient history, or fetch error).")


if __name__ == "__main__":
    main()
