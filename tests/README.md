# tests/

Deep integration tests for DhanBoy. These exercise the **real** production
code paths - the actual webhook handler, `PositionStore` locking, entry/exit
logic, retry wrapping, and reconciliation attribution - not reimplementations
or shallow unit mocks. The only thing ever faked is the Dhan **network**
boundary (ranking's price fetch, ATM option lookup, broker-position lookup,
order placement/fill). Every bit of real logic runs for real.

Not wired into any CI or deploy step, and not pytest-based - these are plain
`asyncio.run(main())` scripts, run manually whenever you want confidence that
a change hasn't broken concurrency, capacity, retry, or reconciliation
behavior. Good times to run them: after touching `position_store.py`,
`trading_engine.py`, or `trade_history.py` in either `Options/` or
`Futures/`; before a deploy you're nervous about; or just periodically for
peace of mind.

## Files

| File | Covers |
|------|--------|
| `test_deep_integration.py` | Concurrent entry, duplicate-webhook races, retry-on-transient-failure, real exit paths, reconciliation attribution, malformed payloads |
| `test_choppy_stocks.py` | `choppy_stocks.py`'s manually-maintained exclusion list (seed/never-overwrite behavior, read/write round-trip, fails-open + picks-up-manual-edits-live), and its wiring into the real webhook handler (choppy stocks excluded before ranking, non-choppy stocks unaffected, audit log stays unfiltered) |
| `test_risk_threshold_cutoff.py` | The before/after-11:30 split of `MAX_LOSS_PER_TRADE_RS`/`PROFIT_PROTECTION_THRESHOLD_RS` in both Options and Futures - correct value on each side of the cutoff, the exact boundary instant, and `_exit_reason_for` itself firing at the right threshold |
| `test_luxury_package.py` | The Luxury package (a same-account Options duplicate) - concurrent CE entry, the PE webhook entering PUTs on its own capacity pool, duplicate-delivery races, real exit paths, three-way (Options/Futures/Luxury) reconciliation with zero cross-contamination, malformed payloads |
| `test_cross_strategy_registry.py` | `cross_strategy_registry.py`'s claim/release semantics, and REAL concurrent races (via `asyncio.gather`, not simulated) between Options/Futures/Luxury for the same stock at two levels: `_process_one_entry` directly, and the FULL real webhook handler functions (ranking, capacity, choppy-stocks filter, order placement) - exactly one winner every time at both levels, unrelated symbols/stocks in the same alert batch never affected, a mixed multi-stock scenario confirming each package's own unique stock is unaffected by a shared stock racing alongside it |
| `test_get_option_ltp_retry.py` | `get_option_ltp()`'s retry wrapper (added 31 Aug 2026 after a lag audit found 46 unretried transient LTP-fetch failures in one trading day) - recovers from a transient failure, still raises after exhausting retries, and uses a real ~1.5s backoff (not a tight loop) |
| `test_swing_package.py` | The Swing package (futures + ATM PE hedge "basket") - disabled-by-default flag placing zero orders, a full successful all-or-nothing entry, both rollback cases ("neither" when the futures leg fails; the futures leg unwound via a compensating SELL when the PE leg fails after filling), configurable basket capacity, the watchlist webhook, confirms Swing no longer shares `cross_strategy_registry` with Options (both can now enter the same underlying at once, added 1 Sep 2026) while Swing's own `basket_store.reserve_symbol` dedup still fully prevents a Swing-vs-Swing double entry, and `get_futures_contract()`'s nearest-expiry/hyphenated-underlying resolution |
| `test_swing_integration.py` | Full end-to-end Swing flows on top of the above: multi-stock webhook entry with a mixed capacity outcome, a complete enter-via-webhook-then-manual-square-off lifecycle verified against `trade_history`, a realistic 4-way reconciliation (Options/Futures/Luxury/Swing all with real broker positions at once), the "unpaired leg" safety behavior (a lone futures leg is never reconstructed into a partial basket), and the watchlist/enter webhooks operating independently |
| `test_swing_signal_logic.py` | Swing's entry/exit signal (added 31 Aug 2026, entry's price gate RELAXED 1 Sep 2026 from a strict gap-up to "at/above yesterday's close") - crossover detection correctness, a genuine crossover proven through the real `_compute_supertrend` against synthetic candle data, the price-confirmation gate's own caching/latching/invalidation (confirmed at market open on open >= prev close, OR later via a cheap intraday LTP check once prev close is cached, LATCHES permanently once confirmed, never re-fetches once latched), the entry rule's full price-gate + 5-min/1-min truth table (including the gate correctly blocking an otherwise-perfect Supertrend signal), the exit rule firing only on a genuine crossed-below, and full monitor-loop-shaped auto-entry/auto-exit flows (a real signal genuinely enters/exits a basket, not a stub) |
| `test_swing_watchlist_file.py` | Swing's file-backed watchlist (`data/watchlist`, added 31 Aug 2026) - adds/uppercases symbols from a real file, ignores blank lines/comments, never duplicates, fails open on a missing file, picks up a hand-edit with no restart needed, and round-trips the real seed file this task created (AUROPHARMA/OFSS/TORNTPHARM/VEDL) |
| `test_swing_paper_engine.py` | Swing's paper-trading engine (added 1 Sep 2026) - a full simulated entry+exit never calls `place_market_order`/`wait_for_order_result` (proven by making both raise if called), a successful entry prices both legs via `get_option_ltp` and records the basket, a PE-leg pricing failure aborts with nothing recorded (no partial paper position), exit computes correct per-leg/total P&L and persists to its own on-disk log (`swing_paper_trades`) fully isolated from real trade history, the poll loop is a no-op while `PAPER_TRADING_ENABLED` is False, a full poll-loop-shaped auto-entry-then-exit via the real (unchanged) signal functions, and (added same day) real per-leg margin + account fund-snapshot logging at entry - flows correctly to the persisted record, the combined total is only ever a naive sum of both legs (never partial), and a margin/funds fetch failure never aborts the paper entry the way a price-fetch failure does |
| `test_margin_and_funds.py` | `get_margin_required()`/`get_fund_limits()` (`Options/dhan_client.py`, added 1 Sep 2026) against a fake `self.client.Dhan` - returns the real `data` dict on success, RAISES on a failure status (never the silent `0` Tradehull's own margin_calculator() wrapper would return), and retries a transient failure via the shared `_retry()` helper |
| `test_daily_reentry_cap.py` | Options/Futures/Luxury's daily re-entry cap (added 1 Sep 2026) - `trade_history.count_opened_today()`'s own counting/isolation-by-strategy/isolation-by-symbol correctness; a full real 3x enter->exit cycle for Options via `_process_one_entry`/`close_position` followed by a real 4th attempt correctly rejected (`daily_reentry_cap_reached`) with zero orders placed, while a different symbol is unaffected; the cap SURVIVING a simulated restart (a brand-new `PositionStore` with the same on-disk log still blocks the 4th entry - proving it isn't an in-memory counter); Futures'/Luxury's own independent cap wiring; and that the cap is genuinely configurable |
| `test_swing_events_log.py` | Swing's durable event log (added 1 Sep 2026, right after Swing went live) - a full real sequential loop (enter->swap to PE->swap back to futures->manual square-off) produces exactly the 4 expected events in order with the right reasoning/price detail; basket mode's own entry/exit produce `BASKET_ENTERED`/`BASKET_EXITED`; and a failure mid-swap (PE unresolvable after futures is already sold) still produces a `SEQUENTIAL_LEFT_FLAT` event rather than staying silent |
| `test_swing_sequential_mode.py` | Swing's "sequential" trading mode (added 1 Sep 2026; `config.STRATEGY_MODE`'s own default later moved on to "basket_hedge", see `test_swing_basket_hedge_mode.py` below - this file's own test 1 now just confirms "sequential" remains a valid, fully switchable value, not that it's the current default) - a full REAL 2-loop-iteration cycle (NONE->FUTURES->PE->FUTURES->PE->NONE) through `_enter_futures_for_stock`/`_swap_futures_to_pe`/`_swap_pe_to_futures`/`_exit_pe_to_watching`/`_evaluate_pe_exit_signal`, verifying the exact 8-order sequence placed and all 4 legs correctly opened+closed in `trade_history` (confirming a PE loss-cap exit correctly does NOT re-buy futures - the ambiguity resolved via AskUserQuestion); capacity staying reserved through every swap but released only by the loss-cap exit; startup reconciliation recovering a LONE broker leg (the normal shape here, unlike basket mode); the manual kill-switch's mode-aware behavior; `monitor_loop`'s own per-tick mode isolation; and sequential PAPER trading mirroring the full state machine while never placing a real order, persisted to its own log fully isolated from basket mode's paper log |
| `test_swing_basket_hedge_mode.py` | Swing's "basket_hedge" trading mode (added 1 Sep 2026, now the default/active `config.STRATEGY_MODE` and the mode live with real money) - `config.STRATEGY_MODE` defaults to "basket_hedge"; a full REAL cycle (NONE->BASKET->PE_HEDGE->NONE via the loss-cap exit) through `_enter_basket_hedge_for_stock`/`_exit_basket_hedge_to_pe`/`_exit_pe_hedge_to_watching`/`_evaluate_pe_hedge_exit_signal`, verifying the exact 6-order sequence placed and all 3 legs correctly opened+closed in `trade_history` plus the full `swing_events` transition sequence; the PE hedge's profit-lock exit condition independently; the PE hedge's bare Supertrend-reversal exit condition independently (proven to fire even when the full entry-signal gate would not have - "even if buy signal is not yet triggered"); capacity blocking a duplicate entry mid-flight while a different symbol is unaffected; startup reconciliation handling all 3 leg shapes (a lone FUT as a degraded 1-leg BASKET - the real APLAPOLLO grandfathering case, exact entry_price verified - a lone PE as PE_HEDGE, and a matched FUT+PE pair as a normal 2-leg BASKET); the manual kill-switch closing every leg currently held by a position, mode-aware; and `monitor_loop`'s own 3-way per-tick mode isolation |
| `test_wait_for_order_result_price_fix.py` | `wait_for_order_result()`'s cache-vs-REST fill price fix (found live 1 Sep 2026 via Swing's own first-ever real entry, APLAPOLLO futures - a terminal WS order-update push was trusted directly including its own unverified-schema `average_fill_price` field, silently recording a REAL fill's entry_price as ₹0 while the broker's own REST record showed the correct fill the whole time) - against the REAL production function, shared by every real-money package (Options/Futures/Luxury/Swing): a terminal cache hit with an already-correct price still resolves via REST (unconditional, not "only when the cache looks wrong"); the exact live bug scenario (cache terminal + no price field) now returns REST's real price; no cache entry at all falls straight through to REST unaffected; and REST-not-yet-terminal falls back to the cache's own snapshot rather than hanging |
| `test_swing_daily_watchlist_prune.py` | Swing's daily watchlist prune (added 1 Sep 2026, runs once per trading day at/after 09:15 IST) - the new `_compute_ema` against a hand-verifiable series; `_evaluate_daily_trend_break` against REAL synthetic DAILY candle data through the actual shared `_compute_supertrend`/`_compute_ema`: a genuine daily Supertrend crossed-below fires and short-circuits before EMA is even checked, an isolated daily EMA(12) crossed-below fires independently, a non-crossing series and too-little-history both correctly return None; `_daily_watchlist_prune_tick`'s own once-per-day gating (nothing before 09:15, runs on the first eligible tick, a no-op on every later tick the same day, resets on a new trading day); a mixed watchlist pruned correctly (only the broken symbols removed, each with a durably-logged `WATCHLIST_PRUNED` reason); the feature flag disabling everything; and a per-symbol daily-data fetch failure being swallowed/logged without affecting other symbols in the same run |
| `test_swing_chartink_scan.py` | Swing's daily Chartink scan pull (added 1 Sep 2026, the ADD-side mirror of the prune above, runs once per trading day pre-market) - `fetch_scan_symbols_once` against a mocked `requests.Session` (the only thing faked - real GET-for-cookie/POST-the-clause mechanics run for real): a well-formed response parses correctly (symbols uppercased, cookie correctly URL-decoded before being echoed back as the header), and a missing XSRF cookie / network failure / malformed response all RAISE rather than returning a guessed empty list; `_run_chartink_watchlist_scan` adds genuinely new symbols to the REAL watchlist AND resets the stale-age clock (`last_confirmed_at`) for an already-present one it re-returns (the "unless fed in again" mechanism the stale-age prune below depends on), durably logging both in a `CHARTINK_WATCHLIST_SCAN_COMPLETED` event; `_daily_chartink_watchlist_scan_tick`'s own once-per-day gating, including the ONE behavior that deliberately differs from the trend-based prune's own gating - a fetch FAILURE does NOT mark the day as done, so the very next tick (same day) retries; and the feature flag disabling everything |
| `test_swing_stale_age_prune.py` | Swing's SECOND, AGE-based watchlist prune (added 1 Sep 2026, user request: "those stocks that were added 10 days earlier to be removed unless they are again fed in using chartink scan results") - `WatchlistStore.stale_symbols`'s own exact age boundary (>= N calendar days old is stale, one day under is not) against real backdated `WatchlistEntry` objects; `_stale_watchlist_age_prune_tick`'s own once-per-day gating at the same 09:15 IST slot as the trend-based prune; a mixed watchlist pruned correctly by age - a genuinely stale symbol removed, a fresh one survives, and (the crux of the feature) a symbol originally added 15 days ago but RECONFIRMED by the Chartink scan just today survives too, since its own clock is `last_confirmed_at`, not `added_at` - each real removal durably logged via a `WATCHLIST_STALE_AGE_PRUNED` event; and the feature flag disabling everything even for a symbol stale by 100 days |

