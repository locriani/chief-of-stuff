#!/usr/bin/env python3
"""Print a finished one-shot's report, so the coordinator never opens a file under the worker's tree.

With no `--task`, one TOON list of the runs: task, worker, worktree, status, and whether the tracker took the run
(`reconciled`). With `--task <task>`, that task's newest report, exit 1 when there is none. The report is the worker's to
write, so one counts only in a tree the tracker's File ownership row for its task names, and only the launcher's keys print.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _vendor.toon_format import ToonDecodeError, decode as toon_decode, encode as toon_encode
from dispatch_prompt import CONTROL, REPORT_FILE, TRACKER_ERROR, RefusedError, resolve_task, task_keys
from git_trees import read_regular
from md import section
from process_status import shown_task, shown_tree, tracker_texts
from tracker import parse_tracker
from workspace import ConfigError, read_config, worktrees_dir
import ownership

LIMIT = 1 << 20  # a report is a few lines; the file is the worker's to write
CAP = 200  # the longest a worker or status may be
SEVEN = ("status", "task", "worker", "runtime_exit", "reason", "changes", "errors")  # what the launcher writes
TREE = re.compile(r"worktree `([^`]+)` \(")  # how the launcher's `_tree_note` names a tree in a File ownership row


def owned_trees(texts: list[str]) -> dict[str, set[str]]:
    """Each task's item and name, and the trees the File ownership row for it names."""
    owned: dict[str, set[str]] = {}
    for text in texts:
        rows = ownership.parse("\n".join(section(text, ownership.HEADING)))
        for t in parse_tracker(text).tasks:
            keys = task_keys(t.item, t.name)
            trees = {tree for row in rows if row.context in keys for tree in TREE.findall(row.paths)}
            for key in (t.item.strip(), t.name.strip()):
                if key:
                    owned.setdefault(key, set()).update(trees)
    return owned


def one_line(value: object, cap: int = LIMIT) -> bool:
    return isinstance(value, str) and len(value.splitlines()) < 2 and len(value) <= cap


def read_report(path: Path, owned: dict[str, set[str]]) -> dict:
    """The report at `path`, or ValueError when it cannot be trusted: a link, too big, not TOON, not the launcher's
    shape, or in a tree no File ownership row names for its task."""
    if path.parent.is_symlink():
        raise ValueError("the private directory is a link")
    raw = read_regular(path, LIMIT + 1, nofollow=True)
    if raw is None or len(raw) > LIMIT:
        raise ValueError("not a report")
    data = toon_decode(raw.decode())
    if not (isinstance(data, dict) and one_line(data.get("task")) and one_line(data.get("worker"), CAP)
            and one_line(data.get("status"), CAP)):
        raise ValueError("not a report")
    if path.parent.parent.name not in owned.get(data["task"], ()):
        raise ValueError("no File ownership row names this tree")
    return data


def clean(value: object) -> object:
    """A worker's value with control characters (but newlines and tabs) removed; a mapping is not the launcher's shape."""
    if isinstance(value, str):
        return CONTROL.sub("", value)
    if isinstance(value, list):
        return [clean(v) for v in value]
    return None if isinstance(value, dict) else value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="workspace whose CLAUDE.md names the worktrees directory")
    ap.add_argument("--task", help="the task whose report to print: its item, or its name in the Tasks table")
    args = ap.parse_args(argv)
    try:
        trees = args.root / worktrees_dir((args.root / "CLAUDE.md").read_text())
        read_config(args.root).zone  # a CLAUDE.md with no Coordinator block, or an unknown Timezone, names no tracker
    except (OSError, ValueError, ConfigError, KeyError):
        print("result: CLAUDE.md is missing, unreadable or has no Coordinator block", file=sys.stderr)
        return 1
    owned = owned_trees(tracker_texts(args.root))
    runs, skipped = [], 0  # (mtime, tree name, report) of each report that can be trusted
    for path in sorted(trees.glob(f"*/{REPORT_FILE}")):
        try:
            runs.append((path.stat().st_mtime, path.parent.parent.name, read_report(path, owned)))
        except (OSError, ValueError, ToonDecodeError, RecursionError):
            skipped += 1
    if skipped:
        print(f"one-shot reports skipped: {skipped} could not be read", file=sys.stderr)

    if args.task is not None:
        if args.task not in owned:
            print(f"no task '{args.task}' in the tracker", file=sys.stderr)
            return 1
        try:
            task = resolve_task(args.root, None, args.task)  # the item, as the launcher records it
        except RefusedError:
            task = args.task
        found = [r for r in runs if r[2]["task"] == task]
        if not found:
            print(f"no one-shot report for task '{args.task}'", file=sys.stderr)
            return 1
        _, tree, data = max(found, key=lambda r: r[0])
        print(toon_encode({**{key: clean(data.get(key)) for key in SEVEN}, "worktree": shown_tree(tree)}))
        return 0
    rows = []
    for _, tree, data in runs:
        errors = data.get("errors")
        rows.append({"task": shown_task(data["task"], owned), "worker": clean(data["worker"]), "worktree": shown_tree(tree),
                     "status": clean(data["status"]),
                     "reconciled": isinstance(errors, list) and not any(str(e).startswith(TRACKER_ERROR) for e in errors)})
    print(toon_encode(rows) if rows else "[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
