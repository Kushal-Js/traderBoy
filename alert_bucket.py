"""
Alert buckets + loss-triggered bucket switch (user request 19 Sep 2026).

Two shared, restart-persistent buckets - one for CE alerts, one for PE
alerts - hold EVERY alert that arrives at Options/Futures/Luxury today
(whether or not the package took, ignored, or capacity-blocked it). A
background ranker keeps every bucket symbol scored with the same MA-ribbon
"burst possibility" score used for ranking-only (ribbon_score.py: CE =
score_ribbon_expansion, PE = score_ribbon_breakdown), so "the best available
alternative right now" is always a cheap in-memory lookup, never a slow
REST round-trip on the critical path.

The switch itself: when a real open position's unrealized loss reaches
config.BUCKET_SWITCH_LOSS_RS (500 by default), the owning package calls
maybe_switch(). It picks the best-ranked bucket symbol (same option type,
not held anywhere, score >= BUCKET_SWITCH_MIN_SCORE, freshly re-scored),
ENTERS it through the package's own real entry path first (every existing
guard - daily cap, loss-repeat block, RSI/trend checks, volume floor,
liquidity gate, funds, cross-strategy claim - still applies; capacity is
allowed to exceed the cap by exactly one for this call), and only then
exits the losing position (reason "BUCKET_SWITCH"). Enter-first is
deliberate: if no candidate gets through, NOTHING changes - the loser just
keeps running under the existing exit ladder, exactly as before - and the
book can never be left flat by a failed replacement.

Everything here is flag-gated (per package: BUCKET_SWITCH_ENABLED) and the
bucket itself is pure bookkeeping - recording alerts has no effect on
trading. Persistence: history/<date>_alert_bucket_<CE|PE>.json, rewritten
atomically after every change and reloaded at startup, so a mid-day restart
loses nothing. The ranker fetches through its OWN single-thread executor so
its REST traffic can never starve the shared pool real order placement
depends on.

See backtest_bucket_switch.py and trading-skills' designs/alert-bucket-
switch.md for the backtest this was evaluated against.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import trade_history

logger = logging.getLogger("alert_bucket")

IST = ZoneInfo("Asia/Kolkata")
ENTERED_STATUSES = ("entered", "amo_placed", "pending_confirmation")
SWITCHES_LOG_NAME = "bucket_switches"

# Own single-thread pool for the ranker's blocking REST reads - see module docstring.
_RANK_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bucket-rank")
_LOCK = asyncio.Lock()


def _today() -> date:
    return datetime.now(IST).date()


class _Bucket:
    def __init__(self, option_type: str) -> None:
        self.option_type = option_type
        self.day: Optional[date] = None
        self.items: dict[str, dict] = {}


_BUCKETS: dict[str, _Bucket] = {"CE": _Bucket("CE"), "PE": _Bucket("PE")}


# ------------------------------------------------------------ persistence ---
def _path(option_type: str, d: date) -> Path:
    # Looked up at call time (not import time) so tests/tools that redirect
    # trade_history.HISTORY_DIR also redirect the bucket files.
    return trade_history.HISTORY_DIR / f"{d.isoformat()}_alert_bucket_{option_type}.json"


def _load_sync(option_type: str, d: date) -> dict[str, dict]:
    p = _path(option_type, d)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()).get("items", {})
    except Exception:  # noqa: BLE001
        logger.exception("Could not read %s - starting today's %s bucket empty", p, option_type)
        return {}


def _write_sync(option_type: str, d: date, payload: str) -> None:
    p = _path(option_type, d)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(payload)
        os.replace(tmp, p)  # atomic - a crash mid-write can never leave a half-written bucket
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist %s alert bucket - in-memory state is unaffected", option_type)


def _serialize(b: _Bucket) -> str:
    return json.dumps({"date": b.day.isoformat() if b.day else None, "option_type": b.option_type, "items": b.items})


async def _ensure_today_locked(b: _Bucket) -> None:
    """Caller holds _LOCK. First touch of a new day (or first touch after a
    restart) loads that day's file, so restarts and day rollovers are both
    handled here and nowhere else.

    Async + run_in_executor for the file read (added 25 Sep 2026 - audit
    finding, PERFORMANCE_AUDIT_2026-09-25.md Part A, same pattern as
    universe_bucket.py's identical fix) - only hits disk once per
    calendar-date rollover per bucket, but a synchronous read here blocks
    the entire process's one event loop while holding _LOCK."""
    today = _today()
    if b.day != today:
        b.day = today
        loop = asyncio.get_running_loop()
        b.items = await loop.run_in_executor(None, _load_sync, b.option_type, today)
        logger.info("%s alert bucket ready for %s (%d symbol(s) restored from disk)", b.option_type, today, len(b.items))


async def _persist(b: _Bucket) -> None:
    payload, d = _serialize(b), b.day
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_sync, b.option_type, d, payload)