## How to run

```bash
uv run python tests/test_deep_integration.py
uv run python tests/test_choppy_stocks.py
uv run python tests/test_risk_threshold_cutoff.py
uv run python tests/test_luxury_package.py
uv run python tests/test_cross_strategy_registry.py
uv run python tests/test_get_option_ltp_retry.py
uv run python tests/test_swing_package.py
uv run python tests/test_swing_integration.py
uv run python tests/test_swing_signal_logic.py
uv run python tests/test_swing_watchlist_file.py
uv run python tests/test_swing_paper_engine.py
uv run python tests/test_margin_and_funds.py
uv run python tests/test_daily_reentry_cap.py
uv run python tests/test_swing_sequential_mode.py
uv run python tests/test_swing_events_log.py
uv run python tests/test_swing_basket_hedge_mode.py
uv run python tests/test_wait_for_order_result_price_fix.py
uv run python tests/test_swing_daily_watchlist_prune.py
uv run python tests/test_swing_chartink_scan.py
uv run python tests/test_swing_stale_age_prune.py
```

Each takes a few seconds. Prints one PASS line per scenario, or raises an
`AssertionError`/exception on the first failure (nothing is caught/hidden).

## Safety

This suite can **never** place a real order, no matter how it's run -
`place_market_order` and `wait_for_order_result` are mocked in every single
scenario. It also never touches the real `trade_history.py` logs or the real
module-level `position_store` singletons: it points `trade_history.HISTORY_DIR`
at a scratch temp directory (cleaned up automatically at the end) and
constructs its own standalone `PositionStore` instances per test.

