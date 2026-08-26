# Chartink → Dhan Algo Bot

Receives Chartink scanner webhook alerts on two endpoints - a bullish scan
(buys ATM CE) and a bearish scan (buys ATM PE) - on the top-3 %-change
stocks in each alert, and manages exits (target / stop-loss / trailing
stop-loss / Supertrend / EOD square-off) automatically - using Dhan's
[Dhan-Tradehull](https://pypi.org/project/Dhan-Tradehull/) package for REST
calls and the underlying [`dhanhq`](https://pypi.org/project/dhanhq/) SDK's
WebSocket classes for live order updates and market data.

This is the Dhan counterpart to the Groww version of this bot (see the
`main` branch) - same strategy, ported to a different broker.

## Files
| File | Purpose |
|---|---|
| `main.py` | Shared entry point - the `FastAPI` app instance, strategy-agnostic endpoints (`/health`, `/incidents`), and composes each strategy's router + lifespan onto the app |
| `watchdog.py` | Standalone health-check watchdog (own systemd unit) - see NOTES.md |
| `Options/option_main.py` | Options strategy's FastAPI router + lifespan - webhook endpoints + status endpoints, mounted onto `main.py`'s app |
| `Options/trading_engine.py` | Ranking, entry, exit-condition monitoring, square-off logic, AMO order sync |
| `Options/dhan_client.py` | Thin wrapper over Tradehull (REST) + dhanhq's `OrderUpdate`/`MarketFeed` (WebSocket) |
| `Options/position_store.py` | In-memory state: live positions, daily traded-symbols dedup, orders, capacity cap |
| `Options/config.py` | All tunables for the options strategy, sourced from environment variables |
| `Options/paper_webhook.py` | Second, independent Chartink endpoint (`/chartink/webhook-papertrade`) for evaluating a new scan before trusting it with real money - **paper trading only**, reuses the real strategy's own ranking/ATM/exit logic so only the new scan's stock-picking is under test |
| `IndexScalping/index_main.py` | Index scalping strategy's FastAPI router + lifespan - **paper trading only**, no real orders placed |
| `IndexScalping/paper_engine.py` | Signal (opening-range breakout + EMA momentum on NIFTY/BankNifty) + paper entry/exit logic, gross/net P&L tracking |
| `IndexScalping/config.py` | All tunables for the scalping strategy, sourced from environment variables |
| `CopperOptions/copper_main.py` | Copper (MCX) options strategy's FastAPI router + lifespan - **paper trading only**, no real orders placed |
| `CopperOptions/paper_engine.py` | Gap + daily-RSI + dual-Supertrend signal, paper entry/exit logic, expiry-cycle rolling |
| `CopperOptions/config.py` | All tunables, including `STRATEGY_ENABLED` (independent on/off switch) |
| `Futures/futures_main.py` | Futures strategy's FastAPI router + lifespan - **PLACEHOLDER** (buys ATM CE options via Options'-identical mechanics until real futures-contract buying replaces it), real orders, own separate position pool |
| `Futures/trading_engine.py` | Near-verbatim copy of `Options/trading_engine.py`'s ranking/entry/exit logic against this package's own config/position_store |
| `Futures/position_store.py` | Futures' own independent in-memory state - separate capacity/dedup from Options' |
| `Futures/config.py` | All tunables for the Futures strategy, `FUTURES_`-prefixed env vars |
| `Futures/dhan_client.py` | Re-exports `Options.dhan_client.dhan_wrapper` - reuses the one authenticated Dhan connection rather than opening a second |
| `.env` | Your local credentials/config (gitignored - never commit this) |

A future strategy would live in its own top-level package the same way
`Options/`, `IndexScalping/`, and `CopperOptions/` do, exporting `router` +
`lifespan` and getting mounted in `main.py` alongside them - see NOTES.md's
design-decision entries for why this split exists.

## Setup

```bash
uv sync
cp .env .env.local  # or just edit .env directly with your real credentials
```

Get your **Client ID** and generate an **Access Token** from
[web.dhan.co](https://web.dhan.co) → My Profile → DhanHQ Trading APIs, then
fill in `.env`:

```
DHAN_CLIENT_ID=...
DHAN_ACCESS_TOKEN=...
```

Run with:

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

Expose it to the internet (e.g. via `ngrok http 8000` or a real deployment),
and set that public URL + `/chartink/webhook` (bullish scan) and/or
`/chartink/webhook-sell` (bearish scan) as your Chartink scanners' webhook
URLs — matching the `webhook_url` field in your sample payload.

## How the strategy works

1. **Webhook ingestion** — `POST /chartink/webhook` (bullish scan, buys ATM
   CE) and `POST /chartink/webhook-sell` (bearish scan, buys ATM PE) both
   accept the standard Chartink payload (`stocks`, `trigger_prices`,
   `triggered_at`, `scan_name`, `scan_url`, `alert_name`, `webhook_url`) and
   share the same entry/exit/dedup machinery and position pool - a symbol
   already open from one blocks the other from also entering it - but each
   has its own capacity cap (`MAX_LIVE_POSITIONS_CE` /
   `MAX_LIVE_POSITIONS_PE`), so a run of alerts on one side can't crowd
   out capacity for the other.

1b. **Entry cutoff** — `POST /chartink/webhook`, `/chartink/webhook-sell`,
   and `/chartink/webhook-futures` all refuse to open new positions once
   `config.ALLOWED_TRADING_TIME` (default `11:30`) has passed, but only
   when `config.ENABLE_TRADING_TIME_LIMIT` is `true` — when `false`
   (default), new entries are allowed all day up to market
   hours/`SQUARE_OFF_TIME`, unaffected by this flag. Independent of
   `SQUARE_OFF_TIME`, which governs closing *existing* positions, not
   opening new ones.

   **What happens to a position still open when the cutoff passes:**
   nothing — it's untouched. `is_past_allowed_trading_time()` is only
   checked in the webhook handlers, before ranking/entering anything new;
   it's never referenced anywhere in the exit path
   (`monitor_loop`/`_check_one_position`/`on_price_tick`/`_square_off_all`).
   A position opened at, say, 11:05 keeps getting evaluated on every exit
   condition exactly as if this feature didn't exist - target, hard/
   trailing/dynamic stop-loss, the `MAX_LOSS_PER_TRADE_RS` rupee cap, and
   the Supertrend reversal exit all keep firing normally, right up through
   `SQUARE_OFF_TIME` (which force-closes it then, same as any other day).
   The cutoff's only effect is blocking a *new* alert after 11:30 from
   opening something new - it has no interaction with anything already
   live.

2. **Top-N by %change** — on receipt, `rank_and_pick_top_stocks()` fetches
   OHLC data for each stock and ranks by day's %change - highest first for
   the bullish webhook, lowest/most negative first (biggest decliners) for
   the bearish one - taking the top `TOP_N_STOCKS`.

3. **Entry** — for each ranked stock that doesn't already have an open or
   in-flight position (and there's capacity), `Tradehull.ATM_Strike_Selection()`
   picks the ATM CE or PE contract (CE for `/chartink/webhook`, PE for
   `/chartink/webhook-sell`) for the nearest expiry, and a MARKET BUY is
   placed. Outside market hours this is automatically placed as an **AMO**
   (After Market Order) — Dhan requires this to be an explicit flag at
   placement time, unlike some brokers that auto-detect it.
   - **Expiry-day roll-forward** — stock options only have a monthly
     series, and Dhan blocks new positions in one on its own expiry day
     (NSE moved every single-stock contract's monthly expiry to the last
     Tuesday of the month from 1-Sept-2025). If the nearest contract
     expires today, `get_atm_option()` automatically retries with the next
     listed expiry and trades that instead, rather than skipping the stock
     for the day — see NOTES.md bug #28.

4. **Exit monitoring** — a background loop polls every
   `MONITOR_INTERVAL_SECONDS` and exits a leg on:
   - `MAX_LOSS_PER_TRADE_RS` absolute rupee-loss cap (default ₹2,000,
     checked first, ahead of every other exit condition below) — exits
     immediately once `(entry_price - ltp) * quantity` reaches this, independent
     of the percentage stop-loss (a low-premium/high-quantity leg can lose
     far more than this in rupees before its own percentage SL would fire)
   - `+TARGET_PCT` target
   - `PROFIT_PROTECTION_THRESHOLD_RS` rupee profit-lock (default ₹2,000,
     checked right after target) — once a trade's peak unrealized profit
     (`(highest_price - entry_price) * quantity`) has exceeded this, exits
     the moment price is off that peak *at all*, however small the dip -
     deliberately no drawdown tolerance once armed. A full `TARGET_PCT`
     hit still takes priority over this.
   - `-STOP_LOSS_PCT` hard stop loss
   - `TRAILING_SL_PCT` continuous trailing stop (trails the peak price in
     the trade's favor) - set `ENABLE_TRAILING_SL=false` to disable
   - stepped/"ratchet" stop (every step % the option's own premium climbs
     from entry, the floor moves up `DYNAMIC_SL_INCREASE_PCT`) - step
     width is set separately per leg, `DYNAMIC_SL_STEP_PCT_CE` /
     `DYNAMIC_SL_STEP_PCT_PE` - stacks with the continuous trailing stop
     above, set `ENABLE_DYNAMIC_SL=false` to disable. `TARGET_PCT` is
     never touched by either trailing mechanism.
   - the underlying's 5-min Supertrend turning against the position's
     direction - set `ENABLE_SUPERTREND_EXIT=false` to disable
   - `SQUARE_OFF_TIME` hard square-off of everything still open - only
     when `ENABLE_SQUARE_OFF=true` (default). When `false`, there is no
     forced end-of-day exit at all: a position rides past market close and
     keeps being evaluated on every rule above once the next session's
     ticks resume, instead of being flattened and re-entered fresh each
     day. Pairs with `OPTIONS_PRODUCT=MARGIN` (Dhan-Tradehull's code for
     what's commonly called NRML/carry-forward - "NRML" itself is not a
     value it accepts) instead of the default `"MIS"`, since MIS otherwise
     implies a same-day-only position. **The real cost of turning this
     off: zero exit protection while the market is shut** - a position is
     fully exposed to whatever gap happens by the next session's open,
     with no automated response possible during that window. See NOTES.md's
     design-decision entry for the backtest results that motivated this
     and the (already-fixed) day-boundary state bug it required fixing
     first.
   - **Friday carve-out** — even when `ENABLE_SQUARE_OFF=false`, Friday
     still gets its own mandatory cutoff via `ENABLE_FRIDAY_SQUARE_OFF`
     (default `true`) + `FRIDAY_SQUARE_OFF_TIME` (default `15:20`): no new
     entries past that time and every open position force-closed then, so
     nothing carries into the weekend gap - a much longer, riskier window
     than a single overnight one. Monday–Thursday are unaffected by this
     flag; `ENABLE_SQUARE_OFF=true` (every day) still takes priority over
     it when set. See NOTES.md's design-decision entry.

5. **AMO order lifecycle** — a BUY/SELL placed outside market hours doesn't
   fill immediately; `_sync_pending_orders()` re-checks queued AMO orders
   each tick and promotes a filled BUY into a live `Position` (or closes a
   filled SELL), instead of assuming a fill that hasn't happened yet.

6. **Duplicate-order protection** — blocks a new entry only while a
   position for that symbol is genuinely open or in-flight, not for the
   rest of the day; once it closes, a later alert for the same stock is
   free to enter again.
   - `PositionStore.reserve_symbol()` atomically checks + claims a symbol
     before any network I/O, closing a race where two near-simultaneous
     webhook deliveries for the same stock could both place an order.
     `close_position()` releases the claim so the symbol can be re-entered.
   - `has_open_position_for_underlying()` double-checks the broker's own
     portfolio right before entry, catching duplicates from another process
     instance or a manual trade.
   - `reconcile_broker_positions()` runs once at startup and imports any
     position already open at Dhan into local state, so a restart mid-day
     doesn't lose track of it and re-enter.

7. **Real-time order/position tracking** — `dhanhq.OrderUpdate` (order
   status pushes) and `dhanhq.MarketFeed` (LTP ticks) run as background
   WebSocket connections; every REST call site checks the socket cache
   first and falls back to REST polling if no tick/push has arrived yet.
   Set `ENABLE_WS_FEED=false` to disable the sockets entirely and run
   REST-only.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /chartink/webhook` | Bullish scan entry point - buys ATM CE |
| `POST /chartink/webhook-sell` | Bearish scan entry point - buys ATM PE |
| `POST /chartink/webhook-papertrade` | Paper-trading entry point for a second, unproven Chartink scan - bullish/CE only, places no real orders |
| `GET /positions` | Live + closed positions for today |
| `GET /orders` | Every order placed today, with Dhan's real order_status |
| `GET /papertrade/trades` | Results of the paper-trading webhook above - open position, completed trades, win rate |
| `POST /chartink/webhook-futures` | Futures strategy entry point - PLACEHOLDER (buys ATM CE options), real orders, own separate position pool |
| `GET /futures/positions` | Futures strategy's own live + closed positions |
| `GET /futures/orders` | Futures strategy's own orders today |
| `POST /futures/square-off-now` | Futures strategy's own manual kill-switch |
| `GET /health` | Liveness check |
| `POST /square-off-now` | Manual kill-switch: closes every live position immediately |

## Known limitations / things to verify against a live account

- **Order-update WebSocket payload schema isn't fully documented.** REST
  (`get_order_by_id`) is treated as the source of truth for order status;
  the socket is only used as a fast-path cache with defensive field-name
  fallbacks. See the comment on `DhanWrapper._order_snapshot_from_cache`.
- **Day-change % assumes Dhan's OHLC response shape**
  (`{"last_price": ..., "ohlc": {"close": ...}}`) - verify against a live
  call if ranking looks wrong.
- **`Tradehull.order_placement()` swallows its own errors** — on failure it
  prints to console/log and returns `None` rather than raising, so the
  underlying reason for a failed placement (as opposed to a REJECTED status
  on an order that *did* get an order id) is only visible in Tradehull's own
  console output, not in our exception message.
- **`Dependencies/`** — Tradehull writes its own log files and a daily
  token cache into a `Dependencies/` folder in the working directory. It's
  gitignored; harmless, just noise.

This has not been run against a live Dhan account - treat it as
ready-to-test rather than battle-tested, the same caveat that applied to
the Groww version when it was first built.
