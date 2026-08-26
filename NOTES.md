# Engineering Notes

Operational history and lessons learned from actually running this bot
against a live Dhan account — complements `README.md` (architecture/usage).
The goal is that future work on this codebase doesn't rediscover the same
bugs. Deployment specifics (server address, SSH access, restart procedure)
are intentionally **not** in this file — this repo is public and the
webhook endpoint is unauthenticated by design, so infra details are kept
out of git.

## Real bugs found and fixed (via live testing)

1. **Duplicate `SEM_TRADING_SYMBOL` in Dhan's scrip master CSV.** Two
   different option contracts can share the identical trading-symbol
   string with different `security_id`s. Always resolve broker-reported
   positions by `security_id` (unique), never by re-matching the symbol
   string — see `_instrument_meta_by_security_id()` in `dhan_client.py`.

2. **Tradehull's REST methods (`get_ltp_data`, `get_quote_data`, ...)
   force-uppercase the symbol before matching**, which silently breaks
   against the scrip master's mixed-case month format
   (`SBIN-Aug2026-1100-CE`) while surviving against the space-separated
   `SEM_CUSTOM_SYMBOL` format (`SBIN 25 AUG 1100 CALL`). Always use the
   latter format internally.

3. **Exit `product_type` must match what the position was actually opened
   under.** A SELL placed with a mismatched product type (e.g. MIS against
   a MARGIN position) isn't recognized by Dhan's RMS as squaring off the
   existing position — it's priced as a fresh naked short requiring full
   margin and gets rejected ("insufficient funds"). `Position.product_type`
   is now captured/threaded through from the real broker position.

4. **Order tags (`correlationId`) reject special characters.** Stock
   symbols like `GVT&D`, `M&M` contain `&`, which Dhan's API rejects
   outright (`DH-905 Invalid correlationId`). `_gen_tag()` strips to
   alphanumeric only before embedding the symbol.

5. **Dhan's market-data REST calls intermittently rate-limit-fail** on
   rapid back-to-back calls (generic
   `{'status': 'failure', 'remarks': {...None...}}` envelope, no useful
   error detail). Confirmed a single isolated call succeeds where a rapid
   sequence fails. Mitigated with a small retry helper (`_retry()` in
   `dhan_client.py`) plus pacing between symbols in
   `rank_and_pick_top_stocks()`.

