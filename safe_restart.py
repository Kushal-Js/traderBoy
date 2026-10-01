#!/usr/bin/env python3
"""
Safe restart of the bot ON THE DROPLET (30 Sep 2026). Does, as one chained
sequence, what was done by hand five times that day - and refuses to restart
when a check fails (a broken pre-step must never be followed by the restart:
at 11:35 IST a failed seed script was, and a hedge lost its profit trail).

    cd ~/apps/traderBoy && python3 safe_restart.py            # checks, restart, report
    python3 safe_restart.py --check                           # checks only, no restart
    python3 safe_restart.py --force                           # restart even if a check fails

Steps: 1) snapshot every strategy's positions + Super Bollinger's live state
to history/restart_snapshots/<time>_*.json; 2) checks - no order in flight, no
exit in flight, every real Super Bollinger position has its broker stop order
and is in the state file on disk (fresh), every real Swing/Bollinger/Options/
Luxury position is in its position-memory file; 3) systemctl restart; 4) wait for /health;
5) print GET /super-bollinger/restart-report plus what Swing and Bollinger
restored (and Options/Luxury), and exit 1 if anything needs review. Standard library only.
"""
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "http://localhost:8000"
IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = Path("data/super_bollinger_live_state.json")
SNAP_DIR = Path("history/restart_snapshots")
ENDPOINTS = ["scalper/positions", "super-bollinger/positions", "super-bollinger/supervisor", "super-bollinger/scale",
             "super-bollinger/live-state", "positions", "luxury/positions", "swing/positions", "bollinger/positions",
             "paper-mode", "swing-momentum/status"]
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
        except Exception as exc:  # noqa: BLE001
            snap[ep] = None
            (problems if ep.startswith("super-bollinger/p") else notes).append(f"could not read /{ep}: {exc!r}")
    print(f"snapshot {stamp} -> {SNAP_DIR}/")

    sb, sup, live = snap.get("super-bollinger/positions") or {}, snap.get("super-bollinger/supervisor") or {}, \
        snap.get("super-bollinger/live-state")
    real = [("CE", p) for p in sb.get("live_positions", [])] + [("hedge", p) for p in sup.get("open_hedges_real", [])]
    for kind, p in real:
        print(f"  real {kind}: {p['trading_symbol']} x{p['quantity']} entry {p['entry_price']} best {p['best_price']} "
              f"SL {p.get('stop_loss_order_id')}")
        if p.get("pending_exit_order_id"):
            problems.append(f"{p['trading_symbol']}: an exit order is in flight")
        if not p.get("stop_loss_order_id"):
            problems.append(f"{p['trading_symbol']}: no broker stop-loss order on record")
    reserved = set(sb.get("reserved_symbols", [])) - {p["underlying_symbol"] for p in sb.get("live_positions", [])}
    if reserved:
        problems.append(f"an entry is in flight for {sorted(reserved)}")
    if live is None:
        notes.append("the running build has no /super-bollinger/live-state (older code) - the restart will rebuild from "
                     "the broker + best-price memory")
    else:
        if live.get("intents"):
            problems.append(f"{len(live['intents'])} order intent(s) in flight")
        if real:
            try:
                disk = json.loads(STATE_FILE.read_text())
                age = (now - datetime.fromisoformat(disk["saved_at"])).total_seconds()
                on_disk = {p["trading_symbol"] for p in disk.get("positions", []) + disk.get("hedges", [])}
                missing = [p["trading_symbol"] for _k, p in real if p["trading_symbol"] not in on_disk]
                if disk.get("day") != now.date().isoformat():
                    problems.append(f"state file is from {disk.get('day')}")
                if missing:
                    problems.append(f"state file does not hold {missing}")
                for _k, p in real:
                    row = next((x for x in disk.get("positions", []) + disk.get("hedges", [])
                                if x["trading_symbol"] == p["trading_symbol"]), None)
                    if row and row["best_price"] + 1e-9 < p["best_price"]:
                        notes.append(f"{p['trading_symbol']}: best on disk {row['best_price']} < live {p['best_price']} "
                                     f"(state file {age:.0f}s old) - wait a few seconds and re-run")
                print(f"  state file: {len(on_disk)} position(s), saved {age:.0f}s ago")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"state file unreadable: {exc!r}")
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
    try:
        report = get("super-bollinger/restart-report")
    except Exception as exc:  # noqa: BLE001
        print(f"bot is up; no restart report ({exc!r})")
        return 0
    print(f"bot is up {datetime.now(IST):%H:%M:%S}. Restart report:")
    for key in ("positions", "hedges", "closed_while_down", "intents", "warm_up"):
        for row in report.get(key, []):
            print(f"  {key}: {json.dumps(row, default=str)}")
    print(f"  day_state: {report.get('day_state')}  broker_reachable: {report.get('broker_reachable')}  "
          f"needs_review: {report.get('needs_review')}")
    review = bool(report.get("needs_review"))
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
