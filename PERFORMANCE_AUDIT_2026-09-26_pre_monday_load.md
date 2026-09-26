# System + Code Audit — Pre-Monday Load Readiness — 2026-09-26 (afternoon)

Requested scope: memory, disk, CPU, workers, code, concurrency, lags, latency,
thresholds, races, worker congestion, deadlocks, candle-formation slowness, bot
sluggishness — with fixes needed to handle Monday's real market load and
continuous candle formation / real-time LTP fetch (market closed Sat/Sun).

Method: fresh live droplet diagnostics (SSH, read-only, pulled after the just-now
restart at 12:22:13 UTC, git HEAD `bc77c43`) cross-checked against the actual
current code (not just commit messages), and against the four prior audit docs
in this repo ([CODE_AUDIT_2026-09-24.md](CODE_AUDIT_2026-09-24.md),
[CODE_AUDIT_2026-09-25.md](CODE_AUDIT_2026-09-25.md),
[PERFORMANCE_AUDIT_2026-09-25.md](PERFORMANCE_AUDIT_2026-09-25.md),
[PERFORMANCE_AUDIT_2026-09-25_round2.md](PERFORMANCE_AUDIT_2026-09-25_round2.md),
[PERFORMANCE_AUDIT_2026-09-26_bollinger_launch.md](PERFORMANCE_AUDIT_2026-09-26_bollinger_launch.md))
to avoid re-deriving what's already known — every still-open item from those was
individually re-verified against the code as it stands right now, not just cited.

**Live snapshot (pulled just now):** 1 vCPU / 961Mi RAM, uptime 10 days, bot
restarted today 12:22:13 UTC (~3 min old at check time). Worker process RSS
219MB (~23%), 15 threads, 24 open FDs, load average 0.10 (idle Saturday). Swap
51MB (fresh after restart, was 218MB pre-restart — confirms swap isn't a real
leak signal, just accumulated since the last restart and cleared on this one).
Disk 34% used, 16GB free, `history/`: 656 files/31MB. `EXECUTOR_MAX_WORKERS=5`
(the shared thread pool `Options/Futures/Luxury/Swing/Bollinger` all fetch
through via `loop.run_in_executor(None, ...)`). No errors/exceptions/tracebacks
in the post-restart log window.

**Baseline resource numbers are healthy and unchanged from prior audits.** The
real findings below are structural/timing, not resource exhaustion.

---

## 🔴 Critical #1 — Swing and Bollinger now scan the IDENTICAL 13-symbol universe, still with zero mutual awareness, off the same fund bucket

This was flagged in the last audit at 11/13 overlap; **CANBK/VBL's addition to
both watchlists this session makes it 13/13 — every single Bollinger symbol is
now also a Swing symbol.**

- Swing (15): COPPER, NATURALGAS, NIFTY, BANKNIFTY, SONACOMS, ASHOKLEY,
  SOLARINDS, VEDL, CIPLA, BANDHANBNK, TORNTPHARM, DLF, ZYDUSLIFE, CANBK, VBL
- Bollinger (13): the same list minus COPPER/NATURALGAS

Both packages independently reserve symbols only in their own private
`PositionStore.reserved_symbols` (confirmed: `cross_strategy_registry.py` is
used by Options/Futures/Luxury/Paper01/breakout_signal/breakout_paper_engine —
grep confirms Swing and Bollinger are **not** in that list). Both also read
`fund_allocation.has_sufficient_bucket_funds` with **no lock** — confirmed, zero
`asyncio.Lock`/`_lock` anywhere in `fund_allocation.py` — a pure read-compare
race, unchanged since `CODE_AUDIT_2026-09-24.md` finding 1.3.

**Currently NOT live-exploitable, but only by accident, and fragile:** Swing's
global paper mode is ON right now (`SWING_PAPER_MODE_ENABLED=true`, deployed
this session), so all 13 overlapping symbols route to Swing's *paper* engine —
only Bollinger can place a *real* order on any of them today. **This safety net
is one API call away from disappearing**: `POST /paper-mode
{"strategy":"Swing","enabled":false}` (an existing, unauthenticated, already-
open endpoint — see 🟡 below) would instantly restore the double-real-order
scenario for all 13 symbols, with no code deploy and no restart. Anyone
reaching port 8000 could do this, intentionally or by mistake.