async def load_today() -> None:
    """Call once at startup - restores both buckets from disk."""
    async with _LOCK:
        for b in _BUCKETS.values():
            b.day = None
            await _ensure_today_locked(b)


# ---------------------------------------------------------------- recording ---
async def record_alert(option_type: str, stocks: list[str], strategy: str, scan_name: Optional[str] = None) -> None:
    """Fire-and-forget from every webhook handler (never awaited on the
    order-placement path). Never raises."""
    try:
        b = _BUCKETS.get(option_type)
        if b is None or not stocks:
            return
        now = datetime.now(IST).isoformat()
        async with _LOCK:
            await _ensure_today_locked(b)
            for raw in stocks:
                sym = str(raw).strip().upper()
                if not sym:
                    continue
                it = b.items.get(sym)
                if it is None:
                    it = b.items[sym] = {
                        "symbol": sym, "option_type": option_type, "first_alert_at": now, "last_alert_at": now,
                        "alert_count": 0, "strategies": [], "scans": [],
                        "score": None, "score_at": None, "score_detail": None, "blocked_until": None,
                    }
                it["last_alert_at"] = now
                it["alert_count"] += 1
                if strategy not in it["strategies"]:
                    it["strategies"].append(strategy)
                if scan_name and scan_name not in it["scans"]:
                    it["scans"].append(scan_name)
            await _persist(b)
    except Exception:  # noqa: BLE001
        logger.exception("record_alert failed - no effect on trading")


async def snapshot(option_type: Optional[str] = None) -> dict:
    out = {}
    async with _LOCK:
        for ot, b in _BUCKETS.items():
            if option_type and ot != option_type:
                continue
            await _ensure_today_locked(b)
            out[ot] = sorted(b.items.values(), key=lambda i: (i["score"] is None, -(i["score"] or 0)))
    return out


# ------------------------------------------------------------------ ranking ---
def _score_symbol_sync(symbol: str, option_type: str) -> Optional[dict]:
    """Blocking. ribbon score as of the last COMPLETED 5-min bar, or None."""
    import ribbon_score
    from Options.dhan_client import dhan_wrapper

    if dhan_wrapper._client is None:  # never trigger a lazy login from here (see reversal_filters._fetch_raw_candles_sync)
        return None
    try:
        now = datetime.now(IST)
        resp = dhan_wrapper.client.Dhan.intraday_minute_data(
            security_id=dhan_wrapper._equity_security_id(symbol), exchange_segment="NSE_EQ", instrument_type="EQUITY",
            from_date=(now - timedelta(days=7)).strftime("%Y-%m-%d"), to_date=now.strftime("%Y-%m-%d"), interval=5,
        )
        data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
        highs, lows, closes, ts = data.get("high") or [], data.get("low") or [], data.get("close") or [], data.get("timestamp") or []
        if ts and ts[-1] + 300 > time.time():  # drop a still-forming bar
            highs, lows, closes = highs[:-1], lows[:-1], closes[:-1]
        if len(closes) < 100 + ribbon_score.DEFAULT_LOOKBACK_BARS:
            return None
        fn = ribbon_score.score_ribbon_expansion if option_type == "CE" else ribbon_score.score_ribbon_breakdown
        s = fn(highs, lows, closes, symbol=symbol)
        if s is None:
            return None
        return {"score": s.total, "bars_since_trigger": s.bars_since_trigger}
    except Exception:  # noqa: BLE001
        logger.exception("%s: bucket score fetch/compute failed - skipping", symbol)
        return None


