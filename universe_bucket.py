"""
Rolling CE/PE universe bucket (added 21 Sep 2026, user request) - a
dedicated, standalone webhook target for a NEW Chartink screener
("continuously feeding data" once integrated) whose picks become the
breakout-signal scanner's own watchlist, across the last N trading days
(today included) - not just today's own alerts, unlike alert_bucket.py
or a package's own direct webhook.

WINDOW SIZE IS PER OPTION TYPE (WINDOW_TRADING_DAYS_CE/_PE, see
_window_for), NOT ONE SHARED CONSTANT - split 22 Sep 2026 (user request)
after CE and PE ended up needing genuinely different answers on the same
day: the original shared window (3, then briefly 1) produced a 134-symbol
bucket (44 CE + 90 PE) that the breakout dispatcher's own scan cadence
(BREAKOUT_SCAN_MAX_PER_CYCLE=10 per BREAKOUT_SCAN_INTERVAL_SECONDS=60)
couldn't keep up with - a full pass took ~13.4 minutes, long enough for a
genuine breakout candle to go stale before its symbol's turn came up
again. Investigating that number surfaced a SEPARATE, real data-quality
bug (a manual backfill had merged 18-Sep's and 21-Sep's alerts into one
mislabeled file - see TRADING_JOURNAL.md's 22 Sep entry): once the
user's own real Chartink CSV export let CE's prior-day entries be cross-
checked and corrected to their true count, CE's window was raised to 2
(today + yesterday) with confidence in what "yesterday" actually
contained at the time. PE was never individually verified against a CSV
the same way, so it stayed at 1 (today only) per the user's own
separate, explicit instruction. CE brought back down to 1, same day
(later, user request) - matching PE again, same scan-cadence/capacity
reasoning that motivated the original 3->1 shared-window cut still
applies to CE's own count once its own alerts accumulate through a
session. Each side stays independently configurable via
UNIVERSE_BUCKET_WINDOW_TRADING_DAYS_CE/_PE regardless of what today's
particular values are.

WHY A SEPARATE MODULE, NOT alert_bucket.py: that module is a single-day
(today-only), per-symbol SCORED pool feeding the loss-triggered bucket-
switch feature - a different, still-undecided piece of work (see
trading-skills' designs/alert-bucket-switch.md). This one is unscored,
rolls over a configurable multi-day window instead of always resetting
daily, and feeds breakout_signal.py's watchlist rather than a switch
candidate list. Deliberately separate state, deliberately separate
purpose - enabling one implies nothing about the other, same reasoning
alert_bucket.py's own docstring already gives for staying separate from
THIS kind of feature.

DATA MODEL: two independent buckets, CE and PE (never shared state,
matching every other CE/PE split in this codebase). Each is persisted ONE
FILE PER CALENDAR DAY - history/<date>_universe_bucket_<CE|PE>.json -
following the exact convention every other daily log in this repo
already uses (real_trades.log, webhook_alerts.log, alert_bucket's own
files). These per-day files are NEVER deleted - this repo's own standing
convention is that historical logs are an append-only audit trail, never
pruned - so the "rolling window" is implemented as a READ-side merge of
the last N days' own files (active_symbols()), not as a mutation that
destroys older data. Changing either window constant takes effect on the
very next read with NO backfill/cleanup step needed - the already-
persisted older files simply stop (or start) being included in the
merge, even though they stay on disk for anyone who wants the historical
record.

TRADING-DAY AWARENESS, HONESTLY SCOPED: Saturday/Sunday are excluded
deterministically (calendar weekday check). NSE market HOLIDAYS ARE NOT -
this repo has no maintained forward-looking holiday calendar anywhere
(confirmed by grep before writing this; every other mention of "holiday"
in this codebase is a passing comment, never an actual date list), so a
weekday holiday inside the lookback window is silently counted as one of
its "trading days" even though nothing real happened - harmless in
effect (an empty/missing file for that date just contributes zero
symbols, the same as a real trading day where the screener found
nothing), but it means the active window can sometimes cover fewer than
its configured number of REAL trading sessions' worth of picks during a
holiday week. Fixing this properly needs a real NSE holiday list, which
this module does not fabricate.

WEBHOOK: POST /universe-bucket/webhook (bullish -> CE) and /universe-
bucket/webhook-sell (bearish -> PE), same Chartink payload shape as
every other webhook in this codebase (stocks/trigger_prices/triggered_at/
scan_name/scan_url/alert_name) so Chartink's own alert config needs no
special handling - point a new scan's webhook URL directly at either
endpoint. Pure bookkeeping: recording an alert here has NO effect on any
package's real trading by itself - see breakout_signal.py's own
_maybe_seed_universe for how (and whether) a package's scanner actually
reads from this bucket (opt-in per package via BREAKOUT_UNIVERSE_SOURCE,
default "static"/unchanged).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import trade_history

logger = logging.getLogger("universe_bucket")

IST = ZoneInfo("Asia/Kolkata")
# Lowered 3->1 (user request 22 Sep 2026), straight off the real universe
# size this produced (44 CE + 90 PE = 134 symbols) once real Chartink
# alerts had fed it for a few days - the breakout dispatcher only checks
# BREAKOUT_SCAN_MAX_PER_CYCLE(10) symbols per BREAKOUT_SCAN_INTERVAL_
# SECONDS(60), so a 134-symbol bucket meant ~13.4 minutes for a full pass
# (134/10 cycles x 60s) - a genuine breakout candle could go stale (2-3
# candles old) before its symbol's turn came up again.
#
# SPLIT PER OPTION TYPE 22 Sep 2026 (user request) - CE raised back to 2
# (today + yesterday) once yesterday's own CE entries were cross-checked
# against the user's real Chartink CSV export and corrected to the true
# 19 symbols (see TRADING_JOURNAL.md's 22 Sep entry - the old 44 was a
# manual-backfill contamination, not real volume). PE stays at 1 (today
# only) per the user's own explicit, separate instruction earlier the
# same day ("let them come tomorrow only when market starts for PE") -
# no CSV cross-check was done for PE's own multi-day history, so there
# is no verified basis yet to trust a 2-day PE window the way CE's was
# just individually verified. No code change needed to apply either
# window immediately: active_symbols() re-reads the last N days' already-
# persisted files fresh every call (never mutates/deletes them, see
# module docstring), so changing either constant takes effect on the
# very next read, no backfill/cleanup step required.
WINDOW_TRADING_DAYS_CE = int(os.getenv("UNIVERSE_BUCKET_WINDOW_TRADING_DAYS_CE", "1"))
WINDOW_TRADING_DAYS_PE = int(os.getenv("UNIVERSE_BUCKET_WINDOW_TRADING_DAYS_PE", "1"))


def _window_for(option_type: str) -> int:
    return WINDOW_TRADING_DAYS_CE if option_type == "CE" else WINDOW_TRADING_DAYS_PE


def _today() -> date:
    return datetime.now(IST).date()


def _trading_days_back(n: int, as_of: date) -> list[date]:
    """Last n TRADING days ending at (and including) as_of, most recent
    first - Saturday/Sunday skipped, market holidays NOT (see module
    docstring for why)."""
    out: list[date] = []
    d = as_of
    while len(out) < n:
        if d.weekday() < 5:  # Mon-Fri
            out.append(d)
        d -= timedelta(days=1)
    return out


# ------------------------------------------------------------ persistence ---
def _path(option_type: str, d: date) -> Path:
    return trade_history.HISTORY_DIR / f"{d.isoformat()}_universe_bucket_{option_type}.json"


def _load_sync(option_type: str, d: date) -> dict[str, dict]:
    p = _path(option_type, d)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        logger.exception("Could not read %s - treating %s as empty for %s", p, option_type, d)
        return {}


def _write_sync(option_type: str, d: date, payload: str) -> None:
    p = _path(option_type, d)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(payload)
        os.replace(tmp, p)  # atomic
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist today's %s universe bucket - in-memory state unaffected", option_type)


class _Bucket:
    def __init__(self, option_type: str) -> None:
        self.option_type = option_type
        self.day: Optional[date] = None
        self.items: dict[str, dict] = {}


_BUCKETS: dict[str, _Bucket] = {"CE": _Bucket("CE"), "PE": _Bucket("PE")}
_LOCK = asyncio.Lock()


def _ensure_today_locked(b: _Bucket) -> None:
    """Caller holds _LOCK. Same restart-/day-rollover-safe pattern as
    alert_bucket.py's own _ensure_today_locked - the first touch of a new
    calendar date loads (or starts empty) that date's own file."""
    today = _today()
    if b.day != today:
        b.day = today
        b.items = _load_sync(b.option_type, today)
        logger.info("%s universe bucket ready for %s (%d symbol(s) restored from disk)",
                    b.option_type, today, len(b.items))


