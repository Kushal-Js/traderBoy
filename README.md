# Chartink → Groww Algo Bot

Receives Chartink scanner webhook alerts, buys ATM options on the top-3
%-change stocks in the alert, and manages exits (target / stop-loss /
trailing stop-loss / EOD square-off) automatically.

## Files
| File | Purpose |
|---|---|
| `main.py` | FastAPI app — webhook endpoint + status endpoints + app lifecycle |
| `trading_engine.py` | Ranking, entry, exit-condition monitoring, square-off logic |
| `groww_client.py` | Thin wrapper over the `growwapi` SDK (auth, quotes, ATM selection, orders) |
| `position_store.py` | In-memory state: live positions, daily traded-symbols dedup, capacity cap |
| `config.py` | All tunables, sourced from environment variables |
| `.env.example` | Template for required environment variables |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your Groww credentials
```

Get your **TOTP token + secret** (recommended, no expiry) from the
[Groww Cloud API Keys page](https://groww.in/trade-api/api-keys), an
API key + secret if you prefer that flow (requires re-approval daily), or
generate an **access token** directly on that page and set
`GROWW_AUTH_MODE=TOKEN` + `GROWW_ACCESS_TOKEN` (also expires daily — you'll
need to refresh it each day).

Export the env vars (or use `python-dotenv` / your process manager) and run:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Expose it to the internet (e.g. via `ngrok http 8000` or a real deployment),
and set that public URL + `/chartink/webhook` as your Chartink scanner's
webhook URL — matching the `webhook_url` field in your sample payload.

## How it maps to your requirements

1. **Webhook ingestion** — `POST /chartink/webhook` accepts exactly the
   payload shape you gave (`stocks`, `trigger_prices`, `triggered_at`,
   `scan_name`, `scan_url`, `alert_name`, `webhook_url`).

2. **Top-3 by %change** — on receipt, `rank_and_pick_top_stocks()` calls
   Groww's `get_quote()` for each stock in the alert (CASH segment) and
   sorts by `day_change_perc`, keeping the top 3 (`TOP_N_STOCKS`).

3. **ATM option, market order, BUY** — for each qualifying stock,
   `get_atm_option()`:
   - finds the nearest (>= today) expiry for that underlying from the
     instruments master (`get_all_instruments()`),
   - pulls the option chain (`get_option_chain()`),
   - picks the strike closest to the underlying's current LTP,
   - places a `BUY` `MARKET` order (`place_order()`, `product=MIS`,
     `segment=FNO`) for `lot_size × QUANTITY_LOTS`.

   Default leg is **CE** (bullish breakout style alert). Set `OPTION_TYPE=PE`
   in `.env` if you're wiring this to a bearish/breakdown scan instead.

4. **Exit logic** (`monitor_loop()`, polls every `MONITOR_INTERVAL_SECONDS`):
   - **Target**: LTP ≥ entry × 1.10 → exit (`TARGET_HIT`)
   - **Stop-loss**: LTP ≤ entry × 0.97 → exit (`STOP_LOSS_HIT`)
   - **Trailing stop-loss**: as the option's LTP makes new highs after
     entry, the effective stop trails `1%` below the peak
     (`highest_price × 0.99`), but never below the hard 3% stop. So the
     "floor" only ever ratchets upward — it never trails a *loss* further
     down. Whichever of the two (hard SL vs trailing SL) is currently
     higher is the one that's live.
   - **EOD square-off**: at `15:15 IST` (configurable), every remaining
     open position is closed with a `SELL` `MARKET` order regardless of
     P&L.

5. **Duplicate / capacity control** (`position_store.py`):
   - `traded_symbols_today` — once a stock has been entered (even after
     it's later closed), it's ignored for the rest of the day if it
     reappears in a new alert.
   - `MAX_LIVE_POSITIONS = 3` — enforced both when ranking (webhook won't
     even bother processing if capacity is already 0) and per-stock during
     entry (if two alerts race, the cap still holds).
   - State resets automatically at the start of a new calendar day
     (`maybe_reset_for_new_day()`).

## Observability / manual control

- `GET /positions` — live + closed positions for the day, including each
  position's current trailing-SL floor.
- `GET /health` — liveness check.
- `POST /square-off-now` — manual kill-switch to flatten everything
  immediately, independent of the 3:15 PM timer.

## Important caveats — please read before going live

- **This is starter/reference code, not a certified production system.**
  Real-money automated options trading carries real risk (slippage,
  partial fills, API downtime, gaps at market open, etc.) — test thoroughly
  in small size before trusting it with real capital.
- **In-memory state**: if the process restarts mid-day, it forgets open
  positions and the daily dedup list. For real deployment, back
  `position_store.py` with SQLite/Redis so state survives a restart, and
  reconcile against `get_order_list()` / `get_positions_for_user()` on boot.
- **Market orders on options can slip**, especially in illiquid strikes —
  consider adding a liquidity/OI check before entry if you extend this.
- **Rate limits**: Groww limits Orders to 10/sec & 250/min, Live Data to
  10/sec & 300/min (see the Introduction page). With only 3 concurrent
  positions and a 5s poll interval this bot stays well within limits, but
  don't lower `MONITOR_INTERVAL_SECONDS` too aggressively.
- **Options quantity** is `lot_size × QUANTITY_LOTS` — no margin/available-
  funds check is performed before placing orders. Consider calling
  `get_order_margin_details()` / `get_available_margin_details()` first if
  you want a pre-trade margin guard.
- Trigger prices from the Chartink payload are currently only logged
  (available via `payload.trigger_price_list()`) — the strategy as
  specified trades purely on %change ranking, not on the trigger price
  itself. Wire it in if you want a price-confirmation filter.
