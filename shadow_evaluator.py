"""
Shadow trade evaluator - the "arm" (user request 10 Sep 2026: "deploy an
arm to continuously monitor trades based on incoming alerts and evaluate
PnL").

For EVERY distinct symbol alerted to Options / Luxury / Futures today
(whether or not the real bot took it - blocked by a trading window, a
capacity cap, the old cutoff, a cooldown, choppy filter, whatever), open
ONE simulated CE-ATM position at the first alert time and track it through
the production exit ladder until it closes or the day ends. ZERO real
orders - only read-only historical Dhan calls (reuses the bot's already-
validated cached token, same as every backtest this session).

State  : history/<date>_shadow_positions.json   (open + closed shadow trades)
Report : history/<date>_shadow_pnl.log          (one summary line per run)
         + prints the summary to stdout / the systemd journal

Run by a systemd timer every 10 min during market hours; each invocation
is self-contained (persistent state = the JSON file). Paces its own Dhan
calls (1.6s apart, DH-904 rate-limit aware) and fetches each contract at
most once per run so it never starves the live bot's own REST budget.

Simplifications (this measures what the alert STREAM predicted, not a full
bot replay): one shadow trade per underlying per day, first alert wins; no
capacity cap / re-entry / cooldown / repeat-block / liquidity guard /
cross-strategy dedup; entry = the CE's 1-min close at/just after the alert
minute (no real fill; thin OTM contracts differ - see backtest-methodology);
exit ladder on 1-min option closes + 5-min underlying Supertrend, real
Options config thresholds; broker SL-L not modelled; EOD at SQUARE_OFF_TIME.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from Options.dhan_client import dhan_wrapper, _compute_supertrend  # noqa: E402
from Options import config as c  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")
HISTORY = REPO_ROOT / "history"
ST_PERIOD, ST_MULT, ENTRY_TF, OPTION_TF = 10, 3.0, 5, 1
SHADOW_STRATEGIES = {"Options", "Luxury", "Futures"}
PACE_SECONDS = 1.6

_run_cache: dict = {}
_last_call = [0.0]


def _today_iso():
    return datetime.now(IST).strftime("%Y-%m-%d")


def state_path():
    return HISTORY / f"{_today_iso()}_shadow_positions.json"


def load_state():
    p = state_path()
    return json.loads(p.read_text()) if p.exists() else {"open": {}, "closed": []}


def save_state(st):
    state_path().write_text(json.dumps(st, indent=1, default=str))


def _paced(fn, **kw):
    dt = time.monotonic() - _last_call[0]
    if dt < PACE_SECONDS:
        time.sleep(PACE_SECONDS - dt)
    for attempt in range(4):
        try:
            resp = fn(**kw)
        except Exception as exc:  # noqa: BLE001
            resp = {"status": "failure", "remarks": str(exc)}
        _last_call[0] = time.monotonic()
        if isinstance(resp, dict) and resp.get("status") == "success":
            return resp
        rm = str(resp.get("remarks") if isinstance(resp, dict) else resp)
        if "DH-904" in rm or "Rate_Limit" in rm or "Too many" in rm:
            time.sleep(4 + 3 * attempt)
            continue
        return resp
    return resp


def _min_data(security_id: str, seg: str, itype: str, interval: int, days_back: int) -> dict:
    key = (security_id, interval)
    if key in _run_cache:
        return _run_cache[key]
    to_d = datetime.now(IST).strftime("%Y-%m-%d")
    frm = (datetime.now(IST) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    resp = _paced(dhan_wrapper.client.Dhan.intraday_minute_data, security_id=security_id,
                  exchange_segment=seg, instrument_type=itype, from_date=frm, to_date=to_d, interval=interval)
    d = resp.get("data") or {} if isinstance(resp, dict) else {}
    out = {"o": d.get("open") or [], "h": d.get("high") or [], "l": d.get("low") or [],
           "cl": d.get("close") or [], "ts": d.get("timestamp") or []}
    _run_cache[key] = out
    return out


def _underlying(sym):
    try:
        return _min_data(dhan_wrapper._equity_security_id(sym), "NSE_EQ", "EQUITY", ENTRY_TF, 4)
    except Exception:  # noqa: BLE001
        return {"o": [], "h": [], "l": [], "cl": [], "ts": []}


def _option(security_id):
    return _min_data(security_id, "NSE_FNO", "OPTSTK", OPTION_TF, 4)


def real_trades_today() -> dict[str, dict]:
    """{underlying_symbol: its FIRST closed real trade today} across the
    shadow strategies. For a symbol the bot actually traded we use the
    REAL entry/exit/pnl (known) instead of re-simulating - the simulator
    can pick a different ATM strike or a candle price that diverges from
    the real fill (OIL, 10 Sep: sim tracked the 510 CE @ 16.50 candle and
    lost, the bot really traded the 505 CE @ 14.40 fill and made +2,030)."""
    p = HISTORY / f"{_today_iso()}_real_trades.log"
    out: dict[str, dict] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("strategy") not in SHADOW_STRATEGIES or r.get("exit_price") is None:
            continue
        s = (r.get("underlying_symbol") or "").strip().upper()
        if s and (s not in out or r["opened_at"] < out[s]["opened_at"]):
            out[s] = r
    return out


def first_alerts_today() -> dict[str, str]:
    p = HISTORY / f"{_today_iso()}_webhook_alerts.log"
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("strategy") not in SHADOW_STRATEGIES:
            continue
        dt = datetime.fromisoformat(r["logged_at"]).replace(tzinfo=UTC).astimezone(IST)
        for s in (r.get("stocks") or []):
            s = s.strip().upper()
            if s and (s not in out or dt.isoformat() < out[s]):
                out[s] = dt.isoformat()
    return out


def _nearest_ce(underlying: str, ref_price: float):
    df = dhan_wrapper.instruments()
    opts = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")
              & (df["SEM_OPTION_TYPE"] == "CE")]
    m = opts[opts["SEM_TRADING_SYMBOL"].apply(
        lambda x: dhan_wrapper._underlying_from_trading_symbol(str(x)) == underlying)]
    if m.empty:
        return None
    m = m[m["SEM_EXPIRY_DATE"] == m["SEM_EXPIRY_DATE"].min()].copy()
    m["d"] = (m["SEM_STRIKE_PRICE"] - ref_price).abs()
    row = m.sort_values("d").iloc[0]
    return {"security_id": str(int(row["SEM_SMST_SECURITY_ID"])),
            "trading_symbol": str(row["SEM_CUSTOM_SYMBOL"]),
            "strike": float(row["SEM_STRIKE_PRICE"]), "lot_size": int(float(row["SEM_LOT_UNITS"]))}


def _px_at_or_before(ts, cl, target):
    best = None
    for t, v in zip(ts, cl):
        if t <= target:
            best = v
        else:
            break
    return best


def _first_px_after(ts, cl, target, within_s=1800):
    for t, v in zip(ts, cl):
        if target <= t <= target + within_s:
            return v
    return None


def _idx_at_or_after(ts, target):
    for i, t in enumerate(ts):
        if t >= target:
            return i
    return None


def open_new_shadows(st):
    alerts = first_alerts_today()
    real = real_trades_today()
    now_ts = datetime.now(IST).timestamp()
    seen = set(st["open"]) | {x["symbol"] for x in st["closed"]}
    for sym, alert_iso in sorted(alerts.items(), key=lambda kv: kv[1]):
        if sym in seen:
            continue
        alert_dt = datetime.fromisoformat(alert_iso)
        if alert_dt.timestamp() > now_ts:
            continue

        # If the bot ACTUALLY traded this symbol today, record its real
        # outcome (known) rather than re-simulating a possibly-different
        # strike / candle entry.
        if sym in real:
            rt = real[sym]
            pnl = rt.get("pnl")
            if pnl is None and rt.get("exit_price") is not None and rt.get("entry_price") is not None:
                pnl = (rt["exit_price"] - rt["entry_price"]) * rt["quantity"]
            st["closed"].append({
                "symbol": sym, "alert_dt": alert_iso, "source": "real",
                "trading_symbol": rt.get("option_trading_symbol"),
                "entry_price": round(rt["entry_price"], 2), "exit_price": round(rt["exit_price"], 2),
                "quantity": rt["quantity"], "highest": round(rt["exit_price"], 2),
                "exit_reason": f"REAL/{rt.get('exit_reason')}", "exit_dt": rt.get("closed_at"),
                "pnl": round(pnl or 0, 2), "status": "closed",
            })
            print(f"  = REAL {sym:<12} {rt.get('exit_reason'):<20} pnl={pnl or 0:+.0f}  (bot actually traded this)")
            continue

        und = _underlying(sym)
        if not und["cl"]:
            print(f"  {sym}: skip - no underlying data (rate-limited? retries next run)")
            continue
        ui = _idx_at_or_after(und["ts"], alert_dt.timestamp())
        if ui is None:
            print(f"  {sym}: skip - alert after last underlying bar")
            continue
        ce = _nearest_ce(sym, und["cl"][ui])
        if ce is None:
            print(f"  {sym}: skip - no CE contracts")
            continue
        opt = _option(ce["security_id"])
        entry = _px_at_or_before(opt["ts"], opt["cl"], alert_dt.timestamp() + 60) \
            or _first_px_after(opt["ts"], opt["cl"], alert_dt.timestamp())
        if entry is None:
            print(f"  {sym}: skip - no {ce['trading_symbol']} price near alert (retries next run)")
            continue
        qty = ce["lot_size"] * c.QUANTITY_LOTS
        st["open"][sym] = {
            "symbol": sym, "alert_dt": alert_iso, "source": "shadow",
            "trading_symbol": ce["trading_symbol"],
            "security_id": ce["security_id"], "strike": ce["strike"], "lot_size": ce["lot_size"],
            "entry_price": round(entry, 2), "quantity": qty, "highest": round(entry, 2),
            "entry_underlying_ts": und["ts"][ui], "opened_eval_at": datetime.now(IST).isoformat(),
        }
        print(f"  + OPEN {sym:<12} {ce['trading_symbol']:<26} entry={entry:<7.2f} qty={qty}  (alerted {alert_dt.strftime('%H:%M')})")


def eval_open_shadows(st):
    now = datetime.now(IST)
    cut_h, cut_m = (int(x) for x in c.RISK_THRESHOLD_CUTOFF_TIME.split(":"))
    sq_h, sq_m = (int(x) for x in c.SQUARE_OFF_TIME.split(":"))
    gb = getattr(c, "PROFIT_PROTECTION_GIVEBACK_PCT", 0.0)
    for sym in list(st["open"]):
        pos = st["open"][sym]
        opt = _option(pos["security_id"])
        if not opt["cl"]:
            continue
        und = _underlying(sym)
        ust = _compute_supertrend(und["h"], und["l"], und["cl"], period=ST_PERIOD, multiplier=ST_MULT) if und["cl"] else []
        entry, qty = pos["entry_price"], pos["quantity"]
        hard_sl, target = entry * (1 - c.STOP_LOSS_PCT), entry * (1 + c.TARGET_PCT)
        entry_ts = datetime.fromisoformat(pos["alert_dt"]).timestamp()
        highest = pos["highest"]
        reason = ex_px = ex_dt = None
        for t in [x for x in opt["ts"] if x > entry_ts]:
            dt = datetime.fromtimestamp(t, tz=IST)
            px = _px_at_or_before(opt["ts"], opt["cl"], t)
            if px is None:
                continue
            highest = max(highest, px)
            if dt.time() >= dtime(sq_h, sq_m):
                reason, ex_px, ex_dt = "EOD_SQUARE_OFF", px, dt
                break
            bc = dt.time() < dtime(cut_h, cut_m)
            ml = c.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF if bc else c.MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF
            pp = c.PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF if bc else c.PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF
            if (entry - px) * qty >= ml:
                reason, ex_px, ex_dt = "MAX_LOSS_HIT", px, dt
                break
            if px >= target:
                reason, ex_px, ex_dt = "TARGET_HIT", px, dt
                break
            if (highest - entry) * qty > pp and px < highest * (1 - gb):
                reason, ex_px, ex_dt = "PROFIT_PROTECTION_HIT", px, dt
                break
            trail = hard_sl
            if c.ENABLE_DYNAMIC_SL and entry:
                steps = int(((highest - entry) / entry) // c.DYNAMIC_SL_STEP_PCT_CE) if highest > entry else 0
                if steps > 0:
                    trail = max(trail, entry * (1 - (c.STOP_LOSS_PCT - steps * c.DYNAMIC_SL_INCREASE_PCT)))
            if px <= trail:
                reason, ex_px, ex_dt = ("TRAILING_SL_HIT" if trail > hard_sl else "STOP_LOSS_HIT"), px, dt
                break
            if c.ENABLE_SUPERTREND_EXIT and ust and und["ts"]:
                uidx = _idx_at_or_after(und["ts"], t)
                if (uidx and uidx > 0 and und["ts"][uidx] > pos["entry_underlying_ts"]
                        and und["ts"][uidx] <= t + 300 and ust[uidx] is not None
                        and und["cl"][uidx] < ust[uidx]):
                    reason, ex_px, ex_dt = "SUPERTREND_EXIT", px, dt
                    break
        pos["highest"] = round(highest, 2)
        if reason:
            pnl = (ex_px - entry) * qty
            pos.update(exit_reason=reason, exit_price=round(ex_px, 2), exit_dt=ex_dt.isoformat(),
                       pnl=round(pnl, 2), status="closed")
            st["closed"].append(pos)
            del st["open"][sym]
            print(f"  - CLOSE {sym:<12} {reason:<21} exit={ex_px:<7.2f} pnl={pnl:+.0f}")
        else:
            cur = _px_at_or_before(opt["ts"], opt["cl"], now.timestamp())
            pos["mark_price"] = round(cur, 2) if cur is not None else entry
            pos["unrealized_pnl"] = round(((cur if cur is not None else entry) - entry) * qty, 2)


def report(st):
    closed, opn = st["closed"], st["open"]
    realized = sum(x.get("pnl", 0) for x in closed)
    unreal = sum(x.get("unrealized_pnl", 0) for x in opn.values())
    wins = [x for x in closed if x.get("pnl", 0) > 0]
    real_pnl = sum(x.get("pnl", 0) for x in closed if x.get("source") == "real")
    n_real = sum(1 for x in closed if x.get("source") == "real")
    sim_pnl = realized - real_pnl + unreal
    line = (f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M')} | {len(closed)} closed "
            f"({len(wins)}W/{len(closed) - len(wins)}L) realized={realized:+.0f} | "
            f"{len(opn)} open unrealized={unreal:+.0f} | NET={realized + unreal:+.0f} "
            f"[real-traded {n_real}: {real_pnl:+.0f} | missed/simulated: {sim_pnl:+.0f}]")
    print("\n" + line)
    with (HISTORY / f"{_today_iso()}_shadow_pnl.log").open("a") as f:
        f.write(line + "\n")
    if closed or opn:
        print(f"\n{'sym':<13}{'alert':>6}{'src':>7}{'entry':>9}{'now/exit':>10}{'qty':>7}  {'reason':<22}{'pnl':>9}")
        for x in sorted(closed, key=lambda z: z.get("alert_dt", "")):
            print(f"{x['symbol']:<13}{datetime.fromisoformat(x['alert_dt']).strftime('%H:%M'):>6}"
                  f"{x.get('source', 'shadow'):>7}{x['entry_price']:>9.2f}{x['exit_price']:>10.2f}{x['quantity']:>7}  "
                  f"{x['exit_reason']:<22}{x['pnl']:>+9.0f}")
        for x in sorted(opn.values(), key=lambda z: z.get("alert_dt", "")):
            print(f"{x['symbol']:<13}{datetime.fromisoformat(x['alert_dt']).strftime('%H:%M'):>6}"
                  f"{x.get('source', 'shadow'):>7}{x['entry_price']:>9.2f}{x.get('mark_price', x['entry_price']):>10.2f}{x['quantity']:>7}  "
                  f"{'(open)':<22}{x.get('unrealized_pnl', 0):>+9.0f}")


def _market_hours_now() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return dtime(9, 14) <= now.time() <= dtime(15, 40)


def main():
    HISTORY.mkdir(exist_ok=True)
    st = load_state()
    if not _market_hours_now() and (st["open"] or st["closed"]):
        # outside market hours: don't burn Dhan calls, just re-print the last state
        report(st)
        return
    if not _market_hours_now():
        print(f"{datetime.now(IST).strftime('%H:%M')} - outside market hours, nothing to do")
        return
    open_new_shadows(st)
    eval_open_shadows(st)
    save_state(st)
    report(st)


if __name__ == "__main__":
    main()