async def _persist(b: _Bucket) -> None:
    payload, d = json.dumps(b.items), b.day
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_sync, b.option_type, d, payload)


# ---------------------------------------------------------------- recording ---
async def record_alert(option_type: str, stocks: list[str], scan_name: Optional[str] = None,
                        alert_name: Optional[str] = None) -> None:
    """Fire-and-forget from the webhook handlers below (and safe to call
    from anywhere else too - never raises, never affects trading)."""
    try:
        b = _BUCKETS.get(option_type)
        if b is None or not stocks:
            return
        now = datetime.now(IST).isoformat()
        async with _LOCK:
            _ensure_today_locked(b)
            for raw in stocks:
                sym = str(raw).strip().upper()
                if not sym:
                    continue
                it = b.items.get(sym)
                if it is None:
                    it = b.items[sym] = {"first_alert_at": now, "alert_count": 0, "scans": [], "alert_names": []}
                it["last_alert_at"] = now
                it["alert_count"] += 1
                if scan_name and scan_name not in it["scans"]:
                    it["scans"].append(scan_name)
                if alert_name and alert_name not in it["alert_names"]:
                    it["alert_names"].append(alert_name)
            await _persist(b)
    except Exception:  # noqa: BLE001
        logger.exception("universe_bucket.record_alert failed - no effect on trading")


