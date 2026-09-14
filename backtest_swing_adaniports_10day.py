"""
User request 14 Sep 2026: backtest the SWING v2 strategy on ADANIPORTS
(currently the only symbol on the live watchlist) over the last 10
trading days. Unlike every Options/Futures/Luxury backtest this session,
Swing does NOT react to Chartink alerts at all - it continuously scans
its own watchlist using two signals computed straight from real Dhan
candle data (Swing/signals.py):

  - REGIME: 5-min EMA(200) vs 15-min EMA(200) on the underlying equity -
    bullish if the 5-min EMA is above the 15-min EMA.
  - SUPERTREND: 5-min Supertrend(10, 3.0) crossover on the same
    underlying - crossed_above / crossed_below is a state CHANGE between
    the last two closed candles, not "is currently above/below".

Entry (Swing/trading_engine.py._evaluate_entry_signal, ported verbatim):
  regime.is_bullish AND crossed_above -> "BULLISH" signal
  NOT regime.is_bullish AND crossed_below -> "BEARISH" signal
Then resolve_instrument_side(BASKET_TYPE, regime) - live BASKET_TYPE is
"futures", so: BULLISH -> LONG (buy futures), BEARISH -> SHORT (sell
futures to open). ADANIPORTS is not in MCX_OPTIONS_ONLY_SYMBOLS, so this
mapping applies unconditionally.

Exit ladder (Swing/trading_engine.py._exit_reason_for +
_evaluate_exit_signal, ported verbatim, direction-aware for LONG/SHORT
via Swing/position_store.py's own pure helpers):
  1. MAX_LOSS_HIT (loss_rs >= SWING_MAX_LOSS_PROTECTION_RS, code default
     4500, not overridden live)
  2. TARGET_HIT (+/-20%, ENABLE_TARGET_EXIT, code default on)
  3. PROFIT_PROTECTION_HIT (peak profit > SWING_PROFIT_PROTECTION_RS=5000
     deployed, giveback SWING_PROFIT_PROTECTION_GIVEBACK_PCT=0.02
     deployed)
  4. STOP_LOSS_HIT (+/-20% hard stop, code default, not overridden live)
  5. SUPERTREND_REVERSAL (opposite crossover on the 5-min series, only
     once past the position's own entry candle - same entry-candle-skip
     protection Options/Futures/Luxury use, applied here via the SAME
     at-or-before-minus-interval fix this session already found necessary
     for exit_ladder_backtest_helper.py - see that module's own
     "entry-candle-skip look-ahead bug" note for why idx_at_or_after
     would silently defeat this protection).

The TRADED instrument for a FUTURES-basket entry is the futures contract
itself, not the equity - its own price series (fetched separately, NSE_FNO/
FUTSTK) drives entry/exit fills and P&L, while the regime/Supertrend
SIGNAL is computed from the equity series, exactly mirroring how every
Options/Futures/Luxury backtest this session computes its ranking/exit
signal off the underlying equity while filling on the option's own
premium series. quantity = futures lot_size * QUANTITY_LOTS(1);
pnl_multiplier = quantity (ADANIPORTS is not MCX, so no lot-size/pnl-
multiplier split is needed - see Swing/config.py's own MCX_PNL_
MULTIPLIERS docstring for when that split actually matters).

NOT modeled (same standing disclosure as every backtest this session):
  - SL-L broker-side stop-loss.
  - cross_strategy_registry.
  - Funds check.
  - MAX_CONCURRENT_TRADES capacity is moot here (a single-symbol
    watchlist can only ever hold 0 or 1 position on itself at a time,
    same as live).

Run via SSH on the droplet (never locally while the live bot's own
session is active):
    uv run python backtest_swing_adaniports_10day.py [DAYS_BACK_TRADING]
    (optional - default 10)
"""
from __future__ import annotations

import csv
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from Options.dhan_client import dhan_wrapper, _compute_ema, _compute_supertrend, _retry  # noqa: E402
from Swing import config as swing_config  # noqa: E402
from Swing.position_store import (  # noqa: E402
    resolve_instrument_side, entry_transaction_type, unrealized_pnl_rs,
    target_price_for, hard_stop_for, price_past_target, price_past_hard_stop,
    giveback_floor, price_past_giveback_floor,
)

IST = ZoneInfo("Asia/Kolkata")
SYMBOL = "ADANIPORTS"

TEST_DAYS_BACK = int(sys.argv[1]) if len(sys.argv) > 1 else 10
SIGNAL_LOOKBACK_DAYS = swing_config.REGIME_EMA_LOOKBACK_DAYS + 15  # buffer past the 200-EMA warmup itself
FUTURES_FETCH_DAYS_BACK = 90

CACHE_DIR = Path("/private/tmp/claude-501/-Users-kushalgaur-Desktop-projects-trading-traderBoy/"
                  "60a0e686-2110-4e20-bad5-fe817a57a72a/scratchpad/swing_backtest_cache_adaniports")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BASKET_TYPE = swing_config.BASKET_TYPE.upper()