It also needs **no live Dhan session** - every Dhan network call is mocked,
so it can be run offline, repeatedly, anytime, with zero risk of tripping
Dhan's own authentication rate limiter (see "Why every Dhan call is mocked"
below).

## What each test covers

| # | Test | What it guards against |
|---|------|------------------------|
| 1 | Real concurrent entry through the real webhook handler | Capacity isn't respected under real `asyncio.gather` concurrency; ranking's top-N trim happening at the wrong stage |
| 2 | Duplicate webhook delivery race | Chartink (or a flaky network) delivering the same alert twice and the bot double-entering the same symbol |
| 3 | Transient Dhan failure mid-burst recovers | A transient failure on `get_open_fno_positions` during a concurrent entry burst causing a stock's entry to be silently abandoned instead of retried |
| 4 | Real exit path (target + stop-loss) | `_exit_reason_for`/`close_position` failing to close correctly, or not freeing the symbol for re-entry after close |
| 5 | Reconciliation with a realistic mixed scenario | Options and Futures reconciliation cross-contaminating each other's broker positions |
| 6 | Malformed webhook payloads rejected cleanly | A malformed Chartink payload (empty/missing fields) crashing the handler or silently passing through to order placement |

## Why every Dhan call is mocked, not just order placement

The first version of this suite used real (read-only) Dhan calls for ranking
and ATM lookup, and tripped Dhan's own authentication rate limiter
("Too many attempts. Please try again after sometime.") after the several
separate local test-script logins already run that same night. This was
confirmed to be a real operational constraint on Dhan's side, not a bug -
the live droplet's own already-authenticated session was completely
unaffected throughout. This version needs no live session at all, removing
that risk entirely, so it's safe to run anytime.

## Background

Written 31 Aug 2026 following the user's request to "do deep paper testing
to make sure everything works fine when market opens and real trades comes
in," after a night of fixing concurrency, retry, memory-leak, and feed
resilience issues found via code audit and dummy webhook testing. See
[NOTES.md](../NOTES.md) entries #43-52 for the full history of what each
scenario here is actually guarding against.
