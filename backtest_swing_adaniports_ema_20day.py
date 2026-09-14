"""
User request 14 Sep 2026: backtest a NEW proposed Swing v2 entry/exit
signal on ADANIPORTS over the last 20 trading days, BEFORE deploying it
to the live droplet. This is NOT the currently-deployed regime+Supertrend
logic (see backtest_swing_adaniports_10day.py for that) - it's a
candidate replacement, confirmed via two explicit clarifying questions
before building this:

  1. "Does this new EMA20/EMA100 rule REPLACE the current entry signal
     (regime + Supertrend crossover)?" -> user answered: REPLACE ENTIRELY.
     Supertrend is not used for entries in this script at all.
  2. "Does the EMA20-vs-EMA100 square-off REPLACE the entire exit ladder,
     or sit alongside the existing risk caps?" -> user answered: KEEP THE
     RISK CAPS, add the EMA cross as a new trigger. MAX_LOSS_HIT/
     TARGET_HIT/PROFIT_PROTECTION_HIT/STOP_LOSS_HIT still apply FIRST,
     exactly as before; the EMA20/EMA100 cross REPLACES only the old
     SUPERTREND_REVERSAL exit (same position in the priority order).

New rule, exactly as specified by the user:
  - "If 5 min 20 EMA cross above 15 min 200 EMA then only buy the basket
     and square off when 20 EMA cross below 100 EMA"
  - "If 5 min 20 EMA cross below 15 min 200 EMA then only sell the basket
     and square off when 20 EMA cross above 100 EMA"

Two NEW indicators added (both on the 5-min underlying series, same
series the old Supertrend/regime signals used):
  - EMA(20) on 5-min closes
  - EMA(100) on 5-min closes
The existing 15-min EMA(200) is reused unchanged as the slow reference -
the user's "15 min 200 EMA" is exactly Swing/config.py's existing
REGIME_SLOW_INTERVAL_MINUTES=15 / REGIME_EMA_PERIOD=200 pairing, just
now compared against a CROSSOVER of the new 5-min EMA(20) instead of the
old level-comparison ("is fast EMA(200) above slow EMA(200)") + separate
Supertrend crossover.

Entry (this script, replacing _evaluate_entry_signal entirely):
  ema20(5min) crosses ABOVE ema200(15min, latest value at-or-before the
    5-min bar's own close) -> "BULLISH" signal
  ema20(5min) crosses BELOW ema200(15min) -> "BEARISH" signal
Then resolve_instrument_side(BASKET_TYPE, regime) - unchanged, same
Swing/position_store.py helper as the current live logic: BASKET_TYPE is
"futures" live, so BULLISH -> LONG (buy futures), BEARISH -> SHORT (sell
futures to open).

Exit ladder (unchanged risk caps, only the LAST condition is new):
  1. MAX_LOSS_HIT (>= SWING_MAX_LOSS_PROTECTION_RS, 4500 code default)
  2. TARGET_HIT (+/-20%, ENABLE_TARGET_EXIT on)
  3. PROFIT_PROTECTION_HIT (peak profit > 5000 deployed, giveback 2%
     deployed)
  4. STOP_LOSS_HIT (+/-20% hard stop)
  5. EMA_CROSS_SQUAREOFF (NEW - replaces SUPERTREND_REVERSAL): for a LONG,
     ema20(5min) crosses BELOW ema100(5min); for a SHORT, ema20(5min)
     crosses ABOVE ema100(5min). Same entry-candle-skip protection as
     every other crossover exit this session (only counts a crossover on
     a candle strictly after the position's own entry candle, using the
     at-or-before-minus-interval lookup this session's exit_ladder_
     backtest_helper.py established is necessary - idx_at_or_after would
     silently defeat this the same way it did there).

Same instrument/pricing architecture as backtest_swing_adaniports_10day.py:
signals computed off the equity series, fills/P&L computed off the
futures contract's own 1-min price series, simulation driven at 1-min
resolution off the FUTURES series (not the 5-min signal series) so
price-based exits get checked as often as the live poll loop actually
checks them - this was a real fidelity bug caught and fixed in the
10-day backtest before it was ever reported, carried forward correctly
here from the start.

NOT modeled (same standing disclosure as every backtest this session):
  SL-L broker-side stop-loss, cross_strategy_registry, funds check,
  MAX_CONCURRENT_TRADES (moot for a single-symbol watchlist).

This is a BACKTEST ONLY - no live code (Swing/signals.py,
Swing/trading_engine.py) has been touched. Deploying this as the live
Swing entry/exit logic is an explicit, separate step the user asked to
gate on reviewing these results first.

Run via SSH on the droplet (never locally while the live bot's own
session is active):
    uv run python backtest_swing_adaniports_ema_20day.py [DAYS_BACK_TRADING]
    (optional - default 20)
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from Options.dhan_client import dhan_wrapper, _compute_ema, _retry  # noqa: E402
from Swing import config as swing_config  # noqa: E402
from Swing.position_store import (  # noqa: E402
    resolve_instrument_side, unrealized_pnl_rs,
    target_price_for, hard_stop_for, price_past_target, price_past_hard_stop,
    giveback_floor, price_past_giveback_floor,
)

IST = ZoneInfo("Asia/Kolkata")
SYMBOL = "ADANIPORTS"

TEST_DAYS_BACK = int(sys.argv[1]) if len(sys.argv) > 1 else 20
SIGNAL_FETCH_DAYS_BACK = 90  # comfortably covers TEST_DAYS_BACK=20 plus 200-period-EMA warmup either way
FUTURES_FETCH_DAYS_BACK = 90

CACHE_DIR = Path("/private/tmp/claude-501/-Users-kushalgaur-Desktop-projects-trading-traderBoy/"
                  "60a0e686-2110-4e20-bad5-fe817a57a72a/scratchpad/swing_backtest_cache_adaniports_ema")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BASKET_TYPE = swing_config.BASKET_TYPE.upper()
QUANTITY_LOTS = swing_config.QUANTITY_LOTS
MAX_LOSS_PROTECTION_RS = swing_config.MAX_LOSS_PROTECTION_RS
PROFIT_PROTECTION_RS = swing_config.PROFIT_PROTECTION_RS
PROFIT_PROTECTION_GIVEBACK_PCT = swing_config.PROFIT_PROTECTION_GIVEBACK_PCT
TARGET_PCT = swing_config.TARGET_PCT
HARD_STOP_LOSS_PCT = swing_config.HARD_STOP_LOSS_PCT
ENABLE_TARGET_EXIT = swing_config.ENABLE_TARGET_EXIT
REGIME_FAST_INTERVAL_MINUTES = swing_config.REGIME_FAST_INTERVAL_MINUTES  # 5
REGIME_SLOW_INTERVAL_MINUTES = swing_config.REGIME_SLOW_INTERVAL_MINUTES  # 15
SLOW_EMA_PERIOD = swing_config.REGIME_EMA_PERIOD  # 200 - reused unchanged, "15 min 200 EMA"
FAST_EMA_PERIOD = 20   # NEW indicator this script adds
MID_EMA_PERIOD = 100   # NEW indicator this script adds

print(f"[backtest] SYMBOL={SYMBOL} BASKET_TYPE={BASKET_TYPE} TEST_DAYS_BACK={TEST_DAYS_BACK} "
      f"MAX_LOSS_PROTECTION_RS={MAX_LOSS_PROTECTION_RS} PROFIT_PROTECTION_RS={PROFIT_PROTECTION_RS} "
      f"GIVEBACK_PCT={PROFIT_PROTECTION_GIVEBACK_PCT} TARGET_PCT={TARGET_PCT} "
      f"HARD_STOP_LOSS_PCT={HARD_STOP_LOSS_PCT} ENABLE_TARGET_EXIT={ENABLE_TARGET_EXIT} "
      f"ENTRY=EMA{FAST_EMA_PERIOD}({REGIME_FAST_INTERVAL_MINUTES}min) x EMA{SLOW_EMA_PERIOD}"
      f"({REGIME_SLOW_INTERVAL_MINUTES}min) crossover "
      f"EXIT(new)=EMA{FAST_EMA_PERIOD}({REGIME_FAST_INTERVAL_MINUTES}min) x EMA{MID_EMA_PERIOD}"
      f"({REGIME_FAST_INTERVAL_MINUTES}min) crossover")


def idx_at_or_before(ts_list, target_ts):
    best = None
    for i, ts in enumerate(ts_list):
        if ts <= target_ts:
            best = i
        else:
            break
    return best


def fetch_equity_cached(interval_minutes: int, days_back: int) -> dict:
    cache_file = CACHE_DIR / f"{SYMBOL}_{interval_minutes}min_{days_back}d.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    security_id = dhan_wrapper._equity_security_id(SYMBOL)
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data,
                  security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
                  from_date=from_date, to_date=to_date, interval=interval_minutes)
    data = resp.get("data") or {}
    result = {"closes": data.get("close") or [], "timestamps": data.get("timestamp") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


def resolve_futures_contract() -> dict:
    df = dhan_wrapper.instruments()
    futs = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "FUTSTK")]
    matches = futs[futs["SEM_TRADING_SYMBOL"].apply(
        lambda s: dhan_wrapper._underlying_from_trading_symbol(str(s)) == SYMBOL
    )]
    if matches.empty:
        raise ValueError(f"No futures contract found for {SYMBOL}")
    nearest_expiry = matches["SEM_EXPIRY_DATE"].min()
    row = matches[matches["SEM_EXPIRY_DATE"] == nearest_expiry].iloc[0]
    return {
        "security_id": str(int(row["SEM_SMST_SECURITY_ID"])), "trading_symbol": str(row["SEM_CUSTOM_SYMBOL"]),
        "lot_size": int(float(row["SEM_LOT_UNITS"])),
    }


def fetch_futures_1min_cached(security_id: str) -> dict:
    cache_file = CACHE_DIR / f"FUT_{security_id}_1min.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=FUTURES_FETCH_DAYS_BACK)).strftime("%Y-%m-%d")
    resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data,
                  security_id=security_id, exchange_segment="NSE_FNO", instrument_type="FUTSTK",
                  from_date=from_date, to_date=to_date, interval=1)
    data = resp.get("data") or {}
    result = {"closes": data.get("close") or [], "timestamps": data.get("timestamp") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


def compute_cross_series(fast_values, ref_values):
    """Generic crossed_above/crossed_below series for two same-length,
    same-index-aligned value arrays (both already None-padded to line up
    with a shared timestamp array)."""
    n = len(fast_values)
    crossed_above = [False] * n
    crossed_below = [False] * n
    is_above = [None] * n
    for i in range(n):
        if fast_values[i] is not None and ref_values[i] is not None:
            is_above[i] = fast_values[i] > ref_values[i]
    for i in range(1, n):
        if is_above[i] is None or is_above[i - 1] is None:
            continue
        if (not is_above[i - 1]) and is_above[i]:
            crossed_above[i] = True
        if is_above[i - 1] and not is_above[i]:
            crossed_below[i] = True
    return crossed_above, crossed_below


def main():
    print(f"[backtest] Fetching {SYMBOL} equity {REGIME_FAST_INTERVAL_MINUTES}min/"
          f"{REGIME_SLOW_INTERVAL_MINUTES}min series ({SIGNAL_FETCH_DAYS_BACK}d lookback)...")
    fast = fetch_equity_cached(REGIME_FAST_INTERVAL_MINUTES, SIGNAL_FETCH_DAYS_BACK)
    slow = fetch_equity_cached(REGIME_SLOW_INTERVAL_MINUTES, SIGNAL_FETCH_DAYS_BACK)
    print(f"  fast({REGIME_FAST_INTERVAL_MINUTES}min): {len(fast['closes'])} bars, "
          f"slow({REGIME_SLOW_INTERVAL_MINUTES}min): {len(slow['closes'])} bars")

    ema20 = _compute_ema(fast["closes"], FAST_EMA_PERIOD)
    ema100 = _compute_ema(fast["closes"], MID_EMA_PERIOD)
    ema200_slow = _compute_ema(slow["closes"], SLOW_EMA_PERIOD)

    # Align the 15-min EMA200 onto the 5-min timeline (latest value at-or-
    # before each 5-min bar's own close) so it can be compared bar-by-bar
    # against the 5-min EMA20, same alignment pattern the OLD regime
    # computation already used for its own cross-timeframe comparison.
    ema200_on_fast_grid = [None] * len(fast["timestamps"])
    for i, ts in enumerate(fast["timestamps"]):
        s_idx = idx_at_or_before(slow["timestamps"], ts)
        if s_idx is not None and ema200_slow[s_idx] is not None:
            ema200_on_fast_grid[i] = ema200_slow[s_idx]

    entry_crossed_above, entry_crossed_below = compute_cross_series(ema20, ema200_on_fast_grid)
    exit_crossed_above, exit_crossed_below = compute_cross_series(ema20, ema100)

    fut = resolve_futures_contract()
    print(f"[backtest] Futures contract: {fut['trading_symbol']} (security_id={fut['security_id']}, "
          f"lot_size={fut['lot_size']})")
    fut_data = fetch_futures_1min_cached(fut["security_id"])
    print(f"  futures 1-min: {len(fut_data['closes'])} bars")

    all_days = sorted({datetime.fromtimestamp(t, tz=IST).date() for t in fast["timestamps"]})
    test_days = set(all_days[-TEST_DAYS_BACK:])
    print(f"[backtest] Testing entries over the last {TEST_DAYS_BACK} trading days: "
          f"{sorted(test_days)[0]} to {sorted(test_days)[-1]}")

    qty = fut["lot_size"] * QUANTITY_LOTS
    pnl_multiplier = qty
    signal_interval_seconds = REGIME_FAST_INTERVAL_MINUTES * 60

    trades = []
    position = None
    consumed_signal_idx = None

    def price_at_or_before(ts_list, closes, target_ts):
        idx = idx_at_or_before(ts_list, target_ts)
        return closes[idx] if idx is not None else None

    master_ts = [t for t in fut_data["timestamps"]
                 if datetime.fromtimestamp(t, tz=IST).date() in test_days]

    for t in master_ts:
        dt = datetime.fromtimestamp(t, tz=IST)
        fut_price = price_at_or_before(fut_data["timestamps"], fut_data["closes"], t)
        if fut_price is None:
            continue
        sig_idx = idx_at_or_before(fast["timestamps"], t - signal_interval_seconds)

        if position is not None:
            position["best_price"] = (max if position["side"] == "LONG" else min)(position["best_price"], fut_price)
            loss_rs = -unrealized_pnl_rs(position["side"], position["entry_price"], fut_price, pnl_multiplier)
            reason = None
            if loss_rs >= MAX_LOSS_PROTECTION_RS:
                reason = "MAX_LOSS_HIT"
            elif ENABLE_TARGET_EXIT and price_past_target(position["side"], fut_price, position["target_price"]):
                reason = "TARGET_HIT"
            else:
                peak_profit_rs = unrealized_pnl_rs(position["side"], position["entry_price"],
                                                    position["best_price"], pnl_multiplier)
                if peak_profit_rs > PROFIT_PROTECTION_RS:
                    floor = giveback_floor(position["side"], position["best_price"], PROFIT_PROTECTION_GIVEBACK_PCT)
                    if price_past_giveback_floor(position["side"], fut_price, floor):
                        reason = "PROFIT_PROTECTION_HIT"
                if reason is None and price_past_hard_stop(position["side"], fut_price, position["hard_stop_loss"]):
                    reason = "STOP_LOSS_HIT"
                if (reason is None and sig_idx is not None
                        and fast["timestamps"][sig_idx] > position["entry_candle_ts"]):
                    # NEW square-off: LONG exits on EMA20 crossing BELOW EMA100;
                    # SHORT exits on EMA20 crossing ABOVE EMA100.
                    squareoff_long = position["side"] == "LONG" and exit_crossed_below[sig_idx]
                    squareoff_short = position["side"] == "SHORT" and exit_crossed_above[sig_idx]
                    if squareoff_long or squareoff_short:
                        reason = "EMA_CROSS_SQUAREOFF"
            if reason:
                pnl = unrealized_pnl_rs(position["side"], position["entry_price"], fut_price, pnl_multiplier)
                print(f"  EXIT  {position['side']} @ {dt} reason={reason} exit_price={fut_price} pnl={pnl:+.0f}")
                trades.append({
                    "side": position["side"], "entry_dt": str(position["entry_dt"]),
                    "entry_price": position["entry_price"], "exit_dt": str(dt), "exit_price": fut_price,
                    "exit_reason": reason, "quantity": qty, "pnl": pnl, "status": "closed",
                })
                position = None

        if position is None and sig_idx is not None and sig_idx != consumed_signal_idx:
            entry_signal = None
            if entry_crossed_above[sig_idx]:
                entry_signal = "BULLISH"
            elif entry_crossed_below[sig_idx]:
                entry_signal = "BEARISH"
            if entry_signal:
                consumed_signal_idx = sig_idx
                side = resolve_instrument_side(BASKET_TYPE, entry_signal)
                if side is None:
                    print(f"  SKIP  {dt} signal={entry_signal} reason=basket_type_regime_combo_not_tradeable")
                    continue
                entry_price = fut_price
                target_price = target_price_for(side, entry_price, TARGET_PCT)
                hard_stop_loss = hard_stop_for(side, entry_price, HARD_STOP_LOSS_PCT)
                print(f"  ENTER {side} @ {dt} signal={entry_signal} entry_price={entry_price}")
                position = {
                    "side": side, "entry_dt": dt, "entry_price": entry_price, "best_price": entry_price,
                    "target_price": target_price, "hard_stop_loss": hard_stop_loss,
                    "entry_candle_ts": fast["timestamps"][sig_idx],
                }

    if position is not None:
        trades.append({
            "side": position["side"], "entry_dt": str(position["entry_dt"]), "entry_price": position["entry_price"],
            "status": "still open through end of available data", "pnl": None,
        })

    (CACHE_DIR / "results_adaniports_swing_ema_20day.json").write_text(json.dumps(trades, default=str, indent=2))

    print(f"\n[backtest] {len(trades)} trades")
    print("\n=== TRADE-WISE P&L ===")
    total = 0.0
    for i, tr in enumerate(trades, 1):
        pnl = tr.get("pnl")
        if pnl is not None:
            total += pnl
        print(f"{i}. {tr['side']} entry={tr['entry_dt']} exit={tr.get('exit_dt', 'N/A')} "
              f"status={tr['status']} exit_reason={tr.get('exit_reason')} "
              f"pnl={pnl if pnl is not None else 'N/A'}")

    print("\n=== DATE-WISE P&L ===")
    by_date: dict = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    for tr in trades:
        pnl = tr.get("pnl")
        if pnl is None:
            continue
        d = tr["entry_dt"][:10]
        by_date[d]["pnl"] += pnl
        by_date[d]["trades"] += 1
        if pnl > 0:
            by_date[d]["wins"] += 1
        elif pnl < 0:
            by_date[d]["losses"] += 1
    for d in sorted(by_date):
        s = by_date[d]
        print(f"{d}: trades={s['trades']:2d} wins={s['wins']:2d} losses={s['losses']:2d} net_pnl={s['pnl']:+.0f}")

    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    print(f"\n=== SUMMARY ===")
    print(f"Total trades: {len(trades)} (closed={len(closed)}, still-open={len(trades) - len(closed)})")
    print(f"Wins: {len(wins)}  Losses: {len(losses)}  "
          f"Win rate: {(len(wins) / len(closed) * 100) if closed else 0:.1f}%")
    print(f"TOTAL net P&L: Rs {total:+.0f}")


if __name__ == "__main__":
    main()
