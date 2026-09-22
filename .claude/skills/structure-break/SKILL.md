---
name: structure-break
description: Multi-timeframe (5m/15m/1h/1d) structure-break check for an NSE stock - a Python port of the BOSWaves "Smart Money Flow Cloud" Pine Script indicator's band-cross regime signal. Read-only analysis, not wired to any live strategy. Use when the user asks to check a symbol's "structure break", trend regime, or wants this indicator's signal for a stock across timeframes.
---

# structure-break

Runs `structure_break.py` (repo root) against live Dhan candle data and
reports, per timeframe, whether the symbol is in a bullish or bearish
"structure" regime, whether that regime just flipped ("broke") on the most
recently closed candle, and a trend-strength percentage.

## What "structure break" means here

This is **not** classic swing-structure analysis (higher-high/higher-low
breaks). It's a direct port of the Pine indicator pasted by the user on 22
Sep 2026 (BOSWaves' "Smart Money Flow Cloud", MPL-2.0) — a money-flow-
weighted adaptive ATR band around a smoothed EMA/ALMA baseline. A
"structure break" is a fully-closed candle's close crossing outside that
band, i.e. the indicator's own `switchUp`/`switchDown` (Buy/Sell label)
event. The name follows the user's own framing when this was requested —
price breaking the adaptive band "structure" it had been contained in.

Full math: `structure_break.py`'s module docstring and
`compute_structure_break()`. Defaults match the Pine indicator's own input
defaults exactly (length=34 EMA baseline, mfLen=24 money-flow window,
atrLen=14, minMult=0.9/maxMult=2.2 adaptive band range).

## How to run it

Requires a Dhan session. Running it from a local (non-systemd) process
never uses PIN+TOTP directly — that would mint a brand-new Dhan session
and kick out the live droplet bot's own current one (see trading-skills'
`incidents/2026-09-21-local-backtest-dhan-session-collision.md`). Instead,
set `HANDOFF_DHAN_ACCESS_TOKEN` to a token generated from Dhan's own
dashboard — **the user runs this themselves, in their own terminal; an
API token is never something Claude enters, handles, or is given via
chat**:

```bash
HANDOFF_DHAN_ACCESS_TOKEN='<token>' python3 structure_break.py RELIANCE
python3 structure_break.py RELIANCE --timeframes 5m,1h
```

Symbol must be an NSE equity trading symbol resolvable via
`dhan_wrapper._equity_security_id` (same lookup Supertrend/EMA-cross
signals use) — indices and options aren't supported by that lookup.
For an MCX commodity (e.g. COPPER), pass `mcx=True` to `fetch_timeframe`
(not exposed on the CLI yet) — it resolves the symbol's current MCX
futures contract instead, the same way `Swing/signals.py`'s own
`_underlying_reference` does. `fetch_timeframe` also takes an optional
`lookback_days_override` to widen history for a longer backtest window.

Programmatic use (e.g. to build a report across several symbols in one
Python session):

```python
from structure_break import analyze_symbol
results = analyze_symbol("RELIANCE", timeframes=("5m", "15m", "1h", "1d"))
for tf, r in results.items():
    if r.error or not r.warm:
        continue
    print(tf, r.last_regime, r.broke_this_bar, r.last_strength_pct)
```

`StructureBreakResult` fields worth reading: `last_regime` (1 bullish / -1
bearish), `broke_this_bar` ("up"/"down"/None — a fresh break on the last
closed candle), `bars_since_break`, `last_strength_pct`,
`retest_this_bar` ("bull"/"bear"/None — the indicator's retest dots).

## Data + correctness notes

- Candles come from `Options.dhan_client.dhan_wrapper` —
  `fetch_continuous_intraday` for 5m/15m/1h (a continuous multi-session
  series, never fragmented at day boundaries — see [[continuous-
  candles]]), `historical_daily_data` for 1d. Same path every real signal
  in this repo already uses.
- The still-forming last candle is always dropped before computing,
  same discipline `refresh_supertrend_signal` uses.
- EMA/ATR use this repo's existing SMA-seeded/Wilder convention
  (`dhan_client.py`'s `_compute_ema`/`_compute_supertrend`), not Pine's
  bar-0-seeded `ta.ema` — values converge to the Pine chart's after the
  warm-up window but won't match bar-for-bar right at the start of a
  freshly fetched window. Each timeframe fetches enough history
  (15/30/90 calendar days for 5m/15m/1h, 400 days for 1d) that the
  reported last bar sits well past that warm-up.

## What this is NOT

Read-only analysis only. It is never imported by Options/Futures/Luxury/
Swing, places no orders, and does not gate any live decision — per
[[feedback-live-trading-safety]], turning this into an actual entry/exit
signal for a live strategy is a separate, much bigger task (config
wiring, backtest verification, explicit go-ahead) that hasn't been done.