6. **Two independent race conditions between the normal order-placing flow
   and periodic background sync**, both confirmed live and fixed with an
   explicit-ownership pattern (never inferred from timing):
   - **Entry race:** `_sync_pending_orders()`'s AMO-recovery scan could
     promote the same BUY order to a `Position` twice if a poll landed
     while `_enter_single_position()` was still resolving it inline (e.g.
     delayed by #5's rate limit). Fixed via `OrderRecord.owned_by_placer` —
     the sync path may only touch an order after the placer explicitly
     hands it off.
   - **Exit race:** once exits became event-driven (see below), the same
     class of race existed between `monitor_loop`'s poll and instant
     WebSocket-tick-driven exits both trying to close the same position.
     Fixed via `PositionStore.try_start_exit()` — an atomic claim, verified
     offline with 10 concurrent callers racing for one claim (exactly one
     winner, every time) before deploying.

7. **`Tradehull.order_placement()` swallows its own errors** — on failure
   it prints to console/log and returns `None` rather than raising, so the
   underlying reason for a failed placement (as opposed to a REJECTED
   status on an order that *did* get an order id) is only visible in
   Tradehull's own console output, not in our exception message.

8. **Tradehull's instrument-cache code has a Windows-path bug on
   macOS/Linux** — hardcoded `"Dependencies\\" + filename` creates a flat
   file literally named `Dependencies\<name>` instead of a subdirectory.
   `.gitignore` needs both `Dependencies/` and `Dependencies\\*` to catch it.

9. **The Supertrend exit fired on the same 5-min candle a position was
   entered on, cutting winners flat at breakeven.** Chartink triggers a
   stock the instant its 5-min candle shows a breakout; that same candle's
   *close* can already sit below the Supertrend band if the move faded
   before the candle closed, so checking the signal right after entry
   often just re-reads the same breakout candle, not a confirmed reversal.
   Found via backtesting the exit against a real day's webhook triggers
   (49 alerts, 23 entries replayed against real Dhan candles): 6 of 9
   divergent trades were same-candle reads, costing ₹9,796 of forfeited
   target hits, against only ₹4,580 gained from genuine later reversals —
   net ₹5,216 worse than target/stop-loss alone. Fixed by capturing the
   underlying's Supertrend candle boundary at entry
   (`Position.supertrend_entry_candle_start`) and only honoring a bearish
   read once the cached signal has moved to a *later* candle (see
   `trading_engine._supertrend_signal_for`). Re-running the same backtest
   with the fix flipped the net effect from −₹5,216 to +₹752 vs. baseline,
   while keeping every genuine multi-candle-later catch (e.g. turning one
   stock's −₹2,640 stop-loss into +₹810).

10. **The single-day backtest above wasn't enough data — a 7-day, 99-trade
    backtest (13–21 Aug 2026) showed the entry-candle fix alone was
    net-negative (−₹5,964 vs. target/stop-loss alone) once a full week's
    variety of trades was included.** Two distinct mechanisms were driving
    it, found by clustering the divergent trades' exit timestamps:
    - 17 of 29 divergent trades all exited via Supertrend at exactly
      **10:10** regardless of stock or day. `SUPERTREND_PERIOD=10` on
      5-min candles starting at the 09:15 market open means *every*
      underlying's Supertrend first has enough data to produce a value at
      exactly 10:10 — and that first value is seeded from a naive
      close-vs-band heuristic (confirmed directly against real 5-min
      candles) that's structurally biased toward reading "bearish" on its
      very first bar, independent of the stock's actual trend. This
      cluster's net effect happened to be roughly neutral on this
      dataset, but it isn't a real per-stock signal - it's an artifact of
      every underlying's indicator warming up at the same wall-clock
      moment.
    - The other 11 divergent trades: 9 of them exited exactly **one
      candle (5 min) after entry** - even past the entry candle itself,
      the very next candle was still often riding the same breakout's
      aftershock rather than confirming an independent later reversal.
      This was the larger drag (−₹8,627 across those 11 trades).

    Fixed by adding `config.SUPERTREND_ENTRY_GRACE_MINUTES` (default 5,
    i.e. one extra 5-min candle) on top of the entry-candle skip -
    `trading_engine._supertrend_signal_for` now requires the signal to
    have moved that far past the entry candle before honoring a bearish
    read. Swept grace periods of 0/5/10/15 minutes against the same 99
    trades: 5 minutes was the best performer, flipping the week's net
    effect to **+₹2,951** vs. baseline with a better win rate (70.7% vs.
    67.7%) using fewer, higher-quality exits (25 vs. 29). The 10:10
    warmup cluster still fires under this fix (grace alone doesn't reach
    past it for early-morning entries) - it just wasn't the main problem
    this particular week. A smarter Supertrend seed (or a longer
    mandatory warmup independent of any position's entry time) would be
    the next thing to try if a future backtest shows that cluster turning
    net-harmful.

11. **`reserve_symbol()`'s capacity check counted the wrong set, leaving a
    window where concurrent entries could exceed `MAX_LIVE_POSITIONS`.**
    A symbol joins `reserved_symbols` the instant it's reserved, but only
    joins `live_positions` later, after its order is placed *and* filled -
    a multi-second (or, for an AMO, much longer) window. The capacity gate
    checked `len(live_positions)`, not `len(reserved_symbols)`, so two
    entries reserved concurrently (e.g. one from `/chartink/webhook`, one
    from `/chartink/webhook-sell`, firing near-simultaneously) could each
    see `live_positions` still under the cap and both proceed - landing
    one over the configured cap once both filled. Low-risk with a single
    webhook (Chartink rarely double-fires the same alert at the exact same
    instant); became a real path once two independent webhooks could both
    be entering at once. Found by reasoning through exactly that scenario
    when asked whether the two-webhook setup could handle concurrent
    triggers safely - not caught live. Fixed by gating on
    `len(reserved_symbols)` instead, which is always a superset of
    `live_positions` (every `add_position()` / `reconcile_from_broker()`
    call adds to both) - strictly tighter, no change to normal
    single-webhook behavior since that loop is sequential and already kept
    the two sets in sync.

12. **The stepped/"ratchet" dynamic stop-loss (`ENABLE_DYNAMIC_SL`), as
    first specified (every +4% raises the floor 1%), backtested
    net-negative on real CE data - not a code bug, a strategy finding.**
    Replayed against the same 7-day, 99-trade CE dataset used for bugs
    #9/#10 (see `BACKTEST_RESULTS.md` for the full numbers and mechanism
    breakdown):
    - Alone (no Supertrend): **−₹2,170** vs. target/SL-only baseline.
    - Stacked on the already-deployed Supertrend exit (current
      production): **+₹1,326.75** vs. baseline - positive, but **−₹1,625**
      worse than Supertrend alone would have done by itself.

    Mechanism: 18 of 99 trades diverged from baseline. 9 worked exactly as
    intended (the raised floor caught a real reversal earlier than the
    fixed stop-loss would have, +₹3,067 combined). But 2 trades whipsawed
    - rallied enough to raise the floor, pulled back and got stopped out
    by it, then *recovered* to close near flat under baseline (COFORGE, 19
    Aug: −₹2,898 under dynamic SL vs. −₹48 under baseline, riding the same
    whipsaw to EOD). Those 2 whipsaw cases cost more than the 9 genuine
    catches gained. This is the textbook trade-off of any tightening
    stop-loss - protection against continued decline vs. sacrificing
    recoveries after a reversal - and this specific week's data, the
    whipsaws won. A wider step (tested: 7%, see `BACKTEST_RESULTS.md`)
    triggers on fewer, more decisive moves and is the natural next lever
    to try before concluding the whole approach doesn't work - a narrow
    4% step is inherently more whipsaw-prone than a wider one, independent
    of whether the ratchet concept itself is sound.

    **Follow-up: widening the step from 4% to 7% (increase left at 1%)
    fixed it.** Same 99-trade dataset, same mechanism, both known
    whipsaw cases (COFORGE, GLENMARK) gone entirely - 0 of 9 divergent
    trades were negative, vs. 2 of 18 at 4%:
    - Alone (no Supertrend): **+₹1,779.50** vs. baseline (was −₹2,170 at
      4%).
    - Stacked on Supertrend: **+₹3,226.75** vs. baseline - now *better*
      than Supertrend alone (+₹2,951.75), not worse. Confirms the
      hypothesis above: the 4% figure was the problem, not the ratchet
      concept. **Deployed** - CE's dynamic SL step defaulted to 7% at this
      point (before the CE/PE split below). See `BACKTEST_RESULTS.md` for
      the full comparison table.

13. **7% wasn't automatically safe for PE too - a single severe whipsaw
    showed the same failure mode can still happen, on real PE data, at
    the width that fully fixed it for CE.** Ran the same Supertrend +
    Dynamic SL (7%/1%) combo against a 7-day, 68-trade PE dataset (see
    `BACKTEST_RESULTS.md`'s PE section for the full table and day-by-day
    breakdown). Note: this run pulled 68 completed trades vs. 61 in the
    original PE Supertrend-only validation on the *same* CSV - not a bug,
    confirmed by checking `MAX_POS`/`TOP_N`/ranking direction/strike
    selection all match the working script exactly. PE's `%change` values
    cluster far more tightly than CE's (mostly −0.1% to −3%), so which 3
    symbols rank in the top-N is more sensitive to small precision/timing
    differences between separate historical-data fetches from Dhan than
    it was for CE. All variants below are compared within this one
    68-trade run, so the comparison itself is still apples-to-apples even
    though the absolute baseline number isn't comparable to the old
    61-trade run's ₹79,653.50.
    - Baseline (this run): ₹55,333.75.
    - Supertrend alone: +₹96.25 vs. baseline - roughly flat, consistent
      with the original PE validation (bug/entry above - only a few
      trades ever hit the Supertrend exit in this dataset).
    - Dynamic SL alone (7%/1%): **−₹2,015.00** vs. baseline.
    - Combined (then-current production): **−₹2,418.75** vs. baseline.

    Mechanism: almost entirely one trade. VOLTAS, 17 Aug, entered 09:35 @
    ₹20.30. The option spiked >7% within 14 minutes, so the ratchet raised
    the floor and stopped it out at 09:49 for −₹1,406. Left alone
    (baseline, and separately confirmed under Supertrend-only too - this
    was purely a dynamic-SL effect, not a Supertrend one), it kept climbing
    and hit the 25% target at 11:02 for +₹2,006. A single −₹3,412 swing,
    outweighing the other 4 divergent trades' combined +₹1,398 of genuine
    catches. Same class of risk as bug #12's COFORGE/GLENMARK whipsaws at
    4% on CE - just occurring for PE at a step width (7%) that had fully
    eliminated it for CE. One severe trade in one week isn't enough
    evidence that 7% is *wrong* for PE (same "don't trust a small sample"
    lesson as bugs #9→#10, just cutting the other way this time) - but
    it's also not evidence 7% is *right* for PE, since the two legs never
    had independent evidence either way until this run.

    **Decision: split `DYNAMIC_SL_STEP_PCT` into
    `DYNAMIC_SL_STEP_PCT_CE` / `DYNAMIC_SL_STEP_PCT_PE`, both still
    defaulting to 7% (strategy unchanged for now).** The mechanism itself
    is symmetric by construction (reads only the option's own premium),
    but there's no reason the two legs' *optimal* width has to match -
    CE and PE options behave differently (different underlyings' typical
    volatility, different premium price levels day to day). Deployed as a
    config split only, not a behavior change; revisit once more weeks of
    PE data accumulate to see whether PE genuinely wants a wider step than
    CE, or whether this VOLTAS case was just one week's noise.

14. **Tested whether an ATR-scaled (volatility-adaptive) dynamic-SL step
    would fix the VOLTAS whipsaw better than just widening the flat PE
    step - a flat, wider number won outright, no ATR needed.** Built a
    second backtest harness that replays the identical 68 PE / 99 CE
    fixed entries (loaded verbatim from bug #13's own result files, not
    re-ranked, to avoid the entry-drift problem those files' own history
    already surfaced), with the ratchet step scaled per-trade as
    `clamp(K * daily_ATR_pct, 3%, 15%)` - `daily_ATR_pct` from the
    underlying's 14-day daily ATR (Wilder's smoothing) as of the day
    *before* entry (no lookahead), `K` swept across 3/5/7. Two real bugs
    surfaced and were fixed before trusting any result: a dict-shape
    crash building the output rows, then the *exact* PE-direction-flip
    bug from bug #13's own history recurring in the new script - using
    `not bearish` instead of the `is False` identity check, which reads
    "Supertrend hasn't warmed up yet" (`None`) as a false PE exit signal
    (`not None` is truthy in Python). Sanity-checked against known numbers
    (baseline/fixed-7%/Supertrend-alone all reproduced exactly, both CE
    and PE) before trusting anything past that.

    ATR-scaled result: PE's best K (5, median implied step ≈12.5%,
    frequently pinned against the 15% ceiling) fully cleared the VOLTAS
    whipsaw and beat every prior PE variant - combined
    ₹55,576.25 (+₹242.50 vs. baseline), vs. the deployed fixed-7% combo's
    −₹2,418.75. CE's best K (3, median implied step ≈7.6%, close to its
    already-good flat 7%) was statistically a wash vs. the existing fixed
    7% (−₹75 to −₹275 out of ~₹110k) - no new whipsaws reintroduced
    (checked COFORGE/GLENMARK specifically), just slightly fewer genuine
    catches (4 vs. 9) since the ATR-derived step happened to run a touch
    wider than 7% for this week's CE names.

    The asymmetry itself was the interesting signal: CE wanted a *narrow*
    multiplier (steps landing near its already-good 7%) while PE wanted a
    *wide* one (steps mostly pinned at the ceiling, ≈12-15%) - which
    raised the obvious cheaper question: does PE actually need
    per-trade ATR scaling, or does it just need a wider *flat* number?
    Tested directly with a third, much simpler script (no daily-OHLC
    fetch at all) sweeping flat PE steps 7-15% against the same 68 fixed
    entries: **a flat 9% step matched the ATR-scaled combined result
    exactly (₹55,576.25) and beat it in isolation (₹55,980.00, +₹646.25
    vs. baseline - the single best PE result found this session)** -
    catching a BIOCON reversal (+₹500) that ATR K=5's wider-on-average
    step had missed. VOLTAS itself clears the whipsaw at 9% and stays
    clear all the way through 15%, with zero new whipsaws appearing
    anywhere in that range - not a knife-edge fit to one trade.

    **Deployed: `DYNAMIC_SL_STEP_PCT_PE` raised from 7% to 9%.**
    `DYNAMIC_SL_STEP_PCT_CE` left at 7% (its own sweep already won there -
    no reason to touch it). ATR-scaling itself was *not* built into the
    live bot - a live daily-OHLC fetch/cache would have been real added
    complexity and a new failure surface for a result a one-line config
    change already matched or beat. Same caution as every round before
    this one: still one calendar week of PE data, and 9% is calibrated to
    fix the one whipsaw actually observed (VOLTAS) - the flat plateau
    from 9-15% is reassuring but not proof against a whipsaw shape we
    simply haven't seen yet. See `BACKTEST_RESULTS.md`'s PE Round 3 for
    the full comparison table.

15. **A restart on 22 Aug 2026 landed in a ~3.5-minute window where Dhan
    was rejecting the access token, self-healed by `Restart=always`, and
    would have left zero trace once journald's retention passed.**
    `dhanboy.service` crash-looped from 08:08:16 to 08:11:57 UTC (~30
    rapid restarts, `RestartSec=5`), each attempt failing with `DH-901
    Invalid_Authentication` on the *same* access token that had worked
    right before and worked again immediately after - not a bad token, a
    transient rejection on Dhan's side. `Restart=always` did exactly what
    it should: kept retrying until Dhan started accepting the token again,
    then came up clean.

    Investigating this exposed a real footgun: **the droplet's system
    clock is UTC, but journalctl prints bare local timestamps with no
    timezone label** - "08:08" in the log is 13:38 IST, squarely mid-
    trading-session on a weekday, not the pre-market time it looks like
    at a glance. Misread this exact way during the initial investigation
    before catching it with `timedatectl`. Also confirmed (via the
    `Stopping dhanboy.service...` log line, which only appears for a
    deliberate `systemctl restart`/`stop`, never an organic crash, plus a
    concurrent SSH session from 08:04-08:30 UTC) that this was a
    deliberate restart landing badly, not a spontaneous crash - though
    which restart specifically (a deploy from earlier the same session,
    most likely) couldn't be pinned down from available logs.

    No live-trading impact this time (22 Aug 2026 was a Saturday), but
    the underlying risk is real: any ordinary deploy restart - the kind
    covered by the existing "check `/positions` is empty first" checklist
    - could occasionally take a few minutes instead of ~5-8 seconds if it
    lands in one of these Dhan-side blips, during which no entries/exits
    fire, with no prior mechanism to detect or record it happening. See
    the watchdog design decision below for the fix.

16. **The 10:10 Supertrend warmup cluster (bug #10), backtested as
    "roughly neutral" on the original 7-day week, turned decisively
    net-harmful on a larger 14-day dataset - exactly the risk bug #10
    flagged as worth fixing "if a future backtest shows this cluster
    turning net-harmful."** User supplied a fresh 15-day CSV (4-21 Aug
    2026, 14 actual trading days - the original week plus 7 new earlier
    days), replayed against the current deployed config
    (`MAX_LIVE_POSITIONS_CE/_PE=2`, matching true production capacity,
    not the old `MAX_POS=3` convention). Results, target/SL baseline
    ₹152,913.50 (CE) / ₹77,313.50 (PE):
    - CE Supertrend alone: **−₹29,525.25** vs. baseline (was +₹2,951.75
      on the original week) - combined (then-production): −₹29,230.25.
    - PE Supertrend alone: −₹662.50 (small, not the same mechanism - see
      below).
    - Dynamic SL (7% CE / 9% PE) stayed clean: 6 divergent trades total
      across both legs, all genuine catches, zero whipsaws - confirms
      that tuning is holding up on entirely new data.

    Mechanism, confirmed directly: **14 of 104 CE trades exited at
    exactly 10:10 via SUPERTREND_EXIT, netting −₹20,559.75 by
    themselves** - about 70% of the total damage. Real winners got cut
    flat: HINDALCO would have hit target at 14:50 for +₹8,505, instead
    exited at 10:10 for +₹420; same story for HEROMOTOCO, BOSCHLTD,
    MPHASIS, POLYCAB, HYUNDAI, TATASTEEL, BAJFINANCE. A few genuine
    catches (AMBER, PGEL, OBEROIRLTY, ADANIGREEN, +₹8,712 combined) don't
    come close to offsetting it. **Zero PE trades exited at 10:10** -
    confirmed why: the naive seed (see bug #10) is structurally biased
    toward reading "bearish" on the indicator's first computable bar,
    which is exactly the direction CE's exit condition needs but the
    *opposite* of what PE's exit condition needs, so the same artifact
    that hammers CE structurally almost never fires for PE.

    **Investigated two candidate fixes, backtested against the same
    14-day CE+PE data (identical fixed entries, not re-ranked):**
    - **Fix A - smarter seed**: replace the naive band-width-comparison
      seed with a comparison against the SMA of closes seen so far
      (roughly balanced instead of structurally biased). Result: helped
      CE (+₹19,010 vs. the unfixed behavior measured in this run) but
      **hurt PE badly in every combination tested (−₹3,496 to −₹7,583)**
      - whatever bias it removes from CE's seed introduces new false
        signals that specifically hit PE. Rejected for this reason alone.
    - **Fix B - longer mandatory warmup**: gate ANY Supertrend signal
      behind a minimum candle count above the bare `SUPERTREND_PERIOD`
      minimum, independent of any position's own entry+grace gating.
      Doesn't fix the seed's bias, just delays when the (still-biased)
      first signal can fire. Swept 15/20/25 candles: **20 was the clear
      best (+₹22,027.50 vs. unfixed CE), non-monotonically - 25 was
      worse than 20** (a longer warmup isn't simply "safer," there's a
      real sweet spot). PE was unaffected at 15/20 (no PE trades ever
      hit those thresholds anyway) and mildly *improved* at 25 (+₹770) -
      no case where Fix B hurt PE, unlike Fix A.

    One methodology note surfaced while sanity-checking the fix script
    against the known 14-day numbers: re-fetching a historical day's
    underlying candles from Dhan **on a separate call** returned a
    slightly different value for 15 of 104 CE trades' Supertrend
    readings than the original run's fetch had - not a script bug
    (traced and confirmed the full ₹7,717.75 gap came from exactly those
    15 trades), but the same underlying-data non-determinism already
    seen in this session as entry-ranking drift (see bug in PE Round 2's
    68-vs-61-trade discrepancy), now manifesting as exit-signal drift
    instead. Since every variant in a given run shares the same
    (re-)fetched data, within-run comparisons stay valid; only exact
    cross-run number reproduction is affected.

    **Honest bottom line: even Fix B (the better fix) still leaves CE's
    Supertrend contribution at −₹14,920.50 vs. plain target/SL on this
    14-day set** - roughly half the damage of the unfixed behavior, not
    a full recovery, and a long way from the +₹2,951.75 it scored on the
    original week. Supertrend's edge for CE has now shown real
    inconsistency across two different weeks even with a working fix
    applied, unlike dynamic-SL, which has been clean and modestly
    positive on both weeks tested. **Deployed Fix B anyway** (per
    explicit instruction) as a genuine improvement over the broken
    behavior - not a claim that Supertrend is now reliably good for CE,
    which the data doesn't yet support either way. `SUPERTREND_MIN_WARMUP_CANDLES=20`
    added to `config.py`/`dhan_client.refresh_supertrend_signal()`; Fix A
    (smart seed) was not implemented anywhere, since Fix B alone
    outperformed it on CE and Fix A actively hurt PE. See
    `BACKTEST_RESULTS.md`'s 14-day validation section for the full
    tables.

17. **The `Options/` package-split deploy (23 Aug 2026) crash-looped for
    ~2m15s on restart - not a refactor bug, a genuinely expired access
    token on the droplet that had silently drifted out of sync with the
    local one.** Restarting after the code refactor failed with the same
    `DH-901 Invalid_Authentication` error as bug #15, but this one didn't
    self-heal - because unlike bug #15's transient Dhan-side rejection,
    this token was *actually* expired: the droplet's `.env` still had a
    token with `exp` 2026-08-23 08:10:55 UTC while the restart attempt
    was at 14:53 UTC, nearly 7 hours past expiry. The local `.env` (this
    machine) already had a newer, valid token (`exp` 2026-08-24 10:53:51
    UTC) - it had been refreshed at some point without ever being pushed
    to the droplet. Confirmed by decoding both tokens' JWT `exp` claims
    directly rather than guessing from the error message alone. Fixed by
    copying the valid token's line into the droplet's `.env` (only that
    line - the droplet's `.env` has other intentionally-divergent values,
    see the deployment memory file) and restarting.

    The watchdog (bug #15's fix) caught this cleanly - first real,
    non-test incident it's recorded, with the full ~135s outage and
    journal excerpt captured automatically via `GET /incidents`. It did
    exactly the job it was built for.

    **Open gap, not yet fixed:** nothing currently keeps the droplet's
    `DHAN_ACCESS_TOKEN` in sync with a locally-refreshed one. If the
    token gets regenerated locally (or any other way) without a matching
    push to the droplet's `.env`, the bot will run fine until its next
    restart, then crash-loop indefinitely on a token that looks identical
    in shape to a normal transient rejection - nothing distinguishes
    "temporarily rejected" from "actually expired" in the error message
    itself, only decoding the JWT's own `exp` claim does. See "Open
    questions" below.

18. **Explored a NIFTY/BANKNIFTY index-options scalping strategy
    (opening-range breakout + short EMA momentum, tight target/stop/
    time-box exits) - backtested it, found a hard data ceiling specific
    to index options, and shipped it as paper-trading only.** Confirmed
    technically feasible first: both indices' ATM options resolve via
    the same `ATM_Strike_Selection` call already used for stock options,
    live index ticks come from the same `dhanhq` `MarketFeed` already
    wired up (security IDs: NIFTY=13, BANKNIFTY=25, segment `IDX_I`), and
    1-min historical candles are available for both.

    **The real constraint: index options are weekly/near-term, and
    Dhan's instrument master only lists currently-live contracts** -
    unlike stock options' monthly expiry (which stays resolvable for the
    whole month, how every other backtest this session worked), an
    expired index contract is gone entirely, no security_id to fetch
    historical candles for. Both NIFTY and BANKNIFTY's nearest listed
    expiry was 2026-08-25 at test time; their previous expiry (~18 Aug)
    was already delisted. That left only 19-21 Aug 2026 (3 trading days)
    historically resolvable via this API at all - a hard ceiling, not a
    "not enough time to fetch more" limitation.

    Backtested that 3-day window anyway as a mechanism sanity-check, not
    a real validation (this session's own repeated lesson - a single day
    wasn't enough to trust in bugs #9→#10, a week wasn't always enough
    either - 3 days is worse than both): NIFTY 9 trades, gross ≈breakeven
    (+₹27.89), **net −₹332.11** once a flat ₹40/trade cost + 0.5%/side
    slippage were applied - the entire result is transaction-cost
    erosion, not a real losing signal. BANKNIFTY looked better on the
    surface (12 trades, net +₹2,199.25) but checking trade-level
    concentration showed **2 of 12 trades (both `TARGET_HIT` on 21 Aug)
    account for 100% of that net total** - the other 10 trades wash out
    to roughly zero. Neither number should be read as a validated edge;
    the one structural finding worth keeping regardless of sample size
    is that NIFTY's lower option premiums (~₹60-150 here vs.
    BANKNIFTY's ~₹280-420) make this exact target/stop sizing far more
    cost-sensitive on NIFTY than BankNifty, independent of signal
    quality. See `BACKTEST_RESULTS.md` for the full trade tables.

    **Decision: ship it as `IndexScalping/`, paper-trading only
    (`config.PAPER_TRADING_ONLY = True`, asserted at startup in both
    `paper_engine.poll_loop()` and `index_main.lifespan()`).** Since the
    data ceiling above means no amount of additional backtesting can
    produce a bigger historical sample, the only way to get real evidence
    is to run the signal logic live and log what it would have done -
    see the design-decision entry below for how that's built and why
    it's deliberately REST-polling rather than tick-driven.

19. **Added a third strategy, `CopperOptions/` - MCX Copper options
    buying, paper trading only.** Rules as given: buy ATM+20-point CE
    when today's open > yesterday's close AND today's daily RSI(14) >
    yesterday's, AND the underlying's 5-min close is above both
    Supertrend(12,3) and Supertrend(11,2); PE is the exact mirror. Exit
    on a 5-min close crossing back through Supertrend(12,3), or a
    ₹5,000 unrealized loss, whichever first. Active only from 15:31 IST
    until commodity market close, and only when `config.STRATEGY_ENABLED`
    is true (the explicit on/off flag requested for after paper results
    are in - separate from `PAPER_TRADING_ONLY`, which is a hard
    invariant, not a switch).

    **Assumptions made explicit** where the given rules were
    underspecified (see `CopperOptions/config.py`'s own docstring for
    the same list, kept in sync):
    - *"ATM + 20 points" for both legs* would make the CE cheaper
      (more OTM) but the PE *more expensive* (more ITM) if taken
      literally on both sides - contradicts the stated goal ("so that
      option contract is little cheaper") for PE. Read instead as
      "20 points more OTM than ATM" for each leg - CE uses
      `ATM_strike + 20`, PE uses `ATM_strike - 20`.
    - *"Today's"/"yesterday's" open, close, RSI* are DAILY-timeframe
      values. MCX commodities have no continuously-quoted spot via this
      API - only futures - so these are computed on the underlying
      Copper futures contract, not a spot price.
    - *"Crossed above/below"* is read as a plain state check (is the
      close currently on that side of the Supertrend line right now),
      not edge-detection against the prior bar - algebraically
      equivalent for a loop re-evaluating every poll.
    - *MCX metals-segment close time* isn't exposed directly via this
      API; 23:30 IST is used, matching the time component on every
      Copper contract's own expiry timestamp in Dhan's instrument
      master - a reasonable proxy, worth confirming against Dhan's
      published session times if this ever needs to be exact.

    **A real, unavoidable timing issue found while building this**:
    Copper options expire monthly, and the nearest listed expiry at
    build time (2026-08-24) was literally the next calendar day - the
    first live/paper test would otherwise have traded a same-day-expiry
    (extreme gamma/theta) contract purely because it happened to be
    "nearest," not because anyone chose that risk deliberately.
    Fixed with `config.MIN_DAYS_TO_EXPIRY` (default 3): the engine
    resolves the nearest expiry with at least that many days left and
    automatically rolls to the next monthly cycle otherwise, then finds
    the futures contract for the *same calendar month* to use as the
    signal's underlying (Aug options -> Aug future, Sep options -> Sep
    future, etc. - verified this pairing holds in the real instrument
    data). Verified end-to-end against live data before deploying:
    correctly skipped the expiring-tomorrow Aug cycle for Sep, computed
    a sane daily RSI/gate from real Copper futures history, and resolved
    symmetric ATM+/-20 strikes (1410 CE / 1370 PE around a shared ATM of
    ~1390) from the real strike chain.

20. **Backtested the Copper strategy against the ~20-day window (24 Jul
    - 20 Aug 2026) and found the backtest itself unreliable - not a
    strategy-negative result, a data-quality ceiling specific to MCX
    Copper *options'* historical intraday data.** Used the August-2026
    expiry (the only cycle actually listed throughout that window) fixed
    for the whole run - a fair approach given that's genuinely all that
    would have been tradeable on those historical days.

    Raw output: 14 trades, net ≈+₹4.34, 21.4% win rate - looked flat, but
    **11 of 14 trades showed entry price exactly equal to exit price**,
    which is not a real flat outcome. Traced one directly: `COPPER 24 AUG
    1360 CALL` on 30 Jul has 1-min data only from 10:34 to **17:59** -
    nothing after, all day. Unlike the Copper *futures* (full 5-min
    coverage to 23:30), the *options'* historical data thins out or stops
    entirely partway through the day, inconsistently by contract/day -
    and this strategy's whole operating window (15:31 onward) sits right
    in the part of the day where that data goes quiet most often. The
    backtest script's price lookup silently fell back to "last known
    price" whenever a signal fired after data went stale, turning a
    "we don't actually know what happened" case into a fake ₹0.00 trade.

    Only 3 of 14 trades had genuine price movement between entry and
    exit (all modestly positive: +₹2.64, +₹1.32, +₹0.38, all Supertrend
    exits that happened to fire before that day's data went quiet) - n=3
    across ~20 days is nowhere near enough to conclude anything, and
    isn't even a representative sample of the strategy's actual signals
    (it's specifically the subset lucky enough to avoid the data gap).

    **Important distinction: this may not be a live-trading problem, only
    a backtesting one.** The live paper-trading engine
    (`CopperOptions/paper_engine.py`) uses `get_option_ltp()` - a live
    quote - not historical candles, so it doesn't inherit this *specific*
    artifact. Whether the live LTP itself also effectively goes stale on
    a thin evening contract is a real open question, but it's one only
    the actual paper-trading run can answer - watch for the same
    telltale sign (an open position's unrealized P&L not moving for a
    long stretch despite the underlying moving) rather than assuming the
    backtest's problem does or doesn't carry over.

    **Verdict: inconclusive, not negative.** Treat this backtest as
    "couldn't get a trustworthy read," not "the strategy loses." The
    live paper-trading results (already running, `STRATEGY_ENABLED`
    flag already in place for turning it off if warranted) are the real
    evidence to watch going forward. See `BACKTEST_RESULTS.md` for the
    full trade table.

21. **Fixed the stale-token gap from bug #17 at its root, instead of
    just monitoring for it - switched authentication from a manually-
    refreshed access token to Dhan's `pin_totp` mode.** Confirmed first
    (bug #17's own open question): Dhan access tokens are capped at 24h
    *by regulation* (SEBI/exchange rule, effective 1 Oct 2025) regardless
    of which flow generates them - there's no "longer-lived token"
    option to switch to instead, from Dhan or anyone else.

    What *is* long-lived: a TOTP secret (the RFC 6238 seed, not the
    rotating 6-digit code - captured once from web.dhan.co's API
    settings page, under "Optional Settings -> Set-up TOTP") and the
    account's trading PIN. `Dhan_Tradehull` (already the library this
    bot uses) supports generating a session from these two directly -
    `Tradehull(ClientCode, mode="pin_totp", pin=..., totp_secret=...)` -
    computing the current TOTP code from the secret internally via
    `pyotp` on every call, no browser, no manual step, no expiry to
    track. Verified working twice before switching over: once as an
    isolated script against the real account (confirmed genuine
    success, not a cached-token false positive), once as a full local
    `TestClient` run of the whole app - both showed a freshly-generated
    token each time ("New PIN + TOTP access token validated
    successfully"), not a reused one.

    `DHAN_AUTH_MODE` added to `config.py` (`"access_token"` default,
    `"pin_totp"` opt-in) so the change is backward-compatible - nothing
    breaks for anyone not setting the new env vars.
    `dhan_client.authenticate()` branches on it; `DHAN_ACCESS_TOKEN` is
    left in `.env`, unused while `pin_totp` is active, as a fallback to
    revert to without needing to regenerate anything if `pin_totp` ever
    needs troubleshooting.

    **`DHAN_PIN` is meaningfully more sensitive than an access token** -
    it doesn't expire or rotate on its own the way a token does, and
    it's the credential that authorizes transactions on the account, not
    just data access. Worth remembering next time this `.env` is
    touched, copied, or included in a handoff folder.

22. **A market BUY order that took longer than `wait_for_order_result`'s
    poll budget (6 retries, ~6s) to reach a terminal status caused two
    distinct failures**, both observed live during market open on 24 Aug
    2026 (a session where Dhan's order-status/OHLC/LTP APIs were
    noticeably slower/flakier than usual - several unrelated `OHLC`/`ltp`
    calls failed transiently the same morning). Previously,
    `_enter_single_position` treated "not rejected, not cancelled, not
    AMO" as good enough to proceed as if filled:
    - **UNOMINDA PE**: the order never reached a terminal status within
      the poll budget and stayed `PENDING` (0 filled qty) for several
      minutes at the broker. The code's fallback-to-LTP call itself threw
      (a transient Dhan API failure), the exception propagated up and was
      caught by `enter_positions_for_stocks`'s outer handler, which called
      `release_symbol` - freeing the symbol reservation entirely while the
      order was still live and un-cancelled at the broker. Confirmed via a
      direct read-only broker check (`get_order_by_id` +
      `get_open_fno_positions`) that no position ever materialized;
      manually cancelled the stuck order once confirmed abandoned.
    - **INOXWIND CE**: the order also didn't reach a terminal status
      within the poll budget, but this time the LTP fallback call
      *succeeded* - so the code created a Position using that LTP
      (0.59) as `entry_price`. The order actually filled moments later for
      real, but at a materially different average price (0.46, confirmed
      via a follow-up `get_order_by_id` check) - a ~19% discrepancy
      between the assumed and actual entry price. This pushed the
      computed stop-loss level (based on the wrong 0.59 basis) to trigger
      a real exit that wouldn't have fired against the true 0.46 basis,
      and misreported the trade's P&L as -832 when the real economic
      result (0.46 entry, 0.44 real exit fill) was roughly -128.

    **Fix**: added a branch in `_enter_single_position` - if
    `wait_for_order_result` returns a non-terminal, non-AMO status (not
    rejected/cancelled either), don't guess a fill price or create a
    Position at all. Call `release_order_ownership` (not `release_symbol`
    - this keeps the symbol reserved so a repeat alert can't also enter it
    while the order's fate is still open) and return early. This hands the
    order off to `_sync_pending_orders` - already-existing machinery
    originally built for AMO orders, which re-polls non-terminal BUY
    orders every monitor tick (`MONITOR_INTERVAL_SECONDS`) and promotes to
    a Position using the real fill price once Dhan actually confirms it,
    or releases the reservation if it ends up rejected/cancelled. No
    behavior change for the normal (fast, well within budget) case - this
    only activates when six retries genuinely aren't enough.

23. **`reconcile_broker_positions()` copied the broker's own display
    label into `Position.product_type` instead of the order-placement
    code our own API needs**, breaking every exit on a reconciled
    position. Found live on 24 Aug 2026, minutes after a routine restart:
    BIOCON's stop-loss triggered correctly but the SELL failed to place
    at all (`'Got exception in place_order as 'INTRADAY''`), retrying
    with growing backoff while the loss grew from a routine stop-out into
    a much larger one before being caught. Root cause: Dhan's positions
    API reports `product_type: "INTRADAY"` (a human-readable label), but
    `dhan_client.place_market_order`'s `trade_type` parameter needs the
    actual product code (`"MIS"`) - Tradehull's `order_placement()`
    swallows the resulting mapping failure and returns `None` instead of
    raising, so the only trace was that one console line. Fix:
    `reconcile_broker_positions()` now always uses
    `config.OPTIONS_PRODUCT` for a reconciled position's `product_type`,
    since this strategy only ever trades that one product type itself -
    the broker's label was never actually needed. Deployed same-day;
    confirmed working on the very next real exit (CONCOR) seconds after
    the restart.

24. **A gap in bug #22's own fix let a 3rd CE position through past the
    2-position cap** (`MAX_LIVE_POSITIONS_CE=2`), found live the same
    morning. `_enter_single_position` (bug #22's fix) returns
    `status="pending_confirmation"` for an order deferred to background
    sync, deliberately calling `release_order_ownership` (not
    `release_symbol`) to keep that stock's capacity reservation alive
    while its fate is still open. But `enter_positions_for_stocks` (the
    caller, in the per-stock loop) had its own separate allow-list -
    `if entry_result.get("status") not in ("entered", "amo_placed"):
    release_symbol(...)` - written before "pending_confirmation" existed,
    so it treated the deferred order as a failure and released the
    reservation anyway, undoing what the inner fix had just protected.
    Confirmed live: CE was already at its 2-position cap (VEDL, one slot
    free) when a single webhook alert ranked two more CE stocks
    (LODHA, PETRONET); LODHA entered normally, PETRONET hit the slow-fill
    path, got its reservation wrongly released, and its order filled for
    real minutes later via `_sync_pending_orders` - landing a 3rd CE
    position with no capacity check having ever refused it. Same gap
    would also have let a duplicate alert for PETRONET re-enter it while
    the order was still pending, since the dedup guard uses the same
    reservation. Fix: added `"pending_confirmation"` to
    `enter_positions_for_stocks`'s allow-list. The 3 CE positions this
    produced (VEDL, LODHA, PETRONET) were left open rather than force-
    closed - they're all genuine, already-funded positions; the cap is a
    forward-looking entry control, not a reason to unwind a real position
    early.

25. **A webhook alert arriving after `config.SQUARE_OFF_TIME` could open a
    position with zero further automated exit monitoring for the rest of
    the day.** Found live on 24 Aug 2026, seconds after square-off itself
    fired: a Chartink alert for INDIANB arrived at 09:45:07 UTC (square-off
    fired at 09:45:03), the webhook handler had no time gate at all and
    entered it normally. `monitor_loop`'s square-off is a one-time pass
    per day (`squared_off_today_for`, keyed by date) - once it's fired,
    every later loop iteration fails *both* branches of `if now >=
    square_off_at and today_key not in squared_off_today_for: ... elif now
    < square_off_at: ...` (the first because the date's already in the
    set, the second because time has passed), so a position entered after
    that point gets no target/SL check and no square-off for the rest of
    the process's life - not caught until the broker's own MIS
    auto-square-off (if the position survives that long) or the next
    day's `reconcile_broker_positions()`. Manually closed by hand once
    spotted live. Fix: added `trading_engine.is_past_square_off_time()`,
    checked at the top of `_handle_chartink_webhook` (`option_main.py`) -
    an alert arriving after `SQUARE_OFF_TIME` is now ignored outright
    (`status: "ignored", reason: "past_square_off_time"`) rather than
    silently entering an unmonitored position. Doesn't touch
    `monitor_loop` itself - the one-time square-off pass is still correct
    for positions that existed *before* the cutoff, this only stops new
    ones from slipping in after it.

26. **`CopperOptions/paper_engine.py`'s open paper position lives in
    memory only, same limitation as the live Options strategy's
    `position_store` (see "Known external constraints" below) - but
    unlike that strategy, nothing reconciles it back from a real broker
    position on restart, because there isn't one (it's paper trading).**
    Only *completed* trades are persisted (`PaperTradeStore._load_from_disk`
    / `.record()`); `_state.open_position` is a plain module-level
    variable. Found live on 24 Aug 2026: a COPPER 1410 CE paper position
    (entered 15:31, the strategy's activation time) was still open when
    `dhanboy.service` was restarted for an unrelated config change (the
    live Options strategy's target/SL update) - the restart silently
    dropped it with no exit ever recorded, no trace of how it would have
    resolved. Not a financial loss (paper trading), but a loss of the
    evidence this strategy exists to accumulate (see `BACKTEST_RESULTS.md`'s
    Copper section - the whole point of running this in paper mode is
    building a trustworthy live track record after the backtest itself
    came back inconclusive). A fresh position (1420 CE) was entered
    normally after the restart and closed normally via
    `SUPERTREND_EXIT`. **Not fixed** - noting for awareness: any restart
    while Copper has an open paper position will silently lose that
    trade's outcome. Worth fixing the same way if this strategy ever
    goes live for real (persist the open position too, not just
    completed trades), but low urgency while paper-only and restarts
    during Copper's 15:31-market-close window are infrequent.

27. **Bug #21's `pin_totp` switch automated *generating* a fresh Dhan
    token, but nothing automated actually getting the long-running
    process to *pick one up* before the previous one expires.**
    `authenticate()` only runs once, at process startup
    (`option_main.lifespan`) - there's no re-authentication anywhere in
    the running process's lifetime after that. Dhan tokens are still
    capped at ~24h by regulation regardless of how they're generated
    (this was already known - see bug #21's own writeup - the part that
    was missed is what happens to a process that's simply never
    restarted across that boundary). Found live on 24/25 Aug 2026: a
    process last (re)started at 18:24 IST on 24 Aug logged
    `Token validity: 25/08/2026 06:24` - meaning without a restart after
    that point, every Dhan API call from 06:24 IST onward on 25 Aug
    (including the market open at 09:15 and the new paper-trade
    webhook's first expected alert at 09:16 - see the design-decision
    entry below) would have started failing with an expired-token error,
    silently, with nothing about the process itself signaling that
    anything was wrong.

    **Fix**: a new systemd timer, `dhanboy-morning-refresh.timer`
    (`OnCalendar=*-*-* 02:30:00` UTC = 08:00 IST daily, `Persistent=true`),
    triggers `dhanboy-morning-refresh.service` (a oneshot
    `systemctl restart dhanboy.service`) every trading day - 1h36m past
    the observed ~06:24 IST expiry boundary (so a still-cached token from
    the day before is guaranteed to have actually expired and gets
    freshly regenerated, not just reused) and 1h15m before the 09:15
    market open. Deliberately a *restart*, not an in-process re-auth
    call - `authenticate()` already fully replaces `dhan_wrapper.client`
    on every call, and restarting reuses the exact same startup path
    already proven correct (reconcile_broker_positions, WS feed
    reconnect, etc.) rather than adding a second, less-tested code path
    for the same outcome. No live-position risk at 08:00 IST specifically
    - nothing can be open that early, so this is the same "positions
    guaranteed empty" safety window every other same-day restart this
    session had to check for manually.

28. **Every stock-option order placed on 25 Aug 2026 was RMS-rejected
    ("transactions are blocked by our risk systems for this stock") -
    across 14+ unrelated stocks, 100% failure rate.** Diagnosed via Dhan
    MCP (orderbook/portfolio agent tools): funds/margin were healthy
    (₹89,304.77 available, 0% utilized), so this wasn't a margin issue,
    and the identical message across every unrelated stock ruled out a
    genuinely per-scrip surveillance flag. Root cause: **25 Aug 2026 was
    the monthly F&O expiry day for every single-stock option contract.**
    NSE moved monthly expiry for all single-stock contracts to the *last
    Tuesday* of the month from 1-Sept-2025 (previously the last Thursday);
    25 Aug 2026 is that day. Stock options only have a monthly series (no
    weekly expiry exists for single stocks), so every stock-option
    contract available to buy that day had its current-month expiry
    landing that same day. Dhan (confirmed via a Dhan rep's statement on
    their own community forum) blocks *new* positions in a stock option on
    its own expiry day, across all product types, citing liquidity risk
    near expiry - not a bug, not an account-wide ban, just every candidate
    stock hitting the same routine, predictable, monthly restriction at
    once. A separate batch of LTP/OHLC API failures (empty error bodies,
    also briefly breaking IndexScalping's paper-trade pricing) showed up
    in the same window but looks like an unrelated, likely transient
    Dhan-side data-feed issue - not confirmed as expiry-related.

    **Fix (v1, same day)**: `_enter_single_position()` in both
    `Options/trading_engine.py` and `Futures/trading_engine.py` checked the
    ATM contract's own `expiry_date` (added to `AtmOption`/
    `_instrument_meta` in `dhan_client.py`, read from the scrip master's
    `SEM_EXPIRY_DATE` column) against today's date and skipped the entry
    outright on a match.

    **Fix (v2, same day, supersedes v1)**: skipping loses a trading day
    every month for no real reason - the underlying stock still has a
    tradeable option chain, just under next month's contract. Rather than
    skip, `DhanWrapper.get_atm_option()` now **rolls forward to the next
    listed expiry** (`ATM_Strike_Selection(..., Expiry=1)`) whenever the
    nearest one (`Expiry=0`) expires today, and only returns the
    still-expiring-today contract if the roll itself lands on the same
    date too (i.e. no further expiry is listed yet for that stock - an
    edge case, not the normal case). `_enter_single_position()`'s
    expiry-date check is now just the last line of defense for that edge
    case, not the primary behavior - normal days and stocks with a next
    expiry already listed trade straight through expiry day on the rolled
    contract, with no rejection and no skip. Verified fully offline
    (mocked `dhan_wrapper._get_atm_option_once`, zero real network calls
    reachable) in `/private/tmp/.../scratchpad/test_expiry_guard.py` -
    covers the roll succeeding, the no-further-expiry fallback, and a
    normal (non-expiry) day being unaffected - before deploy, per the
    stricter-verification practice established after the Futures
    package's testing incident (see the design-decision entry below).

## Design decisions

- **`Futures/` package + `POST /chartink/webhook-futures` (added 25 Aug
  2026), and the `dhan_wrapper.on_price_tick` collision it surfaced.**
  A fifth strategy package, explicitly a PLACEHOLDER by request: buys ATM
  CE *options* via mechanics identical to `Options/` (same ranking, entry,
  exit rules, near-verbatim copies of `trading_engine.py`/
  `position_store.py`), standing in until real futures-contract buying
  replaces it. Places REAL orders (explicitly requested, not paper) - own
  independent position pool/capacity (`FUTURES_*` env vars), own
  `dhan_client.py` that just re-exports `Options.dhan_client.dhan_wrapper`
  rather than opening a second Dhan session (same reuse pattern
  IndexScalping/CopperOptions already use).

  **Does NOT run `reconcile_broker_positions()` at startup**, unlike every
  other real-order strategy. `get_open_fno_positions()` returns every open
  FNO position in the account with no notion of which strategy placed it -
  if Futures also reconciled the same way Options does, a restart could
  re-import Options' own live positions into Futures' separate tracker
  too, and both strategies could then try to independently manage/exit
  the same real broker position. Trades restart-resilience (a real
  Futures position open across a restart won't be automatically
  recovered) for correctness (never double-tracking) - the right
  tradeoff for a new package. Also accepted, not fixed: since Options and
  Futures both rank/enter independently with identical instrument-
  selection logic, they could each open their own separate position on
  the same underlying if both alert on it around the same time - same
  class of tradeoff already accepted for the paper-trade webhook, now
  with real money on both sides. Worth revisiting if it's ever observed
  live.

  **Found and fixed while building this, before it could cause a live
  incident**: `dhan_wrapper.on_price_tick` was a single
  `Optional[Callable]` slot, set via direct assignment
  (`dhan_wrapper.on_price_tick = _on_price_tick`) in `option_main.py`'s
  lifespan. Had Futures' lifespan done the same thing, whichever
  strategy's lifespan ran last would have silently overwritten the
  other's handler - degrading the *already-live* Options strategy's
  instant tick-driven exits down to poll-only (`MONITOR_INTERVAL_SECONDS`,
  5s) with no error anywhere. Fixed in `Options/dhan_client.py`: replaced
  the single slot with `_on_price_tick_subscribers: list[Callable]` and a
  new `add_price_tick_subscriber()` method: `_on_market_tick` now calls
  every registered subscriber, each wrapped in its own try/except so one
  strategy's handler failing can't block another's. Verified offline
  (mocked, no real Dhan calls) that both Options' and Futures' handlers
  fire independently on the same tick before this was deployed.

- **`POST /chartink/webhook-papertrade` (`Options/paper_webhook.py`, added
  24 Aug 2026)** - a second, independent Chartink endpoint for evaluating
  a new scan before trusting it with real money, without touching the
  live strategy at all. Bullish/CE only, matching `/chartink/webhook`'s
  own convention (PE wasn't requested). Deliberately reuses the real
  strategy's own `rank_and_pick_top_stocks`, `Position`,
  `_exit_reason_for`, and `_supertrend_signal_for` (imported from
  `trading_engine.py`, not reimplemented) so the only variable under test
  is the new scan's stock-picking quality, not a different set of exit
  rules - the same principle the Robot01 CSV backtest used. Entirely
  separate position pool from the real strategy (`PAPERTRADE_TOP_N_STOCKS`
  / `PAPERTRADE_MAX_POSITIONS`, independent of `TOP_N_STOCKS` /
  `MAX_LIVE_POSITIONS_CE`) so a burst of alerts on either side can't
  starve the other's capacity. `PAPER_TRADING_ONLY = True` is a hardcoded
  module-level constant (not an env var), matching the
  IndexScalping/CopperOptions pattern - this module never imports
  `place_market_order`. Unlike CopperOptions' paper engine (bug #26), the
  open position here is persisted to disk on every change, not just
  completed trades - fixes that same restart-loses-history gap at the
  design stage instead of hitting it live first. Check results via
  `GET /papertrade/trades`.

- **Separate capacity caps per option type** (`MAX_LIVE_POSITIONS_CE` /
  `MAX_LIVE_POSITIONS_PE`, both default 2) replaced the single shared
  `MAX_LIVE_POSITIONS`. `reserved_symbols` changed from a `set[str]` to a
  `dict[str, str]` (underlying_symbol -> option_type) so `reserve_symbol()`
  can count "how many of *this* type are reserved" rather than "how many
  total" - a burst of bearish alerts filling PE capacity no longer has any
  effect on CE capacity, or vice versa. Dedup-by-symbol is unchanged and
  still spans both types: a symbol already reserved/open as either CE or
  PE still blocks a new entry of the *other* type for that same symbol -
  no simultaneous CE+PE bet on one underlying. Verified offline before
  deploying: 10 concurrent CE reservations against a cap of 2 (exactly 2
  win), 10 CE + 10 PE racing simultaneously (2 CE and 2 PE win,
  independently, zero cross-contamination), and the same symbol requested
  as both CE and PE at once (exactly one type wins, confirming dedup still
  holds across types).

- **Event-driven exits** — `dhan_client._on_market_tick` fires an
  `on_price_tick` callback (bridged from the WebSocket thread to the event
  loop via `asyncio.run_coroutine_threadsafe`) that evaluates
  target/trailing-SL immediately on every tick, instead of only on
  `monitor_loop`'s fixed poll. The poll loop remains as a
  fallback/heartbeat. Added after confirming live that exits were only
  running on the 5s poll even though the feed had ticks available in real
  time — two trades had already closed within ~60-70s of entry, so up to
  5s of that lifetime was spent "blind" between polls.
- **`/feed-stats` endpoint** (`dhan_client.DhanWrapper.stats`) exists
  specifically to prove or disprove whether the WebSocket caches are
  actually carrying load vs. REST silently doing everything — built after
  realizing there was no way to tell from logs alone.
- **`ENABLE_WS_FEED` / `ENABLE_TRAILING_SL`** are runtime toggles (env
  vars), not hardcoded, since both were expected to need tuning/disabling
  without a code deploy.
- **`reserved_symbols`** (formerly `traded_symbols_today`) only blocks a
  symbol while something is genuinely open/in-flight for it — a symbol is
  free to re-enter once its earlier position closes, for the rest of the
  same day.
- **Supertrend exit** (`dhan_client.refresh_supertrend_signal` /
  `get_cached_supertrend_bearish`) exits a position when the *underlying's*
  5-min candle close crosses below its 5-min Supertrend, alongside (not
  instead of) target/stop-loss. Computed on the underlying stock, not the
  option's own premium — option prices are too noisy/decay-affected for a
  clean trend read. Split into a blocking refresh (REST call to Dhan's
  `intraday_minute_data`, cached, called only from `monitor_loop`'s poll)
  and a synchronous cache-only read (called from `on_price_tick`) — the
  WebSocket tick path must never block the event loop on a REST call.
  Verified against real intraday candles before shipping: the indicator
  flips cleanly at genuine price crossovers rather than sticking in one
  state, and the still-forming current candle is dropped so the signal is
  always based on a fully-closed 5-min bar. Toggle via
  `ENABLE_SUPERTREND_EXIT`, same reasoning as `ENABLE_TRAILING_SL`.
  `SUPERTREND_MIN_WARMUP_CANDLES` (default 20) additionally withholds any
  signal at all until that many candles have formed since open,
  independent of `SUPERTREND_PERIOD` and independent of any position's
  own entry+grace gating - fixes the 10:10 warmup-cluster bug (#10/#16)
  by delaying when the indicator's still-biased first value can fire,
  not by fixing the bias itself. See bug #16 for why this fix was chosen
  over a smarter seed (which backtested worse for PE).
- **Two webhooks, one shared position pool, separate capacity caps.**
  `POST /chartink/webhook` (bullish, buys ATM CE) and
  `POST /chartink/webhook-sell` (bearish, buys ATM PE) both funnel into
  the same `enter_positions_for_stocks()` / `PositionStore` — a symbol
  already open from one blocks the other from also entering it
  (`has_open_position_for_underlying()` checks by underlying only, not
  underlying+option_type). Capacity itself is *not* shared - each type has
  its own cap (`MAX_LIVE_POSITIONS_CE` / `MAX_LIVE_POSITIONS_PE`, see bug
  #11 above for how that's enforced under concurrency). `option_type` is
  threaded through as a
  parameter (`enter_positions_for_stocks` → `_enter_single_position` →
  `get_atm_option`) rather than read from the global `config.OPTION_TYPE`,
  which now only serves as the fallback for reconciling a broker position
  of unknown origin. `rank_and_pick_top_stocks()` also takes a
  `prefer_highest` flag — the bearish webhook ranks by *lowest* %change
  (biggest decliners) rather than highest, since "strongest signal in the
  alert" points the opposite direction for a bearish scan.
- **The Supertrend exit direction depends on option_type, not just
  bearish/bullish.** A CE (long call) profits when the underlying rises,
  so a bearish crossover is the reversal-against-it signal — that's what
  the exit was originally built and backtested against. A PE (long put)
  profits when the underlying *falls*, so the reversal-against-it signal
  is the opposite: a *bullish* crossover. Using the same "bearish = exit"
  check for both would have exited PE positions exactly backwards —
  treating the move that confirms the PE thesis as the exit trigger.
  `trading_engine._supertrend_signal_for()` now branches on
  `position.option_type` to pick the correct direction. Caught by
  reasoning through the exit math while wiring up PE support, before any
  PE position had a chance to hit it live — the existing 7-day/99-trade
  backtest only covered CE trades, so this specific direction hasn't been
  independently backtested yet; worth doing once there's a real day of PE
  trades to replay.

- **`watchdog.py` + `dhanboy-watchdog.service`** - a separate,
  independent process (own systemd unit, own `Restart=always`) that polls
  `GET /health` every 5s and, if the app is unreachable for
  ≥30s (`INCIDENT_THRESHOLD_SECONDS`), appends a self-contained record to
  `incidents.log` on the droplet: start/end time in IST (explicit
  timezone label, after bug #15's UTC/IST mix-up during investigation),
  duration, resolved/ongoing status, and the actual `dhanboy.service`
  journal output for that exact window - captured *then*, not relying on
  journald's own retention later. Runs as its own unit specifically
  because it needs to detect the main app being *down*, which the app
  obviously can't do about itself. If an outage is still ongoing past the
  threshold, re-logs an updated record every 5 minutes
  (`ONGOING_UPDATE_SECONDS`) rather than staying silent indefinitely.
  Exposed read-only via `GET /incidents` (`main.py`) - most recent
  incidents first, so they're checkable over HTTP without SSHing in.
  Deliberately stdlib-only (no `requests`, no importing `config`/
  `dhan_client`) to stay cheap to run continuously on a ~1GB droplet
  regardless of what the main app's venv has installed. Built after bug
  #15's incident (a restart landing in a transient Dhan auth-rejection
  window, self-healed by `Restart=always`, would have left no trace once
  journald's retention passed) surfaced that the bot had no way to detect
  or remember this class of event on its own.

- **Options strategy code lives in its own `Options/` package
  (`config.py`, `dhan_client.py`, `position_store.py`, `trading_engine.py`,
  `option_main.py`), separate from the shared top-level `main.py`.**
  Done in preparation for adding non-options strategies later without
  entangling them with this one. `main.py` owns only what's genuinely
  strategy-agnostic - the `FastAPI` app instance, `/health`, `/incidents`
  (reads a file path, not any strategy's state) - and composes each
  strategy's own router + lifespan onto it (`Options/option_main.py`
  exports `router` and `lifespan`; a future second strategy would export
  the same two names from its own package and get mounted the same way
  in `main.py`, side by side). The four inner modules now use relative
  imports (`from . import config`, etc.) since they're a package, not
  loose top-level scripts - everything else about them is unchanged.
  `main.py` still lives at the repo root and is still run the exact same
  way (`uv run uvicorn main:app`) - no `WorkingDirectory`/systemd change
  needed, since Python resolves `Options` as a subpackage from the
  existing working directory rather than needing to *be* the working
  directory. `watchdog.py` needed no changes (already stdlib-only, no
  app imports). Verified locally end-to-end via FastAPI's `TestClient`
  before deploying - full lifespan (real Dhan auth, WebSocket feed,
  monitor loop) plus a live request against every endpoint, both the
  common ones and the ones routed through `include_router`.

- **`IndexScalping/` - a second strategy package, PAPER TRADING ONLY**
  (see bug #18 for why it's paper-only). Same `router` + `lifespan`
  export pattern as `Options/option_main.py`, mounted the same way in
  `main.py`. Two deliberate design choices worth knowing if this is ever
  touched again:
  - **Imports `Options.dhan_client`'s already-authenticated singleton
    directly**, rather than getting its own Dhan connection. Broker
    connectivity is genuinely shared infrastructure (auth, instrument
    master, WebSocket feed), so standing up a second one would double
    API/rate-limit usage and open a second redundant WebSocket for no
    benefit. The cleaner long-term shape would promote `dhan_client.py`
    out of `Options/` into a truly shared location - not done now,
    specifically to avoid touching a live, real-money-integrated module
    for a paper-only feature. Worth doing if a third strategy needs the
    same connection.
  - **REST-polling (`config.POLL_INTERVAL_SECONDS`, default 15s), not
    tick-driven off the WebSocket feed** like the options bot's exits.
    Building a real-time 1-min-bar aggregator from raw index ticks is a
    meaningfully bigger engineering lift, and the open question right
    now is whether the *signal logic* holds up over more data, not
    execution speed - 15s is fast enough to test that honestly without
    adding that complexity or hammering Dhan's rate limits (bug #5).
    Revisit if paper results ever look good enough to consider real
    capital, where execution latency would start to matter for real.

  **Hard safety invariant, not just a convention**:
  `config.PAPER_TRADING_ONLY = True`, asserted (not just documented) at
  the top of both `paper_engine.poll_loop()` and `index_main.lifespan()`
  - the process refuses to start if this is ever flipped without also
  removing the assertions. Every Dhan/Tradehull call in `paper_engine.py`
  is read-only (`instruments()`, `ATM_Strike_Selection`,
  `intraday_minute_data`, `get_option_ltp`) - it never calls
  `dhan_wrapper.place_market_order` (the only real order-placement entry
  point in `dhan_client.py`) or `dhan_wrapper.client.order_placement`
  directly. Completed paper trades persist to `paper_trades.log` (JSONL,
  gitignored) so a multi-week paper-trading run survives a process
  restart - unlike the live options bot, there's no broker to reconcile
  paper trades from, so this file *is* the only record. Exposed
  read-only via `GET /scalping/paper-trades` (gross vs. net P&L tracked
  separately, current open paper position if any, most recent trades
  first). Verified locally end-to-end via `TestClient` before deploying,
  same as the `Options/` split - both strategies' lifespans starting
  together, both routers reachable, no errors.

- **`CopperOptions/` - a third strategy package, PAPER TRADING ONLY,
  with its own independent on/off flag.** Same `router` + `lifespan`
  export pattern, same shared-`dhan_client` reasoning, same REST-polling
  design as `IndexScalping/` - see that entry above and bug #19 for the
  strategy-specific details (rules, assumptions, the expiry-rolling
  fix). Two things specific to this one:
  - **`config.STRATEGY_ENABLED`, separate from `config.PAPER_TRADING_ONLY`.**
    The latter is a hard invariant (asserted at startup, not meant to be
    toggled casually); the former is the actual requested on/off switch
    for after paper results are evaluated - when false, the poll loop
    keeps running (no restart needed to flip it) but does nothing at
    all: no data fetches, no signal checks, no side effects. Checked
    first thing in `_poll_copper()`.
  - **Options here are on futures (MCX `OPTFUT`), not a spot index** -
    every rule (open/RSI/Supertrend) reads the Copper *futures*
    contract's own price series, resolved to match the chosen option
    expiry's calendar month (see bug #19). `GET /copper/paper-trades`
    surfaces which expiry cycle is currently in use, alongside the usual
    trade history and today's daily gate state.

- **Absolute per-trade rupee-loss cap added to both `Options/` and
  `Futures/` (25 Aug 2026, user request), on top of the existing
  percentage-based stop-loss.** `config.MAX_LOSS_PER_TRADE_RS` (default
  ₹3,000, `MAX_LOSS_PER_TRADE_RS` / `FUTURES_MAX_LOSS_PER_TRADE_RS` env
  vars) is checked first in `_exit_reason_for()`, ahead of target,
  trailing/dynamic stop-loss, and the Supertrend exit - as soon as
  `(entry_price - ltp) * quantity` reaches this cap, the position exits
  immediately with reason `MAX_LOSS_HIT`, regardless of what the
  percentage stop-loss would otherwise allow.

  The reason this is a genuinely separate control from `STOP_LOSS_PCT`,
  not a duplicate of it: the percentage stop-loss is relative to entry
  premium, so a low-premium/high-lot-size contract can lose far more than
  ₹3,000 in rupee terms before its own percentage SL ever fires (e.g. a
  ₹10 entry × 5,000 qty position's 16% SL sits at a ₹8,000 loss - this cap
  now exits it at ₹3,000 instead, well before that). Applies identically
  to CE and PE without any direction-specific logic, since this strategy
  only ever *buys* options (never sells) for either leg - a loss is
  `(entry_price - ltp) * quantity` the same way regardless of option type.
  Verified fully offline (no network calls involved - pure function of
  `Position` + a given LTP) in
  `/private/tmp/.../scratchpad/test_max_loss_exit.py`: both packages, both
  option types, the cap firing before a distant percentage SL would, and
  a profitable position being unaffected - 18/18 checks passed.

  **Lowered from ₹3,000 to ₹2,000 on 26 Aug 2026, by user request**,
  alongside adding `PROFIT_PROTECTION_THRESHOLD_RS` (see that entry below)
  - both the code default and `.env`/`FUTURES_.env` values updated
  together so a bare deploy without an explicit override still lands on
  ₹2,000. Test suite updated to assert the new default and re-verified
  (still 18/18, values shifted from 2999/3050 to 1999/2050 around the new
  cap).

- **New-entry time cutoff added to both `Options/` and `Futures/` (25 Aug
  2026, user request): `ENABLE_TRADING_TIME_LIMIT` +
  `ALLOWED_TRADING_TIME`, deliberately separate from `SQUARE_OFF_TIME`.**
  `SQUARE_OFF_TIME` (default `15:15`) governs closing *existing* open
  positions at end of day; this new pair governs whether a *new* position
  is allowed to open at all. `is_past_allowed_trading_time()` (mirrors the
  existing `is_past_square_off_time()`, bug #25's pattern) is checked in
  each webhook handler (`option_main.py`'s two endpoints,
  `futures_main.py`'s one) before any ranking or order placement - a match
  returns `{"status": "ignored", "reason": "past_allowed_trading_time"}`
  without touching the Dhan API at all. When `ENABLE_TRADING_TIME_LIMIT`
  is `false` (the code default), this always returns `False` - entries are
  allowed all day exactly as before this feature existed, gated only by
  `SQUARE_OFF_TIME` as always. Positions already open when the cutoff
  passes are completely unaffected - they keep full target/stop-loss/
  dynamic-SL/Supertrend/square-off monitoring regardless of this flag,
  since it only intercepts new entries at the webhook layer.

  Env vars: `ENABLE_TRADING_TIME_LIMIT` / `ALLOWED_TRADING_TIME` (Options),
  `FUTURES_ENABLE_TRADING_TIME_LIMIT` / `FUTURES_ALLOWED_TRADING_TIME`
  (Futures) - **both currently set to `true` / `11:30` in `.env`** (the
  user's request came with a concrete value they wanted active
  immediately, not just a new off-by-default option) - flip the
  `ENABLE_*` var to `false` to go back to all-day entries without
  removing the code. Verified fully offline in
  `/private/tmp/.../scratchpad/test_trading_time_limit.py`: the on/off
  flag itself, before/at/after the cutoff, and both webhook handlers
  short-circuiting before `rank_and_pick_top_stocks`/order placement are
  ever reached - 16/16 checks passed, zero real network calls reachable.

- **NRML/overnight-carry mode added to both `Options/` and `Futures/` (25
  Aug 2026, user request, backtested first) - `OPTIONS_PRODUCT=MARGIN` +
  `ENABLE_SQUARE_OFF=false`, letting a position ride past market close into
  the next trading day instead of being force-flattened at 15:15.**
  Requested after two backtests on a new scan source ("Krishvi") showed
  this materially helping - the "no cutoff, no EOD exit" variant beat the
  "MIS + 11:30 cutoff" baseline by +₹34,531.55 (61 → 103 trades), almost
  entirely from 4 trades that genuinely carried overnight (net positive)
  and from the 11:30 cutoff no longer excluding an entire day's alerts.

  **Three separate pieces had to change together, not just a config flip:**
  1. `OPTIONS_PRODUCT`: "MIS" → **"MARGIN"**, not "NRML" - confirmed by
     reading Tradehull's own `order_placement()` source
     (`Dhan_Tradehull.py`): its `trade_type` param only recognizes
     MIS/MARGIN/MTF/CO/BO/CNC as dict keys, and "MARGIN" is exactly what
     the account's own real carry-forward Copper positions already showed
     up as (`[MARGIN] LONG`) via the Dhan MCP portfolio check earlier this
     session. Passing the literal string "NRML" would have KeyError'd
     inside Tradehull on the very first live order. Note this changes
     nothing about the actual P&L math for this strategy - it only ever
     *buys* options (long premium), so max loss is always the premium paid
     regardless of product type; the real difference is purely that
     "MARGIN" doesn't get an automatic broker-side intraday square-off.
  2. New `config.ENABLE_SQUARE_OFF` (default `true`, unchanged behavior)
     gates BOTH `is_past_square_off_time()` (now always `False` when off -
     stops blocking new entries late in the day too, matching the
     backtest's "no cutoff" semantics) AND `monitor_loop`'s automatic
     `_square_off_all` call. When off, `monitor_loop` instead keeps
     evaluating every live position's target/stop-loss/dynamic-SL/
     Supertrend/`MAX_LOSS_HIT` for as long as it takes - but gated on
     `dhan_wrapper.is_market_open()`, not unconditionally, so it doesn't
     hammer Dhan's LTP REST endpoint every `MONITOR_INTERVAL_SECONDS`
     (5s) for the ~17.75 hours the market is shut each night. That
     overnight window is exactly where the real risk lives: **zero exit
     protection while the market is closed** - a position is fully
     exposed to whatever gap happens by next session's open, with no
     automated response possible. The backtest's 4 overnight holds were
     all net-neutral-to-positive, but that's 4 data points, not a
     guarantee gap risk won't bite on some other day.
  3. **`PositionStore.maybe_reset_for_new_day()` in BOTH packages had to
     stop unconditionally clearing `live_positions`/`reserved_symbols` on
     a day-boundary tick when `ENABLE_SQUARE_OFF` is off.** This was found
     *during this same change*, not a pre-existing bug someone hit live -
     without this fix, turning off square-off would have made things
     actively WORSE than before: a position still genuinely open at the
     broker overnight would get silently deleted from the bot's own
     in-memory state the moment the calendar date rolled over, orphaning
     it from all future exit monitoring. For Options, the only recovery
     path would be `reconcile_broker_positions()` - which only runs at
     process STARTUP, not on this periodic check - so the position would
     sit unmanaged until the next restart at the earliest. For Futures
     it's worse still: that package never runs broker reconciliation at
     all (by design, see the `Futures/` design-decision entry above), so
     an in-memory clear here would have had **no recovery path, ever**.
     Fixed by only clearing `closed_positions_today`/`orders_today` (safe,
     purely historical logs) when `ENABLE_SQUARE_OFF` is off, while
     preserving `live_positions`/`reserved_symbols` across the boundary.

  Verified fully offline (mocked `dhan_wrapper`/`_check_one_position`/
  `_square_off_all`, zero real network calls reachable) in
  `/private/tmp/.../scratchpad/test_nrml_carryforward.py` - the env-var
  product-type override, `is_past_square_off_time()`'s on/off behavior,
  `monitor_loop`'s four branches (square-off on/before, on/after, off/
  market-open, off/market-closed), and `maybe_reset_for_new_day` actually
  preserving vs. clearing live positions in both packages - 21/21 checks
  passed. Side effect worth knowing: `Options/paper_webhook.py`'s entry
  gate also reuses the shared `is_past_square_off_time()`, so paper-trade
  alerts also stop being blocked past 15:15 once this is on - harmless
  (paper trading, no real money), not something this change tried to
  avoid.

- **Friday-specific square-off carve-out added to both `Options/` and
  `Futures/` (26 Aug 2026, user request), on top of the NRML/overnight-
  carry mode above - `ENABLE_FRIDAY_SQUARE_OFF` (default `true`) +
  `FRIDAY_SQUARE_OFF_TIME` (default `15:20`).** Motivated directly by the
  EICHERMOT example found in that same day's backtesting: a position
  carried Thursday evening through the following Monday morning (skipping
  a data-sparse Fri/weekend) with zero exit checks the whole way, landing
  a materially worse `MAX_LOSS_HIT` than it would have with same-day
  protection. A weekend gap is categorically worse than a single overnight
  one - two-plus days of zero monitoring instead of one night - so Friday
  gets its own mandatory cutoff regardless of `ENABLE_SQUARE_OFF`'s
  Mon-Thu setting.

  Implementation: both packages' `is_past_square_off_time()` and
  `monitor_loop()` were refactored around a new shared
  `_todays_square_off_time()` helper that returns the effective cutoff for
  *today specifically* - `config.SQUARE_OFF_TIME` if `ENABLE_SQUARE_OFF`
  is on (unconditional, every day, unchanged from before), else
  `config.FRIDAY_SQUARE_OFF_TIME` if today is Friday
  (`datetime.weekday() == 4`) and `ENABLE_FRIDAY_SQUARE_OFF` is on, else
  `None` (no square-off at all - the Mon-Thu NRML-carry default).
  `is_past_square_off_time()` and `monitor_loop`'s force-close branch both
  now key off this one helper instead of duplicating the day-of-week
  check, so the two can't drift out of sync with each other. The force-
  close reason is logged as `EOD_SQUARE_OFF_FRIDAY` (vs. the existing
  `EOD_SQUARE_OFF_3_15PM`) so a Friday-specific close is distinguishable
  in `/positions`/`/futures/positions` from an every-day one.
  `ENABLE_SQUARE_OFF=true` still takes priority when set (it already
  covers every day including Friday, so the carve-out is a no-op then).

  Verified fully offline (mocked `_now_ist`/`_check_one_position`/
  `_square_off_all`, zero real network calls reachable) in
  `/private/tmp/.../scratchpad/test_friday_square_off.py`, using
  2026-08-21 (a real Friday) and 2026-08-24 (a real Monday) as reference
  dates: the priority ordering (`ENABLE_SQUARE_OFF` > Friday carve-out >
  neither), `is_past_square_off_time()` at before/at/after the Friday
  cutoff and on an unaffected Monday, `monitor_loop` actually force-
  closing on Friday past cutoff vs. still running normal monitoring
  before it, and Monday being completely unaffected in both packages -
  20/20 checks passed. Also re-ran the earlier NRML-carryforward suite to
  confirm this refactor didn't regress it - unchanged behaviorally, same
  4 failures as before (those check code defaults against the currently-
  loaded `.env`, not a regression).

- **Absolute per-trade rupee profit-protection added to both `Options/`
  and `Futures/` (26 Aug 2026, user request) - `PROFIT_PROTECTION_
  THRESHOLD_RS` (default ₹2,000), the mirror image of
  `MAX_LOSS_PER_TRADE_RS` but on the upside.** Once a trade's PEAK
  unrealized profit (`(highest_price - entry_price) * quantity` -
  `highest_price` is already maintained for the trailing-SL mechanism,
  reused here rather than adding a new field) exceeds this threshold,
  "protection" is armed: the very next tick where price is off that peak
  *at all* (`ltp < highest_price`) exits immediately with reason
  `PROFIT_PROTECTION_HIT`. Deliberately the simple version requested - no
  drawdown tolerance once armed, not a percentage-based trailing floor.
  Checked in `_exit_reason_for()` right after `TARGET_HIT` (reaching the
  full target is a strictly better outcome and still takes priority) but
  before the existing percentage-based trailing/hard stop-loss. Uses `>`
  (strictly more than ₹2,000), not `>=`, matching the user's own wording.
  Applies identically to CE and PE for the same reason
  `MAX_LOSS_PER_TRADE_RS` does - both are long-premium positions, so
  profit is `(ltp - entry_price) * quantity` either way.

  Because `update_highest_price()` always runs before `_exit_reason_for()`
  is called (both in the poll loop and the event-driven tick path), the
  tick that itself sets a new peak can never trigger this - `ltp ==
  highest_price` at that moment, not `<` - only a subsequent tick below an
  already-recorded peak does. In practice this means once a trade clears
  ₹2,000 of profit, it will almost always exit within a tick or two of its
  actual high-water mark, since prices rarely rise monotonically forever -
  a deliberately aggressive lock, not a lenient one.

  Verified fully offline (pure function of a `Position` + an LTP, zero
  network calls involved) in
  `/private/tmp/.../scratchpad/test_profit_protection.py`: both packages,
  both option types, no-exit-below-threshold, the exact-₹2,000 boundary
  correctly NOT arming (strictly "more than"), no exit on the peak-setting
  tick itself, firing on the smallest possible decline once armed,
  `TARGET_HIT` still winning when price is at/above target, and
  `MAX_LOSS_HIT` still winning in a crash-from-peak scenario - 30/30
  checks passed. Also re-ran the `MAX_LOSS_PER_TRADE_RS` suite to confirm
  no regression - unchanged, all still passing.

## Capital requirements

Since this strategy only ever *buys* options (never sells/writes), capital
needed is just premium × quantity per leg — no additional margin, since
max loss is capped at premium paid. Derived from 99 real entries across 7
trading days (13–21 Aug 2026):

| | Cost per leg |
|---|---|
| Median | ₹11,257 |
| 75th percentile | ₹14,980 |
| 90th percentile | ₹18,000 |
| Max seen | ₹31,894 (ADANIENSOL) |

With the original single shared `MAX_LIVE_POSITIONS=3`, that scaled to
**~₹34k** for a typical day, **~₹45k** for a somewhat pricier day, **~₹54k**
for a 90th-percentile day, and a worst-case tail of **~₹75k–95k** if all 3
concurrent slots happened to land on expensive-premium stocks at once.

**Since splitting into separate `MAX_LIVE_POSITIONS_CE=2` /
`MAX_LIVE_POSITIONS_PE=2` caps, the true worst case is now 4 concurrent
positions (2 CE + 2 PE), not 3** — both webhooks running actively at once
can each fill their own 2 slots independently. Scaling the same per-leg
percentiles to 4 positions: **~₹45k** typical, **~₹60k** pricier day,
**~₹72k** 90th-percentile day, and a worst-case tail of **~₹100k–125k**.
Recommended working minimum if running both webhooks: **₹70,000–80,000**.
This is drawn from one week of data (CE) plus a second week (PE), not a
full month — typical premium levels can shift with market conditions.

## Backtesting methodology

Built out over three rounds this session (see bugs #9/#10 above) as a
standalone, read-only script — never modifies the live bot, only reads
Dhan's historical REST endpoints:

1. Parse the Chartink scanner's exported CSV (`Date,Symbol,...` columns,
   one row per stock per scan interval) into `{timestamp: [symbols]}`
   triggers, grouped by trading day.
2. For each underlying that appears: `dhanhq.intraday_minute_data()` for
   1-min candles (entry pricing / ranking) and 5-min candles (Supertrend),
   plus one `historical_daily_data()` call per symbol (not per day) to
   derive previous-close for %-change ranking — cache and reuse across
   days to cut API calls.
3. Replay `rank_and_pick_top_stocks` / dedup / `MAX_LIVE_POSITIONS` capacity
   logic exactly as production does, at 1-min simulation resolution.
4. Resolve the ATM strike at entry time from the scrip master (closest
   strike to spot, nearest expiry) and fetch that specific contract's own
   1-min candles for real entry/exit pricing — not the underlying's price.
5. Replay the exit rules (target/SL/Supertrend) minute-by-minute, mirroring
   the production code's own candle-boundary/lookahead logic exactly (e.g.
   dropping a still-forming candle) so the backtest can't silently diverge
   from what the bot actually does live.

Key lesson: **a single day is not enough sample size to trust a backtest
result** — the Supertrend fix that looked like a clean win on one day
(bug #9) was net-negative over a full week (bug #10) for reasons the
single day couldn't have surfaced. Always clarify which grouping
transformation ("last N days") reflects the intended sample before
concluding anything from an exit-logic change.

- **Stepped/"ratchet" dynamic stop-loss** (`ENABLE_DYNAMIC_SL`,
  `Position.current_trailing_sl`) - independent of and stackable with the
  continuous `ENABLE_TRAILING_SL` mechanism above (the effective floor is
  whichever is more protective). Every step % the option's own premium
  climbs from entry - measured off `highest_price` (the peak ever seen),
  not the live price, so a pullback after a step doesn't undo protection
  already earned - the stop-loss floor moves up `DYNAMIC_SL_INCREASE_PCT`
  (default 1%) of entry price. Step width is configured separately per
  option type - `DYNAMIC_SL_STEP_PCT_CE` (7%) / `DYNAMIC_SL_STEP_PCT_PE`
  (9%) - since backtesting found the same width doesn't necessarily suit
  both legs equally (see bugs #13/#14). `TARGET_PCT` is untouched; this
  only tightens how much room a trade has to give back before target. The
  mechanism itself is symmetric for CE and PE with no direction-awareness
  needed, unlike the Supertrend exit - both are always a BUY of the option
  itself, so "premium rising" means profit either way. Not capped at
  breakeven: enough accumulated steps can push the floor above entry
  price, locking in a guaranteed profit - intended behavior, not a bug,
  though in practice `TARGET_PCT` usually fires first at the configured
  defaults (e.g. steps only reach breakeven-and-beyond past
  `STOP_LOSS_PCT / DYNAMIC_SL_INCREASE_PCT` steps - 20 steps = 80% up, at
  the defaults - while target is typically 10-25%). Verified offline
  before deploying: entry=100, STOP_LOSS_PCT=20%, step=4%/1% - floor
  stays at the fixed 80 below the first step, then climbs step-wise
  (81 at +4%, 82 at +8-9%, 84 at +16%, 85 at +20%) exactly matching the
  hand-computed expected values. Backtest verdict on the initial 4%/1%
  spec: see bug #12 above and `BACKTEST_RESULTS.md` - net-negative in
  isolation, small net-negative on top of Supertrend, on the one week of
  CE data tested so far.

## Known external constraints (not fixable in code)

- Dhan requires whatever IP the bot runs from to be separately whitelisted
  (in Dhan's own dashboard) before real orders will go through — unrelated
  to anything in this codebase. A successful REST call and a valid
  `order_id` back is not sufficient evidence an order will actually reach
  the exchange from a given environment.
- Dhan's market-data REST API has an undocumented rate limit (see bug #5
  above) — no published threshold, discovered empirically.
- This bot's state (`position_store.py`) is in-memory only. A process
  restart loses order-tracking history and, notably, a live position's
  accumulated `highest_price` (trailing-stop memory) even though
  `reconcile_broker_positions()` recovers the position itself from the
  broker.

## Open questions (unresolved)

- **A position can go flat at the broker without the bot ever marking it
  closed.** Observed live (2026-08-21): a VMM position sat in `/positions`
  as `OPEN` with a stale `next_exit_retry_at` timestamp for ~2h45m, while
  Dhan's own portfolio showed that exact contract already flat (bought and
  fully sold, realized P&L booked). The exit clearly succeeded at the
  broker at some point, but whatever placed it didn't go through the code
  path that calls `close_position()`. A service restart's
  `reconcile_broker_positions()` silently absorbed the discrepancy (no
  financial exposure — broker was flat, ₹0 unrealized), but the root cause
  of *how* it closed without the bot's own bookkeeping updating is still
  unknown. Worth instrumenting further next time it's observed live rather
  than only reasoning about it after the fact from logs.

- ~~Nothing keeps the droplet's `DHAN_ACCESS_TOKEN` in sync with a
  locally-refreshed one~~ **- resolved, see bug #21.** Switched to
  `DHAN_AUTH_MODE=pin_totp`, which regenerates its own fresh token from
  scratch on every authentication - there's no more manually-managed
  token to go stale or fall out of sync in the first place.

## Safety practice for anyone working on this repo

This bot places real trades with real money the moment it's running
against live credentials — `reconcile_broker_positions()` +
`monitor_loop()` act automatically on whatever the broker reports as open,
with no separate "dry run" mode. Before starting/restarting a live
instance:
1. Check `GET /positions` first — if `live_positions` is non-empty,
   understand what the bot is about to do to it before proceeding.
2. Never assume a "read-only" test is actually read-only without checking
   whether the code path can reach an order-placement call.
