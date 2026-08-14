#!/usr/bin/env python3
"""Cron publish wrapper for the Montana Wildfire Tracker.

Runs the bake (collect.py), then commits + pushes ONLY when the material data
changed. Silent when nothing new (no_agent cron delivery semantics: empty
stdout = silent run; stdout IS the report).

Usage:  python scripts/publish.py [--dry-run]
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SNAP = os.path.join(ROOT, "data", "snapshot.json")
PREV = os.path.join(ROOT, "data", "snapshot_prev.json")
DRY = "--dry-run" in sys.argv

MATERIAL = ["incidents", "perimeters", "aqi", "alerts", "smoke", "counties", "cities", "stats"]


def run(cmd, **kw):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, **kw)


def fingerprint(data):
    """Material fields only — ignore the 'updated' timestamp."""
    return json.dumps({k: data.get(k) for k in MATERIAL}, sort_keys=True, separators=(",", ":"))


def main():
    t0 = time.time()
    sys.path.insert(0, HERE)
    import collect  # noqa: E402

    # run the bake, capturing its output (kept out of our stdout so the
    # "silent when unchanged" contract holds)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        collect.main()

    with open(SNAP, encoding="utf-8") as f:
        snap = json.load(f)

    prev_fp = None
    if os.path.exists(PREV):
        try:
            with open(PREV, encoding="utf-8") as f:
                prev_fp = fingerprint(json.load(f))
        except Exception:
            prev_fp = None

    cur_fp = fingerprint(snap)
    changed = cur_fp != prev_fp

    if not changed:
        # undo the timestamp-only rewrite so the worktree stays clean
        if not DRY:
            run(["git", "restore", "--worktree", "index.html", "data/timestamp.json"])
        return 0  # silent

    if DRY:
        print("[dry-run] data changed — would commit+push")
        return 0

    with open(PREV, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))

    # ensure git identity for cron commits
    who = run(["git", "config", "user.name"])
    if not who.stdout.strip():
        run(["git", "config", "user.name", "montana-fire-bot"])
        run(["git", "config", "user.email", "montana-fire-bot@users.noreply.github.com"])

    run(["git", "add", "index.html", "data/"])
    r = run(["git", "commit", "-m", "auto: fire data update"])
    if r.returncode != 0:
        print("commit failed:", r.stderr.strip()[:300])
        return 1
    # pull --rebase first (cron bot push-race safety), then push
    run(["git", "pull", "--rebase", "origin", "master"])
    p = run(["git", "push", "origin", "master"])
    if p.returncode != 0:
        print("push failed:", p.stderr.strip()[:300])
        return 1

    s = snap.get("stats", {})
    worst = s.get("worstAqi")
    inc = snap.get("incidents", [])
    top = ", ".join(i["n"] for i in inc[:3])
    print(f"🔥 Montana fires update — {s.get('fires')} active · {int(s.get('acres', 0)):,} ac · "
          f"{s.get('redFlags', 0)} red flags"
          + (f" · worst AQI {worst['aqi']} {worst['cat']}" if worst else "")
          + f"\nTop: {top}\n({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    sys.exit(main())
