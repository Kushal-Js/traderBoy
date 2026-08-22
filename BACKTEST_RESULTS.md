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
| 3 | Supertrend (5-min grace) + Dynamic SL (4%/1%) | ₹108,861.30 | 70.7% | +₹1,326.75 | −₹1,625 vs. Supertrend alone - dynamic SL was a net drag on top of Supertrend too, just a smaller one |
| 4 | Dynamic SL only (7% step / 1% raise, no Supertrend) | ₹109,314.05 | 67.7% | **+₹1,779.50** | Both known whipsaw cases (COFORGE, GLENMARK) gone entirely - 0 of 9 divergent trades negative, vs. 2 of 18 at 4% |
| 4 | Supertrend (5-min grace) + Dynamic SL (7%/1%) (**deployed**) | ₹110,761.30 | 70.7% | **+₹3,226.75** | Now *better* than Supertrend alone (+₹2,951.75) - the 4% figure was the problem, not the ratchet concept |

## PE webhook — exit logic validation

**Round 1** (Supertrend only): 61-trade, 7-day PE dataset. Validates that
the Supertrend exit's direction-flip for PE (bullish crossover = exit,
not bearish - see NOTES.md's PE design-decision entry) is correct, not
just theoretically argued.

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

**Round 2** (Dynamic SL added, same CSV, later re-fetch): 68 completed
trades, not 61 - not a bug. PE's `%change` values cluster far more
tightly than CE's (mostly −0.1% to −3%), so which 3 symbols rank in the
top-N per alert is more sensitive to small precision/timing differences
between separate historical-data fetches from Dhan than it was for CE.
Verified `MAX_POS`/`TOP_N`/ranking direction/strike selection all match
the Round 1 script exactly - the discrepancy is entirely which trades got
entered, not how they were exited. All four variants below are compared
*within this one 68-trade run*, so the comparison is still apples-to-apples
even though the absolute baseline isn't comparable to Round 1's
₹79,653.50.

| Variant | P&L | Win rate | Δ vs. baseline |
|---|---:|---:|---:|
| Baseline (target/SL only) | ₹55,333.75 | 70.6% (48/68) | — |
| Supertrend only (5-min grace) | ₹55,430.00 | 69.1% (47/68) | +₹96.25 |
| Dynamic SL only (7%/1%) | ₹53,318.75 | 69.1% (47/68) | −₹2,015.00 |
| Combined (then-current production) | ₹52,915.00 | 67.6% (46/68) | **−₹2,418.75** |

Day-by-day (baseline vs. combined):

| Date | Trades | Baseline | Combined | Δ |
|---|---:|---:|---:|---:|
| 13 Aug | 3 | 1,629 | −2,075 | −446 |
| 14 Aug | 8 | 11,804 | 12,844 | +1,040 |
| 17 Aug | 10 | 7,998 | 6,085 | −1,913 (VOLTAS) |
| 18 Aug | 11 | 4,512 | 2,661 | −1,851 |
| 19 Aug | 14 | 13,484 | 14,228 | +744 |
| 20 Aug | 6 | 3,246 | 3,246 | 0 |
| 21 Aug | 16 | 15,920 | 15,927 | +7 |

Almost the entire negative delta traces to one trade: **VOLTAS**, 17 Aug,
entered 09:35 @ ₹20.30. The option spiked >7% within 14 minutes, so the
ratchet raised the floor and stopped it out at 09:49 for −₹1,406. Left
alone (baseline, and separately confirmed under Supertrend-only - this
was purely a dynamic-SL effect, not a Supertrend one), it rode to the 25%
target at 11:02 for +₹2,006 - a single −₹3,412 swing, outweighing the
other 4 divergent trades' combined +₹1,398 of genuine catches. Same class
of whipsaw risk as the CE 4%-step finding (bug #12) - just resurfacing
for PE at the 7% width that had fully eliminated it for CE. See NOTES.md
bug #13 for the full writeup and the resulting decision to split
`DYNAMIC_SL_STEP_PCT` into `DYNAMIC_SL_STEP_PCT_CE` / `_PE`.

**Round 3** (fixing the VOLTAS whipsaw: ATR-scaled step vs. a simple
wider flat step): same 68 fixed entries as Round 2, loaded verbatim from
Round 2's own result file - not re-ranked, so this sidesteps the
entry-drift issue Round 2 itself surfaced. Two variants tested, both
detailed in NOTES.md bug #14:

1. **ATR-scaled step** - `clamp(K × underlying's 14-day daily ATR%, 3%, 15%)`,
   captured once per trade at entry, K swept 3/5/7. Best PE result at
   K=5 (median implied step ≈12.5%): fully cleared VOLTAS, combined
   ₹55,576.25 (+₹242.50 vs. baseline). CE's best K (3, median implied
   step ≈7.6%, close to its existing flat 7%) was a statistical wash
   vs. the existing fixed 7% (−₹75 to −₹275 out of ~₹110k) - no new
   whipsaws, just marginally fewer genuine catches.