QUANTITY_LOTS = swing_config.QUANTITY_LOTS
MAX_LOSS_PROTECTION_RS = swing_config.MAX_LOSS_PROTECTION_RS
PROFIT_PROTECTION_RS = swing_config.PROFIT_PROTECTION_RS
PROFIT_PROTECTION_GIVEBACK_PCT = swing_config.PROFIT_PROTECTION_GIVEBACK_PCT
TARGET_PCT = swing_config.TARGET_PCT
HARD_STOP_LOSS_PCT = swing_config.HARD_STOP_LOSS_PCT
ENABLE_TARGET_EXIT = swing_config.ENABLE_TARGET_EXIT
ENABLE_SUPERTREND_EXIT = swing_config.ENABLE_SUPERTREND_EXIT
REGIME_EMA_PERIOD = swing_config.REGIME_EMA_PERIOD
REGIME_FAST_INTERVAL_MINUTES = swing_config.REGIME_FAST_INTERVAL_MINUTES
REGIME_SLOW_INTERVAL_MINUTES = swing_config.REGIME_SLOW_INTERVAL_MINUTES
SUPERTREND_PERIOD = swing_config.SUPERTREND_PERIOD
SUPERTREND_MULTIPLIER = swing_config.SUPERTREND_MULTIPLIER
SUPERTREND_INTERVAL_MINUTES = swing_config.SUPERTREND_INTERVAL_MINUTES

print(f"[backtest] SYMBOL={SYMBOL} BASKET_TYPE={BASKET_TYPE} TEST_DAYS_BACK={TEST_DAYS_BACK} "
      f"MAX_LOSS_PROTECTION_RS={MAX_LOSS_PROTECTION_RS} PROFIT_PROTECTION_RS={PROFIT_PROTECTION_RS} "
      f"GIVEBACK_PCT={PROFIT_PROTECTION_GIVEBACK_PCT} TARGET_PCT={TARGET_PCT} "
      f"HARD_STOP_LOSS_PCT={HARD_STOP_LOSS_PCT} ENABLE_TARGET_EXIT={ENABLE_TARGET_EXIT} "
      f"ENABLE_SUPERTREND_EXIT={ENABLE_SUPERTREND_EXIT} "
      f"REGIME=({REGIME_FAST_INTERVAL_MINUTES}min/{REGIME_SLOW_INTERVAL_MINUTES}min EMA{REGIME_EMA_PERIOD}) "
      f"SUPERTREND=({SUPERTREND_INTERVAL_MINUTES}min,{SUPERTREND_PERIOD},{SUPERTREND_MULTIPLIER})")


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
    result = {"highs": data.get("high") or [], "lows": data.get("low") or [],
              "closes": data.get("close") or [], "timestamps": data.get("timestamp") or []}
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


def compute_regime_series(fast_closes, fast_ts, slow_closes, slow_ts):
    """Returns a dict: for each FAST(5-min) bar index, the regime is_bullish
    value using the fast EMA at that bar and the LATEST slow(15-min) EMA
    value available at-or-before that bar's own timestamp (mirrors
    get_regime_state reading two independently-cadenced series)."""
    fast_ema = _compute_ema(fast_closes, REGIME_EMA_PERIOD)
    slow_ema = _compute_ema(slow_closes, REGIME_EMA_PERIOD)
    result = [None] * len(fast_closes)
    for i, ts in enumerate(fast_ts):
        if fast_ema[i] is None:
            continue
        s_idx = idx_at_or_before(slow_ts, ts)
        if s_idx is None or slow_ema[s_idx] is None:
            continue
        result[i] = fast_ema[i] > slow_ema[s_idx]
    return result


def compute_supertrend_cross_series(highs, lows, closes):
    st = _compute_supertrend(highs, lows, closes, period=SUPERTREND_PERIOD, multiplier=SUPERTREND_MULTIPLIER)
    n = len(closes)
    is_above = [None] * n
    crossed_above = [False] * n
    crossed_below = [False] * n
    for i in range(n):
        if st[i] is not None:
            is_above[i] = closes[i] > st[i]
    for i in range(1, n):
        if is_above[i] is None or is_above[i - 1] is None:
            continue
        if (not is_above[i - 1]) and is_above[i]:
            crossed_above[i] = True
        if is_above[i - 1] and not is_above[i]:
            crossed_below[i] = True
    return st, crossed_above, crossed_below


