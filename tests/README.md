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