**Failure scenario once that flag flips**: a genuine Supertrend (Swing) and
Bollinger-ribbon+Vortex (Bollinger) signal fire on the same underlying (e.g.
VEDL) within the same few seconds — plausible, since both derive from the same
price action. Both see the symbol as unclaimed, both see the primary bucket as
sufficient, both place real option orders — 2x the intended risk on a position
neither strategy knows the other holds.

**Fix (pick one):**
- **(a) Real fix**: add Swing and Bollinger to `cross_strategy_registry.try_claim`/
  `release_claim`, same pattern already proven in Options/Futures/Luxury.
  Medium effort, touches the live entry path in both packages.
- **(b) Fast stopgap**: de-duplicate the two watchlists (decide which strategy
  owns which symbols) so the overlap doesn't exist. Zero code risk, one
  watchlist edit, doesn't fix the underlying missing-registry gap but removes
  today's concrete trigger.
- Either way, add a lock around `fund_allocation.has_sufficient_bucket_funds`
  (finding 1.3) — this is the second half of the same risk and is currently
  unmitigated by anything, including the paper-mode accident above.

**This is real order-placement code — per your standing rule, none of this gets
touched or deployed without your explicit go-ahead.**

---

## 🔴 Critical #2 (new this audit) — Swing's per-symbol entry-scan pacing now eats 98% of its own tick budget; Bollinger is close behind

`Swing/trading_engine.py`'s `_monitor_tick` sleeps `SYMBOL_PACING_SECONDS=0.35s`
**unconditionally** before evaluating every watchlist symbol after the first
(`if i: await asyncio.sleep(...)`) — regardless of whether that symbol's signal
check actually needs a network call. At today's 15-symbol Swing watchlist:
**14 × 0.35s = 4.9 seconds of guaranteed forced sleep**, against a
`MONITOR_INTERVAL_SECONDS=5s` tick — **0.1 seconds of margin** left for every
symbol's actual signal evaluation, indicator computation, and REST-fallback
fetch time, combined. Bollinger's 13-symbol watchlist: `12 × 0.35s = 4.2s`
against the same 5s budget — 0.8s margin, also tight.

**Why this pacing is now mostly dead weight, not real rate-limiting**: both
strategies have `USE_WS_CANDLES=true` with `WS_STALE_AFTER_SECONDS=90` —
confirmed in `Swing/signals.py`/`Bollinger's` shared code path
(`candle_feed.is_fresh(...)` → `candle_feed.get_candles_dict(...)`, a pure
in-memory dict lookup with **no REST call and no rate-limit concern at all**)
returns immediately whenever the WS feed has delivered a tick in the last 90
seconds. The `SYMBOL_PACING_SECONDS` sleep exists specifically to protect the
REST-fallback path (`dhan_wrapper.fetch_continuous_intraday`, confirmed
comment: "respects Dhan's market-data rate limits on back-to-back calls") — but
it fires **before** the code even knows whether this symbol will hit that
fallback or the free in-memory cache. Once the WS feed is flowing normally
during real market hours, most symbols should be WS-cache hits, and the bot is
still paying the full REST-rate-limiting tax on every one of them anyway.

