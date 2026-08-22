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

## Design decisions

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
  option type - `DYNAMIC_SL_STEP_PCT_CE` / `DYNAMIC_SL_STEP_PCT_PE`, both
  default 7% - since backtesting found the same width doesn't necessarily
  suit both legs equally (see bug #13). `TARGET_PCT` is untouched; this
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
