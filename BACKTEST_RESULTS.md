# Backtest Results

A running log of every exit-logic backtest run against this bot's real
webhook trigger history, with the concrete numbers behind each round.
`NOTES.md` tells the story (bugs found, why a fix shipped); this file is
the reference table to check before proposing a new exit-logic change -
what's already been tried, on what data, with what result - so tuning
decisions build on evidence instead of re-deriving it from scratch.

**Methodology** (see `NOTES.md`'s "Backtesting methodology" section for
the full explanation): a standalone, read-only script replays a
Chartink-exported CSV of real webhook triggers against real historical
1-min underlying + option candles from Dhan, using the exact same
ranking/dedup/capacity/exit logic as production. Never modifies the live
bot. Entries are held fixed across variants being compared in the same
round - only the exit rule differs - so a P&L delta isolates the exit
rule's own effect.

## Datasets used

| Dataset | Webhook | Trading days | Trigger timestamps | Symbols | Trades (after dedup/capacity) |
|---|---|---|---|---|---|
| `Backtest DanDanaDan- Last 6 days.csv` | CE (`/chartink/webhook`) | 13,14,17,18,19,20,21 Aug 2026 | ~250 | 127 | 99 |
| `Backtest DanDanaDanBecho-Last 6 days.csv` | PE (`/chartink/webhook-sell`) | 13,14,17,18,19,20,21 Aug 2026 | ~200 | 140 | 61 |
| `Backtest DanDanaDan 01.csv` | CE | 21 Aug 2026 only | 49 | 25 | 23 |

All three cover the same calendar week; the single-day CE file is a
subset used for the first (later found insufficient) Supertrend backtest.

## CE webhook — exit logic rounds

All rounds below replay the same 99-trade, 7-day CE dataset unless noted.
`MAX_LIVE_POSITIONS=3` (shared cap, pre-dates the CE/PE split) in every
CE round so the numbers are comparable to each other.

| Round | Variant | P&L | Win rate | Δ vs. baseline | Notes |
|---|---|---:|---:|---:|---|
| Baseline | target/SL only | ₹107,534.55 | 67.7% | — | Reference point for every round below |
| 1 (single day, 23 trades) | Supertrend, checked from entry | ₹17,927.25* | 69.6%* | +₹752.50* | *single-day numbers, not comparable to the 99-trade rows - see NOTES.md bug #9. Looked like a clean win; wasn't. |
| 2 | Supertrend, 0-min grace | ₹101,570.05 | 68.7% | −₹5,964.50 | Net-negative once tested on a full week - two mechanisms found: a 10:10 warmup-artifact cluster (17/29 divergent trades, roughly neutral net) and a one-candle-after-entry whipsaw pattern (9/11 remaining, −₹8,627) |
| 2 | Supertrend, 5-min grace (**deployed**) | ₹110,486.30 | 70.7% | **+₹2,951.75** | Best of 0/5/10/15-min grace sweep - see NOTES.md bug #10 |
| 2 | Supertrend, 10-min grace | ₹108,767.55 | 68.7% | +₹1,233.00 | |
| 2 | Supertrend, 15-min grace | ₹109,422.55 | 67.7% | +₹1,888.00 | |
| 3 | Dynamic SL only (4% step / 1% raise, no Supertrend) | ₹105,364.55 | 67.7% | −₹2,170.00 | 9 genuine catches (+₹3,067) outweighed by 2 whipsaw cases that would have recovered to near-flat under baseline |
| 3 | Supertrend (5-min grace) + Dynamic SL (4%/1%) (**current production**) | ₹108,861.30 | 70.7% | +₹1,326.75 | −₹1,625 vs. Supertrend alone - dynamic SL is a net drag on top of Supertrend too, just a smaller one |

## PE webhook — exit logic validation

61-trade, 7-day PE dataset. Validates that the Supertrend exit's
direction-flip for PE (bullish crossover = exit, not bearish - see
NOTES.md's PE design-decision entry) is correct, not just theoretically
argued.

| Variant | P&L | Win rate | Δ vs. baseline |
|---|---:|---:|---:|
| Baseline (target/SL only) | ₹79,653.50 | 80.3% | — |
| Supertrend, 5-min grace (**deployed**) | ₹81,692.25 | 78.7% | +₹2,038.75 |

Only 4 of 61 trades used the Supertrend exit at all (this dataset didn't
reproduce the CE backtest's warmup-cluster or one-candle-whipsaw
patterns - entries were later in the morning on average and less
clustered right at open). Direction-flip confirmed correct by checking
BIOCON's real 5-min candles directly (17 Aug): bearish through 12:35,
flipped bullish at 12:40 - a genuine reversal against the PE bet - exit
fired the next candle at 12:45.

## Capital sizing

See `NOTES.md`'s "Capital requirements" section for the full
percentile table. Headline numbers: median cost per leg ₹11,257 (CE,
99-trade sample); with the current split caps
(`MAX_LIVE_POSITIONS_CE=2` + `MAX_LIVE_POSITIONS_PE=2`, worst case 4
concurrent positions), recommended working minimum is ₹70,000–80,000.

## Open questions for future tuning

- **Dynamic SL step width.** 4% was the first-specified value and
  backtested net-negative (see above). A wider step (7% queued next -
  results to be added here once run) should trigger on fewer, more
  decisive moves and may avoid most of the whipsaw cases without losing
  the genuine catches - untested until run.
- **The 10:10 Supertrend warmup cluster** (bug #10) still fires under
  the deployed 5-min-grace fix - its net effect happened to be
  roughly neutral on the one week tested, but it isn't a real per-stock
  signal (every underlying's indicator finishes warmup at the same
  wall-clock moment, seeded from a heuristic biased toward "bearish"
  regardless of actual trend). Worth a smarter seed or a longer
  mandatory warmup if a future backtest shows this cluster turning
  net-harmful.
- **PE dynamic SL** hasn't been independently backtested - the
  mechanism is symmetric by construction (reads only the option's own
  premium, never underlying direction) so it should behave the same as
  CE in principle, but "should" isn't "confirmed" per this repo's own
  house rule (see NOTES.md's backtesting-methodology lesson: a single
  day - or an untested case - is not enough to trust without a real
  backtest).
- **Sample size.** Every result above is one calendar week. NOTES.md's
  own lesson from bugs #9→#10: a single day wasn't enough to trust:
  the same caution applies to one week vs. a full month. Re-run
  periodically as more webhook trigger history accumulates.
