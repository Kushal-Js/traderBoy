#!/usr/bin/env python3
"""
Safe restart of the bot ON THE DROPLET (30 Sep 2026). Does, as one chained
sequence, what was done by hand five times that day - and refuses to restart
when a check fails (a broken pre-step must never be followed by the restart:
at 11:35 IST a failed seed script was, and a hedge lost its profit trail).

    cd ~/apps/traderBoy && python3 safe_restart.py            # checks, restart, report
    python3 safe_restart.py --check                           # checks only, no restart
    python3 safe_restart.py --force                           # restart even if a check fails

Steps: 1) snapshot every strategy's positions + the live state of the two
ledger strategies (Unified Momentum - real money since 1 Oct 2026 - and Super
Bollinger) to history/restart_snapshots/<time>_*.json; 2) checks - no order in
flight, no exit in flight, every real Unified Momentum / Super Bollinger
position (calls, momentum puts, hedges) has its broker stop order and is in its
state file on disk (fresh), every real Swing/Bollinger/Options/Luxury position
is in its position-memory file; 3) systemctl restart; 4) wait for /health;
5) print both ledger strategies' restart reports plus what Swing and Bollinger
restored (and Options/Luxury), and exit 1 if anything needs review. Standard library only.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "http://localhost:8000"
IST = timezone(timedelta(hours=5, minutes=30))
SNAP_DIR = Path("history/restart_snapshots")
# Strategies with a live-state ledger (write-ahead order intents + a state file): endpoint prefix -> state file
LEDGERS = {"unified-momentum": Path("data/unified_momentum_live_state.json"),
           "super-bollinger": Path("data/super_bollinger_live_state.json")}
ENDPOINTS = ["scalper/positions",
             "unified-momentum/positions", "unified-momentum/supervisor", "unified-momentum/live-state",
             "super-bollinger/positions", "super-bollinger/supervisor", "super-bollinger/live-state",
             "positions", "luxury/positions", "swing/positions", "bollinger/positions", "paper-mode"]
# Swing and Bollinger remember their real positions' trailing state themselves (position_memory.py)
MEMORY_FILES = {"swing/positions": Path("data/swing_position_memory.json"),
                "bollinger/positions": Path("data/bollinger_position_memory.json"),
                "positions": Path("data/options_position_memory.json"),          # Options (1 Oct 2026)
                "luxury/positions": Path("data/luxury_position_memory.json")}
REPORTS = {"swing": "swing/restart-report", "bollinger": "bollinger/restart-report",
           "options": "options/restart-report", "luxury": "luxury/restart-report"}


def get(path: str, timeout: float = 10):
    with urllib.request.urlopen(f"{BASE}/{path}", timeout=timeout) as r:
        return json.loads(r.read())


def _check_ledger(prefix: str, state_file: Path, snap: dict, now: datetime, problems: list, notes: list) -> None:
    """A ledger strategy's real positions: calls (top level), engine B's puts ("engine_b", Unified Momentum only)
    and the supervisor's hedges - each needs its broker stop on record, no exit in flight and a row in the state
    file on disk; no entry or order intent may be in flight."""
    pos, sup, live = snap.get(f"{prefix}/positions"), snap.get(f"{prefix}/supervisor") or {}, snap.get(f"{prefix}/live-state")
    if pos is None:
        return
    b = pos.get("engine_b") or {}
    real = ([("call", p) for p in pos.get("live_positions", [])] + [("put", p) for p in b.get("live_positions", [])]
            + [("hedge", p) for p in sup.get("open_hedges_real", [])])
    for kind, p in real:
        print(f"  real {prefix} {kind}: {p['trading_symbol']} x{p['quantity']} entry {p['entry_price']} "
              f"best {p['best_price']} SL {p.get('stop_loss_order_id')}")
        if p.get("pending_exit_order_id"):
            problems.append(f"{p['trading_symbol']} ({prefix}): an exit order is in flight")
        if not p.get("stop_loss_order_id"):
            problems.append(f"{p['trading_symbol']} ({prefix}): no broker stop-loss order on record")
    reserved = ((set(pos.get("reserved_symbols", [])) - {p["underlying_symbol"] for p in pos.get("live_positions", [])})
                | (set(b.get("reserved_symbols", [])) - {p["underlying_symbol"] for p in b.get("live_positions", [])}))
    if reserved:
        problems.append(f"{prefix}: an entry is in flight for {sorted(reserved)}")
    if live is None:
        notes.append(f"the running build has no /{prefix}/live-state - the restart will rebuild from the broker")
        return
    if live.get("intents"):
        problems.append(f"{prefix}: {len(live['intents'])} order intent(s) in flight")
    if not real:
        return
    try:
        disk = json.loads(state_file.read_text())
        age = (now - datetime.fromisoformat(disk["saved_at"])).total_seconds()
        rows = disk.get("positions", []) + disk.get("hedges", []) + disk.get("puts", [])
        on_disk = {x["trading_symbol"] for x in rows}
        missing = [p["trading_symbol"] for _k, p in real if p["trading_symbol"] not in on_disk]
        if disk.get("day") != now.date().isoformat():
            problems.append(f"{state_file} is from {disk.get('day')}")
        if missing:
            problems.append(f"{state_file} does not hold {missing}")
        for _k, p in real:
            row = next((x for x in rows if x["trading_symbol"] == p["trading_symbol"]), None)
            if row and row["best_price"] + 1e-9 < p["best_price"]:
                notes.append(f"{p['trading_symbol']}: best on disk {row['best_price']} < live {p['best_price']} "
                             f"(state file {age:.0f}s old) - wait a few seconds and re-run")
        print(f"  {state_file}: {len(on_disk)} position(s), saved {age:.0f}s ago")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{state_file} unreadable: {exc!r}")


def main() -> int:
    check_only, force = "--check" in sys.argv, "--force" in sys.argv
    now = datetime.now(IST)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap, problems, notes = {}, [], []
    for ep in ENDPOINTS:
        try:
            snap[ep] = get(ep)
            (SNAP_DIR / f"{stamp}_{ep.replace('/', '_')}.json").write_text(json.dumps(snap[ep], default=str))
        except urllib.error.HTTPError as exc:
            snap[ep] = None
            if exc.code == 404:        # not in the running build (e.g. the first deploy of a new strategy)
                notes.append(f"/{ep} is not in the running build")
            else:
                (problems if ep.endswith("/positions") and ep.split("/")[0] in LEDGERS else notes).append(
                    f"could not read /{ep}: {exc!r}")
        except Exception as exc:  # noqa: BLE001
            snap[ep] = None
            (problems if ep.endswith("/positions") and ep.split("/")[0] in LEDGERS else notes).append(
                f"could not read /{ep}: {exc!r}")
    print(f"snapshot {stamp} -> {SNAP_DIR}/")

    for prefix, state_file in LEDGERS.items():
        _check_ledger(prefix, state_file, snap, now, problems, notes)
    # Scalper (1 Oct 2026): real BANKNIFTY positions remember best price / entry candle in
    # data/scalper_position_memory.json; an exit in flight must finish first.
    sc = snap.get("scalper/positions") or {}
    try:
        sc_mem = json.loads(Path("data/scalper_position_memory.json").read_text())
    except Exception:  # noqa: BLE001
        sc_mem = {}
    for p in sc.get("live_positions", []):
        sym = p.get("trading_symbol")
        if p.get("pending_exit_order_id"):
            problems.append(f"{sym} (scalper): an exit order is in flight")
        if not p.get("stop_loss_order_id"):
            notes.append(f"{sym} (scalper): no broker stop on record")
        print(f"  real scalper: {sym} x{p.get('quantity')} entry {p.get('entry_price')} best {p.get('best_price')} "
              f"(remembered best {(sc_mem.get(sym) or {}).get('best_price')})")
    for ep in ("positions", "luxury/positions", "swing/positions", "bollinger/positions"):
        rows = (snap.get(ep) or {}).get("live_positions", [])
        if not rows:
            continue
        try:
            mem = json.loads(MEMORY_FILES[ep].read_text())
        except Exception as exc:  # noqa: BLE001
            mem = {}
            notes.append(f"/{ep}: memory file unreadable ({exc!r})")
        name = ep.split("/")[0] if "/" in ep else "options"
        for p in rows:
            sym = p.get("trading_symbol") or p.get("option_trading_symbol")
            row = mem.get(sym)
            best = p.get("best_price", p.get("highest_price"))
            if p.get("pending_exit_order_id"):
                problems.append(f"{sym}: an exit order is in flight")
            if row is None:
                problems.append(f"{sym} ({name}): not in {MEMORY_FILES[ep]} - its trailing state would be lost")
            else:
                print(f"  real {name}: {sym} x{p.get('quantity')} entry {p.get('entry_price')} best {best} "
                      f"(remembered best {row.get('best_price', row.get('highest_price'))})")

    for n in notes:
        print("  note:", n)
    for p in problems:
        print("  PROBLEM:", p)
    if problems and not force:
        print("NOT restarting. Fix the above or re-run with --force.")
        return 2
    if check_only:
        print("checks passed (no restart requested)")
        return 0

    subprocess.run(["systemctl", "restart", "dhanboy.service"], check=True)
    print(f"restarted {datetime.now(IST):%H:%M:%S} - waiting for the bot")
    for _ in range(60):
        time.sleep(2)
        try:
            if get("health", timeout=3).get("status") == "ok":
                break
        except Exception:  # noqa: BLE001
            continue
    else:
        print("PROBLEM: /health did not come back within 2 minutes - check journalctl -u dhanboy.service")
        return 3
    time.sleep(3)
    print(f"bot is up {datetime.now(IST):%H:%M:%S}.")
    review = False
    for prefix in LEDGERS:
        try:
            report = get(f"{prefix}/restart-report")
        except Exception as exc:  # noqa: BLE001
            print(f"  no {prefix} restart report ({exc!r})")
            continue
        print(f"  {prefix} restart report:")
        for key in ("positions", "hedges", "puts", "closed_while_down", "intents", "warm_up"):
            for row in report.get(key, []):
                print(f"    {key}: {json.dumps(row, default=str)}")
        print(f"    day_state: {report.get('day_state')}  broker_reachable: {report.get('broker_reachable')}  "
              f"needs_review: {report.get('needs_review')}")
        review = review or bool(report.get("needs_review"))
    for name, path in REPORTS.items():
        try:
            last = (get(path) or {}).get("last_restore") or {}
        except Exception:  # noqa: BLE001
            continue
        for row in last.get("restored", []):
            print(f"  {name} restored: {json.dumps(row, default=str)}")
        for row in last.get("not_restored", []):
            print(f"  {name} NOT restored: {json.dumps(row, default=str)}")
            review = True
        for sym in last.get("remembered_but_not_at_broker", []):
            print(f"  {name}: {sym} was open before the restart but is not at the broker now (closed while down?)")
            review = True
    return 1 if review else 0


if __name__ == "__main__":
    sys.exit(main())