2. **Flat, wider PE step** (no ATR, just sweeping 7-15% directly) -
   **a flat 9% step matched the ATR-scaled combined result exactly
   (₹55,576.25) and beat it in isolation (₹55,980.00, +₹646.25 vs.
   baseline - the best PE result found this session)**, catching a
   BIOCON reversal (+₹500) the wider-on-average ATR step had missed.
   VOLTAS clears the whipsaw at 9% and stays clear through 15% - not a
   knife-edge fit to one trade.

| Variant | P&L | Win rate | Δ vs. baseline |
|---|---:|---:|---:|
| Baseline (target/SL only) | ₹55,333.75 | 70.6% (48/68) | — |
| Fixed 7% dynamic SL + Supertrend (prior prod) | ₹52,915.00 | 67.6% (46/68) | −₹2,418.75 |
| ATR-scaled dynamic SL (K=5) + Supertrend | ₹55,576.25 | 69.1% (47/68) | +₹242.50 |
| Flat 9% dynamic SL alone | ₹55,980.00 | 70.6% (48/68) | **+₹646.25** |
| Flat 9% dynamic SL + Supertrend (**deployed**) | ₹55,576.25 | 69.1% (47/68) | +₹242.50 |

Since a one-line config change matched or beat the ATR-scaled version
outright, ATR-scaling was **not** built into the live bot - it would have
added a live daily-OHLC fetch/cache and a new failure surface for no
extra return on this dataset. **Deployed: `DYNAMIC_SL_STEP_PCT_PE` raised
from 7% to 9%.** `DYNAMIC_SL_STEP_PCT_CE` stays at 7% - CE's own sweep
already won there.

## Capital sizing

See `NOTES.md`'s "Capital requirements" section for the full
percentile table. Headline numbers: median cost per leg ₹11,257 (CE,
99-trade sample); with the current split caps
(`MAX_LIVE_POSITIONS_CE=2` + `MAX_LIVE_POSITIONS_PE=2`, worst case 4
concurrent positions), recommended working minimum is ₹70,000–80,000.

## Open questions for future tuning

- **Dynamic SL step width - resolved for CE at 7%, resolved for PE at 9%
  (PE Round 3 above).** 4% backtested net-negative for CE; 7% fixed it,
  eliminating both known whipsaw cases while keeping the genuine catches.
  Whether 7% is actually the *best* CE width, or just better than 4%, is
  still open - untested beyond 7-8%, and not urgent to chase further
  given 7% already beats Supertrend-alone on the CE dataset. For PE, 7%
  itself whipsawed on a later dataset (VOLTAS, PE Round 2) the same way
  4% had for CE - fixed by widening PE's own step to 9% (Round 3), tested
  against both an ATR-scaled adaptive step and a plain flat step; the
  flat step won outright, so ATR-scaling wasn't shipped. Whether 9% is
  the *best* PE width (vs. just wide enough to clear the one whipsaw
  observed) is open the same way CE's 7% is - the 9-15% plateau found in
  Round 3 is reassuring but drawn from one dataset.
- **The 10:10 Supertrend warmup cluster** (bug #10) still fires under
  the deployed 5-min-grace fix - its net effect happened to be
  roughly neutral on the one week tested, but it isn't a real per-stock
  signal (every underlying's indicator finishes warmup at the same
  wall-clock moment, seeded from a heuristic biased toward "bearish"
  regardless of actual trend). Worth a smarter seed or a longer
  mandatory warmup if a future backtest shows this cluster turning
  net-harmful.
- **Sample size.** Every result above is one calendar week. NOTES.md's
  own lesson from bugs #9→#10: a single day wasn't enough to trust:
  the same caution applies to one week vs. a full month. Re-run
  periodically as more webhook trigger history accumulates.