async def _apply_score(option_type: str, symbol: str, res: Optional[dict]) -> None:
    b = _BUCKETS[option_type]
    async with _LOCK:
        it = b.items.get(symbol)
        if it is None:
            return
        it["score"] = res["score"] if res else None
        it["score_detail"] = {"bars_since_trigger": res["bars_since_trigger"]} if res else None
        it["score_at"] = datetime.now(IST).isoformat()  # set even on failure -> natural retry backoff, no fetch storms
        await _persist(b)


async def rescore_now(option_type: str, symbol: str) -> Optional[float]:
    """Fresh, on-demand re-score of one bucket symbol (used right before a
    switch entry). Returns the new total, or None if unavailable."""
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(_RANK_EXECUTOR, _score_symbol_sync, symbol, option_type)
    await _apply_score(option_type, symbol, res)
    return res["score"] if res else None


def _market_hours_now() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 10) <= (now.hour, now.minute) <= (15, 35)


async def _rank_cycle(cfg) -> None:
    now = datetime.now(IST)
    work: list[tuple[int, float, str, str]] = []   # (priority, sort_key, option_type, symbol)
    async with _LOCK:
        for ot, b in _BUCKETS.items():
            await _ensure_today_locked(b)
            for sym, it in b.items.items():
                if it["score_at"] is None:
                    work.append((0, 0.0, ot, sym))          # never scored - first
                else:
                    age = (now - datetime.fromisoformat(it["score_at"])).total_seconds()
                    if age >= cfg.BUCKET_RESCORE_MAX_AGE_SECONDS:
                        # stale: best-scoring first, so the head of the ranking is always the freshest
                        work.append((1, -(it["score"] or 0.0), ot, sym))
    work.sort()
    for _prio, _k, ot, sym in work[: cfg.BUCKET_RANK_MAX_FETCHES_PER_CYCLE]:
        await rescore_now(ot, sym)
        await asyncio.sleep(cfg.BUCKET_RANK_PACE_SECONDS)


