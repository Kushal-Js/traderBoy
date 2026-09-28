"""
User request 27 Sep 2026: build and backtest a new strategy from the video
"You're Using RSI WRONG - The Dual RSI 50-10 System That Changes Everything"
by Bharat Jhunjhunwala, https://www.youtube.com/watch?v=EaAgHNcurAM , over
the last 30 trading days, with a day-wise and trade-wise PnL report.

Source material caveat: this video has NO captions/transcript available on
YouTube (confirmed - "Subtitles/closed captions unavailable" on the player,
and the transcript panel returned empty). The strategy below is therefore
reconstructed from the video's TITLE and its own (long, detailed) WRITTEN
DESCRIPTION, quoted verbatim where it matters, not from watching the video
frame-by-frame. Ambiguities the description leaves open are flagged
explicitly below rather than guessed silently - same convention as
backtest_dual_ema_band_nifty_30day.py.

Strategy, as stated in the description (quoted):
  "The 50-Period RSI Trend Identifier - RSI above 50 means uptrend, below
   50 means downtrend."
  "The 10-Period RSI Entry System - using a shorter lookback to time EXACT
   pullback entries"
  "RSI crossing 60 and 40 on the 10-period as precise buy and sell triggers"
  "The 50-10 RSI Combo - how combining both lookback periods creates a
   complete system for trend identification AND entry timing in one
   indicator" - i.e. RSI(50) and RSI(10) are plotted on the SAME chart/
   timeframe (not a daily-bias + intraday-entry split like the dual-EMA-
   band video), both computed on the SAME continuous intraday close series.
  "Works for both LONG and SHORT setups with equal precision."

Mechanical rules implemented here:
  TREND FILTER (per bar): RSI(50) > 50 -> bullish regime (longs only).
                           RSI(50) < 50 -> bearish regime (shorts only).
  ENTRY: LONG  when regime is bullish AND RSI(10) closes-crosses UP through
                60 (prev <= 60, now > 60).
         SHORT when regime is bearish AND RSI(10) closes-crosses DOWN
                through 40 (prev >= 40, now < 40).
  EXIT (ASSUMPTION - not stated in the description, flagged not guessed-
        and-hidden): the opposite RSI(10) trigger level, i.e. the same
        60/40 lines the description calls "precise buy AND sell triggers":
          LONG exits on RSI(10) crossing DOWN through 40.
          SHORT exits on RSI(10) crossing UP through 60.
        This is the most literal reading of "60 and 40 ... as buy and sell
        triggers" without inventing an unstated third rule (a fixed R
        target, a trend-flip exit, etc - the video never specifies one).
  SQUARE-OFF (ASSUMPTION, borrowed from this repo's own live convention -
        Options/config.py SQUARE_OFF_TIME): every stock trade is modeled
        INTRADAY - force-closed at 15:15 IST if still open, no new entries
        after 15:00 IST. The description's own hashtags mix #SwingTrading
        and #IntradayTrading and never settle it either way; intraday was
        chosen because (a) the video's own top comment describes testing
        it on "Nifty ke 2 aur 5 minute" (2 and 5-min charts, explicitly
        intraday), and (b) a 10-period RSI on 5/15-min bars reacts on a
        timescale (~1hr lookback) that is naturally intraday, not
        multi-day swing.
  TIMEFRAME (unstated in the description - swept, not guessed, same
        convention as the dual-EMA-band video): tested at both 5-min and
        15-min, see INTERVALS_TO_TEST.
  WATCHLIST: the 6 real NSE stocks the description explicitly names as
        chart examples - Tata Motors, Kotak Mahindra Bank, CG Power, Zee
        Entertainment, Navin Fluorine, Tata Communications. (Bitcoin/
        Ethereum, also named, are excluded - no crypto data path in this
        Dhan-based repo.) NOTE: Tata Motors demerged in 2025 into separate
        Commercial/Passenger Vehicle listings; the original TATAMOTORS
        ticker no longer trades. Substituted with its passenger-vehicle
        successor, TMPV (Tata Motors Passenger Vehicles), confirmed present
        in Dhan's own instrument master (security_id 3456) - the closest
        real, currently-tradeable stand-in for what the video showed.
  POSITION SIZING (ASSUMPTION, not stated): CAPITAL_PER_TRADE_RS notional
        per trade, qty = floor(capital / entry_price), for the rupee PnL
        figures. The points-based (per-share) PnL is sizing-independent.

NOT modeled: intraday option-premium P&L (this tests the underlying cash-
  equity price only, like the dual-EMA-band video's NIFTY-points scope),
  brokerage/slippage, multi-position portfolio capital constraints (each
  symbol is sized independently, as if trading it in isolation).

Continuity: intraday candles for each symbol are fetched ONCE as a single
multi-day range call, and RSI(50)/RSI(10) are computed over the FULL
continuous array before slicing to the test window - see feedback-
continuous-candles memory; same convention as backtest_dual_ema_band_
nifty_30day.py and every other backtest in this repo.

Auth: uses a hand-off access token (HANDOFF_DHAN_ACCESS_TOKEN env var),
NEVER local pin_totp - see incidents/2026-09-21-local-backtest-dhan-
session-collision.md. access_token mode VALIDATES an existing token
instead of minting one, so it coexists with the live bot's own session:

    HANDOFF_DHAN_ACCESS_TOKEN=... uv run python backtest_dual_rsi_50_10_bharat_jhunjhunwala_30day.py [TEST_DAYS_BACK]
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from Options import config as ocfg  # noqa: E402
from Options.dhan_client import dhan_wrapper, _compute_rsi, _retry  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

# symbol -> (security_id, notes)
WATCHLIST = {
    "TMPV": "3456",       # Tata Motors Passenger Vehicles (TATAMOTORS demerger successor)
    "KOTAKBANK": "1922",
    "CGPOWER": "760",
    "ZEEL": "3812",
    "NAVINFLUOR": "14672",
    "TATACOMM": "3721",
}

TEST_DAYS_BACK = int(sys.argv[1]) if len(sys.argv) > 1 else 30
INTRADAY_LOOKBACK_DAYS = max(55, TEST_DAYS_BACK + 25)  # calendar days - covers TEST_DAYS_BACK trading days + RSI(50) warmup
RSI_TREND_PERIOD = 50
RSI_ENTRY_PERIOD = 10
ENTRY_HIGH = 60.0
ENTRY_LOW = 40.0
INTERVALS_TO_TEST = [5, 15]  # unstated in the source video - swept, not guessed
SQUARE_OFF_TIME = (15, 15)   # IST, matches Options/config.py's own live SQUARE_OFF_TIME default
ENTRY_CUTOFF_TIME = (15, 0)  # IST - no new entries in the last 15 minutes of the session
CAPITAL_PER_TRADE_RS = 100_000  # ASSUMPTION - not stated in the video; sizing convention for rupee PnL only

CACHE_DIR = Path("/tmp/dual_rsi_50_10_backtest_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def fetch_intraday_cached(symbol: str, security_id: str, interval_minutes: int) -> dict:
    cache_file = CACHE_DIR / f"{symbol}_{interval_minutes}min_{INTRADAY_LOOKBACK_DAYS}d.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    to_date = datetime.now(IST).strftime("%Y-%m-%d")
    from_date = (datetime.now(IST) - timedelta(days=INTRADAY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    resp = _retry(
        dhan_wrapper.client.Dhan.intraday_minute_data,
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=from_date, to_date=to_date, interval=interval_minutes,
    )
    data = resp.get("data") or {}
    result = {
        "opens": data.get("open") or [], "highs": data.get("high") or [],
        "lows": data.get("low") or [], "closes": data.get("close") or [],
        "timestamps": data.get("timestamp") or [],
    }
    cache_file.write_text(json.dumps(result))
    time.sleep(1.0)
    return result


def _cross_up(prev: Optional[float], now: Optional[float], level: float) -> bool:
    return prev is not None and now is not None and prev <= level < now


def _cross_down(prev: Optional[float], now: Optional[float], level: float) -> bool:
    return prev is not None and now is not None and prev >= level > now


def run_symbol_interval(symbol: str, security_id: str, interval_minutes: int) -> list[dict]:
    intraday = fetch_intraday_cached(symbol, security_id, interval_minutes)
    closes = intraday["closes"]
    timestamps = intraday["timestamps"]
    if not closes:
        print(f"  [{symbol} {interval_minutes}min] no intraday data returned - skipping")
        return []

    rsi50 = _compute_rsi(closes, RSI_TREND_PERIOD)
    rsi10 = _compute_rsi(closes, RSI_ENTRY_PERIOD)

    all_days = sorted({datetime.fromtimestamp(t, tz=IST).date() for t in timestamps})
    test_days = set(all_days[-TEST_DAYS_BACK:])
    if not test_days:
        return []

    trades: list[dict] = []
    position = None  # {"side","entry_dt","entry_price","day"}
    last_bar_of_day: dict = {}  # date -> (dt, price) most recent bar seen for that day
    day = None
    last_bar = None

    def close_position(exit_dt, exit_price, exit_reason):
        pnl_per_share = (exit_price - position["entry_price"]) if position["side"] == "LONG" \
            else (position["entry_price"] - exit_price)
        qty = int(CAPITAL_PER_TRADE_RS // position["entry_price"]) or 1
        trades.append({
            "symbol": symbol, "interval": interval_minutes, "day": str(position["day"]),
            "side": position["side"],
            "entry_dt": str(position["entry_dt"]), "entry_price": round(position["entry_price"], 2),
            "exit_dt": str(exit_dt), "exit_price": round(exit_price, 2),
            "exit_reason": exit_reason, "qty": qty,
            "pnl_per_share": round(pnl_per_share, 2),
            "pnl_rs": round(pnl_per_share * qty, 2),
        })

    for i, t in enumerate(timestamps):
        dt = datetime.fromtimestamp(t, tz=IST)
        d = dt.date()
        if d not in test_days:
            continue
        if rsi50[i] is None or rsi10[i] is None or i == 0:
            continue
        prev_rsi10 = rsi10[i - 1]
        price = closes[i]
        t_of_day = (dt.hour, dt.minute)

        # New trading day: force-close anything still open at the previous
        # day's own last seen bar (real intraday trading - never carried
        # overnight).
        if day is not None and d != day and position is not None and last_bar is not None:
            close_position(last_bar[0], last_bar[1], "SESSION_END_SQUARE_OFF")
            position = None
        day = d

        # Intraday square-off / no-new-entry cutoff.
        past_square_off = t_of_day >= SQUARE_OFF_TIME
        past_entry_cutoff = t_of_day >= ENTRY_CUTOFF_TIME

        if position is not None and past_square_off:
            close_position(dt, price, "SQUARE_OFF_TIME")
            position = None

        # Exit: opposite RSI(10) trigger level.
        if position is not None:
            exit_reason = None
            if position["side"] == "LONG" and _cross_down(prev_rsi10, rsi10[i], ENTRY_LOW):
                exit_reason = "RSI10_CROSS_DOWN_40"
            elif position["side"] == "SHORT" and _cross_up(prev_rsi10, rsi10[i], ENTRY_HIGH):
                exit_reason = "RSI10_CROSS_UP_60"
            if exit_reason:
                close_position(dt, price, exit_reason)
                position = None

        # Entry: trend filter (RSI50) + entry trigger (RSI10 cross), only when flat.
        if position is None and not past_entry_cutoff:
            if rsi50[i] > 50 and _cross_up(prev_rsi10, rsi10[i], ENTRY_HIGH):
                position = {"side": "LONG", "entry_dt": dt, "entry_price": price, "day": d}
            elif rsi50[i] < 50 and _cross_down(prev_rsi10, rsi10[i], ENTRY_LOW):
                position = {"side": "SHORT", "entry_dt": dt, "entry_price": price, "day": d}

        last_bar = (dt, price)

    if position is not None and last_bar is not None:
        close_position(last_bar[0], last_bar[1], "SESSION_END_SQUARE_OFF_END_OF_DATA")

    return trades


def day_wise_report(trades: list[dict]) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_day[t["day"]].append(t)
    rows = []
    running = 0.0
    for d in sorted(by_day):
        day_trades = by_day[d]
        pnl = sum(t["pnl_rs"] for t in day_trades)
        running += pnl
        wins = sum(1 for t in day_trades if t["pnl_rs"] > 0)
        losses = sum(1 for t in day_trades if t["pnl_rs"] <= 0)
        rows.append({
            "day": d, "trades": len(day_trades), "wins": wins, "losses": losses,
            "pnl_rs": round(pnl, 2), "cumulative_pnl_rs": round(running, 2),
        })
    return rows


def summarize(label: str, trades: list[dict]) -> None:
    print(f"\n=== {label} ===")
    print(f"  Total trades: {len(trades)}")
    if not trades:
        return
    wins = [t for t in trades if t["pnl_rs"] > 0]
    losses = [t for t in trades if t["pnl_rs"] <= 0]
    total_rs = sum(t["pnl_rs"] for t in trades)
    win_rate = 100.0 * len(wins) / len(trades)
    avg_win = sum(t["pnl_rs"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["pnl_rs"] for t in losses) / len(losses) if losses else 0.0
    print(f"  Win rate: {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"  Avg win: Rs {avg_win:+,.0f}   Avg loss: Rs {avg_loss:+,.0f}")
    print(f"  Net PnL: Rs {total_rs:+,.0f}")
    by_reason: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_reason[t["exit_reason"]].append(t["pnl_rs"])
    for reason, pnls in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"    exit={reason}: {len(pnls)} trades, net Rs {sum(pnls):+,.0f}")
    worst = min(trades, key=lambda t: t["pnl_rs"])
    best = max(trades, key=lambda t: t["pnl_rs"])
    print(f"  Worst trade: Rs {worst['pnl_rs']:+,.0f} ({worst['symbol']} {worst['day']} {worst['side']})")
    print(f"  Best trade:  Rs {best['pnl_rs']:+,.0f} ({best['symbol']} {best['day']} {best['side']})")


def _authenticate_avoiding_session_collision() -> None:
    """See backtest_dual_ema_band_nifty_30day.py's identical helper / the
    incident writeup referenced in the module docstring. A hand-off token
    VALIDATES an existing session instead of minting a new one, so it
    doesn't kick out the live droplet bot's own session."""
    handoff = os.environ.get("HANDOFF_DHAN_ACCESS_TOKEN")
    if not handoff:
        raise SystemExit(
            "HANDOFF_DHAN_ACCESS_TOKEN not set - refusing to fall back to local pin_totp "
            "(would collide with the live droplet bot's session, see the module docstring)."
        )
    ocfg.DHAN_AUTH_MODE = "access_token"
    ocfg.DHAN_ACCESS_TOKEN = handoff
    print("[backtest] using hand-off access token (access_token mode) - live bot's own session left untouched.")
    dhan_wrapper.authenticate()


def main():
    print(f"[backtest] TEST_DAYS_BACK={TEST_DAYS_BACK} RSI_TREND_PERIOD={RSI_TREND_PERIOD} "
          f"RSI_ENTRY_PERIOD={RSI_ENTRY_PERIOD} INTERVALS_TO_TEST={INTERVALS_TO_TEST} "
          f"WATCHLIST={list(WATCHLIST)}")
    _authenticate_avoiding_session_collision()

    all_trades_by_interval: dict[int, list[dict]] = {}
    for interval in INTERVALS_TO_TEST:
        print(f"\n[backtest] === interval={interval}min ===")
        interval_trades: list[dict] = []
        for symbol, security_id in WATCHLIST.items():
            print(f"  Fetching {symbol} ({interval}min, {INTRADAY_LOOKBACK_DAYS}d lookback)...")
            trades = run_symbol_interval(symbol, security_id, interval)
            print(f"    {len(trades)} trades")
            interval_trades.extend(trades)
        all_trades_by_interval[interval] = interval_trades
        summarize(f"interval={interval}min (all symbols combined)", interval_trades)

    for interval, trades in all_trades_by_interval.items():
        rows = day_wise_report(trades)
        day_csv = CACHE_DIR / f"day_wise_{interval}min_{TEST_DAYS_BACK}day.csv"
        with day_csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["day", "trades", "wins", "losses", "pnl_rs", "cumulative_pnl_rs"])
            w.writeheader()
            w.writerows(rows)
        trade_csv = CACHE_DIR / f"trade_wise_{interval}min_{TEST_DAYS_BACK}day.csv"
        with trade_csv.open("w", newline="") as f:
            fieldnames = ["symbol", "day", "side", "entry_dt", "entry_price", "exit_dt",
                          "exit_price", "exit_reason", "qty", "pnl_per_share", "pnl_rs"]
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for t in sorted(trades, key=lambda t: (t["day"], t["entry_dt"])):
                w.writerow({k: t[k] for k in fieldnames})
        print(f"\n[backtest] interval={interval}min: day-wise -> {day_csv}, trade-wise -> {trade_csv}")

    out_file = CACHE_DIR / f"results_dual_rsi_50_10_{TEST_DAYS_BACK}day.json"
    out_file.write_text(json.dumps(all_trades_by_interval, default=str, indent=2))
    print(f"[backtest] Full trade log written to {out_file}")


if __name__ == "__main__":
    main()
