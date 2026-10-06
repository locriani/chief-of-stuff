#!/usr/bin/env python3
"""Print a finished one-shot's report, so the coordinator never opens a file under the worker's tree.

With no `--task`, one TOON list of the runs: task, worker, worktree, status, and whether the tracker took the run
(`reconciled`). With `--task <task>`, that task's newest whole report, exit 1 when there is none.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _vendor.toon_format import ToonDecodeError, decode as toon_decode, encode as toon_encode
from git_trees import read_regular
from one_shot import REPORT
from process_status import TASK_MAX, TREE_NAME, known_tasks
from workspace import ConfigError, read_config, worktrees_dir

LIMIT = 1 << 20  # a report is a few lines; the file is the worker's to write


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="workspace whose CLAUDE.md names the worktrees directory")
    ap.add_argument("--task", help="the task whose report to print, exactly as the tracker names it")
    args = ap.parse_args(argv)
    try:
        trees = args.root / worktrees_dir((args.root / "CLAUDE.md").read_text())
        read_config(args.root)  # a CLAUDE.md with no Coordinator block names no worktrees directory
    except (OSError, ValueError, ConfigError):
        print("result: CLAUDE.md is missing, unreadable or has no Coordinator block", file=sys.stderr)
        return 1
    try:
        known = known_tasks(args.root) - {""}  # printed or selected: a task only when a tracker holds it
    except (OSError, ConfigError, ValueError, KeyError):
        print("one-shot tasks not shown: the tracker could not be read", file=sys.stderr)
        known = set()
    runs, skipped = [], 0  # (mtime, tree name, report) of each report that can be trusted
    for path in sorted(trees.glob(f"*/{REPORT}")):
        try:
            raw = read_regular(path, LIMIT + 1, nofollow=True)
            data = toon_decode(raw.decode()) if raw is not None and len(raw) <= LIMIT else None
            if not isinstance(data, dict):
                raise ValueError("not a report")
            runs.append((path.stat().st_mtime, path.parent.parent.name, data))
        except (OSError, ValueError, ToonDecodeError):
            skipped += 1
    if skipped:
        print(f"one-shot reports skipped: {skipped} could not be read", file=sys.stderr)

    def plain(tree: str) -> str:
        return tree if TREE_NAME.fullmatch(tree) else ""

    if args.task is not None:
        found = [r for r in runs if args.task in known and r[2].get("task") == args.task]
        if not found:
            print(f"no one-shot report for task '{args.task}'", file=sys.stderr)
            return 1
        _, tree, data = max(found, key=lambda r: r[0])
        print(toon_encode({**data, "worktree": plain(tree)}))
        return 0
    rows = []
    for _, tree, data in runs:
        task, errors = data.get("task"), data.get("errors")
        rows.append({"task": task[:TASK_MAX] if isinstance(task, str) and task in known else "", "worker": data.get("worker"),
                     "worktree": plain(tree), "status": data.get("status"),
                     "reconciled": not (isinstance(errors, list) and any(str(e).startswith("tracker:") for e in errors))})
    print(toon_encode(rows) if rows else "[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