async def bucket_ranker_loop() -> None:
    """Started once from Options' lifespan (which owns the Dhan connection)."""
    from Options import config as ocfg

    await load_today()
    logger.info("Alert-bucket ranker started (interval=%ss, max %d fetches/cycle).",
                ocfg.BUCKET_RANK_INTERVAL_SECONDS, ocfg.BUCKET_RANK_MAX_FETCHES_PER_CYCLE)
    while True:
        try:
            if ocfg.BUCKET_RANKER_ENABLED and _market_hours_now():
                await _rank_cycle(ocfg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Bucket ranker cycle failed - will retry next interval")
        await asyncio.sleep(ocfg.BUCKET_RANK_INTERVAL_SECONDS)


async def _best_candidates(option_type: str, exclude: set[str], min_score: float, limit: int, max_age_s: float) -> list[tuple[str, float]]:
    now = datetime.now(IST)
    out = []
    async with _LOCK:
        b = _BUCKETS[option_type]
        await _ensure_today_locked(b)
        for sym, it in b.items.items():
            if sym in exclude or it["score"] is None or it["score"] < min_score or it["score_at"] is None:
                continue
            if (now - datetime.fromisoformat(it["score_at"])).total_seconds() > max_age_s:
                continue
            if it["blocked_until"] and now < datetime.fromisoformat(it["blocked_until"]):
                continue
            out.append((sym, it["score"], it["last_alert_at"]))
    out.sort(key=lambda t: (-t[1], t[2]))
    return [(s, sc) for s, sc, _ in out[:limit]]


async def _block_candidate(option_type: str, symbol: str, seconds: float) -> None:
    async with _LOCK:
        it = _BUCKETS[option_type].items.get(symbol)
        if it is not None:
            it["blocked_until"] = (datetime.now(IST) + timedelta(seconds=seconds)).isoformat()


# ------------------------------------------------------------------- switch ---
_STATE: dict[str, dict] = {}


def _state(strategy: str) -> dict:
    st = _STATE.setdefault(strategy, {"day": None, "last_attempt": {}, "switched_from": set(), "count": 0, "lock": asyncio.Lock()})
    if st["day"] != _today():
        st.update(day=_today(), last_attempt={}, switched_from=set(), count=0)
    return st


async def maybe_switch(
    *, strategy: str, cfg, position, symbol: str, ltp: float, held_symbols: set[str], allowed_now: bool,
    enter_fn: Callable[[str], Awaitable[dict]], exit_fn: Callable[[], Awaitable[None]],
) -> Optional[str]:
    """Called from a package's poll loop for a position that has NOT already
    got a normal exit signal. Returns the symbol switched INTO, or None if
    nothing was done (the position then just carries on as usual). Never
    raises. `cfg` is the calling package's own config module."""
    try:
        if not cfg.BUCKET_SWITCH_ENABLED:
            return None
        st = _state(strategy)
        if symbol in st["switched_from"]:
            # Replacement is already in; the loser's exit just hasn't completed - keep retrying it only.
            await exit_fn()
            return None
        loss_rs = (position.entry_price - ltp) * position.quantity
        if loss_rs < cfg.BUCKET_SWITCH_LOSS_RS or not allowed_now:
            return None
        if st["count"] >= cfg.BUCKET_SWITCH_MAX_PER_DAY:
            return None
        mono = time.monotonic()
        if mono - st["last_attempt"].get(symbol, -1e9) < cfg.BUCKET_SWITCH_RETRY_SECONDS:
            return None
        st["last_attempt"][symbol] = mono

        async with st["lock"]:   # one switch at a time per strategy - two losers must not race for the same candidate
            if symbol in st["switched_from"] or position.pending_exit_order_id:
                return None
            cands = await _best_candidates(
                position.option_type, held_symbols | {symbol}, cfg.BUCKET_SWITCH_MIN_SCORE,
                cfg.BUCKET_SWITCH_CANDIDATES_TRIED, cfg.BUCKET_SWITCH_MAX_SCORE_AGE_SECONDS,
            )
            if not cands:
                logger.info("%s: -%.0f Rs loss but no eligible bucket candidate right now - keeping the position under normal exits",
                            symbol, loss_rs)
                return None
            for cand, cached in cands:
                fresh = await rescore_now(position.option_type, cand)
                if fresh is None or fresh < cfg.BUCKET_SWITCH_MIN_SCORE:
                    logger.info("%s: bucket candidate %s failed fresh re-score (%s vs min %.0f)", symbol, cand, fresh, cfg.BUCKET_SWITCH_MIN_SCORE)
                    continue
                result = await enter_fn(cand)
                status = (result or {}).get("status")
                if status in ENTERED_STATUSES:
                    st["switched_from"].add(symbol)
                    st["count"] += 1
                    logger.warning("BUCKET SWITCH [%s]: %s (loss %.0f Rs) -> %s (score %.1f, status %s)", strategy, symbol, loss_rs, cand, fresh, status)
                    trade_history.append_jsonl(SWITCHES_LOG_NAME, {
                        "strategy": strategy, "option_type": position.option_type, "from_symbol": symbol, "loss_rs": round(loss_rs, 2),
                        "to_symbol": cand, "to_score": fresh, "entry_status": status, "logged_at": datetime.now().isoformat(),
                    })
                    await exit_fn()
                    return cand
                logger.info("%s: bucket candidate %s not entered (%s) - trying next", symbol, cand, (result or {}).get("reason") or status)
                await _block_candidate(position.option_type, cand, cfg.BUCKET_SWITCH_CANDIDATE_BLOCK_SECONDS)
            return None
    except Exception:  # noqa: BLE001
        logger.exception("%s: bucket switch attempt failed - position left under normal exits", symbol)
        return None


# ------------------------------------------------------------- observability ---
from fastapi import APIRouter  # noqa: E402

router = APIRouter()


@router.get("/alert-bucket")
async def alert_bucket_view():
    """Both buckets, best-ranked first - read-only observability."""
    return await snapshot()