**This directly threatens the exact thing you asked about — continuous candle
formation and real-time LTP responsiveness.** `monitor_loop` does
`await asyncio.sleep(MONITOR_INTERVAL_SECONDS)` unconditionally *after*
`_monitor_tick()` finishes, so a tick that takes longer than 5s doesn't crash or
stack up — it just stretches the *effective* polling period past the intended
5s. At 15 symbols, even a few slow REST-fallback fetches (e.g. right after
Monday's open, or during any WS hiccup) push the real interval to 6-8+ seconds.
Combined with today's new freshness-priority entry backlog
(`entry_backlog.py`, deployed this session), a stretched tick means fewer, later
dispatch passes — signals sit in the backlog longer, and a burst of genuinely
simultaneous signals is more likely under a slow tick than a fast one.
(See `entry_backlog.py`'s own module docstring for that mechanism's design.)

**Fix (two tiers, in order of cheapness):**
1. **Cheap, near-zero-risk**: only pace when the symbol actually falls through
   to the REST fetch, not unconditionally. E.g. have `get_supertrend_state`/
   `get_regime_state`/`get_day_range_state` report whether they served from WS
   cache or hit REST, and skip the sleep on a cache hit. This alone could
   reclaim most of the 4.9s/4.2s dead time on a healthy WS day without touching
   order-placement logic at all — purely a scan-timing change.
2. **Medium**: bounded semaphore (3-4 concurrent) around the whole per-symbol
   scan instead of a linear sleep chain — the fix already proposed in the two
   prior audits for this exact gap, never implemented. Matters most for the
   REST-fallback burst case (WS outage, post-open cold cache).

**Recommend doing #1 before Monday** — it's cheap, testable now with the market
closed, and closes the largest concrete gap in this whole audit. Still a change
to the live entry-scan hot path, so I'd want your go-ahead before deploying it,
same as any change to `trading_engine.py`.

---

## 🟠 High — still open: shared 5-thread executor pool now serves 4 live packages, unbounded fan-out

Options, Luxury, Swing, and Bollinger all dispatch blocking Dhan REST calls
through `loop.run_in_executor(None, ...)` → the same process-wide
`EXECUTOR_MAX_WORKERS=5` pool (confirmed: `main.py:147-166`). No
`asyncio.Semaphore` bounds the `asyncio.gather` fan-out in any package's
exit-check loop (only pool found:
`Options/dhan_client.py`'s own `ltp_rest_fallback_semaphore = Semaphore(2)`,
which is narrower-scoped, not the general pool). The 12s HTTP timeout fix
(already live, confirmed) bounds how long one stuck call can hold a thread, so
this is materially less dangerous than the original finding, but still
uncapped, and now serving one more package than when last sized.

**Worth noting**: `main.py`'s own code default for `EXECUTOR_MAX_WORKERS`
recently changed to `10` (a commit landed since the last audit), but the
droplet's `.env` still explicitly pins `EXECUTOR_MAX_WORKERS=5`, so the *live*
value is unchanged at 5 — the code-level default bump had no real effect yet.

**Fix:** revisit raising `EXECUTOR_MAX_WORKERS` in `.env` now that Bollinger
adds real load (config-only, no code risk — safe to test with market closed).
Pair with a bounded semaphore around each package's `asyncio.gather` fan-out
when there's time for the medium-effort version. **Effort: small (env) +
small-medium (semaphore).**

## 🟠 High — still open: `breakout_signal.py` keeps failing on Swing-only symbols

Unchanged from the round-2 audit: `breakout_signal.py` has no filter for
index/MCX-commodity symbols, so NIFTY/BANKNIFTY/NATURALGAS (Swing's, not its
own) keep reaching its NSE-equity-only lookup and failing every scan cycle —
confirmed still no `NSETEST`/`is_mcx_commodity`/`INDEX_SYMBOLS` filtering
anywhere in the file. Caught and retried, not a crash, not fund-unsafe — but
real wasted thread-pool submissions and log noise every cycle, worse now that
the pool is shared by more packages (see above). **Fix: small** — filter these
symbols out of whatever feeds its candidate universe.

---

## 🟡 Medium — `/paper-mode` and the new `/capacity/max-concurrent-trades` are both unauthenticated

Confirmed: neither endpoint in `main.py` has any auth dependency. `/paper-mode`
controls real-vs-paper trading for 5 strategies; `/capacity/max-concurrent-
trades` (added this session) controls how many concurrent real positions Swing/
Bollinger can hold. Both are reachable by anyone who can reach port 8000 — same
gap already flagged for `/paper-mode` in `CODE_AUDIT_2026-09-25.md`, and my own
new endpoint inherited the identical pattern rather than fixing it. Given 🔴 #1
above shows a paper-mode flip alone can reintroduce a real double-order risk,
this is more consequential than "just" an admin-endpoint gap. **Fix: small** —
shared-secret header or IP-allowlist on both endpoints (the webhook endpoints
are deliberately open per your own instruction, but these two toggle live risk
controls, not accept market alerts).

## 🟡 Medium — `fund_allocation.py`'s bucket-funds check remains fully unlocked

Standalone from 🔴 #1's compounding effect: `has_sufficient_bucket_funds` is a
pure read-compare with no lock, so even within Options+Futures+Luxury's own
shared secondary bucket, two concurrent entries could both see "sufficient
funds" and both proceed. Confirmed still open, unchanged since first flagged.
**Fix: small-medium** — an `asyncio.Lock` around the check-and-reserve, same
shape as `PositionStore.reserve_symbol`'s existing atomic pattern.

## 🟢 Low — my own new `capacity_control.py` (and pre-existing `paper_mode_control.py`) write their override file synchronously inside an `async def`+lock

Both call `OVERRIDE_FILE.write_text(...)` directly (blocking file I/O) inside
an `async with self._lock:` block, unwrapped in `run_in_executor` — the same
class of issue Phase 3 of `CODE_AUDIT_2026-09-24.md` already fixed elsewhere
(`universe_bucket.py`, `alert_bucket.py`, etc.). Impact is negligible here since
both are only called on a rare, explicit admin action (a human hitting
`POST /paper-mode` or `POST /capacity/...`), never on the per-tick hot path —
flagging for completeness/consistency, not urgency. **Effort: trivial.**

---

## ✅ Confirmed healthy / already fixed (re-verified against current code, not just cited)

- **Memory/disk/CPU**: 219MB RSS, in line with every prior audit's steady-state
  plateau (215-221MB). No leak signature. Swap reset to 51MB on this restart,
  consistent with "accumulates, doesn't leak."
- **Options day-rollover stuck-reservation bug — corrected from the last
  audit's stale note**: it says "still open" for Options, but the actual fix
  (`"...still non-terminal (status=%s) at day rollover - releasing..."`) is
  present in **all three** of `Options/`, `Futures/`, `Luxury/position_store.py`
  right now, and `tests/test_day_rollover_stuck_reservation.py` explicitly
  covers and passes for all three. This item can come off future recap lists.
- **No new deadlock shape from today's own new code** (`entry_backlog.py`,
  `capacity_control.py`, deployed this session): every lock-protected method in
  `EntryBacklog` does pure in-memory dict/list work with **no `await` inside the
  lock** — same invariant every prior audit round verified for the rest of the
  codebase. `dispatch()` calls `place_real`/`place_paper` (which do the real
  `await`s, including `position_store`'s own lock) strictly *outside* any
  `entry_backlog` lock scope — no nested-lock path exists between the two.
- **`EXECUTOR_MAX_WORKERS`/HTTP-timeout fix**: confirmed still loaded live
  (12s bound), meaningfully reduces the executor-congestion risk vs. the
  original finding even though the pool itself stays uncapped.
- **`WS_STALE_AFTER_SECONDS=90`** is a reasonable tolerance — not itself a
  problem; the problem is the pacing sleep not being conditioned on it (🔴 #2).
- Bollinger's own exit-check loop already uses `asyncio.gather` from day one
  (never had Swing's old sequential-exit-check bug) — reconfirmed.

---

## Priority order for before Monday's open

1. **🔴 #2 — make the entry-scan pacing conditional on an actual REST fallback**,
   not unconditional per symbol. Cheapest, highest-impact, most directly tied
   to what you asked about (candle formation / real-time LTP responsiveness).
2. **🔴 #1 — decide on the Swing/Bollinger overlap**: de-duplicate watchlists
   (fast) or wire both into `cross_strategy_registry` (real fix), and lock
   `fund_allocation.has_sufficient_bucket_funds` either way.
3. **🟠 `EXECUTOR_MAX_WORKERS` tuning** — config-only, zero code risk, safe to
   raise now with market closed.
4. **🟡 Auth on `/paper-mode` and `/capacity/max-concurrent-trades`** — closes
   off the accidental path back into finding #1.
5. **🟠 `breakout_signal.py` symbol filter** — cosmetic/waste, independent,
   small.
6. Everything else (🟡 fund-lock, 🟢 sync-write-in-lock) — low urgency, safe
   anytime.

Items 1, 2, and 5 touch live order-placement or entry-scan code — per your
standing rule, nothing here gets built or deployed without your explicit
go-ahead, and I'd want to test outside market hours regardless. Item 3 is
config-only with no code risk if you want it done now. Item 4 is a small,
independent auth addition, also low-risk to do now.
