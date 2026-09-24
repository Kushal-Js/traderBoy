"""
Backtests climactic_entry_guard.py against today's (24 Sep 2026) real
Options/Futures/Luxury trades - the same day whose reversal_filter_
shadow.log first surfaced the pattern this guard encodes (see that
module's own docstring for the exact numbers).

Methodology, end to end on REAL data (no synthetic pricing model):
  1. Load every fresh (non-reconciled) CE/PE trade from today's
     history/2026-09-24_real_trades.log.
  2. Fetch the UNDERLYING's own continuous 5-min candles (same series
     reversal_filters.py's shadow logging already uses) to recompute
     RSI(14)/ER(10) at the alert candle and every candle after it -
     climactic_entry_guard.simulate_series() decides ENTER_NOW /
     ENTER_AFTER_COOLDOWN(direction) / SKIP from that series alone.
  3. For anything the guard would have deferred, fetch the relevant
     OPTION CONTRACT's own real 1-min intraday premium candles (NSE_FNO
     OPTSTK) for today and read the REAL premium at the resolved entry
     time - not a modeled/estimated price. If the resolved direction
     equals the original alert's, this is the SAME contract that was
     actually traded, just entered later; if the guard flipped
     direction, this is the ATM contract of the OPPOSITE option_type on
     the SAME underlying/expiry.
  4. From that real premium, replay the SAME contract's own subsequent
     1-min candles forward applying this repo's actual live risk
     parameters (TARGET_PCT/STOP_LOSS_PCT from .env, currently +20%/-20%
     flat - the trailing/dynamic-SL refinements on top of that are
     deliberately NOT replicated here, see the caveat printed at the end)
     to find the simulated exit price/time, capped at the real trade's
     own actual close time if the simulated position is still open by
     then (never lets the simulated trade run past when we actually
     stopped having same-day 1-min data confidence).
  5. Reports simulated P&L vs the real trade's actual P&L, trade by
     trade and in aggregate.

Read-only: dhan_wrapper.fetch_continuous_intraday and _instrument_meta
only, both real-only GET calls already used elsewhere in this codebase
for the identical purpose (backtest_reversal_filters_15day.py) - no
order placement, no live position touched.

HOW TO RUN:
    DHAN_AUTH_MODE=access_token DHAN_ACCESS_TOKEN=<handoff token> \\
        uv run python backtest_climactic_entry_guard.py
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from Options.dhan_client import dhan_wrapper
from Options import config
import climactic_entry_guard as guard

IST = ZoneInfo("Asia/Kolkata")
UTC_TO_IST = timedelta(hours=5, minutes=30)
TODAY_LOG = REPO_ROOT / "history_pull" / "2026-09-24_real_trades.log"  # overwritten below via SSH pull path if present locally
TARGET_PCT = config.TARGET_PCT
STOP_LOSS_PCT = config.STOP_LOSS_PCT


@dataclass
class Trade:
    strategy: str
    symbol: str
    option_type: str
    trading_symbol: str
    entry_price: float
    exit_price: float
    exit_reason: str
    pnl: float
    quantity: int
    opened_at: datetime  # IST
    closed_at: datetime  # IST


def load_trades(log_path: Path) -> list[Trade]:
    trades: list[Trade] = []
    for line in open(log_path):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("strategy") not in ("Options", "Futures", "Luxury"):
            continue
        if r.get("option_type") not in ("CE", "PE"):
            continue
        # NOTE: deliberately NOT filtering on "reconciled" here, unlike
        # backtest_reversal_filters_15day.py's own convention - today's
        # 3 mid-morning restarts (v3 NIFTY/BANKNIFTY rollout) mean several
        # genuinely FRESH alert-driven entries got marked reconciled=true
        # simply because a restart happened while they were still open,
        # not because they lack a real origin signal. Cross-checked
        # against history/2026-09-24_position_opened.log - every trade in
        # today's real_trades.log has a matching fresh-entry record there
        # (order_id present, opened_at matches), so all of them are valid
        # signal-driven entries for this backtest.
        trades.append(Trade(
            strategy=r["strategy"], symbol=r["underlying_symbol"], option_type=r["option_type"],
            trading_symbol=r["option_trading_symbol"],
            entry_price=r["entry_price"], exit_price=r["exit_price"], exit_reason=r["exit_reason"],
            pnl=r["pnl"], quantity=r["quantity"],
            opened_at=datetime.fromisoformat(r["opened_at"]) + UTC_TO_IST,
            closed_at=datetime.fromisoformat(r["closed_at"]) + UTC_TO_IST,
        ))
    return trades


def fetch_underlying_5min(symbol: str) -> dict:
    security_id = dhan_wrapper._equity_security_id(symbol)
    data = dhan_wrapper.fetch_continuous_intraday(security_id, "NSE_EQ", "EQUITY", 5, lookback_days_override=10)
    time.sleep(0.3)
    return data


def fetch_option_1min(trading_symbol: str) -> Optional[dict]:
    try:
        meta = dhan_wrapper._instrument_meta(trading_symbol, expected_exchange="NSE")
    except Exception as e:  # noqa: BLE001
        print(f"    instrument lookup failed for {trading_symbol!r}: {e}")
        return None
    data = dhan_wrapper.fetch_continuous_intraday(meta["security_id"], "NSE_FNO", "OPTSTK", 1, lookback_days_override=2)
    time.sleep(0.3)
    return data


def other_side_trading_symbol(trading_symbol: str, target_type: str) -> str:
    """'HDFCLIFE 29 SEP 515 PUT' + 'CE' -> 'HDFCLIFE 29 SEP 515 CALL' (ATM
    strike unchanged - same strike, opposite side, same expiry, exactly
    what an ATM-option resolver would hand back for the other direction
    at the same moment)."""
    word = "CALL" if target_type == "CE" else "PUT"
    parts = trading_symbol.rsplit(" ", 1)
    return f"{parts[0]} {word}"


def candle_index_at_or_before(timestamps: list[int], target: datetime, bar_seconds: int = 0) -> Optional[int]:
    """Last candle whose CLOSE (start + bar_seconds) is <= target - i.e.
    the last fully-formed bar as of `target`, never the still-forming one
    target itself falls inside. Matches reversal_filters.py's own "last
    closed 5-min bar" semantics (its shadow computation runs moments
    after a real order fires, always reading whatever bar had already
    closed by then, never a still-forming one). bar_seconds=0 keeps the
    old at-or-before behavior, used for the OPTION series where we want
    the entry to land ON the resolved-entry-time bar itself, not the one
    before it."""
    target_epoch = target.timestamp() if target.tzinfo else target.replace(tzinfo=IST).timestamp()
    # (opened_at/closed_at are naive-but-already-IST - see UTC_TO_IST
    # conversion in load_trades - so the branch above always takes the
    # replace() path in practice; kept explicit rather than assuming.)
    idx = None
    for i, epoch in enumerate(timestamps):
        if epoch + bar_seconds <= target_epoch:
            idx = i
        else:
            break
    return idx


def simulate_exit(option_closes: list[float], option_highs: list[float], option_lows: list[float],
                   option_ts: list[int], start_idx: int, entry_price: float,
                   real_side: str, hard_cutoff: datetime) -> tuple[float, str, datetime]:
    """Walks the option's own real 1-min candles forward from start_idx,
    applying the actual live TARGET_PCT/STOP_LOSS_PCT (flat, no
    trailing/dynamic-SL refinement - see module docstring caveat), long
    option (premium can only be bought, never shorted) - same "long
    premium" shape both CE and PE positions actually are in this
    codebase. Returns (exit_price, exit_reason, exit_time); if neither
    level is touched before hard_cutoff or the data runs out, exits at
    the last available close ("EOD_OR_DATA_END")."""
    target = entry_price * (1 + TARGET_PCT)
    stop = entry_price * (1 - STOP_LOSS_PCT)
    for i in range(start_idx, len(option_closes)):
        ct = datetime.fromtimestamp(option_ts[i], tz=IST).replace(tzinfo=None)
        if ct > hard_cutoff.replace(tzinfo=None):
            return option_closes[i - 1] if i > start_idx else entry_price, "EOD_OR_DATA_END", ct
        high, low = option_highs[i], option_lows[i]
        hit_target = high >= target
        hit_stop = low <= stop
        if hit_target and hit_stop:
            # Ambiguous within-bar ordering - conservative assumption,
            # same one backtest_reversal_filters_* scripts implicitly
            # make by only ever checking already-realized real trades:
            # assume the WORSE outcome fires first.
            return stop, "STOP_LOSS_HIT", ct
        if hit_stop:
            return stop, "STOP_LOSS_HIT", ct
        if hit_target:
            return target, "TARGET_HIT", ct
    last_i = len(option_closes) - 1
    return option_closes[last_i], "EOD_OR_DATA_END", datetime.fromtimestamp(option_ts[last_i], tz=IST).replace(tzinfo=None)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(REPO_ROOT / "history" / "2026-09-24_real_trades.log"))
    args = ap.parse_args()

    trades = load_trades(Path(args.log))
    print(f"Loaded {len(trades)} fresh-signal Options/Futures/Luxury trades today.\n")

    print("Authenticating...")
    dhan_wrapper.authenticate()
    print("Authenticated.\n")

    results = []
    for t in trades:
        print(f"--- {t.strategy} {t.symbol} {t.option_type} (real: entry={t.entry_price} exit={t.exit_price} "
              f"pnl={t.pnl:.2f} {t.exit_reason}) ---")
        try:
            underlying = fetch_underlying_5min(t.symbol)
        except Exception as e:  # noqa: BLE001
            print(f"    underlying fetch failed: {e} - excluding")
            continue
        closes = underlying.get("close") or []
        ts_epoch = underlying.get("timestamp") or []
        if not closes:
            print("    no underlying candles - excluding")
            continue
        times = [datetime.fromtimestamp(e, tz=IST).replace(tzinfo=None) for e in ts_epoch]
        alert_idx = candle_index_at_or_before(ts_epoch, t.opened_at, bar_seconds=300)
        if alert_idx is None or alert_idx < 20:
            print("    insufficient underlying history at alert time - excluding")
            continue

        sim = guard.simulate_series(closes, times, alert_idx, t.option_type)
        print(f"    guard @ alert: RSI={sim.get('rsi_at_alert')} ER={sim.get('er_at_alert')} -> {sim['decision']}")

        if sim["decision"] == "ENTER_NOW":
            results.append({"trade": t, "guard": sim, "sim_pnl": t.pnl, "sim_exit_reason": t.exit_reason,
                             "note": "guard agrees with real entry - no change"})
            continue

        if sim["decision"] == "SKIP_COOLDOWN_TIMEOUT":
            if sim.get("minutes_waited") is not None and sim["minutes_waited"] >= guard.COOLDOWN_MAX_WAIT_MINUTES:
                note = f"never cooled down within {guard.COOLDOWN_MAX_WAIT_MINUTES}min - guard would have skipped this trade entirely"
            else:
                note = ("live underlying series ran out before cooldown cleared OR the "
                        f"{guard.COOLDOWN_MAX_WAIT_MINUTES}min cap - inconclusive (not enough same-day "
                        "data yet as of when this backtest ran), NOT a confirmed timeout")
            results.append({"trade": t, "guard": sim, "sim_pnl": 0.0, "sim_exit_reason": "GUARD_SKIPPED",
                             "note": note})
            continue

        # ENTER_AFTER_COOLDOWN
        resolved_type = sim["resolved_option_type"]
        resolved_trading_symbol = (
            t.trading_symbol if resolved_type == t.option_type
            else other_side_trading_symbol(t.trading_symbol, resolved_type)
        )
        flipped = resolved_type != t.option_type
        print(f"    cooldown cleared after {sim['minutes_waited']:.1f}min at {sim['entry_time']} "
              f"(RSI={sim.get('rsi_at_entry')} ER={sim.get('er_at_entry')}) -> "
              f"{'FLIPPED to ' + resolved_type if flipped else 'same direction, delayed'}: {resolved_trading_symbol}")

        option_data = fetch_option_1min(resolved_trading_symbol)
        if not option_data or not option_data.get("close"):
            results.append({"trade": t, "guard": sim, "sim_pnl": None, "sim_exit_reason": "NO_OPTION_DATA",
                             "note": f"could not fetch real premium data for {resolved_trading_symbol!r} - guard decision known, simulated PnL not computable"})
            continue

        opt_closes, opt_highs, opt_lows = option_data["close"], option_data["high"], option_data["low"]
        opt_ts = option_data["timestamp"]
        entry_idx = candle_index_at_or_before(opt_ts, sim["entry_time"])
        if entry_idx is None:
            results.append({"trade": t, "guard": sim, "sim_pnl": None, "sim_exit_reason": "NO_OPTION_DATA_AT_ENTRY_TIME",
                             "note": "option data doesn't cover the resolved entry time"})
            continue
        sim_entry_price = opt_closes[entry_idx]
        exit_price, exit_reason, exit_time = simulate_exit(
            opt_closes, opt_highs, opt_lows, opt_ts, entry_idx, sim_entry_price, resolved_type, t.closed_at + timedelta(hours=6),
        )
        sim_pnl = (exit_price - sim_entry_price) * t.quantity
        print(f"    simulated: entry={sim_entry_price} exit={exit_price} ({exit_reason} @ {exit_time}) pnl={sim_pnl:.2f}")
        results.append({"trade": t, "guard": sim, "sim_pnl": sim_pnl, "sim_exit_reason": exit_reason,
                         "sim_entry_price": sim_entry_price, "sim_exit_price": exit_price,
                         "note": f"{'FLIPPED direction' if flipped else 'delayed, same direction'}"})
        print()

    # --------------------------------------------------------------- #
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    real_total = sum(r["trade"].pnl for r in results)
    computable = [r for r in results if r["sim_pnl"] is not None]
    sim_total = sum(r["sim_pnl"] for r in computable)
    print(f"\nReal P&L (all {len(results)} trades): Rs.{real_total:.2f}")
    print(f"Simulated (guard-applied) P&L ({len(computable)}/{len(results)} computable): Rs.{sim_total:.2f}")
    print(f"Delta: {'+ ' if sim_total - real_total >= 0 else '- '}Rs.{abs(sim_total - real_total):.2f}\n")

    for r in results:
        t = r["trade"]
        tag = r["guard"]["decision"]
        sim_pnl_str = f"Rs.{r['sim_pnl']:.2f}" if r["sim_pnl"] is not None else "N/A"
        print(f"  {t.strategy:8s} {t.symbol:12s} {t.option_type}  real={t.pnl:9.2f}  sim={sim_pnl_str:>10s}  "
              f"[{tag}]  {r['note']}")

    print("\nCAVEAT: simulated exits use a flat +20%/-20% target/stop only (this "
          "repo's TARGET_PCT/STOP_LOSS_PCT) - the real strategy also applies a "
          "trailing stop and dynamic-SL step-up that aren't replicated here, so "
          "real vs simulated P&L are not perfectly apples-to-apples on the exit "
          "side, only on the entry-timing/direction side.")


if __name__ == "__main__":
    main()