def main():
    print(f"[backtest] Fetching {SYMBOL} equity {REGIME_FAST_INTERVAL_MINUTES}min/"
          f"{REGIME_SLOW_INTERVAL_MINUTES}min series ({SIGNAL_LOOKBACK_DAYS}d lookback for EMA warmup)...")
    fast = fetch_equity_cached(REGIME_FAST_INTERVAL_MINUTES, SIGNAL_LOOKBACK_DAYS)
    slow = fetch_equity_cached(REGIME_SLOW_INTERVAL_MINUTES, SIGNAL_LOOKBACK_DAYS)
    print(f"  fast({REGIME_FAST_INTERVAL_MINUTES}min): {len(fast['closes'])} bars, "
          f"slow({REGIME_SLOW_INTERVAL_MINUTES}min): {len(slow['closes'])} bars")

    regime_is_bullish = compute_regime_series(fast["closes"], fast["timestamps"], slow["closes"], slow["timestamps"])
    st_values, crossed_above, crossed_below = compute_supertrend_cross_series(
        fast["highs"], fast["lows"], fast["closes"])

    fut = resolve_futures_contract()
    print(f"[backtest] Futures contract: {fut['trading_symbol']} (security_id={fut['security_id']}, "
          f"lot_size={fut['lot_size']})")
    fut_data = fetch_futures_1min_cached(fut["security_id"])
    print(f"  futures 1-min: {len(fut_data['closes'])} bars")

    # Restrict the TEST window to the last TEST_DAYS_BACK trading days present
    # in the fast(5-min) series (the EMA warmup lookback fetched far more
    # history than we actually test entries over).
    all_days = sorted({datetime.fromtimestamp(t, tz=IST).date() for t in fast["timestamps"]})
    test_days = set(all_days[-TEST_DAYS_BACK:])
    print(f"[backtest] Testing entries over the last {TEST_DAYS_BACK} trading days: "
          f"{sorted(test_days)[0]} to {sorted(test_days)[-1]}")

    qty = fut["lot_size"] * QUANTITY_LOTS
    pnl_multiplier = qty
    signal_interval_seconds = REGIME_FAST_INTERVAL_MINUTES * 60

    trades = []
    position = None  # dict or None
    consumed_signal_idx = None  # avoid re-entering repeatedly off the same transition bar

    def price_at_or_before(ts_list, closes, target_ts):
        idx = idx_at_or_before(ts_list, target_ts)
        return closes[idx] if idx is not None else None

    # Drive the loop off the FUTURES 1-min series, not the 5-min signal
    # series - price-based exits (MAX_LOSS/TARGET/PP/STOP) need to be
    # checked far more often than every 5 minutes, matching how the live
    # poll loop (and WS tick path) actually monitors a position. Checking
    # only every 5 minutes (an earlier draft of this script did this) let
    # MAX_LOSS_HIT overshoot its own Rs 4,500 cap by ~Rs 2,000 in one trade
    # simply because the next check point was 5 minutes later - a real
    # backtest-fidelity gap, not a live-system behavior.
    master_ts = [t for t in fut_data["timestamps"]
                 if datetime.fromtimestamp(t, tz=IST).date() in test_days]

    for t in master_ts:
        dt = datetime.fromtimestamp(t, tz=IST)
        fut_price = price_at_or_before(fut_data["timestamps"], fut_data["closes"], t)
        if fut_price is None:
            continue
        # Last CLOSED 5-min signal candle at-or-before t - entry-candle-
        # skip-safe (see exit_ladder_backtest_helper.py's own note on why
        # idx_at_or_after would silently defeat this protection).
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
                if (reason is None and ENABLE_SUPERTREND_EXIT and sig_idx is not None
                        and fast["timestamps"][sig_idx] > position["entry_candle_ts"]):
                    reversed_against_long = position["side"] == "LONG" and crossed_below[sig_idx]
                    reversed_against_short = position["side"] == "SHORT" and crossed_above[sig_idx]
                    if reversed_against_long or reversed_against_short:
                        reason = "SUPERTREND_REVERSAL"
            if reason:
                pnl = unrealized_pnl_rs(position["side"], position["entry_price"], fut_price, pnl_multiplier)
                print(f"  EXIT  {position['side']} @ {dt} reason={reason} exit_price={fut_price} pnl={pnl:+.0f}")
                trades.append({
                    "side": position["side"], "entry_dt": str(position["entry_dt"]),
                    "entry_price": position["entry_price"], "exit_dt": str(dt), "exit_price": fut_price,
                    "exit_reason": reason, "quantity": qty, "pnl": pnl, "status": "closed",
                })
                position = None

        if position is None and sig_idx is not None and sig_idx != consumed_signal_idx \
                and regime_is_bullish[sig_idx] is not None:
            regime_signal = None
            if regime_is_bullish[sig_idx] and crossed_above[sig_idx]:
                regime_signal = "BULLISH"
            elif (not regime_is_bullish[sig_idx]) and crossed_below[sig_idx]:
                regime_signal = "BEARISH"
            if regime_signal:
                consumed_signal_idx = sig_idx
                side = resolve_instrument_side(BASKET_TYPE, regime_signal)
                if side is None:
                    print(f"  SKIP  {dt} signal={regime_signal} reason=basket_type_regime_combo_not_tradeable")
                    continue
                entry_price = fut_price
                target_price = target_price_for(side, entry_price, TARGET_PCT)
                hard_stop_loss = hard_stop_for(side, entry_price, HARD_STOP_LOSS_PCT)
                print(f"  ENTER {side} @ {dt} signal={regime_signal} entry_price={entry_price}")
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

    (CACHE_DIR / "results_adaniports_swing_10day.json").write_text(json.dumps(trades, default=str, indent=2))

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
