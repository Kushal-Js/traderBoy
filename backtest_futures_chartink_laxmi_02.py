"""
User request 13 Sep 2026: backtest the FUTURES strategy against a real
Chartink alert file ("02 laxmi.csv", 27 Aug - 11 Sep 2026 across 12
trading days, 54 unique symbols, CE-only scan). Futures is currently a
PLACEHOLDER strategy (buys ATM CE options via the identical mechanics as
Options/, standing in until real futures-contract buying replaces it -
see main.py's own module docstring) and its trading_engine.py is a
byte-for-byte copy of Options'/Luxury's own (only string literals/logger
name differ - confirmed via diff during this session's earlier "do we
have same entry/exit conditions" investigation). This script mirrors
backtest_options_chartink_range_breakout_03.py exactly, reading
Futures/config.py instead of Options/config.py - CE-only (Futures runs
no PE webhook in practice, per that same investigation), so there's no
prefer_highest/PE branch needed here either.

Modeled (identical mechanism to every Options/Luxury backtest this
session):
  - Batch ranking: every same-minute alert batch is ranked by day-change%
    (vs previous day's close) and only TOP_N_STOCKS are even attempted -
    specifically the BOTTOM N (SELECT_BOTTOM_N_STOCKS) - the WEAKEST
    performers of the batch.
  - MAX_LIVE_POSITIONS_CE capacity (whole batch ignored outright if
    already at capacity, not entered partially).
  - MAX_DAILY_ENTRIES_PER_SYMBOL cap.
  - Same-day RSI-gated loss re-entry block (ENABLE_RSI_LOSS_REENTRY_
    BLOCK): a symbol that hits MAX_LOSS_HIT stays blocked until RSI(14,
    5-min continuous) is neither overbought nor still falling vs the
    previous confirmed candle.
  - LOSS_REPEAT_BLOCK (same-day outcome-counting block on MAX_LOSS_HIT/
    STOP_LOSS_HIT specifically).
  - ENABLE_TRADING_TIME_LIMIT/ALLOWED_TRADING_TIME entry cutoff (reads
    whatever's actually live in Futures/config.py - unlike Options this
    has historically defaulted OFF in code, so check the printed banner
    below for what was actually in effect for this run).
  - Full exit ladder: MAX_LOSS_HIT -> TARGET_HIT -> PROFIT_PROTECTION_HIT
    (with PROFIT_PROTECTION_GIVEBACK_PCT) -> TRAILING_SL_HIT/STOP_LOSS_HIT
    (dynamic SL) -> SUPERTREND_EXIT. All read live from Futures/config.py
    at import time.

NOW MODELED (added 13 Sep 2026, user request): ENABLE_GAP_DOWN_CE_DELAY
(Nifty gap-down/sharp-fall CE cool-off) via nifty_gap_down_backtest_
helper.py - see that module's own docstring and
backtest_options_chartink_range_breakout_03.py's matching update.

NOT modeled (disclosed, not silently dropped - same standing limitations
as every prior pass this session):
  - SL-L broker-side stop-loss (real effect can only tighten a loss,
    never worsen it).
  - cross_strategy_registry (Options/Futures/Luxury mutual symbol lock).

Run via SSH on the droplet (never locally while the live bot's own
session is active):
    uv run python backtest_futures_chartink_laxmi_02.py [PP_BEFORE] [PP_AFTER] [GIVEBACK_PCT]
    (all three optional - default to whatever's live in Futures/config.py)
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

from Options.dhan_client import dhan_wrapper, _compute_supertrend, _compute_rsi, _retry  # noqa: E402
from Options import config as options_config  # noqa: E402
from Futures import config as futures_config  # noqa: E402
from nifty_gap_down_backtest_helper import fetch_nifty_continuous_cached, NiftyGapDownGate  # noqa: E402
from exit_ladder_backtest_helper import evaluate_exit_reason, compute_ema_cross_series, ExitLadderConfig, build_full_minute_grid  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

CSV_PATH = Path("/root/apps/traderBoy/02 laxmi.csv")
CACHE_DIR = Path("/private/tmp/claude-501/-Users-kushalgaur-Desktop-projects-trading-traderBoy/"
                  "60a0e686-2110-4e20-bad5-fe817a57a72a/scratchpad/futures_chartink_backtest_cache_laxmi02")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DAYS_BACK = 90
ST_PERIOD = 10
ST_MULT = 3.0
ENTRY_TF = 5
OPTION_TF = 1
QUANTITY_LOTS = futures_config.QUANTITY_LOTS
TOP_N_STOCKS = futures_config.TOP_N_STOCKS
SELECT_BOTTOM_N_STOCKS = futures_config.SELECT_BOTTOM_N_STOCKS
MAX_LIVE_POSITIONS_CE = futures_config.MAX_LIVE_POSITIONS_CE
MAX_DAILY_ENTRIES_PER_SYMBOL = futures_config.MAX_DAILY_ENTRIES_PER_SYMBOL
ENABLE_RSI_LOSS_REENTRY_BLOCK = futures_config.ENABLE_RSI_LOSS_REENTRY_BLOCK
LOSS_REPEAT_BLOCK_ENABLED = futures_config.LOSS_REPEAT_BLOCK_ENABLED
LOSS_REPEAT_BLOCK_COUNT = futures_config.LOSS_REPEAT_BLOCK_COUNT
LOSS_REPEAT_BLOCK_EXIT_REASONS = futures_config.LOSS_REPEAT_BLOCK_EXIT_REASONS
TARGET_PCT = futures_config.TARGET_PCT
STOP_LOSS_PCT = futures_config.STOP_LOSS_PCT
MAX_LOSS_BEFORE = futures_config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF
MAX_LOSS_AFTER = futures_config.MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF
PP_BEFORE = float(sys.argv[1]) if len(sys.argv) > 1 else futures_config.PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF
PP_AFTER = float(sys.argv[2]) if len(sys.argv) > 2 else futures_config.PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF
PP_GIVEBACK_PCT = float(sys.argv[3]) if len(sys.argv) > 3 else futures_config.PROFIT_PROTECTION_GIVEBACK_PCT
CUTOFF_H, CUTOFF_M = (int(x) for x in futures_config.RISK_THRESHOLD_CUTOFF_TIME.split(":"))
ENABLE_DYNAMIC_SL = futures_config.ENABLE_DYNAMIC_SL
DYNAMIC_SL_STEP_PCT_CE = futures_config.DYNAMIC_SL_STEP_PCT_CE
DYNAMIC_SL_INCREASE_PCT = futures_config.DYNAMIC_SL_INCREASE_PCT
ENABLE_SUPERTREND_EXIT = futures_config.ENABLE_SUPERTREND_EXIT
ENABLE_TRADING_TIME_LIMIT = futures_config.ENABLE_TRADING_TIME_LIMIT
ALLOWED_TRADING_TIME = futures_config.ALLOWED_TRADING_TIME
ALLOWED_H, ALLOWED_M = (int(x) for x in ALLOWED_TRADING_TIME.split(":"))
RSI_PERIOD = options_config.RSI_LOSS_REENTRY_PERIOD
RSI_OVERBOUGHT = options_config.RSI_LOSS_REENTRY_OVERBOUGHT
ENABLE_GAP_DOWN_CE_DELAY = futures_config.ENABLE_GAP_DOWN_CE_DELAY

print(f"[backtest] MAX_LIVE_POSITIONS_CE={MAX_LIVE_POSITIONS_CE} TOP_N_STOCKS={TOP_N_STOCKS} "
      f"SELECT_BOTTOM_N_STOCKS={SELECT_BOTTOM_N_STOCKS} MAX_DAILY_ENTRIES_PER_SYMBOL={MAX_DAILY_ENTRIES_PER_SYMBOL} "
      f"TARGET_PCT={TARGET_PCT} STOP_LOSS_PCT={STOP_LOSS_PCT} MAX_LOSS={MAX_LOSS_BEFORE}/{MAX_LOSS_AFTER} "
      f"PROFIT_PROTECTION={PP_BEFORE}/{PP_AFTER} GIVEBACK_PCT={PP_GIVEBACK_PCT} cutoff={CUTOFF_H}:{CUTOFF_M:02d} "
      f"DYNAMIC_SL={ENABLE_DYNAMIC_SL}({DYNAMIC_SL_STEP_PCT_CE}/{DYNAMIC_SL_INCREASE_PCT}) "
      f"OPTION_TF={OPTION_TF}min LOSS_REPEAT_BLOCK={LOSS_REPEAT_BLOCK_ENABLED}(count={LOSS_REPEAT_BLOCK_COUNT}) "
      f"RSI_LOSS_REENTRY_BLOCK={ENABLE_RSI_LOSS_REENTRY_BLOCK}(period={RSI_PERIOD},overbought={RSI_OVERBOUGHT}) "
      f"ENTRY_CUTOFF={ENABLE_TRADING_TIME_LIMIT}({ALLOWED_TRADING_TIME})")


def parse_alerts() -> list[tuple[datetime, list[str]]]:
    rows = []
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            dt = datetime.strptime(row["Date"], "%d-%m-%Y %I:%M %p").replace(tzinfo=IST)
            rows.append((dt, row["Symbol"].strip().upper()))
    rows.sort(key=lambda r: r[0])
    grouped: dict[datetime, list[str]] = defaultdict(list)
    for dt, sym in rows:
        if sym not in grouped[dt]:
            grouped[dt].append(sym)
    return sorted(grouped.items(), key=lambda kv: kv[0])


def fetch_5min_cached(symbol: str) -> dict:
    cache_file = CACHE_DIR / f"{symbol}_5min.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    security_id = dhan_wrapper._equity_security_id(symbol)
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data,
                  security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
                  from_date=from_date, to_date=to_date, interval=ENTRY_TF)
    data = resp.get("data") or {}
    result = {"highs": data.get("high") or [], "lows": data.get("low") or [],
              "closes": data.get("close") or [], "timestamps": data.get("timestamp") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


def nearest_ce_for_strike_ref(underlying: str, ref_price: float) -> dict:
    df = dhan_wrapper.instruments()
    opts = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")
              & (df["SEM_OPTION_TYPE"] == "CE")]
    matches = opts[opts["SEM_TRADING_SYMBOL"].apply(
        lambda s: dhan_wrapper._underlying_from_trading_symbol(str(s)) == underlying
    )]
    if matches.empty:
        raise ValueError(f"No CE contracts found for {underlying}")
    nearest_expiry = matches["SEM_EXPIRY_DATE"].min()
    matches = matches[matches["SEM_EXPIRY_DATE"] == nearest_expiry].copy()
    matches["dist"] = (matches["SEM_STRIKE_PRICE"] - ref_price).abs()
    row = matches.sort_values("dist").iloc[0]
    return {
        "security_id": str(int(row["SEM_SMST_SECURITY_ID"])), "trading_symbol": str(row["SEM_CUSTOM_SYMBOL"]),
        "strike": float(row["SEM_STRIKE_PRICE"]), "lot_size": int(float(row["SEM_LOT_UNITS"])),
    }


def fetch_option_1min_cached(security_id: str) -> dict:
    """Caches closes/timestamps/volumes (volume added 14 Sep 2026 for the
    liquidity-guard exit). A cache file written before that lacks
    "volumes" - treated as a cache miss so it gets re-fetched."""
    cache_file = CACHE_DIR / f"OPT_{security_id}_{OPTION_TF}min.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text())
        if "volumes" in cached:
            return cached
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    try:
        resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data,
                      security_id=security_id, exchange_segment="NSE_FNO", instrument_type="OPTSTK",
                      from_date=from_date, to_date=to_date, interval=OPTION_TF)
        data = resp.get("data") or {}
    except Exception as exc:  # noqa: BLE001
        print(f"    OPTION FETCH FAILED for {security_id}: {exc}")
        data = {}
    result = {"closes": data.get("close") or [], "timestamps": data.get("timestamp") or [],
              "volumes": data.get("volume") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


def idx_at_or_after(ts_list, target_ts):
    for i, ts in enumerate(ts_list):
        if ts >= target_ts:
            return i
    return None


def idx_at_or_before(ts_list, target_ts):
    best = None
    for i, ts in enumerate(ts_list):
        if ts <= target_ts:
            best = i
        else:
            break
    return best


def price_at_or_before(ts_list, closes, target_ts):
    idx = idx_at_or_before(ts_list, target_ts)
    return closes[idx] if idx is not None else None


def current_max_loss_rs(dt: datetime) -> float:
    return MAX_LOSS_BEFORE if dt.time() < dtime(CUTOFF_H, CUTOFF_M) else MAX_LOSS_AFTER


def current_profit_protection_rs(dt: datetime) -> float:
    return PP_BEFORE if dt.time() < dtime(CUTOFF_H, CUTOFF_M) else PP_AFTER


def current_trailing_sl(entry_price: float, highest_price: float, hard_stop_loss: float) -> float:
    floor = hard_stop_loss
    if ENABLE_DYNAMIC_SL and entry_price:
        pct_up = (highest_price - entry_price) / entry_price
        steps = int(pct_up // DYNAMIC_SL_STEP_PCT_CE) if pct_up > 0 else 0
        if steps > 0:
            dynamic_sl_pct = STOP_LOSS_PCT - steps * DYNAMIC_SL_INCREASE_PCT
            floor = max(floor, entry_price * (1 - dynamic_sl_pct))
    return floor


def main():
    alerts = parse_alerts()
    all_symbols = sorted({sym for _, syms in alerts for sym in syms})
    print(f"[backtest] {len(alerts)} distinct alert events, {len(all_symbols)} unique symbols: {all_symbols}")

    nifty_gate = None
    if ENABLE_GAP_DOWN_CE_DELAY:
        nifty_data = fetch_nifty_continuous_cached(dhan_wrapper, _retry, CACHE_DIR, DAYS_BACK)
        nifty_gate = NiftyGapDownGate(
            nifty_data,
            threshold_points=options_config.GAP_DOWN_THRESHOLD_POINTS,
            sharp_fall_pct=options_config.GAP_DOWN_SHARP_FALL_PCT,
            ce_delay_minutes=options_config.GAP_DOWN_CE_DELAY_MINUTES,
            extra_delay_minutes_per_100_points=options_config.GAP_DOWN_EXTRA_DELAY_MINUTES_PER_100_POINTS,
            max_delay_minutes=options_config.GAP_DOWN_MAX_DELAY_MINUTES,
            recovery_gate_enabled=options_config.ENABLE_NIFTY_RECOVERY_GATE,
            market_open_time=options_config.MARKET_OPEN_TIME,
        )
        print(f"[backtest] Nifty gap-down CE delay ENABLED, {len(nifty_gate.bars)} NIFTY 1-min bars loaded")

    ladder_cfg = ExitLadderConfig(pkg_config=futures_config)
    print(f"[backtest] Exit ladder: TARGET_EXIT={ladder_cfg.enable_target_exit} "
          f"MAX_LOSS_BEFORE_CUTOFF={ladder_cfg.enable_max_loss_before_cutoff} "
          f"SUPERTREND_EXIT={ladder_cfg.enable_supertrend_exit} "
          f"EMA_CROSS_EXIT={ladder_cfg.enable_ema_cross_exit}({ladder_cfg.ema_cross_fast}/{ladder_cfg.ema_cross_slow}) "
          f"LIQUIDITY_GUARD={ladder_cfg.liquidity_guard_enabled}(bars={ladder_cfg.liquidity_guard_zero_volume_bars})")

    udata: dict[str, dict] = {}
    ust: dict[str, list] = {}
    ema_cross: dict[str, list] = {}
    rsi_series: dict[str, list] = {}
    day_last_close: dict[str, dict] = {}
    skipped_symbols: set[str] = set()
    for sym in all_symbols:
        try:
            d = fetch_5min_cached(sym)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: SKIPPED - could not fetch underlying data ({exc})")
            skipped_symbols.add(sym)
            continue
        if not d["closes"]:
            print(f"  {sym}: SKIPPED - no underlying candle data returned")
            skipped_symbols.add(sym)
            continue
        s = _compute_supertrend(d["highs"], d["lows"], d["closes"], period=ST_PERIOD, multiplier=ST_MULT)
        r = _compute_rsi(d["closes"], RSI_PERIOD)
        e = compute_ema_cross_series(d["closes"], ladder_cfg.ema_cross_fast, ladder_cfg.ema_cross_slow)
        udata[sym] = d
        ust[sym] = s
        ema_cross[sym] = e
        rsi_series[sym] = r
        by_date: dict = {}
        for ts, c in zip(d["timestamps"], d["closes"]):
            by_date[datetime.fromtimestamp(ts, tz=IST).date()] = c
        day_last_close[sym] = by_date
        print(f"  {sym}: {len(d['closes'])} 5-min bars")

    def day_change_pct_at(sym, dt):
        price_now = price_at_or_before(udata[sym]["timestamps"], udata[sym]["closes"], dt.timestamp())
        if price_now is None:
            return None
        prior_dates = sorted(d for d in day_last_close[sym] if d < dt.date())
        if not prior_dates:
            return None
        prev_close = day_last_close[sym][prior_dates[-1]]
        if not prev_close:
            return None
        return (price_now - prev_close) / prev_close * 100.0

    def rsi_at(sym, dt):
        ts_list = udata[sym]["timestamps"]
        idx = idx_at_or_before(ts_list, dt.timestamp())
        if idx is None or idx < 1:
            return None, None
        return rsi_series[sym][idx], rsi_series[sym][idx - 1]

    # Full 1-minute grid (09:15-15:29) per trading day, NOT derived from
    # the 5-min underlying timestamps - Dhan's 5-min NSE_EQ candles stop at
    # the 15:10 bar, so extending them would blind the sim clock to the
    # last ~15 minutes of each day (the documented sim-clock trap - see
    # exit_ladder_backtest_helper.build_full_minute_grid's own docstring).
    master_ts = build_full_minute_grid(udata)

    alert_queue = list(alerts)
    live: dict[str, dict] = {}
    trades: list[dict] = []
    entries_today: dict[tuple, int] = defaultdict(int)
    max_loss_hits_today: dict[tuple, int] = defaultdict(int)
    loss_repeat_count_today: dict[tuple, int] = defaultdict(int)

    def try_enter_one(alert_dt, sym):
        if sym in live:
            return False
        key = (alert_dt.date(), sym)
        if entries_today[key] >= MAX_DAILY_ENTRIES_PER_SYMBOL:
            print(f"  SKIP  {sym} @ {alert_dt} reason=daily_reentry_cap_reached "
                  f"({entries_today[key]}/{MAX_DAILY_ENTRIES_PER_SYMBOL})")
            return False
        if ENABLE_RSI_LOSS_REENTRY_BLOCK and max_loss_hits_today[key] >= 1:
            rsi, prev_rsi = rsi_at(sym, alert_dt)
            if rsi is not None and prev_rsi is not None and (rsi > RSI_OVERBOUGHT or rsi < prev_rsi):
                reason = "overbought" if rsi > RSI_OVERBOUGHT else "falling"
                print(f"  SKIP  {sym} @ {alert_dt} reason=rsi_loss_reentry_block_active "
                      f"(RSI={rsi:.1f} {reason}, prev={prev_rsi:.1f})")
                return False
        if LOSS_REPEAT_BLOCK_ENABLED and loss_repeat_count_today[key] >= LOSS_REPEAT_BLOCK_COUNT:
            print(f"  SKIP  {sym} @ {alert_dt} reason=loss_repeat_block_active "
                  f"(already {loss_repeat_count_today[key]} loss-designated exit(s) today)")
            return False

        ts_list, closes = udata[sym]["timestamps"], udata[sym]["closes"]
        entry_idx = idx_at_or_after(ts_list, alert_dt.timestamp())
        if entry_idx is None:
            return False
        entry_underlying_price = closes[entry_idx]
        entry_dt = datetime.fromtimestamp(ts_list[entry_idx], tz=IST)
        try:
            ce = nearest_ce_for_strike_ref(sym, entry_underlying_price)
            ce_data = fetch_option_1min_cached(ce["security_id"])
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: SKIPPED entry @ {alert_dt} - could not resolve/fetch its ATM CE ({exc})")
            skipped_symbols.add(sym)
            return False
        entry_price = price_at_or_before(ce_data["timestamps"], ce_data["closes"], entry_dt.timestamp())
        if entry_price is None:
            return False
        hard_stop_loss = entry_price * (1 - STOP_LOSS_PCT)
        target_price = entry_price * (1 + TARGET_PCT)
        print(f"  ENTER {sym} @ {entry_dt} CE={ce['trading_symbol']} entry_price={entry_price}")
        live[sym] = {
            "entry_dt": entry_dt, "entry_price": entry_price, "highest_price": entry_price,
            "hard_stop_loss": hard_stop_loss, "target_price": target_price,
            "ce": ce, "ce_data": ce_data, "entry_ts": entry_dt.timestamp(),
            "entry_candle_ts": ts_list[entry_idx],
        }
        entries_today[key] += 1
        return True

    def try_enter_batch(alert_dt, syms):
        if ENABLE_TRADING_TIME_LIMIT and alert_dt.time() >= dtime(ALLOWED_H, ALLOWED_M):
            print(f"  IGNORE batch @ {alert_dt} {syms} reason=past_allowed_trading_time "
                  f"(cutoff={ALLOWED_TRADING_TIME})")
            return
        if nifty_gate is not None:
            delay, cond = nifty_gate.should_delay_ce_entry_at(alert_dt)
            if delay:
                print(f"  IGNORE batch @ {alert_dt} {syms} reason=nifty_gap_down_ce_delay "
                      f"(gap={cond.get('gap_points')} fall={cond.get('fall_pct')}% "
                      f"delay_until={cond['delay_until'].strftime('%H:%M') if cond.get('delay_until') else None})")
                return
        remaining = MAX_LIVE_POSITIONS_CE - len(live)
        if remaining <= 0:
            print(f"  IGNORE batch @ {alert_dt} {syms} reason=max_live_positions_reached "
                  f"({len(live)}/{MAX_LIVE_POSITIONS_CE})")
            return

        candidates = [s for s in syms if s not in skipped_symbols]
        scored = []
        for s in candidates:
            pct = day_change_pct_at(s, alert_dt)
            if pct is None:
                print(f"  {s}: SKIPPED ranking @ {alert_dt} - could not compute day-change "
                      f"(no prior-day close in fetched window)")
                continue
            scored.append((s, pct))
        if not scored:
            print(f"  IGNORE batch @ {alert_dt} {syms} reason=could_not_rank_any_stock")
            return

        scored.sort(key=lambda t: t[1], reverse=True)  # prefer_highest=True for CE
        ranked = [s for s, _ in (scored[-TOP_N_STOCKS:] if SELECT_BOTTOM_N_STOCKS else scored[:TOP_N_STOCKS])]
        print(f"  RANKED @ {alert_dt}: batch={len(syms)} scored={len(scored)} -> picked(bottom-N)={ranked} "
              f"(day-change%: {dict(scored)})")

        for sym in ranked:
            if len(live) >= MAX_LIVE_POSITIONS_CE:
                break
            try_enter_one(alert_dt, sym)

    for t in master_ts:
        for sym in list(live.keys()):
            pos = live[sym]
            if t <= pos["entry_ts"]:
                continue
            premium = price_at_or_before(pos["ce_data"]["timestamps"], pos["ce_data"]["closes"], t)
            if premium is None:
                continue
            pos["highest_price"] = max(pos["highest_price"], premium)
            dt = datetime.fromtimestamp(t, tz=IST)
            qty = pos["ce"]["lot_size"] * QUANTITY_LOTS
            trailing_sl = current_trailing_sl(pos["entry_price"], pos["highest_price"], pos["hard_stop_loss"])
            reason = evaluate_exit_reason(
                option_type="CE", entry_price=pos["entry_price"], highest_price=pos["highest_price"],
                hard_stop_loss=pos["hard_stop_loss"], target_price=pos["target_price"], trailing_sl=trailing_sl,
                premium=premium, qty=qty, dt=dt,
                current_max_loss_rs=current_max_loss_rs(dt), current_profit_protection_rs=current_profit_protection_rs(dt),
                giveback_pct=PP_GIVEBACK_PCT,
                underlying_ts_list=udata[sym]["timestamps"], underlying_closes=udata[sym]["closes"],
                supertrend_values=ust[sym], ema_cross_series=ema_cross[sym],
                entry_candle_ts=pos["entry_candle_ts"], t=t,
                option_volumes=pos["ce_data"]["volumes"], option_ts_list=pos["ce_data"]["timestamps"],
                ladder_config=ladder_cfg,
            )
            if reason:
                pnl = (premium - pos["entry_price"]) * qty
                print(f"  EXIT  {sym} @ {dt} reason={reason} exit_price={premium} pnl={pnl:+.0f}")
                trades.append({
                    "symbol": sym, "entry_dt": str(pos["entry_dt"]), "entry_price": pos["entry_price"],
                    "exit_dt": str(dt), "exit_price": premium, "exit_reason": reason,
                    "quantity": qty, "pnl": pnl, "status": "closed",
                })
                key = (dt.date(), sym)
                if reason == "MAX_LOSS_HIT":
                    max_loss_hits_today[key] += 1
                if reason in LOSS_REPEAT_BLOCK_EXIT_REASONS:
                    loss_repeat_count_today[key] += 1
                del live[sym]

        while alert_queue and alert_queue[0][0].timestamp() <= t:
            alert_dt, syms = alert_queue.pop(0)
            try_enter_batch(alert_dt, syms)

    for sym, pos in live.items():
        trades.append({
            "symbol": sym, "entry_dt": str(pos["entry_dt"]), "entry_price": pos["entry_price"],
            "status": "still open through end of available data", "pnl": None,
        })

    (CACHE_DIR / "results_laxmi_02.json").write_text(json.dumps(trades, default=str, indent=2))

    print(f"\n[backtest] {len(trades)} trades")
    print("\n=== TRADE-WISE P&L ===")
    total = 0.0
    for i, tr in enumerate(trades, 1):
        pnl = tr.get("pnl")
        if pnl is not None:
            total += pnl
        print(f"{i}. {tr['symbol']} entry={tr['entry_dt']} exit={tr.get('exit_dt', 'N/A')} "
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
        print(f"{d}: trades={s['trades']:2d} wins={s['wins']:2d} losses={s['losses']:2d} "
              f"net_pnl={s['pnl']:+.0f}")

    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    print(f"\n=== SUMMARY ===")
    print(f"Total trades: {len(trades)} (closed={len(closed)}, still-open={len(trades) - len(closed)})")
    print(f"Wins: {len(wins)}  Losses: {len(losses)}  "
          f"Win rate: {(len(wins) / len(closed) * 100) if closed else 0:.1f}%")
    print(f"TOTAL net P&L: Rs {total:+.0f}")
    if skipped_symbols:
        print(f"\nSKIPPED symbols (no usable data/no F&O options found): {sorted(skipped_symbols)}")


if __name__ == "__main__":
    main()
