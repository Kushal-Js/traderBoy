#!/usr/bin/env python3
"""
Archive the weekly shadow watchlist into git (1 Oct 2026, user: "commit the
weekly shadow lists after each Friday run").

The Friday 00:00 IST job on the droplet (weekly_watchlist_refresh.py ->
stock_selection.run_shadow) writes data/super_bollinger_shadow_watchlist.json
(that week's live + shadow lists, every pick's fit) and appends one line to
data/super_bollinger_shadow_scores.jsonl (the previous lists scored on the week
that followed). data/ is gitignored and exists only on the droplet. This copies
both into research_results/shadow_lists/ and commits + pushes ONLY those files:

  research_results/shadow_lists/<as_of>_shadow_record.json   one per week
                                                              (as_of = last session the lists used)
  research_results/shadow_lists/shadow_scores.jsonl          the full weekly score history

Only stock lists and stock-price scores - no account data, positions or P&L.
Droplet access comes from the environment, never from this file (the repo is
public): DHANBOY_SSH (user@host) and DHANBOY_SSH_KEY (path to the private key).

    DHANBOY_SSH=... DHANBOY_SSH_KEY=... python3 archive_shadow_lists.py [--no-push]

Exit 0 = archived, or nothing new to archive; 1 = something failed (says what).
Standard library only.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
DEST = REPO / "research_results" / "shadow_lists"
REMOTE_DIR = "~/apps/traderBoy/data"
RECORD, SCORES = "super_bollinger_shadow_watchlist.json", "super_bollinger_shadow_scores.jsonl"
BRANCH = "dhanBoy"
TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def run(*args, check=True, **kw):
    return subprocess.run(list(args), cwd=str(REPO), capture_output=True, text=True, check=check, **kw)


def fetch(host: str, key: str, name: str, into: Path) -> Path | None:
    target = into / name
    proc = run("scp", "-q", "-i", key, "-o", "ConnectTimeout=20", "-o", "BatchMode=yes",
               f"{host}:{REMOTE_DIR}/{name}", str(target), check=False)
    if proc.returncode != 0:
        if "No such file" in proc.stderr:
            return None
        raise RuntimeError(f"could not copy {name} from the droplet: {proc.stderr.strip()}")
    return target


def main() -> int:
    host, key = os.environ.get("DHANBOY_SSH"), os.environ.get("DHANBOY_SSH_KEY")
    if not host or not key:
        print("DHANBOY_SSH and DHANBOY_SSH_KEY must be set (droplet access is never stored in this public repo)")
        return 1
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != BRANCH:
        print(f"the repo is on branch {branch!r}, not {BRANCH!r} - not committing")
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="shadow_archive_"))
    try:
        record_file = fetch(host, os.path.expanduser(key), RECORD, tmp)
        scores_file = fetch(host, os.path.expanduser(key), SCORES, tmp)
        if record_file is None:
            print("no shadow record on the droplet yet - nothing to archive")
            return 0
        record = json.loads(record_file.read_text())
        DEST.mkdir(parents=True, exist_ok=True)
        paths = []
        rec_dest = DEST / f"{record['as_of']}_shadow_record.json"
        shutil.copyfile(record_file, rec_dest)
        paths.append(rec_dest)
        last_score = None
        if scores_file is not None:
            sc_dest = DEST / "shadow_scores.jsonl"
            shutil.copyfile(scores_file, sc_dest)
            paths.append(sc_dest)
            lines = [line for line in scores_file.read_text().splitlines() if line.strip()]
            last_score = json.loads(lines[-1]) if lines else None
        rel = [str(p.relative_to(REPO)) for p in paths]
        run("git", "add", "--", *rel)
        if run("git", "diff", "--cached", "--quiet", "--", *rel, check=False).returncode == 0:
            print(f"nothing new: the lists of {record['as_of']} are already archived")
            return 0
        lines = [f"Shadow watchlist archive: lists of {record['as_of']} (picked {record.get('picked_at')})", "",
                 f"live:   {' '.join(record['live']['symbols'])}",
                 f"shadow: {' '.join(record['shadow']['symbols'])}",
                 f"only live: {' '.join(record.get('only_live', []))} | only shadow: {' '.join(record.get('only_shadow', []))}"]
        if last_score:
            lines.append(f"latest weekly score (lists of {last_score['as_of']}, {len(last_score['sessions'])} sessions): "
                         f"live {last_score['live']['pct']:+.2f}% vs shadow {last_score['shadow']['pct']:+.2f}% "
                         "(stock-price replay with the 1-hour filter)")
        lines += ["", "Copied from the droplet's data/ by archive_shadow_lists.py.", "", TRAILER]
        run("git", "commit", "-q", "-m", "\n".join(lines), "--", *rel)
        commit = run("git", "log", "--oneline", "-1").stdout.strip()
        print(f"committed {commit} ({', '.join(rel)})")
        if "--no-push" in sys.argv:
            return 0
        if run("git", "push", "-q", "origin", BRANCH, check=False).returncode != 0:
            run("git", "pull", "-q", "--rebase", "--autostash", "origin", BRANCH)
            run("git", "push", "-q", "origin", BRANCH)
        print(f"pushed to origin/{BRANCH}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc!r}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
