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

29. **Replaced `IndexScalping/`'s original opening-range-breakout +
    EMA-momentum signal entirely with a new rule set (user request, 26
    Aug 2026) - still paper-trading only.** New rules:

    CE entry: today's daily open > yesterday's daily close AND today's
    daily RSI(14) > yesterday's daily RSI [both on the NIFTY/BANKNIFTY
    index spot] AND the index's 5-min close is above its own 5-min
    Supertrend(10,3) AND the index's 1-min close just crossed ABOVE its
    own 1-min Supertrend(10,3). PE is the exact mirror. Exit (either
    side): the index's 1-min close crosses back the other way through its
    own 1-min Supertrend, or the paper position's unrealized loss exceeds
    ₹1,000 - whichever first. Still force-closed at `SQUARE_OFF_TIME`
    regardless, same as before.

    **Assumptions made explicit** (same practice as bug #19's Copper
    entry, kept in sync with `IndexScalping/config.py`'s own docstring):
    - "Today's"/"yesterday's" open, close, RSI are DAILY values on the
      index SPOT itself (NIFTY=13, BANKNIFTY=25, segment `IDX_I` - the
      same segment the original signal already used successfully for
      1-min candles). RSI includes today's still-forming daily close
      (today's price-so-far), same interpretation as Copper's identical
      rule. The daily gate is computed once per day and frozen from the
      first successful poll - since this strategy starts right at
      `MARKET_OPEN`, that first poll's price-so-far is very close to the
      actual day's open.
    - The 5-min condition ("close is greater/lesser than") is read as a
      plain current-state check, matching Copper's precedent. The 1-min
      condition is explicitly worded "crossed above/below" - different
      wording from the 5-min rule - so **unlike Copper's blanket
      state-check interpretation, this one gets genuine edge-detection**:
      `_crossed()` only returns true on the bar where the close was on
      the wrong side of the 1-min Supertrend the PRIOR confirmed bar and
      is on the right side THIS confirmed bar. The daily gate and the
      5-min check are the slower "regime" filters; the 1-min crossover is
      the precise entry/exit timing trigger. This is a deliberate
      departure from bug #19's "crossed = plain state check, algebraically
      equivalent" reasoning - that equivalence holds when a poll loop
      exits/enters the instant a state becomes true, but here the 1-min
      condition is explicitly worded differently from the 5-min one in
      the same rule set, which reads as intentional (fast precise trigger
      vs. slower regime filter), not incidental phrasing.
    - Both Supertrends and the crossover check only look at COMPLETED
      candles - the current still-forming bar (poll landed before its own
      close time) is dropped first, same reasoning as
      `Options/dhan_client.py`'s `refresh_supertrend_signal` - otherwise a
      crossover could flicker true/false multiple times within one
      still-forming minute as new ticks arrive.

    Cost modeling (`ROUND_TRIP_COST_RS`, `SLIPPAGE_PCT`) carried over
    unchanged from the original strategy - gross vs. net P&L is still
    tracked separately. The old `MAX_TRADES_PER_DAY` cap, opening-range,
    and EMA tunables were removed outright (not part of the new rules,
    and the new signal already self-limits via one-position-at-a-time +
    the daily gate).

    Verified fully offline (mocked every I/O boundary -
    `_fetch_index_daily`, `_fetch_index_intraday`, `_lot_size_for`,
    `dhan_wrapper.get_option_ltp`, and `dhan_wrapper.client.ATM_Strike_Selection`
    via a fake pre-injected `_client` so the lazily-authenticating
    `.client` property is never actually touched - zero real network
    calls reachable) in `/private/tmp/.../scratchpad/test_index_scalping_v2.py`:
    RSI correctness, `_crossed()`'s edge-detection (fires on a genuine
    cross, does NOT fire when already on the right side with no edge),
    still-forming-candle exclusion, a full CE entry end-to-end, a full
    PE entry end-to-end (mirror), exit via 1-min crossed-below, and exit
    via the ₹1,000 max-loss cap - 22/22 checks passed.

30. **A restart-surviving stale broker order made a SELL-to-close get
    RMS-rejected for "insufficient funds," even though the position was
    just being closed, not shorted (BHARATFORG, 26 Aug 2026).** A SELL
    order (`3252608268636`, qty 500) was still `PENDING` at the broker
    when `dhanboy.service` restarted for an unrelated deploy. The restart
    wiped `pending_exit_order_id` (in-memory only), so on the new process
    `reconcile_broker_positions()` correctly re-recovered the position but
    `_exit_position()` had no idea an exit order was already outstanding
    - it placed a **second** fresh SELL for the same 500 qty, which Dhan
    rejected with `DH-906 "insufficient funds, add ₹151,637.20"` (~6x the
    position's own ₹25,100 premium value - naked-short-scale margin, not
    premium-scale). Confirmed via Dhan's own portfolio API that no
    oversell/double-sell actually occurred (the second order never
    filled) - this was a spurious rejection, not a real capital shortfall.

    **Root cause, confirmed against Dhan's own published support docs**
    (see Sources below): Dhan's RMS locks margin against a pending order,
    and doesn't necessarily net a second order against the same holding
    up front - it can price the second SELL as if it might create a fresh
    naked short and demand full margin for it. Dhan's own guidance for
    exactly this situation: *"If you're trying to exit a position but
    already have an open order (maybe it's pending)... either cancel that
    order or... exit at market."* This is a different failure mode from
    bug #3 (product-type mismatch) - the product type matched correctly
    here (`MARGIN` on both the original and the retry); the issue was
    purely "two live orders against one holding, order book not netted
    upfront."

    **Fix**: `DhanWrapper.get_pending_order_id(trading_symbol,
    transaction_type)` (new, `dhan_client.py`) checks Dhan's live order
    list for a non-terminal (`TRANSIT`/`PENDING`/`PART_TRADED`) order on
    the EXACT contract + transaction type. `_exit_position()` (both
    `Options/` and `Futures/`) now calls this **before every placement
    attempt** (not gated by `exit_failure_count`, since this exact
    incident happened on the very first post-restart attempt) - if found,
    cancels it via the new `DhanWrapper.cancel_order()` first, then
    proceeds with a fresh SELL as normal. Both the lookup and the cancel
    are wrapped so a failure in either one (e.g. the stale order resolved
    on its own between the check and the cancel) falls through to placing
    a fresh order anyway rather than blocking the exit entirely.

    Verified fully offline (every `dhan_wrapper` method mocked directly,
    and the new order-list unit tests inject a fake `_client` rather than
    patching the `client` property itself - patching a property with no
    setter both fails AND, confirmed interactively before writing the
    test, its own internal capture-the-original-value step triggers a
    REAL Dhan login as a side effect) in
    `/private/tmp/.../scratchpad/test_exit_reconciliation.py` (extended,
    not a new file): `get_pending_order_id`'s exact-match filtering
    (symbol/transaction_type/non-terminal-status), the stale order being
    cancelled BEFORE the fresh SELL is placed (call-ordering asserted
    explicitly), a normal exit being completely unaffected when no stale
    order exists, and graceful fallback-to-placement when either the
    lookup or the cancel itself errors - 42/42 checks passed, no
    regression in the existing broker-quantity-reconciliation checks it
    shares the file with.

    Sources: [Dhan Support - order rejected despite sufficient
    funds](https://dhan.co/support/orders-and-positions/order-rejections/i-have-sufficient-funds-in-my-account-yet-my-order-was-rejected-with-the-reason-fund-limit-insufficient-why-did-this-happen/)
    ("check if you have any pending order - your margin is blocked for
    your pending order"); [Dhan Support - basket order rejected despite
    available
    funds](https://dhan.co/support/orders-and-positions/order-rejections/why-was-my-basket-order-rejected-even-though-funds-available-was-higher-than-overall-margin/)
    (pending-order margin locks not yet reflected in displayed
    totals); general guidance to cancel a conflicting open/pending order
    before placing a new exit, from [Dhan's order-rejection FAQ
    hub](https://dhan.co/support/orders-and-positions/order-rejections/).

31. **`PROFIT_PROTECTION_THRESHOLD_RS` raised 1200->1500 (both Options
    and Futures, user request 26 Aug 2026), backed by a backtest instead
    of a gut call this time.** Ran the CE ATM buying strategy against
    `01 Krishvi-1 day.csv` (4 trading days: 21/24/25/26 Aug 2026, live
    config otherwise unchanged - SELECT_BOTTOM_N_STOCKS=true,
    MAX_LOSS_PER_TRADE_RS=1000, 1-min/1-min-grace Supertrend) at three
    threshold values via a `PROFIT_PROTECTION_THRESHOLD_RS` env override
    passed to the backtest subprocess only (never touched the live
    `.env`/`config.py` for the comparison runs themselves):

    | Threshold | Closed trades | Win rate | Realized P&L | Combined P&L |
    |---|---|---|---|---|
    | 1200 (previous) | 73 | 56.2% | +37,973.30 | +37,473.30 |
    | 2000 | 70 | 55.7% | +48,024.55 | +47,524.55 |

    (1500, the value actually deployed, wasn't separately re-run as its
    own backtest - see below for why that's still reasonable.)

    2000 beat 1200 by +26.5% on this sample - fewer trades got
    profit-locked early (24 vs 30 `PROFIT_PROTECTION_HIT` exits), letting
    2 of them run all the way to `TARGET_HIT` instead (one +5,000 on
    KOTAKBANK), at the cost of slightly larger `MAX_LOSS_HIT` damage
    (-8,338.75 vs -7,070.00, 7 vs 6 trades - the normal tradeoff of giving
    a position more room before protection arms). User chose 1500 as a
    middle ground between the tested 1200 and 2000 values rather than
    deploying the backtested-best 2000 directly - not itself re-verified
    against this exact dataset before deploy, but every underlying
    mechanism (peak-profit calc, strict-greater-than arming, priority vs.
    MAX_LOSS_HIT/TARGET_HIT) is identical and already covered by
    `test_profit_protection.py`'s threshold-agnostic checks.

    No code changes needed - `_exit_reason_for()` already reads this
    config value directly. Verified offline against the new 1500 value in
    the existing `test_profit_protection.py` suite (parameterized off the
    live config value, not hardcoded - only the one literal "defaults to
    N" assertion needed updating) - all checks passed, no regression in
    `test_max_loss_exit.py`.

32. **`SUPERTREND_MIN_WARMUP_CANDLES` lowered 20->0 (user request 26 Aug
    2026) - effectively OFF, deployed despite genuinely mixed backtest
    evidence.** This is bug #10/#16's own fix, reversed. Backtested both
    values against two different CE ATM datasets before deploy:

    | Dataset | Trades (20 / 0) | Realized P&L (20 / 0) |
    |---|---|---|
    | `01 Krishvi-1 day.csv` (4 days) | 39 / 39 | +29,691.30 / +29,259.45 |
    | `01 Kaashvi-1week.csv` (3 days) | 108 / 76 | +46,879.75 / +58,053.40 |

    The two datasets **disagree**: the smaller Krishvi sample says the
    warmup gate is doing its job (0 is very slightly worse, -1.5%); the
    larger Kaashvi sample says removing it is a clear win (+23.8%) -
    mechanically, because Supertrend firing earlier (right after the bare
    `SUPERTREND_PERIOD+1`=11 candles instead of waiting for 20) let it cut
    several positions out before they rode all the way down to
    `MAX_LOSS_HIT`, on the day-wise breakdown concentrated almost entirely
    in one especially choppy session (25 Aug). User chose to deploy 0
    anyway, aware of the disagreement - this is exactly the failure mode
    bug #10 originally described (a naively-seeded early Supertrend
    reading with no real trend/band history yet), so if live Supertrend
    exits start clustering suspiciously early and often across many
    freshly-entered CE positions again, this is the first place to look.

    No code changes needed beyond the config default - `_compute_supertrend`,
    `refresh_supertrend_signal()`, and `_supertrend_signal_for()` were
    already fully driven by this value; the underlying
    `SUPERTREND_PERIOD+1` (11-candle) ATR-seed minimum is untouched and
    still enforced regardless of this setting. Verified fully offline
    (mocked `dhan_wrapper._client` directly - the lazily-authenticating
    `.client` property is never touched, confirmed the safe way after an
    earlier session incident where merely referencing it via `patch.object`
    triggered a real Dhan login) in
    `/private/tmp/.../scratchpad/test_supertrend_warmup0.py`: the new
    default is 0, 12 candles (just above the ATR-seed minimum) now cache a
    signal where the old warmup=20 gate would have refused to, the bare
    11-candle ATR-seed minimum is still enforced independent of this
    setting, and a comfortably-large candle count caches either way -
    6/6 checks passed, no regression in `test_supertrend_1min.py`.

33. **`MAX_LOSS_HIT` re-entry escalation - added, then explicitly reverted
    the same day (27 Aug 2026).** Prompted by ADANIPOWER cycling through 5
    legs in one morning (3 `MAX_LOSS_HIT` exits) and LICHSGFIN separately
    doing the same (3 consecutive `MAX_LOSS_HIT` legs, -4,300 realized):
    the concern was that a genuinely choppy stock re-entering right after
    a `MAX_LOSS_HIT` exit was using the exact same flat cap and often
    getting chopped again "irrespective of how it's performing after
    re-entering" (the user's framing).

    **What was built and deployed**: `PositionStore.get_max_loss_cap_for()`
    escalated the cap 1.75x per consecutive `MAX_LOSS_HIT` exit on an
    underlying that day (1000 -> 1750 -> 3000, capped at 3x base), reset to
    base on any other exit reason, reset entirely every trading day. New
    `Position.max_loss_override_rs` field carried the computed cap onto
    each position at entry. Verified offline (32/32 checks) and deployed.

    **What real trading data showed almost immediately**: LICHSGFIN's own
    history that same morning revealed the escalation counter doesn't
    survive a process restart (it's in-memory only, same as
    `live_positions` itself) - a restart landing between two re-entries
    silently reset the count back to 0, so a leg that should have gotten
    the 2nd-tier ~3000 cap only got the base 1000 instead. With several
    deploys/restarts happening that same session, this materially
    undercut the feature's own purpose before it had converged on a real
    interaction to react to (see the `_client` login-safety /
    stale-order-cancel entries above for what those restarts were fixing).

    **Reverted via `git revert` of the feature's own commit** (clean,
    single-commit revert - nothing else had touched the same code since)
    per explicit user request the same day, in favor of a simple flat
    cap - `MAX_LOSS_PER_TRADE_RS` raised straight to Rs.1500 (both
    Options and Futures) instead, applied identically no matter how many
    times a stock has already re-entered. All of
    `Position.max_loss_override_rs`, `PositionStore.
    get_max_loss_cap_for()`/`_consecutive_max_loss_by_underlying`, and the
    `MAX_LOSS_REENTRY_MULTIPLIER`/`MAX_LOSS_REENTRY_CEILING_MULTIPLIER`
    config values (both packages, plus the matching `.env` entries) no
    longer exist in the codebase - this isn't a toggle-off, the mechanism
    itself is gone. `test_max_loss_reentry_escalation.py` (scratchpad) is
    now obsolete accordingly - it would fail to import against current
    code and should not be run as a regression check for anything else.

    Verified the revert + new flat value offline: `test_max_loss_exit.py`'s
    threshold-agnostic checks (which read `cfg.MAX_LOSS_PER_TRADE_RS` live
    rather than a hardcoded literal, precisely for this kind of repeated
    threshold churn) re-passed against the new Rs.1500 value with only its
    one literal "defaults to N" assertion needing updating, and
    `test_profit_protection.py`/`test_exit_reconciliation.py` showed no
    regression. Caught along the way: the code-level default change alone
    had NO effect until `.env`'s own explicit `MAX_LOSS_PER_TRADE_RS=1000`
    override (which takes priority over the Python fallback via
    `os.getenv`) was updated too - a live re-run of the test suite against
    the real `.env` is what surfaced this before deploy.

34. **`SUPERTREND_INTERVAL_MINUTES`/`SUPERTREND_ENTRY_GRACE_MINUTES`
    reverted 1-min/1-min -> 5-min/5-min (user request 27 Aug 2026, the day
    after they were first moved to 1-min/1-min).** Restores the original,
    actually-backtested pairing (see bug #16/BACKTEST_RESULTS.md's 14-day
    validation, run at 5-min/5-min) - no re-validation data was gathered
    at 1-min/1-min before this reverted it. No code changes needed,
    `refresh_supertrend_signal()`/`_supertrend_signal_for()` were already
    fully driven by these two config values.

    **Side effect this flips back**: `SUPERTREND_MIN_WARMUP_CANDLES` is
    still `0` (a separate, later decision - see entry #32 above, not
    touched by this revert) - at 1-min candles that meant "trusted after
    ~11 minutes"; back at 5-min candles the exact same `0` now means
    "trusted after ~55 minutes" (`SUPERTREND_PERIOD+1` = 11 candles x
    5 min). Flagged in both `Options/config.py`'s docstring and `.env`'s
    comment - not itself reverted, since the user's request here was
    specifically about the interval/grace pairing, not the warmup gate.

    Verified offline: `test_supertrend_1min.py` (kept its original
    filename despite now testing the 5-min value - the test logic itself,
    entry-candle skip/grace-boundary/favorable-direction/no-cached-signal,
    is interval-agnostic and only needed its 3 literal "defaults to N"
    assertions updated) - 23/23 checks passed against the new 5-min
    values, both packages, both option types. No regression in
    `test_supertrend_warmup0.py`.

35. **`SUPERTREND_ENTRY_GRACE_MINUTES` and `SUPERTREND_MIN_WARMUP_CANDLES`
    REMOVED entirely (user request 27 Aug 2026, same day as entry #34's
    interval revert) - not set to 0, the config values and the code paths
    that read them no longer exist at all.** User's framing: "as soon as
    supertrend signal is generated for reverse, immediate action has to be
    taken" - no tuned delay of any kind should remain.

    Before implementing, flagged one nuance explicitly to the user: there
    are really THREE separate delays bundled under "waiting" -
    (1) `SUPERTREND_MIN_WARMUP_CANDLES`, a global daily gate before any
    signal is trusted; (2) `SUPERTREND_ENTRY_GRACE_MINUTES`, extra minutes
    past the entry candle; and (3) the entry-candle skip ITSELF (never
    acting on the exact same candle a position was entered on) - which is
    not a tuning knob but a documented fix for a real live bug
    (`_supertrend_signal_for`'s own docstring: "confirmed live... without
    the entry-candle skip, this was cutting winning trades flat at
    breakeven the instant they were entered"). Asked which of the three
    to keep; **user confirmed keeping only #3** - drop the other two
    entirely, but never act on the exact entry candle.

    **What changed**: `dhan_client.refresh_supertrend_signal()` no longer
    has the `SUPERTREND_MIN_WARMUP_CANDLES` gate - a signal is cached as
    soon as the bare `SUPERTREND_PERIOD+1`=11-candle ATR-seed minimum is
    available (a real mathematical requirement to compute Supertrend at
    all, not a "waiting" policy, so this one stays). `_supertrend_signal_for()`
    (both `Options/` and `Futures/`) now compares
    `candle_start > entry_candle_start` directly with no grace offset -
    the very next candle after entry can trigger an exit immediately.
    `bt_common.py` (untracked backtest plumbing) hardcodes both to 0 to
    match, rather than reading the now-nonexistent config attributes.

    **Known, accepted tradeoff reopened by this**: the original bug #10 -
    an early, naively-seeded Supertrend reading with no prior trend/band
    history can read "bearish" on ~every underlying regardless of actual
    trend - is no longer gated against at all. Explicitly flagged in
    `Options/config.py`'s removal comment as the first thing to revisit if
    live Supertrend exits start clustering suspiciously early and often.

    Verified fully offline in a new
    `/private/tmp/.../scratchpad/test_supertrend_immediate.py` (supersedes
    `test_supertrend_1min.py`/`test_supertrend_warmup0.py`, both of which
    now correctly fail loud with `AttributeError` against the removed
    config attributes rather than silently passing something wrong -
    confirmed interactively before writing the replacement): both removed
    attributes genuinely don't exist via `hasattr()`, the entry-candle
    skip is still enforced, the very next candle triggers immediately with
    no grace window, favorable-direction signals never trigger, missing
    data never forces an exit, and `refresh_supertrend_signal()` caches a
    signal right at the bare 11-candle ATR-seed minimum with nothing
    blocking it further - 22/22 checks passed, both packages, both option
    types. No regression in `test_max_loss_exit.py`/
    `test_profit_protection.py`/`test_exit_reconciliation.py`.

36. **`MAX_LIVE_POSITIONS_CE` 2->4, `MAX_LIVE_POSITIONS_PE` 2->0 (PE fully
    off), `TOP_N_STOCKS` 3->4 - Options package only, user request 27 Aug
    2026.** Prompted directly by investigating why a DELHIVERY alert at
    06:13 IST never even reached the ranking step: the webhook log showed
    *"No CE capacity left (2 live/in-flight already) - ignoring alert"* -
    a real capacity-cap block, confirmed by checking every DELHIVERY-
    mentioning alert that day (5 of the other 8 were dedup skips on
    DELHIVERY itself, evidenced by a *different* stock from the same alert
    entering right after - proving capacity, not ranking, was the
    DELHIVERY-specific blocker only once). Also confirmed `TOP_N_STOCKS`
    itself was never the cause for DELHIVERY specifically (every alert
    mentioning it listed <=3 stocks, so bottom-N/top-N select the same
    slice), but raising it to 4 alongside the CE cap means a genuinely
    4-stock alert can now fill all 4 slots instead of only the first 3.

    PE going to 0 is a deliberate full shutoff of `/chartink/webhook-sell`
    (bearish scan) - no code changes needed, both capacity gates already
    handle a zero cap correctly: `PositionStore.reserve_symbol()`'s
    `current >= _cap_for(option_type)` is `0 >= 0` on the very first PE
    attempt, and `option_main.py`'s early webhook-level bail-out
    (`remaining_capacity()`'s `max(0, cap - current)`) also returns 0
    immediately, rejecting the alert before ranking even runs. Existing
    open PE positions at deploy time are unaffected - this only gates NEW
    entries, exit monitoring for anything already open keeps working
    exactly as before. Scoped to `Options/` only - Futures wasn't part of
    this request and its own `FUTURES_MAX_LIVE_POSITIONS_PE`/
    `FUTURES_TOP_N_STOCKS` are untouched.

    Verified fully offline (pure `PositionStore` logic, no `dhan_wrapper`
    involved at all) in
    `/private/tmp/.../scratchpad/test_capacity_ce4_pe0.py`: exactly 4
    concurrent CE reservations succeed and a 5th is rejected, a PE
    reservation is rejected on the very first attempt with zero prior PE
    activity, the two caps stay fully independent of each other, and a
    pre-existing open PE position is left untouched by the cap change
    (still tracked normally, just blocking any *new* PE symbol) - 15/15
    checks passed. Caught the same `.env`-override gotcha as the
    `MAX_LOSS_PER_TRADE_RS` incident earlier - the code-level defaults
    alone had no effect until `.env`'s own explicit
    `TOP_N_STOCKS=3`/`MAX_LIVE_POSITIONS_CE=2`/`MAX_LIVE_POSITIONS_PE=2`
    were updated too.

37. **`MAX_LOSS_PER_TRADE_RS` lowered 1500->1200 (Options + Futures, user
    request 27 Aug 2026), backtest-driven.** Following the CE=4/TOP_N=4
    capacity change (#36 above), re-ran the `02 Kaashvi.csv` CE ATM
    backtest against the new live capacity config: 209 closed trades,
    56.5% win rate, +104,135.20 realized (vs. the CE=2/TOP_N=3 baseline's
    142 trades/61.3%/+88,766.40 - more capacity let more alerts through,
    net more profit despite a lower win rate). MAX_LOSS_HIT itself grew
    from 30 to 49 trades (-61,746.60 -> -100,392.10), proportional to the
    47% more total trade volume, not a change in the odds of any single
    trade going bad (21.1%->23.4% of trades hitting it).

    User then asked what tightening the cap to Rs.1000 would do:
    re-ran the same backtest with `MAX_LOSS_PER_TRADE_RS=1000` as a
    process-env override only (nothing deployed) - 230 trades, 52.2% win,
    +107,431.20. MAX_LOSS_HIT count jumped 49->72 (some trades that used
    to ride out to SUPERTREND_EXIT/PROFIT_PROTECTION at 1500 now clip out
    at the lower cap instead), but per-trade loss is 33% smaller, so total
    MAX_LOSS_HIT P&L barely moved (-100,392.10 -> -103,109.95). Net effect
    was only marginally better (+3,296.00 over 4 days) at the cost of a
    meaningfully lower win rate and ~50% more stop-outs - not a strong
    enough signal either way from a 4-day sample.

    User picked Rs.1200 as the middle ground and asked to deploy it
    directly (no separate 1200 backtest requested). Mirrored to
    `FUTURES_MAX_LOSS_PER_TRADE_RS` as well, consistent with every prior
    `MAX_LOSS_PER_TRADE_RS` change in this file (#31 and the escalation
    revert both mirrored the same way) - the backtest itself was
    Options-only, so the Futures value change is an extrapolation of that
    established pairing, not separately backtested.

    Verified offline against the real `.env` (not just the code default)
    before deploying: `test_max_loss_exit.py`'s two hardcoded "defaults to
    1500" assertions updated to 1200, all boundary-value checks are
    already parameterized off the live `config.MAX_LOSS_PER_TRADE_RS`
    rather than hardcoded, so they needed no changes - reran alongside
    `test_capacity_ce4_pe0.py` and `test_supertrend_immediate.py` to
    confirm no regression on the other two recent changes, all passed.
    Confirmed both `/positions` and `/futures/positions` had empty
    `live_positions` immediately before restarting.

38. **Responsiveness tuning + a real stale-LTP overshoot fix - triggered by
    reviewing 28 Aug 2026's real trades (user request 27 Aug 2026).** While
    reporting yesterday's trades, found SAGILITY's 10:09 IST MAX_LOSS_HIT
    exit realized -Rs.1,800 against the (already-live) Rs.1,200 cap - a
    Rs.600 overshoot. Root cause: `_get_ltp()` prefers the WebSocket feed's
    cached LTP (`get_cached_option_ltp`) with **no staleness check at
    all** - once any tick arrives it's trusted forever, and the REST
    fallback only ever runs if literally no tick has arrived yet. SAGILITY
    is a thin, low-premium (~Rs.2) option with a 12,000-share lot - real
    trades printing on that specific contract are sparse, so its cached
    price can go stale for minutes while `on_price_tick`'s event-driven
    exit path (which fires instantly on a genuinely NEW tick) simply has
    nothing new to react to. `MONITOR_INTERVAL_SECONDS` polling more often
    doesn't help this specific failure mode either - the poll loop just
    re-reads the same stale cached number.

    Three changes, all Options + Futures where applicable:
    - `MONITOR_INTERVAL_SECONDS` (`FUTURES_MONITOR_INTERVAL_SECONDS`)
      5->2 - tightens the fallback-heartbeat ceiling. Confirmed via code
      read that this alone does NOT fix stale-LTP overshoot (see above) -
      it's a real but narrow improvement (missed/delayed `on_price_tick`
      edge case only). No rate-limit cost: `refresh_supertrend_signal` has
      its own independent `SUPERTREND_REFRESH_SECONDS` floor, unaffected by
      poll cadence.
    - `SUPERTREND_REFRESH_SECONDS` 60->15 - the REST call keeping the
      cached Supertrend signal fresh was allowed to lag up to 60s behind a
      newly-closed 5-min candle, in tension with the same day's "immediate
      action, no waiting" Supertrend-exit request (#35 above). 15s caps
      that worst-case detection lag; still cheap (REST call is per
      underlying, independently rate-limited by this same value - 4
      concurrent CE positions worst case is ~0.27 calls/sec, far under
      Dhan's undocumented rate limit, bug #5).
    - **NEW `LTP_STALE_AFTER_SECONDS=5`** - the actual fix for the
      overshoot mechanism. `get_cached_option_ltp()`
      (`Options/dhan_client.py`) now tracks a timestamp per cached tick
      (`_ltp_cache_ts`, updated in `_on_market_tick`) and treats a cache
      entry older than this as a miss, forcing `_get_ltp`'s REST fallback
      to run. A new `note_rest_ltp()` method re-primes the cache with the
      REST result + a fresh timestamp - without this, a persistently-quiet
      option would get a brand-new REST call on *every single poll*
      (every 2s) for as long as it stays silent, risking Dhan's
      undocumented rate limit (bug #5) once more than one position goes
      stale at once; re-priming naturally throttles that to roughly once
      per `LTP_STALE_AFTER_SECONDS`. `LTP_STALE_AFTER_SECONDS=0` disables
      the check entirely (old indefinite-trust behavior). Lives only in
      `Options/config.py` (not mirrored to Futures) since it governs the
      one shared `dhan_client` LTP cache both packages read from, not a
      per-strategy setting. New `stats["ltp_cache_stale"]` counter on
      `/feed-stats` for observability.

    Verified fully offline in
    `/private/tmp/.../scratchpad/test_responsiveness_tuning.py` (17/17
    checks): config defaults for all three values; `get_cached_option_ltp`
    trusts a tick just under the threshold and treats one just over it as
    stale (None); `note_rest_ltp` re-primes correctly and fails soft on an
    unknown symbol; `LTP_STALE_AFTER_SECONDS=0` disables the check;
    `_get_ltp` calls the REST fallback exactly once and re-primes the
    cache with its result. Careful to never reference
    `dhan_wrapper.client` anywhere in the test (including inside
    `_instrument_meta`, which the staleness/re-prime methods call) - that
    property lazily triggers a REAL Dhan login as a side effect (a
    recurring gotcha this session) - `_instrument_meta` itself was patched
    directly instead, since it's a plain method, not a property. Reran
    `test_max_loss_exit.py`, `test_capacity_ce4_pe0.py`, and
    `test_supertrend_immediate.py` too - no regressions.

39. **New `FnoScreener/` package - a daily F&O stock screener, PAPER
    TRADING ONLY, MVP shipped 30 Aug 2026 for a live paper-trading test
    starting the next trading session.** Full design lives in the separate
    `trading-skills` repo (github.com/Kushal-Js/trading-skills,
    `designs/k01.md`) - this is the implementation, not a
    fresh design. Built after also researching and documenting Minervini's
    Trend Template and VCP pattern (`trading-skills/learnings/
    technical-patterns/`), per explicit user request to have those
    "skills" in place before building the screener.

    Pipeline (5 stages designed, 3 shipped in this MVP): Stage 0 (Minervini
    Trend Template - price above 50/150/200-day MAs stacked bullishly,
    200-MA rising >=1 month, within 30%/25% of 52-week low/high - hard
    gate, daily timeframe) + Stage 1 (liquidity floor - ATR(14)/price
    >=1.0%, 20-day avg turnover >=Rs.50cr, anti-SAGILITY band rejecting
    ATM premium<Rs.5 AND lot_size>=5000 together - hard gate) run ONCE
    per day at 10:15 IST across the full ~208-stock F&O universe
    (`dhan_wrapper.instruments()`, filtered `SEM_INSTRUMENT_NAME==
    "OPTSTK"`), producing a watchlist capped to the top 20 by ATR%. Stage 3
    (intraday momentum - 5-min RSI(14) in a 40-75/25-60 band, 5-min close
    vs. its own Supertrend(10,3) - deliberately the bot's OWN exit-side
    parameters, not Krishvi's period-7, closing the mismatch found
    analyzing that screener - 1-min Supertrend(10,3) edge-detected
    crossover, and 5-min ROC(9) sign, all four must agree) is re-evaluated
    every 15s poll per watchlist symbol to decide paper entries.

    Deliberately NOT shipped in this MVP: Stage 2 (OI-buildup gating, needs
    Dhan's Option Chain API - the bot has never called that endpoint
    before, so it's not being made load-bearing on the very first live
    test until built and verified separately) and Stage 0's VCP-detection
    bonus scoring (real, non-trivial pattern-recognition code - swing-high/
    low finding, sequential-contraction measurement - that deserves its
    own build+test pass, not something to rush). Both are documented
    phase-2 items in the trading-skills design doc.

    Paper-only exits (target/stop-loss/MAX_LOSS_PER_TRADE_RS=1200 -
    matching the live Options value for consistency - EOD square-off at
    15:15) are a self-contained reimplementation in `FnoScreener/
    paper_engine.py`, not a reuse of `Options.trading_engine._exit_
    reason_for` - same "per-package independence" convention IndexScalping/
    CopperOptions already established (that function's own dynamic-SL/
    trailing-SL tuning history belongs to the Options strategy specifically,
    not this new experimental one).

    `GET /fno-screener/status` exposes today's watchlist (with each
    stock's Trend Template detail + most recent Stage 3 signal read),
    open paper positions, completed trades, and total P&L - mirrors
    IndexScalping's own `/scalping/paper-trades` shape. Verified fully
    offline (27/27 checks, `test_fno_screener.py`) before deploying:
    Trend Template correctly passes a rising series and rejects flat/
    declining/insufficient-history ones; liquidity floor correctly passes
    healthy ATR%/turnover and rejects thin volume or zero daily range;
    momentum_signal correctly returns CE/PE only when all four Stage-3
    conditions align (verified with hand-constructed, hand-checked
    synthetic price series - RSI computed from a monotonic straight-line
    series pins at 100/0 and is NOT a valid test input, a real mistake
    made and caught while writing this test) and None on a flat/
    insufficient series; `_exit_reason_for` fires each reason at the right
    boundary. No real network call is reachable from the test file.

    **Real bug caught on the very first deploy, fixed same day**:
    `TREND_TEMPLATE_LOOKBACK_DAYS=300` (calendar days) only yielded ~204
    trading days once weekends/holidays were excluded - 17 short of the
    221 (200-day MA + 21-day rising-check) Stage 0 needs, so all 210/210
    stocks failed with "insufficient daily history" rather than a genuine
    trend-quality read. Bumped to 420 calendar days (~285 trading days),
    confirmed live against RELIANCE/TCS/HDFCBANK. Also confirmed a
    zero-delay back-to-back debugging call (no relation to the real
    screener's own 0.3s pacing) can trigger Dhan's undocumented rate limit
    (bug #5) within just 2-3 rapid requests - a useful data point on how
    aggressive that limit actually is.

40. **`FnoScreener/` renamed to `K01`, 30 Aug 2026, per user request ("name
    this strategy for future references and document it").** Full
    mechanical rename - package directory, `main.py`'s import, the
    `K01_`-prefixed env vars (was `FNO_`), the paper-trade log filename
    (`k01_paper_trades.log`, was `fno_screener_paper_trades.log`), the
    logger name (`k01`, was `fno_screener`), the status endpoint
    (`GET /k01/status`, was `/fno-screener/status`), and a new
    `"strategy": "K01"` field in the snapshot JSON so the endpoint
    self-identifies. Done as a clean rename (not just a label/alias)
    since no trade history existed yet to migrate or lose - today's
    Sunday dry-run screen completed but placed zero paper trades (no
    intraday data on a non-trading day), so there was nothing to strand
    under the old name. Reran `test_fno_screener.py` (27/27) against the
    renamed package - no regressions. The `trading-skills` design doc
    (`designs/k01.md`) still describes the strategy's
    logic/rationale in full; this repo's `K01/` is the implementation.

41. **K01 turned OFF before its first live-market session, plus two
    rate-limit mitigations, 30 Aug 2026 - user request, prompted by
    asking "would this paper trading impact real time trading tomorrow?"**
    Real answer worked through: K01 can never place a real order (asserted
    at startup, no code path to `place_market_order`/`order_placement`
    exists in `paper_engine.py`) and has its own fully independent position
    store, so there's no *direct* interference with Options/Futures. But
    K01 reuses the SAME authenticated Dhan connection and runs in the SAME
    process as those real-money strategies - its REST call volume
    (intraday polling for its watchlist, every `POLL_INTERVAL_SECONDS`)
    competes for Dhan's undocumented rate limit (bug #5) alongside their
    own exit-monitoring calls. Not a new risk category - the same
    mechanism already documented in this file's stale-LTP entry (#38) -
    but K01 adds real volume to it, so it's a genuine indirect risk to
    real-money exit-check timeliness, not just theoretical.

    Three changes:
    - **New `K01_STRATEGY_ENABLED` flag, defaulting to `false`** (same
      pattern as `CopperOptions.config.STRATEGY_ENABLED`) - the poll loop,
      daily screen scheduling, and status endpoint all keep running
      either way; the flag gates only the actual Stage 0-3 work each
      tick (an early `continue` right after the loop's startup log line).
      Deployed OFF for 31 Aug 2026's first live session. Flip to `true`
      in `.env` + restart to re-enable once the rate-limit footprint has
      been reasoned through further.
    - `K01_POLL_INTERVAL_SECONDS` 15->45s - cuts K01's own REST call
      frequency by two-thirds.
    - New `K01_WATCHLIST_CAP=8` (was computed as `2*DAILY_SHORTLIST_SIZE`
      = 20 - now a direct, independent value) - fewer stocks polled per
      tick means fewer REST calls per tick regardless of interval.

    Verified offline (31/31 checks now, up from 27 - added a test that
    actually runs `poll_loop()` for a few ticks with `STRATEGY_ENABLED`
    patched `False` and confirms `_run_daily_screen` is never invoked,
    not just that the config value reads correctly) against the real
    `.env`. Confirmed both `/positions` and `/futures/positions` empty
    before this deploy (4th restart of the day - all clean, no crashes
    across any of today's iterations).

42. **`MAX_LIVE_POSITIONS_CE` 4->3 (Options), 2->3 (Futures) - user request
    30 Aug 2026.** Aligns both strategies' CE capacity at the same value
    (previously 4 and 2 respectively, with no prior stated reason for the
    mismatch beyond each being tuned independently on different dates -
    #36 above raised Options' alone). PE untouched on both sides:
    `MAX_LIVE_POSITIONS_PE` stays `0` for Options (still fully off, see
    #36) and `2` for Futures (unused today anyway since
    `futures_main.py` only exposes a bullish/CE webhook).

    Verified by reloading both `Options.config`/`Futures.config` against
    the real `.env` after the edit and asserting
    `MAX_LIVE_POSITIONS_CE == 3` on both. Confirmed `/positions` and
    `/futures/positions` both had empty `live_positions` immediately
    before this deploy's restart.

43. **New `trade_history.py` + `GET /trade-history` - user request 31 Aug
    2026, prompted by asking "do we have a mechanism to understand which
    trades are placed by which package and their history."** Real gap,
    confirmed by reading both stores directly rather than assumed:
    `Options/position_store.py` and `Futures/position_store.py`'s own
    `closed_positions_today`/`orders_today` are BOTH purely in-memory,
    reset every trading day (`maybe_reset_for_new_day`) and wiped entirely
    by any service restart - there was no way to look at trade history
    from a prior day, or after a restart, at all, for either strategy.

    Pure logging addition - `record_closed_trade(strategy, pos)` is called
    from inside `close_position()` AFTER the position is already closed
    (the real exit order has already been placed and confirmed by that
    point), so it cannot affect whether/when/at-what-price any real order
    is placed. Appends one JSON line per closed trade to
    `real_trade_history.log` (gitignored via the existing `*.log` rule),
    tagged `"strategy": "Options"` or `"strategy": "Futures"` so both
    packages' history lives in one file, filterable. Wrapped in try/except
    - a logging failure here must never break the caller's actual
    position-closing flow, same defensive pattern as K01's own
    `PaperTradeStore.record()`.

    `GET /trade-history` (optional `?strategy=Options`/`?strategy=Futures`)
    exposes it, mirroring the existing `/positions`/`/orders` pattern.

    Verified offline (not just read): a scratch-file test constructs a real
    `Options.position_store.Position` and a real
    `Futures.position_store.Position`, closes both through the actual
    `PositionStore.close_position()` method (not a reimplementation), and
    asserts the resulting log has exactly 2 correctly-tagged entries with
    correct P&L math and correct per-strategy filtering. Confirmed
    `main.py` imports cleanly with the new route registered
    (`/trade-history` present in `app.routes`) before deploying.

44. **All trade/incident logs moved into a dated `history/` folder - user
    request 31 Aug 2026** ("create a folder named history... store all
    trades and logs related files named with date prefix... keep on
    adding to it"). Every store now writes `history/<YYYY-MM-DD>_<name>.log`
    (a new file each day) instead of one ever-growing flat file at the
    repo root; reading always globs and merges every dated file for that
    name, so "history" still means the full multi-day record.

    `trade_history.py` (previously just the real-trade log from #43) is
    now the shared owner of this convention (`dated_path`/`append_jsonl`/
    `read_all_jsonl`), used by:
    - `trade_history.py` itself - real trades (`real_trades`)
    - `K01/paper_engine.py` - `k01_paper_trades`
    - `CopperOptions/paper_engine.py` - `copper_paper_trades`
    - `IndexScalping/paper_engine.py` - `index_scalping_paper_trades`
      (renamed from the old generic `paper_trades.log` - a plain
      "paper_trades" name would be ambiguous once multiple strategies'
      files sit side by side in the same folder)

    `watchdog.py`'s incident log follows the identical
    `history/<date>_incidents.log` naming but is implemented directly in
    that file (not via `trade_history.py`'s JSONL helpers) since
    `watchdog.py` is deliberately dependency-free/stdlib-only and incidents
    are a multi-line text block per entry, not JSONL. Dated by the
    incident's own START timestamp, not "now" - so a rare incident
    spanning midnight still lands entirely in one file. `main.py`'s
    `/incidents` endpoint updated to glob every dated file, not one fixed
    path.

    Each package's old `*_LOG_PATH`/`*_PAPER_LOG_PATH` env var
    (`K01_PAPER_LOG_PATH`, `COPPER_PAPER_LOG_PATH`, `SCALP_PAPER_LOG_PATH`)
    was removed, not just left unused - the `history/` location itself is
    now fixed, matching every store's convention consistently rather than
    leaving a half-dead, no-longer-effective config knob behind.

    **Existing accumulated history was migrated, not discarded.** A
    one-time script bucketed every existing flat file's entries by their
    own recorded date (each JSONL line's own `"date"` field; each incident
    block's own start timestamp) into the correct dated file under
    `history/`, then renamed the original flat file to
    `<name>.log.pre-history-migration` (kept, not deleted). Verified before
    running for real: dry-ran against copies of the actual droplet files
    (8 CopperOptions paper trades, 42 IndexScalping paper trades, 39
    incident blocks) and diffed the migrated output against the originals
    byte-for-byte (as line/block sets) - all three matched exactly, same
    counts, same content. `real_trade_history.log`/`k01_paper_trades.log`
    had nothing to migrate (confirmed empty/nonexistent on the droplet at
    migration time - no real trade had closed yet, K01 has never run its
    daily screen since it's been deployed OFF since #41).

45. **Webhook alert logging - `record_webhook_alert()`, `history/<date>_
    webhook_alerts.log`, `GET /webhook-alerts` - user request 31 Aug
    2026**, prompted by asking "are we logging the webhook alerts
    received?" Real gap confirmed before building: every handler already
    called `logger.info(...)` on receipt, but that only reaches journald
    (limited retention, not queryable, no record at all of what happened
    to an *ignored* alert once journald rotates it out). Added to all four
    webhook endpoints (`/chartink/webhook`, `/chartink/webhook-sell` -
    both share `_handle_chartink_webhook` - `/chartink/webhook-futures`,
    `/chartink/webhook-papertrade`), tagged `strategy="Options"`/
    `"Futures"`/`"Options-PaperTrade"` and `status`/`reason` mirroring
    exactly what each handler already returns to Chartink (ignored +
    which gate, no_action, or processed) - no new classification logic,
    just persisting what was already being computed.

    **Explicit hard requirement from the user: this must never add
    latency to, or be able to break, real order placement or position
    monitoring.** Two things make that true, both verified with a real
    test before deploying, not just asserted:
    - The actual disk write runs via `loop.run_in_executor` (thread pool,
      never the event loop thread).
    - Every call site fires via `asyncio.create_task(record_webhook_alert(...))`
      and does NOT await it - the webhook handler's own coroutine (and,
      for a processed alert, the real entry-order placement that already
      completed before the log call) never waits on the write.

    Verified with a scratch-history-dir test that monkeypatches the
    underlying write to sleep 2 real seconds: the calling coroutine
    returned in 0.0ms regardless, and the write still completed correctly
    once the event loop got a turn. A second test made the write raise an
    `OSError` unconditionally - confirmed the exception is logged
    (visible in journald) but never propagates into the caller. A third
    test round-tripped all 4 status/reason combinations through the real
    filtering (`strategy=Options`/`Futures`/`Options-PaperTrade`) - 7/7
    checks passed.

46. **Real lag regression found and fixed during a responsiveness audit -
    user request 31 Aug 2026** ("make sure... no delay in entry and exit
    trade placements... no lag basically"). Audited the full entry/exit/
    monitoring path against NOTES.md's own previously-established
    responsiveness targets (event-driven `on_price_tick`, `MONITOR_
    INTERVAL_SECONDS=2`, `LTP_STALE_AFTER_SECONDS=5`, `SUPERTREND_
    REFRESH_SECONDS=15`, `SUPERTREND_ENTRY_GRACE_MINUTES`/`_MIN_WARMUP_
    CANDLES` confirmed still removed, `monitor_loop`'s position checks
    confirmed still concurrent via `asyncio.gather` not sequential) -
    all intact, no regression found in any of that.

    **Found one real regression, introduced by entry #43's own trade_
    history.py work (30 Aug 2026):** `record_closed_trade()` was a plain
    sync function called directly inside `PositionStore.close_position()`'s
    `async with self._lock:` block - a blocking disk write on the event
    loop thread, WHILE HOLDING THE LOCK every other position operation
    needs (a concurrent exit on a different symbol, a fresh entry trying
    to reserve the very symbol this close just freed a few lines later in
    the same block, a price-tick's `update_highest_price`). Unlike
    `record_webhook_alert` (entry #45, built async/fire-and-forget from
    the start), this one was never given the same treatment when it was
    first written.

    Fixed identically to #45's pattern: `record_closed_trade` is now
    `async def`, its actual write goes through `run_in_executor` (thread
    pool, never the event loop), and both call sites
    (`Options/position_store.py`, `Futures/position_store.py`) fire it via
    `asyncio.create_task(record_closed_trade(...))` WITHOUT awaiting -
    `close_position()` (and the lock it holds) no longer waits on disk I/O
    at all.

    Verified with a real test, not just reasoning about it: monkeypatched
    the write to sleep 2 real seconds, called the actual (not
    reimplemented) `PositionStore.close_position()` for both Options and
    Futures, and confirmed it returned in 0.0ms each time (vs. would have
    blocked ~2000ms before the fix) - AND that the symbol was already
    freed in `reserved_symbols` immediately (proving the lock genuinely
    released without waiting), AND that both trades were still correctly
    logged once the background writes actually completed a moment later.

    **Observation, not changed:** `enter_positions_for_stocks` places
    orders for ranked stocks in a sequential `for` loop (reserve + broker-
    check + order-placement per stock, one after another), not
    concurrently via `asyncio.gather`. With `TOP_N_STOCKS=4` this means up
    to 4 sequential real-order round-trips per alert. Not clearly a
    regression (no evidence this was ever concurrent) and not changed
    without an explicit decision, since parallelizing real order placement
    is a bigger behavioral change than a logging-path fix - flagged for
    the user to decide on separately if entry latency across multiple
    ranked stocks in one alert ever becomes the bottleneck worth trading
    against added complexity/race-condition surface.

47. **Concurrency + reconciliation audit - user request 31 Aug 2026**
    ("is concurrency and reconciliation also handled properly"). Verified
    directly against the current code, not from memory:
    - Every `PositionStore` state mutation (Options AND Futures) goes
      through the same `asyncio.Lock` - `reserve_symbol`, `try_start_exit`,
      `close_position`, `reconcile_from_broker`, `update_highest_price`,
      etc. all take it, no mutation path skips it.
    - `try_start_exit`'s atomic claim correctly guards the exact race two
      concurrent exit triggers (poll loop + event-driven WebSocket tick)
      could otherwise hit - confirmed the claim-then-release contract is
      still intact and used correctly at every close/failure path.
    - Reconciliation runs exactly once, at startup, inside `lifespan()`
      BEFORE `yield` - FastAPI doesn't route any request (including a
      webhook) until lifespan startup completes, so no race between
      reconciliation and a webhook-driven entry is possible by
      construction, not by luck.
    - Futures confirmed still does NOT reconcile at all (by design, see
      the design-decision entry below) - no drift.
    - `dhan_wrapper._on_market_tick`'s fan-out to multiple strategies'
      subscribers (Options/Futures/CopperOptions/IndexScalping all
      register their own `_on_price_tick` closure) is per-callback
      try/except'd, so one strategy's failure can't block another from
      receiving the same tick; the subscriber list itself is only ever
      appended to during startup, before `start_feed()` makes any tick
      possible - no race there either.

    **Found a second real issue, this time in code from earlier the same
    day (#45/#46's own fire-and-forget logging):** `asyncio.create_task(...)`
    was called at 5 sites without keeping a reference to the returned
    `Task` - a documented asyncio pitfall (the event loop only holds a
    *weak* reference to a Task; with no other referrer, it can be
    garbage-collected before it finishes, silently dropping whatever it
    was doing). The two long-lived monitor-loop tasks were already correct
    (held in `_monitor_task`/`_paper_monitor_task` module globals) - only
    the newer fire-and-forget logging calls (`record_closed_trade` x2,
    `record_webhook_alert` x3) had this gap.

    Fixed with the standard pattern: `trade_history.fire_and_forget(coro)`
    wraps `asyncio.create_task`, adds the Task to a module-level
    `_background_tasks` set, and removes it via `add_done_callback` once
    it finishes - a strong reference is held for exactly as long as the
    task is actually running, no longer. All 5 call sites switched from
    bare `asyncio.create_task` to this.

    Verified with a real test, not just citing the docs: fired a task via
    `fire_and_forget`, dropped every other reference to it, called
    `gc.collect()` immediately (deliberately hostile timing), and
    confirmed the task was still tracked and still completed correctly a
    moment later - then confirmed `_background_tasks` correctly emptied
    back out afterward (no permanent leak from the tracking set itself).
    Re-ran both #45/#46's own earlier tests afterward to confirm the
    switch to `fire_and_forget` didn't regress the lag-free/non-blocking
    behavior they'd already verified.

48. **Futures now reconciles broker positions at startup - user request
    31 Aug 2026** ("make trades under Futures also reconcile"). The
    blocker was real and confirmed before writing any code, not assumed:
    checked Dhan's official API docs for the `/positions` endpoint schema
    - `correlationId` (the order tag both strategies already set on every
    entry order via `_gen_tag`) exists ONLY on order-level responses,
    never on the aggregated position record. Since Options and Futures
    both place real orders for the identical instrument type (ATM
    options), Dhan's own data genuinely cannot distinguish which
    strategy's position is which - confirmed, not guessed at.

    **The fix: a new persistent, per-strategy "position opened" log**
    (`trade_history.py`'s `record_opened_position`/
    `attribute_open_broker_position`, `history/<date>_position_opened.log`)
    - mirrors the existing closed-trade log exactly (same fire-and-forget
    discipline via `fire_and_forget`, called from `PositionStore.
    add_position` right after a position is actually opened, can't affect
    the entry order itself). `attribute_open_broker_position(trading_symbol)`
    scans our OWN history (not the broker's) for which single strategy
    shows this exact symbol opened with no later matching close - i.e.
    still open per our own records. Returns `None` (never guesses) if
    there's no record at all (predates this logging, or opened manually
    outside the bot) or if it's ambiguous.

    Wired into `reconcile_broker_positions()` on **both** sides, not just
    Futures: **Options' own reconciliation was already latently
    vulnerable to this exact issue** (it unconditionally imported every
    open FNO position, with no filter) - never observed live, but real
    now that Futures also places real option orders. Both functions now
    skip (with a clear warning, not a silent guess) any broker position
    they can't confidently attribute to themselves specifically. Added
    `Futures/trading_engine.py::reconcile_broker_positions()` (didn't
    exist before) and `Futures/position_store.py::reconcile_from_broker()`
    (didn't exist before) as near-verbatim copies of Options' own,
    wired into `Futures/futures_main.py`'s lifespan the same way Options'
    lifespan already does it.

    **Real limitation, stated plainly, not hidden**: a position opened
    before this logging existed, or placed manually outside the bot
    entirely, has no record and will be safely skipped by both sides
    (visible as a clear warning log, not silently mismanaged) rather than
    guessed into either strategy - it needs manual handling. This is the
    correct failure mode (never double-track), not a gap to "fix" by
    guessing.

    Verified with real tests, not just reasoning about it: constructed
    real `Position` objects, opened/closed them through the actual
    `add_position()`/`close_position()` methods (not reimplemented) for
    both Options and Futures, and confirmed (1) an Options-opened position
    attributes to "Options", (2) a Futures-opened one to "Futures" with no
    cross-contamination, (3) closing a position makes it correctly
    unattributable again (no longer "open"), (4) an unknown symbol
    correctly returns `None`, and (5) a reconciliation-style filter over a
    mixed list of broker positions keeps only the correctly-attributed,
    still-open one for each strategy - 5/5 checks passed. Also re-ran
    #46/#47's own lag/GC-safety tests to confirm the new `add_position`
    call site didn't regress either (`add_position()` returns in 0.0ms
    even under a simulated 2-second slow write, matching `close_position`'s
    already-verified behavior).

49. **LTP-cache memory leak found and fixed - user request 31 Aug 2026**
    ("is this setup fine with no memory issues"). Checked real droplet
    numbers first, not just the code: `free -h` showed 961Mi total/489Mi
    available/only 42Mi of the 1Gi swap in use, and no OOM-kill events in
    `dmesg`/the kernel journal ever - healthy right now, the 21 Aug swap
    addition was precautionary, not reactive to a crash.

    Found a real bug while checking anyway: `dhan_client.py`'s `_ltp_cache`/
    `_ltp_cache_ts` (the WebSocket price cache, keyed by option
    security_id) are written on every tick and every REST fallback, but
    were NEVER cleaned up - confirmed `unsubscribe_option_price()` (called
    at every real exit point in both Options and Futures) only ever
    popped `_security_id_to_symbol`, not these two. Unlike
    `_supertrend_cache` (keyed by underlying_symbol, naturally capped at
    the ~210-stock F&O universe), option contracts' security_ids change
    with every strike/expiry, so this key space never caps on its own -
    a real, if slow, unbounded leak (estimated ~1-2MB/year at realistic
    trade volumes - not urgent given the headroom above, but a real bug,
    not just theoretical).

    Fixed by popping both dicts in `unsubscribe_option_price()` alongside
    the existing `_security_id_to_symbol` pop. Checked every call site
    first to confirm nothing reads a symbol's cached LTP after
    unsubscribing it (unsubscribe only ever happens on a confirmed
    successful exit or a rejected-entry cleanup - both are dead ends for
    that exact contract, never read again) - so clearing the cache at that
    exact moment can't break a legitimate read.

    Verified with a real test (constructed a bare `DhanWrapper` instance,
    monkeypatched only the instrument-lookup/market-feed dependencies
    subscribe/unsubscribe touch - no network calls): populated both cache
    dicts exactly as a real tick + REST fallback would, called the actual
    `unsubscribe_option_price()`, and confirmed all three dicts (including
    the two that leaked before the fix) were empty afterward - plus a
    double-unsubscribe on an already-clean symbol doesn't raise.

50. **Three resilience/responsiveness fixes - user request 31 Aug 2026**
    ("any other place to fine tune... more resilient and responsive to
    price updates"), all three explicitly approved by the user before
    building:

    **(a) Feed reconnect visibility.** Traced into the actual installed
    `dhanhq` library source (not assumed) - `MarketFeed` already self-
    heals on disconnect: its own `_run_async` loop detects a closed socket
    within ~1s and calls `connect()` again, which calls
    `subscribe_instruments()` and replays the FULL current subscription
    list (`self.instruments`, kept persistently updated by
    `subscribe_symbols`/`unsubscribe_symbols`) - so open positions
    automatically resume ticking with no code of ours needed. This is why
    no custom retry wrapper was ever built for it, unlike `OrderUpdate`
    (confirmed NOT to self-heal, per bug #21's era comments) which
    deliberately got one. What WAS missing: our own `MarketFeed(...)`
    construction only wired `on_ticks`, not `on_connect`/`on_close`/
    `on_error` - a real disconnect/reconnect cycle produced zero log trace
    and zero `/feed-stats` visibility, even though the mechanism itself
    was fine. Added `_on_market_connect`/`_on_market_close`/
    `_on_market_error` + three new `stats` counters
    (`feed_connects`/`feed_disconnects`/`feed_errors`) - pure observability,
    no behavior change to the feed itself.

    **(b) Paced REST-fallback bursts.** `monitor_loop` checks all open
    positions concurrently via `asyncio.gather` - if several positions go
    stale in the same poll tick (most likely during the exact kind of feed
    hiccup (a) now surfaces), each fired its own REST LTP call with zero
    pacing between them, a burst against Dhan's known undocumented rate
    limit (bug #5) right when the feed is already struggling. Added
    `dhan_wrapper.ltp_rest_fallback_semaphore` (`asyncio.Semaphore(2)`),
    shared between Options' and Futures' `_get_ltp` (same real rate-limit
    budget) - only gates the REST-fallback branch, so the common case (a
    cache hit, no REST needed at all) is completely unaffected.

    **(c) Concurrent entry-order placement.** `enter_positions_for_stocks`
    placed orders for ranked stocks in a sequential `for` loop (up to
    `TOP_N_STOCKS`=4 real order-placement round-trips queued one after
    another per alert) - flagged as an observation in entry #47, now
    actually changed given explicit approval. Factored the per-stock
    reserve/dedup/enter sequence into `_process_one_entry`, run via
    `asyncio.gather` instead of a `for` loop, in both `Options/
    trading_engine.py` and `Futures/trading_engine.py`. Safe because
    nothing about the per-stock logic itself changed: `reserve_symbol()`
    was already atomically locked (already had to guard concurrent
    duplicate-webhook races for the SAME symbol; guarding different
    symbols racing for the same capacity pool is the identical mechanism),
    and every exception path was already caught locally into a result dict
    rather than left to propagate, so `asyncio.gather` can't have one
    stock's failure affect another's or crash the batch.

    Verified with real tests, not just reasoning about it: (1) the
    reconnect/disconnect/error callbacks correctly increment their
    counters, (2) a real `asyncio.Semaphore(2)` genuinely caps 10
    concurrent workers at exactly 2 in flight, (3) confirmed Options' and
    Futures' `_get_ltp` reference the literal same semaphore object (one
    shared budget, not two independent ones that would defeat the point),
    and (4) - the highest-stakes one - real `PositionStore.reserve_symbol`/
    `release_symbol` (not reimplemented) with capacity capped at 2, 5
    ranked stocks all attempting entry CONCURRENTLY, confirmed exactly 2
    entered and 3 were correctly skipped as capacity-full with zero race
    letting more than capacity through, result order preserved matching
    input order, and remaining capacity correctly at 0 afterward. Re-ran
    all 5 of entries #46-49's own prior tests afterward to confirm none of
    this regressed the lag/GC-safety/reconciliation/leak fixes already
    verified today.

51. **Two more real bugs found via a live "dummy webhook call" health
    check - user request 31 Aug 2026.** Sent a real (but genuinely safe)
    test payload to `/chartink/webhook-papertrade` (never `/chartink/
    webhook`, `/chartink/webhook-sell`, or `/chartink/webhook-futures` -
    those place real orders/AMOs even outside market hours, so a "dummy"
    payload there is not actually dummy) with 3 real liquid stocks
    (RELIANCE, TCS, SBIN). 2 of 3 failed with `"No LTP returned"`.

    **Confirmed this was a real bug, not "market's closed, no data"**: all
    three symbols resolved fine with real premiums moments later when
    queried standalone with 1.5s pacing between them. Root cause: `Options/
    paper_webhook.py`'s entry loop (`for symbol, pct_change in ranked:`)
    had zero pacing between ranked stocks' REST calls - the SAME class of
    bug already found and fixed 3 other places this session (K01's debug
    loop, K01's anti-SAGILITY check, the DanDanaDan backtest loop), this
    time in a real, currently-deployed code path. Fixed with the identical
    convention: `await asyncio.sleep(0.35)` between stocks (skipped before
    the first one).

    **Checking this surfaced a second, more important finding**: entry
    #50's parallelization of the REAL Options/Futures entry loop
    (`enter_positions_for_stocks` via `asyncio.gather`) meant `has_open_
    position_for_underlying()` - which calls `get_open_fno_positions()`,
    which had NO retry wrapper at all - now fires concurrently for up to
    4 stocks where it used to run one-at-a-time with natural sequential
    spacing. Unlike `get_atm_option`/`get_day_change_pct` (both already
    wrapped in `_retry` for exactly this "Dhan's market-data calls can
    transiently rate-limit-fail" reason), a transient failure here meant
    that stock's entire entry was abandoned outright, not retried. Fixed
    by splitting `get_open_fno_positions()` into a thin `_retry`-wrapped
    public method + a `_get_open_fno_positions_once()` private one doing
    the actual call - the same pattern already used for
    `_get_atm_option_once`/`_get_day_change_pct_once`. This benefits BOTH
    the entry-time dedup check and startup reconciliation (both call
    `get_open_fno_positions`), and is the correct fix rather than trying
    to re-serialize what #50 deliberately parallelized.

    Verified with real tests: (1) a flaky mock that fails once then
    succeeds proves the retry actually recovers (not just retries and
    still fails), correctly measuring the ~1.5s backoff elapsed; (2) a
    mock that always fails proves exhausted retries still raise rather
    than being silently swallowed; (3) the actual `chartink_webhook_
    papertrade` handler (not reimplemented) with 3 fake-but-realistic
    stocks measured at ≥0.65s elapsed, confirming the 2 expected 0.35s
    pacing gaps are genuinely there. Re-ran all 6 of tonight's prior test
    suites (entries #46-50) afterward - no regression.

52. **Deep integration test pass before market open - user request 31 Aug
    2026** ("do deep paper testing to make sure everything works fine when
    market opens"). Built a 6-scenario suite exercising the REAL webhook
    handler (`Options.option_main._handle_chartink_webhook`) and REAL
    entry/exit/reconciliation code end-to-end - not reimplemented,
    not isolated unit mocks - with every Dhan network call mocked (ranking,
    ATM lookup, broker-position lookup, order placement) so it validates
    the LOGIC changes from tonight (#43-51) together, under realistic
    multi-stock/concurrent conditions, safely and repeatably.

    **A real operational constraint hit along the way, not a code bug**:
    the first version of this suite used real (read-only) Dhan calls and
    tripped Dhan's own authentication rate limiter after the many separate
    local test-script logins already run tonight ("Too many attempts.
    Please try again after sometime."). Confirmed the live droplet was
    completely unaffected (`/health`, `/positions`, `/futures/positions`
    all normal throughout - it uses its own already-cached session,
    entirely independent of local test scripts each needing a fresh
    login). Rewrote the suite to mock every Dhan network call, not just
    order placement, removing any dependency on live auth for this kind
    of logic-level validation going forward.

    **6 scenarios, all passing, run together with all 26+ checks from
    entries #43-51's own test suites afterward (32+ total, zero
    regressions)**:
    1. A real 5-stock alert through the real webhook handler, ranking
       trimming to `TOP_N_STOCKS` candidates BEFORE capacity is checked
       (exactly like production), capacity correctly capping entries.
    2. Two concurrent identical webhook deliveries (a real Chartink
       duplicate-delivery scenario) - each symbol entered exactly once,
       `reserve_symbol`'s atomic lock holding under real concurrent calls.
    3. A transient Dhan failure injected mid-burst during real concurrent
       entry - the retry (entry #51's fix) recovers it inline, that
       stock's entry is NOT abandoned.
    4. Real target-hit and stop-loss-hit exits via the actual
       `_exit_reason_for`/`close_position` - both close correctly, symbols
       free for re-entry, `trade_history` logs both async and correctly.
    5. A realistic mixed 3-position reconciliation (2 Options-owned, 1
       Futures-owned, attributed via real `record_opened_position` calls)
       - each side picks up only its own, confirmed via the exact expected
       warning logs firing for the cross-strategy skips.
    6. Malformed webhook payloads (empty `stocks`, missing required field)
       rejected cleanly by pydantic validation, not silently passed
       through toward order placement.

    Live droplet confirmed healthy and untouched throughout (all of this
    ran against local, scratch `PositionStore`/`history/` instances - the
    real module-level singletons and the real deployed process were never
    touched by any of tonight's testing).

53. **Deep integration suite persisted into the repo - user request 31 Aug
    2026** ("keep it in our repo, no need to deploy this to droplet but we
    can run it whenever needed and it would be documented also for future
    references"). Moved from a scratchpad file into `tests/test_deep_integration.py`
    (repo-relative paths, `tests/README.md` explaining what it covers and
    how/why to run it, `README.md`'s file table updated). Confirmed all 6
    scenarios still pass from the new location. Deliberately NOT deployed
    to the droplet and NOT wired into any CI/deploy step - this is a
    dev-only tool for validating concurrency/retry/reconciliation logic
    before/after touching it, run manually with
    `uv run python tests/test_deep_integration.py`.

54. **`MAX_LIVE_POSITIONS_CE` 3->2 (Options + Futures), `MAX_LIVE_POSITIONS_PE`
    0->2 (Options) - user request 31 Aug 2026.** Re-enables PE (bearish,
    `/chartink/webhook-sell`) trading, off since #36 (27 Aug 2026), while
    lowering CE on both strategies so combined CE+PE exposure stays in
    line with the prior CE=3/PE=0 total rather than adding on top of it.
    Futures' PE cap (`FUTURES_MAX_LIVE_POSITIONS_PE`) stays untouched at
    its existing default of 2, still unused today since `futures_main.py`
    only exposes a bullish/CE webhook.

    No code changes needed - `reserve_symbol`/`remaining_capacity` already
    handle any cap value correctly (this was proven going the other
    direction too, down to 0, in #36). Verified by reloading
    `Options.config`/`Futures.config` against the real `.env` after the
    edit and asserting `CE == 2` and `PE == 2` on both. Re-ran the full
    `tests/test_deep_integration.py` suite (all 6 scenarios pass - it sets
    its own capacity override per test, so it's independent of this
    config value). Confirmed `/positions` and `/futures/positions` both
    had empty `live_positions` immediately before this deploy's restart.

55. **New `choppy_stocks.py` - weekly "choppy stocks" exclusion list for
    Options - user request 31 Aug 2026** ("stocks with lot size > 6000,
    avoid Options trades in them, refresh every Monday 12 PM"). Every NSE
    stock-option (OPTSTK) underlying whose current lot size exceeds 6000
    units (`choppy_stocks.LOT_SIZE_THRESHOLD`) is excluded from new
    Options entries - both real webhooks (`/chartink/webhook`,
    `/chartink/webhook-sell`), scoped to Options only per the user's own
    wording ("avoid taking trades in these stocks Options from bot") -
    Futures/K01/CopperOptions/IndexScalping are untouched.

    Computed from Tradehull's own cached scrip-master (same source/filter
    K01's `_fetch_fno_universe` already uses for the full F&O universe -
    one lot-size value per underlying, since NSE revises it for everyone
    at once, not per-strike). Persisted to `choppy/choppy_stocks.json`
    (gitignored runtime data, same convention as `history/`) via an
    atomic write (temp file + `Path.replace`), refreshed automatically by
    a new background loop (`choppy_list_refresh_loop`, started from
    Options' lifespan the same way `monitor_loop`/`paper_webhook.poll_loop`
    are) that bootstraps an initial list immediately if none exists on
    disk yet, then refreshes every Monday at 12:00 PM IST after that.
    Visible via new `GET /choppy-stocks`.

    Two deliberately different read paths: `is_choppy()` (the hot path,
    called once per candidate stock per alert) is a pure in-memory set
    lookup - zero I/O - kept current by every refresh and reloaded from
    disk at startup (`load_choppy_cache_at_startup`); `read_choppy_list()`
    (the cold path, `GET /choppy-stocks` and the refresh loop's own
    bootstrap check) reads the file fresh each time. FAILS OPEN throughout
    - a missing/corrupt file means an empty exclusion set (nothing
    blocked) with a warning logged, not a silent halt to all Options
    entries, and a failed refresh just keeps the previous list rather than
    crashing Options' own startup.

    Filtered in `option_main.py`'s webhook handler BEFORE ranking (so a
    choppy stock in an alert can't consume a top-N ranking slot a
    tradeable stock could have used instead) and again, belt-and-suspenders,
    inside `trading_engine.py`'s `_process_one_entry` right before
    `reserve_symbol` (in case some future caller bypasses the pre-filter) -
    same defensive-layering pattern this file already uses for the
    broker-position dedup check. The webhook-alerts audit log
    (`record_webhook_alert`) still records the ORIGINAL, unfiltered stock
    list Chartink sent - only what the bot chose to act on is filtered,
    not what's logged as received.

    New `tests/test_choppy_stocks.py` (6 scenarios, all passing): lot-size
    computation/dedup/exchange-filtering against a fake instrument
    dataframe, write/read round-trip through real disk I/O, cache
    fail-open + refresh + restart-recovery behavior, `_next_monday_noon_ist`
    scheduling edge cases (exact instant, just-before, mid-week, Sunday
    night), and two full-webhook-handler scenarios (a choppy stock
    excluded before ranking while non-choppy stocks still enter normally;
    an alert where every stock is choppy ignored cleanly with an explicit
    `all_stocks_choppy` reason rather than a misleading `could_not_rank_
    any_stock`). Re-ran `tests/test_deep_integration.py` afterward (6/6
    still pass, confirming no interaction with the existing entry flow
    when the choppy cache is empty, its default state).

56. **`choppy_stocks.py` simplified from an automatic weekly scan to a
    manually-maintained fixed list - user request 31 Aug 2026, same day
    as #55.** After reviewing #55's first scan output (14 stocks, lot
    size > 6000 - IDEA, YESBANK, SUZLON, SAGILITY, IDFCFIRSTB, PNB,
    GMRAIRPORT, NHPC, NMDC, CANBK, MAHABANK, NBCC, INOXWIND, MOTHERSON),
    the user chose to keep only 3 (IDEA, YESBANK, SAGILITY) and maintain
    the list by hand going forward rather than have it auto-computed or
    auto-refreshed weekly.

    Removed entirely, not left dormant: `compute_choppy_stocks` (the
    lot-size scan itself), `LOT_SIZE_THRESHOLD`, `refresh_choppy_list`,
    `choppy_list_refresh_loop` (the background asyncio task + its Monday-
    noon scheduling math), and the in-memory symbol cache
    (`_cached_choppy_symbols`/`load_choppy_cache_at_startup`) - all
    dead weight once nothing computes or schedules a refresh anymore.
    `choppy_stocks.py` no longer depends on `dhan_wrapper`/the instrument
    master at all now - it's pure file I/O.

    New design: `DEFAULT_CHOPPY_STOCKS = ["IDEA", "YESBANK", "SAGILITY"]`,
    written once by `ensure_choppy_list_exists()` (called from Options'
    lifespan) ONLY if `choppy/choppy_stocks.json` doesn't already exist -
    never touches an existing file, so a hand-edit is never at risk of
    being silently overwritten by the app itself. `is_choppy()` now reads
    the file fresh on every call rather than an in-memory cache -
    deliberately not cached, so a manual edit to the file (e.g. `ssh` in,
    `nano choppy/choppy_stocks.json`) takes effect on the very next
    webhook alert with no restart needed - a small tradeoff (repeated
    tiny local file reads, OS-page-cached, negligible cost given this
    only runs per-alert not per-tick) deliberately made for that
    immediacy. Filtering call sites in `option_main.py`/`trading_engine.py`
    (excluded before ranking, belt-and-suspenders re-check at entry) and
    `GET /choppy-stocks` are unchanged in shape, just simpler underneath.

    `tests/test_choppy_stocks.py` rewritten for the new design (5
    scenarios, all passing): seed-if-missing + never-overwrite-existing,
    write/read round-trip normalization (uppercase/dedup/sort), fail-open
    with no file + live pickup of a manual edit with no reload step, and
    the same two full-webhook-handler integration scenarios from #55
    (choppy stock excluded before ranking; all-choppy alert ignored
    cleanly) re-verified against the new manual-list mechanism. Caught and
    fixed one of my own test-construction bugs while writing this: a
    scratch-directory-naming helper used `id(object())` for uniqueness,
    which CPython can and does reuse once the temporary object is
    collected - two calls landed on the same scratch path and one test's
    leftover data leaked into the next, failing a fail-open assertion
    that should have passed. Fixed with a monotonic counter instead.
    Re-ran `tests/test_deep_integration.py` afterward (6/6 still pass).

    **Not yet deployed as of this writing** - two real Options PE
    positions (MANAPPURAM, GRASIM, both opened today under #54's new
    PE cap) were live when this was ready to ship, and restarting to
    deploy would reset both positions' trailing-stop memory on broker
    reconciliation (the same real risk #42/HAL-incident risk this file's
    own safety-practice section describes). Asked the user via
    AskUserQuestion rather than restarting anyway; user dismissed the
    question without choosing an option, meaning: do not proceed, wait
    for further instruction. Code is committed, pushed to `dhanBoy`, and
    already pulled onto the droplet - the running process is still the
    prior code until a restart is explicitly requested.

57. **`MAX_LOSS_PER_TRADE_RS`/`PROFIT_PROTECTION_THRESHOLD_RS` split into a
    before/after-11:30 pair, for both Options and Futures - user request
    31 Aug 2026** ("1500/1000 for profit protection, 1200/1000 for max
    loss, before/after 11:30 AM"). Both were previously a single flat
    value all day (1200 and 1500 respectively, both packages - see entry
    #42's backtest-driven history for `MAX_LOSS_PER_TRADE_RS`). Now: the
    morning value (1200 / 1500) stays exactly what it was before this
    change, and a new, tighter afternoon value (1000 / 1000) takes over
    from the cutoff on - a trade still open into the afternoon gets less
    rope on the loss side and locks in profit sooner on the upside.

    New config: `MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF`/`_AFTER_CUTOFF`,
    `PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF`/`_AFTER_CUTOFF`, and
    `RISK_THRESHOLD_CUTOFF_TIME` (default `"11:30"`, `FUTURES_`-prefixed
    for Futures per that package's own convention) - deliberately a
    SEPARATE setting from `ALLOWED_TRADING_TIME` even though both default
    to the same "11:30" today: `ALLOWED_TRADING_TIME` only gates NEW
    entries, while this gates the EXIT check on positions already open,
    regardless of when they were entered - the two could diverge later
    without this change accidentally coupling them.

    New `current_max_loss_per_trade_rs()`/`current_profit_protection_
    threshold_rs()` functions in both `Options/trading_engine.py` and
    `Futures/trading_engine.py` (identical logic, each package's own
    `_now_ist()`/`config`), backed by a shared `_is_before_risk_threshold_
    cutoff()` check so both stay in lockstep on the exact same boundary
    instant. `_exit_reason_for()` in both files now calls these instead of
    reading the old flat `config.MAX_LOSS_PER_TRADE_RS`/`PROFIT_PROTECTION_
    THRESHOLD_RS` constants directly (which no longer exist) - evaluated
    fresh on every call, so a position open since before the cutoff is
    still re-evaluated against the tighter afternoon values the instant
    the clock crosses it, same as every other time-of-day gate in this
    codebase (`is_past_square_off_time`, `is_past_allowed_trading_time`).
    The exact boundary instant (11:30:00) already counts as "after",
    consistent with those same gates' `>=` semantics.

    New `tests/test_risk_threshold_cutoff.py` (3 scenarios, all passing):
    the lookup functions return the correct value on each side of the
    cutoff for both packages; the exact boundary instant resolves to
    "after"; and `_exit_reason_for()` itself - not just the lookup
    functions in isolation - fires `MAX_LOSS_HIT`/`PROFIT_PROTECTION_HIT`
    at the correct threshold on each side, for both Options and Futures.
    Re-ran `tests/test_deep_integration.py` and `tests/test_choppy_
    stocks.py` afterward (14/14 still pass, confirming the `_exit_
    reason_for` refactor didn't change behavior for anything other than
    the intended time-of-day split).

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

  **Update, 31 Aug 2026: now DOES run `reconcile_broker_positions()` at
  startup** (user request - see entry #48 below for the actual mechanism:
  a persistent, per-strategy "position opened" log, since Dhan's own
  `/positions` data still has no notion of which strategy placed a
  position - that fact hasn't changed, only how it's worked around).
  Originally skipped entirely for the reason below, still worth reading
  for why a naive fix would have been dangerous:

  `get_open_fno_positions()` returns every open FNO position in the
  account with no notion of which strategy placed it - if Futures had
  just reconciled the same way Options originally did (an unconditional
  import of every open FNO position), a restart could re-import Options'
  own live positions into Futures' separate tracker too, and both
  strategies could then try to independently manage/exit the same real
  broker position. Entry #48 solves this properly instead of leaving it
  unsolved: neither strategy's reconciliation now imports a position
  unless our OWN persistent history can confidently attribute it as
  theirs.

  Still accepted, not fixed: since Options and Futures both rank/enter
  independently with identical instrument-selection logic, they could
  each open their own separate position on the same underlying if both
  alert on it around the same time - same class of tradeoff already
  accepted for the paper-trade webhook, now with real money on both
  sides. Worth revisiting if it's ever observed live. This is a
  different failure mode than the reconciliation one above (two
  *separately opened* positions, not one position double-managed) and
  entry #48 does not address it.

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

  **Lowered again to ₹1,500 later the same day, by user request, alongside
  `PROFIT_PROTECTION_THRESHOLD_RS` also moving to ₹1,500** (was ₹2,000) -
  same code/`.env` update pattern. This time the test suite itself was
  rewritten to compute its boundary values off the LIVE `cfg.
  MAX_LOSS_PER_TRADE_RS`/`cfg.PROFIT_PROTECTION_THRESHOLD_RS` rather than
  hardcoded literals, since this threshold has now changed twice in one
  day - future tuning shouldn't require editing the test's numbers each
  time, only re-running it. Re-verified: 18/18 (max-loss) and 30/30
  (profit-protection) still passing against the new ₹1,500 value.

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
  THRESHOLD_RS` (originally ₹2,000, lowered to ₹1,500 later the same day -
  see `MAX_LOSS_PER_TRADE_RS`'s entry above for that change), the mirror
  image of `MAX_LOSS_PER_TRADE_RS` but on the upside.** Once a trade's PEAK
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

- **Broker-reconciliation safeguard added to `_exit_position()` in both
  `Options/` and `Futures/` (26 Aug 2026, user request) - after 2+
  consecutive exit failures, check broker truth before retrying a SELL,
  instead of retrying blindly.** Motivated directly by the ADANIPOWER
  incident earlier the same day (see below) and the LICHSGFIN manual-
  close: once `position.exit_failure_count >= 2`, `_exit_position()` now
  calls a new `DhanWrapper.get_broker_net_quantity(trading_symbol)` (exact
  contract match, not just underlying - a manual trade on a different
  strike for the same stock shouldn't be confused with the specific leg
  being exited) before attempting another SELL. If the broker already
  shows 0 quantity for that exact contract, the position is closed
  locally with reason `RECONCILED_ALREADY_FLAT` (marked at the last
  reasonable price available, `exit_price` or `highest_price`) and NO
  further order-placement call is made - trading one cheap position-check
  API call for what would otherwise be an indefinitely-repeating doomed
  SELL attempt. If the broker still shows it open, the normal retry
  proceeds exactly as before - this only changes behavior once something
  is already stuck for 2+ tries, never on a single transient rejection.
  If the reconciliation check itself errors (e.g. a network blip), falls
  through to a normal retry rather than guessing either way.

  This also closes a latent, if narrow, safety gap: without this check, a
  position closed out-of-band (manually, or by some other process) would
  have kept the bot retrying a SELL for it indefinitely, since nothing
  ever told `_exit_position()` the position was already gone - each
  retry would fail for whatever reason applied (e.g. the account no
  longer holding the contract), backing off and retrying forever rather
  than ever resolving. `close_position()` (the same function every normal
  exit path already uses) correctly pops the position, clears the
  `reserved_symbols` claim, and frees the symbol for a fresh entry -
  nothing new needed there.

  ADANIPOWER context (25 Aug->26 Aug session): a SELL to close an
  existing NRML long got RMS-rejected 6+ times over ~6 minutes with
  "insufficient funds, add ~Rs.117,000+" - unusual for a square-off of a
  position already held, not a fresh short. Most likely cause: running
  7-9 concurrent MARGIN/NRML positions simultaneously that day left free
  margin thin enough that Dhan's real-time RMS check needed headroom even
  to process a closing SELL, self-resolving once other positions closed
  and freed capital. This safeguard doesn't fix that underlying margin
  dynamic (a real capital-management tradeoff of NRML, not a bug), but it
  does stop the bot from hammering the same doomed order repeatedly once
  a position turns out to have already resolved by other means.

  Verified fully offline (mocked `dhan_wrapper`, zero real network calls
  reachable) in `/private/tmp/.../scratchpad/test_exit_reconciliation.py`:
  both packages, exact-contract matching in `get_broker_net_quantity`,
  the check being skipped entirely below the 2-failure threshold (no
  behavior change for a single rejection), a normal retry proceeding when
  the broker confirms still-open, closing locally with zero order
  placement when the broker confirms flat, and graceful fallback to a
  normal retry when the reconciliation call itself errors - 21/21 checks
  passed. Re-ran the `MAX_LOSS_PER_TRADE_RS` and `PROFIT_PROTECTION_
  THRESHOLD_RS` suites too - no regression.

- **Stock selection flipped from top-N to bottom-N by %change in both
  `Options/` and `Futures/`, for both CE and PE (26 Aug 2026, user
  request) - `SELECT_BOTTOM_N_STOCKS` (default `true`).** A deliberate
  contrarian/laggard pivot, prompted directly by that same day's trade
  evaluation showing 5 of 7 losing trades were fast, hard reversals right
  after entry - a classic momentum-exhaustion pattern from chasing the
  strongest-confirming names in an alert. This flips which end of the
  ranking `rank_and_pick_top_stocks()` actually selects:
  - **CE** ranks strongest gainers first (unchanged) - with the flag on,
    selection takes the LAST `TOP_N_STOCKS` of that ranking instead of
    the first, i.e. the *weakest* gainers in the alerted list (which can
    even be flat or slightly negative names, if the list has more than
    `TOP_N_STOCKS` candidates) rather than the names already up the most.
  - **PE** ranks biggest decliners first (unchanged) - bottom-N takes the
    *weakest* decliners (can even be flat or slightly positive names)
    rather than the names already down the most.

  The bet: names that haven't yet confirmed the alert's own direction as
  strongly may have more room to move, rather than buying into a name
  that's already made its run and is more prone to snapping back - the
  opposite side of exactly the pattern that produced most of that day's
  losses. This is a genuine, debatable strategy hypothesis, not an
  obviously-correct fix - it trades one failure mode (buying exhausted
  momentum) for a different, unproven one (the weakest names may be weak
  because the move genuinely isn't there for them).

  Implementation: `scored.sort(...)` is unchanged (still strongest-first
  for CE, biggest-decliner-first for PE - the ranking itself, and its own
  log/print output, is identical either way); only the final slice
  changes, from `scored[:top_n]` to `scored[-top_n:]` when the flag is on.
  Guarded against the `list[-0:]` Python gotcha (negative-zero slicing
  returns the WHOLE list, not empty) with an explicit `top_n > 0` check,
  though `TOP_N_STOCKS` is never actually 0 in practice. Only changes
  anything when an alert ranks MORE candidates than `TOP_N_STOCKS` - with
  3 or fewer ranked, top-N and bottom-N are the identical slice. Set
  `SELECT_BOTTOM_N_STOCKS=false` (`FUTURES_SELECT_BOTTOM_N_STOCKS=false`
  for Futures) to revert to the original strongest-mover selection
  without a code change.

  Verified fully offline (mocked `dhan_wrapper.get_day_change_pct`, zero
  real network calls reachable) in
  `/private/tmp/.../scratchpad/test_bottom_n_selection.py`: both packages,
  both option types, bottom-N correctly selecting the weakest end of the
  ranking with the flag on, top-N/legacy behavior restored with it off,
  and bottom-N/top-N producing the identical result when candidates don't
  exceed `TOP_N_STOCKS` - 11/11 checks passed.

- **Supertrend exit signal moved from the underlying's 5-min timeframe to
  1-min, with the entry grace period also moved from 5 minutes to 1
  minute (26 Aug 2026, user request).** `SUPERTREND_INTERVAL_MINUTES`
  5->1 (Options/config.py, shared with Futures via the shared
  `dhan_client.py` - not duplicated there by design, see that package's
  own module docstring) and `SUPERTREND_ENTRY_GRACE_MINUTES` 5->1 in BOTH
  `Options/config.py` and `Futures/config.py` (this one - unlike the
  interval - genuinely is independent per package). No code changes were
  needed beyond the two config values: `dhan_client.refresh_supertrend_
  signal()` already reads `SUPERTREND_INTERVAL_MINUTES` for both the
  candle-fetch interval and the still-forming-candle-drop check, and
  `trading_engine._supertrend_signal_for()` already reads `SUPERTREND_
  ENTRY_GRACE_MINUTES` for the grace comparison - both fully config-driven
  already.

  Net effect: the Supertrend exit now reacts to much shorter-term
  reversals (1-min closes instead of 5-min) and can fire as early as 2
  minutes after entry (skip the entry candle + 1 grace candle, same
  relative shape as the old 5-min/5-min pairing which allowed a trigger
  starting 10 minutes after entry) instead of needing up to 10 minutes.
  This is inherently a noisier, more twitchy signal than 5-min - expect
  more Supertrend exits overall, including some that would have been
  filtered out as short-term noise on the old 5-min timeframe.

  **Important side effect NOT part of what was requested, left unchanged
  but flagged**: `SUPERTREND_MIN_WARMUP_CANDLES` (still 20, see bug
  #10/#16 above) is expressed in CANDLES, not minutes. At the old 5-min
  interval, 20 candles meant a ~100-minute warmup (signal not trusted
  until ~10:55). At the new 1-min interval, the SAME 20-candle count now
  completes warmup at ~09:35 - a much shorter window than the one bug
  #10/#16's fix was actually validated against. This isn't necessarily
  wrong, but it is genuinely untested at this new, faster interval - if
  Supertrend exits start looking biased or erratic particularly in the
  first 20-30 minutes of a session, this is the first thing to revisit
  (e.g. raising `SUPERTREND_MIN_WARMUP_CANDLES` to restore something
  closer to the original ~100-minute real-world warmup window - at 1-min
  candles that would be closer to 100 than 20).

  Verified fully offline (mocked `dhan_wrapper`'s Supertrend cache reads,
  zero real network calls reachable - `_supertrend_signal_for()` is
  otherwise a pure function) in
  `/private/tmp/.../scratchpad/test_supertrend_1min.py`: both packages,
  both option types, the entry-candle-itself never triggering, the exact
  grace-boundary candle still not triggering (strict `>`), the candle one
  step past grace correctly triggering, a favorable-direction signal
  never triggering regardless of timing, and no cached signal yet
  correctly not being treated as an exit signal - 23/23 checks passed.

- **`MAX_LOSS_PER_TRADE_RS` lowered 1500->1000, `PROFIT_PROTECTION_
  THRESHOLD_RS` lowered 1500->1200 (both Options and Futures), user
  request 26 Aug 2026.** Third change to the loss cap this day (3000->
  2000->1500->1000) and second to the profit-protection threshold (2000->
  1500->1200) - the two are now intentionally asymmetric (1000 loss cap
  vs 1200 profit-protection arm point) rather than mirrored 1:1 as they
  were at every prior value. No code changes needed - both
  `_exit_reason_for()` implementations already read these two config
  values directly with no hardcoded numbers anywhere.

  Net effect: MAX_LOSS_HIT now fires ~33% sooner in rupee terms than the
  prior 1500 cap, tightening per-trade downside further. PROFIT_
  PROTECTION_HIT now arms at a lower profit level (1200 instead of 1500)
  but the gap between the two thresholds is narrower than before (200 Rs
  vs the old 0 Rs when both were 1500) - a position now has a real (if
  narrow) window where it's profitable but neither threshold has fired.

  Verified fully offline in the existing `test_max_loss_exit.py` and
  `test_profit_protection.py` suites (in scratchpad, both already written
  to compute their boundary values off the live config value rather than
  hardcoded literals precisely for this kind of repeated threshold churn) -
  only each suite's one literal "defaults to N" assertion needed updating
  to the new numbers; every boundary/priority-ordering check re-passed
  unchanged. 18/18 and 30/30 checks passed respectively.

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
