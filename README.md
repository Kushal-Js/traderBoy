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
| `main.py` | FastAPI app — webhook endpoint + status endpoints + app lifecycle |
| `trading_engine.py` | Ranking, entry, exit-condition monitoring, square-off logic, AMO order sync |
| `dhan_client.py` | Thin wrapper over Tradehull (REST) + dhanhq's `OrderUpdate`/`MarketFeed` (WebSocket) |
| `position_store.py` | In-memory state: live positions, daily traded-symbols dedup, orders, capacity cap |
| `config.py` | All tunables, sourced from environment variables |
| `.env` | Your local credentials/config (gitignored - never commit this) |

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
   share the same entry/exit/dedup/capacity machinery and position pool -
   a symbol already open from one blocks the other from also entering it.

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

4. **Exit monitoring** — a background loop polls every
   `MONITOR_INTERVAL_SECONDS` and exits a leg on:
   - `+TARGET_PCT` target
   - `-STOP_LOSS_PCT` hard stop loss
   - `TRAILING_SL_PCT` trailing stop (trails the peak price in the trade's
     favor) - set `ENABLE_TRAILING_SL=false` to disable this and exit only
     on the target or the fixed hard stop loss
   - `SQUARE_OFF_TIME` hard square-off of everything still open

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
| `GET /positions` | Live + closed positions for today |
| `GET /orders` | Every order placed today, with Dhan's real order_status |
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
