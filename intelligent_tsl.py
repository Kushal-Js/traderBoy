"""
intelligentTSL - a "ride the wave" post-target trailing stop-loss.

STATUS: DORMANT. Not called from any strategy's live code path right
now (Options/Futures/Luxury/Swing) - added 25 Sep 2026 per explicit user
request to keep this named and available for FUTURE use, without wiring
it into any strategy yet. Grep the codebase for "intelligentTSL" to
confirm: this module should be the only hit outside this file's own
history in trading-skills' TRADING_JOURNAL.md.

HISTORY, for whoever picks this up next: built 24 Sep 2026 for Swing
first ("once Target Profit Pct is crossed... the position shouldn't
immediately be squared off... update broker SL and place it very next
to target pct (just a small gap) and keep updating it to fully ride the
profit wave continuously unless it is hit"), then extended the same day
to Options/Futures/Luxury. Backtested against both a signal-regenerated
30/90-day window (Swing, 5 symbols) and 330 REAL historical trades
(Options/Futures/Luxury, 16 trading days) at a 2% trail gap. Result:
inconclusive-to-negative in both cases - Swing had only 1 trade ever
reach target in 43 (and that one case did worse under the trailing
floor than the flat target); Options/Futures/Luxury saw 120 of 330
trades actually engage the mechanism, and it cost roughly Rs24,000 net
versus today's real deployed ladder over that window (helped Options
marginally, hurt Futures and Luxury more). Fully reverted from all 4
packages' live code the same day ("let's drop this completely") rather
than ship a flag-gated-off feature nobody had evidence for. This module
is that same logic, recreated and renamed, kept dormant rather than
deleted - the backtest evidence argues against turning it on as-is
(at a 2% gap, plugged in exactly this way), not necessarily against the
underlying idea in some future, differently-tuned form (a wider gap, a
higher TARGET_PCT so more trades actually reach it, or a different
strategy's exit ladder entirely).

DESIGN (unchanged from the backtested version): once price has crossed
target_price in the favorable direction, returns a trailing floor
trail_gap_pct behind the peak (best_price) instead of squaring off at a
flat target - the position rides further as long as it keeps making new
highs, and only exits once price falls back through that (continuously
ratcheting) floor. Side-aware: LONG's floor sits below the peak, SHORT's
sits above it - Options/Futures/Luxury are always LONG (buy CE/PE,
never short), so `side` is always "LONG" for them; Swing can be either.
"""
from __future__ import annotations

from typing import Optional, Tuple


def _price_past_target(side: str, ltp: float, target_price: float) -> bool:
    return ltp >= target_price if side == "LONG" else ltp <= target_price


def _giveback_floor(side: str, best_price: float, giveback_pct: float) -> float:
    return best_price * (1 - giveback_pct) if side == "LONG" else best_price * (1 + giveback_pct)


def price_past_giveback_floor(side: str, ltp: float, floor: float) -> bool:
    return ltp < floor if side == "LONG" else ltp > floor


def intelligentTSL(
    side: str, target_price: float, best_price: float, trail_gap_pct: float,
) -> Optional[float]:
    """The core decision: None if best_price hasn't reached target_price
    yet - callers must treat None as "not armed, use the ordinary flat
    TARGET_HIT/hard-stop ladder instead." Once armed, returns the
    trailing floor trail_gap_pct below/above the peak - best_price only
    ever grows in the favorable direction, so the floor only ever
    ratchets up for LONG / down for SHORT, never backslides even if
    price pulls back without yet breaching the floor.

    Pair this with price_past_giveback_floor(side, ltp, floor) to decide
    whether the current tick has actually breached the returned floor
    (i.e. the ride is over), and with intelligentTSL_trigger_and_limit
    below to compute where a real broker-side SL-L order should sit if
    a caller wires this up to keep one updated."""
    if not _price_past_target(side, best_price, target_price):
        return None
    return _giveback_floor(side, best_price, trail_gap_pct)


def intelligentTSL_trigger_and_limit(
    side: str, floor_price: float, limit_gap_pct: float,
) -> Tuple[float, float]:
    """(trigger_price, limit_price) for a broker-side SL-L order placed
    at intelligentTSL's own computed floor. LONG's protective SELL sits
    AT the floor with its limit further below (worst acceptable fill
    still below the floor); SHORT's protective BUY sits AT the floor
    with its limit further above."""
    gap = floor_price * limit_gap_pct
    if side == "LONG":
        return floor_price, floor_price - gap
    return floor_price, floor_price + gap