# --------------------------------------------------------------------- read ---
async def active_symbols(option_type: str, as_of: Optional[date] = None) -> set[str]:
    """The rolling window itself: union of symbols recorded on any of the
    last _window_for(option_type) trading days (today included) - CE and
    PE can have different window sizes, see _window_for's own comment.
    This is what breakout_signal.py's curated-universe seeding reads from
    when a package's own cfg.BREAKOUT_UNIVERSE_SOURCE == "universe_bucket"."""
    today = as_of or _today()
    out: set[str] = set()
    async with _LOCK:
        b = _BUCKETS.get(option_type)
        if b is None:
            return out
        _ensure_today_locked(b)
        for d in _trading_days_back(_window_for(option_type), today):
            items = b.items if d == b.day else _load_sync(option_type, d)
            out.update(items.keys())
    return out


async def snapshot() -> dict:
    """Read-only observability - both buckets' active rolling-window
    symbol sets, plus the individual trading days each is drawn from
    (CE and PE can differ - see _window_for's own comment)."""
    today = _today()
    out: dict = {
        "window_trading_days": {
            "CE": [d.isoformat() for d in _trading_days_back(_window_for("CE"), today)],
            "PE": [d.isoformat() for d in _trading_days_back(_window_for("PE"), today)],
        }
    }
    for ot in ("CE", "PE"):
        out[ot] = sorted(await active_symbols(ot, today))
    return out


# ----------------------------------------------------------------- webhook ---
from fastapi import APIRouter  # noqa: E402
from pydantic import BaseModel, field_validator  # noqa: E402

router = APIRouter()


class UniverseBucketWebhookPayload(BaseModel):
    stocks: str
    trigger_prices: str = ""
    triggered_at: str = ""
    scan_name: str = ""
    scan_url: str = ""
    alert_name: str = ""
    webhook_url: Optional[str] = None

    @field_validator("stocks")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("stocks must not be empty")
        return v

    def stock_list(self) -> list[str]:
        return [s.strip().upper() for s in self.stocks.split(",") if s.strip()]


async def _handle_webhook(payload: UniverseBucketWebhookPayload, option_type: str) -> dict:
    stocks = payload.stock_list()
    await record_alert(option_type, stocks, scan_name=payload.scan_name or None, alert_name=payload.alert_name or None)
    logger.info("universe-bucket webhook (%s): scan=%s stocks=%s", option_type, payload.scan_name, stocks)
    return {"status": "recorded", "option_type": option_type, "stocks": stocks}


@router.post("/universe-bucket/webhook")
async def universe_bucket_webhook(payload: UniverseBucketWebhookPayload):
    """Bullish scan -> CE bucket. Point a Chartink scan's own webhook URL
    here directly - same payload shape as /chartink/webhook."""
    return await _handle_webhook(payload, "CE")


@router.post("/universe-bucket/webhook-sell")
async def universe_bucket_webhook_sell(payload: UniverseBucketWebhookPayload):
    """Bearish scan -> PE bucket."""
    return await _handle_webhook(payload, "PE")


@router.get("/universe-bucket")
async def universe_bucket_view():
    """Current rolling-window state for both buckets - read-only observability."""
    return await snapshot()
