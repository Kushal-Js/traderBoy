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

## Design decisions

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
