#!/usr/bin/env python3
"""Check worker PIDs in one call without exposing their command lines.

With no PIDs, check every registered worker and list every live one-shot run. PIDs can be space or comma separated.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import runtimes
from _vendor.toon_format import encode as toon_encode
from workspace import worktrees_dir

# `<launcher pid> <task>` while a one-shot runs in this tree; the count behind `[workers] max_concurrency`.
PIDFILE = Path(".chief-of-stuff") / "one-shot.pid"


def registrations(root: Path) -> dict[int, dict[str, str]]:
    result = {}
    for path in sorted((root / ".chief-of-stuff" / "sessions").glob("*.json")):
        try:
            record = json.loads(path.read_text())
            pid = int(record["pid"])
            if pid > 0 and all(isinstance(record[key], str) for key in ("name", "runtime", "worktree")):
                result[pid] = record
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def parse_pids(values: list[str]) -> list[int]:
    pids = []
    for value in values:
        for part in value.split(","):
            if not part.isdecimal() or int(part) <= 0:
                raise ValueError(f"invalid PID: {part!r}")
            pid = int(part)
            if pid not in pids:
                pids.append(pid)
    return pids


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def one_shot_runs(trees: Path) -> list[tuple[Path, int, str]]:
    """(worktree, launcher pid, task) for each one-shot whose launcher is alive. A launcher that died, or a restart
    that killed it, holds no slot. ponytail: pid reuse can count a dead launcher; add the process start time if it bites."""
    found = []
    for f in sorted(trees.glob(f"*/{PIDFILE}")):
        try:
            pid, _, task = f.read_text().partition(" ")
            if int(pid) > 0 and process_exists(int(pid)):
                found.append((f.parent.parent, int(pid), task.strip() or f.parent.parent.name))
        except (OSError, ValueError):
            continue
    return found


def running_trees(trees: Path) -> dict[Path, str]:
    """Each worktree whose one-shot launcher is alive, and its task."""
    return {tree: task for tree, _, task in one_shot_runs(trees)}


def running_workers(trees: Path) -> list[str]:
    return list(running_trees(trees).values())


def check(root: Path, pids: list[int]) -> list[dict[str, str | int]]:
    registered = registrations(root)
    selected = pids if pids else list(registered)
    present = [pid for pid in selected if process_exists(pid)]
    commands: dict[int, str] = {}
    if present:
        try:
            probe = subprocess.run(["ps", "-ww", "-p", ",".join(map(str, present)),
                                    "-o", "pid=,command="], capture_output=True, text=True,
                                   timeout=3)
            for line in probe.stdout.splitlines():
                head, _, command = line.strip().partition(" ")
                if head.isdecimal():
                    commands[int(head)] = command.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    rows = []
    for pid in selected:
        record = registered.get(pid)
        status = "gone"
        if pid in present:
            command = commands.get(pid)
            if command is None:
                status = "unverified"
            elif record is None:
                status = "running"
            else:
                binary = runtimes.binary(record["runtime"])
                status = ("running" if binary in command and record["worktree"] in command
                          else "pid_reused")
        rows.append({"pid": pid, "name": record["name"] if record else "",
                     "runtime": record["runtime"] if record else "", "status": status})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="workspace containing the worker registry")
    ap.add_argument("pids", nargs="*", help="PIDs, separated by spaces or commas; omit for all workers and one-shot runs")
    args = ap.parse_args(argv)
    try:
        pids = parse_pids(args.pids)
    except ValueError as exc:
        ap.error(str(exc))
    rows: list[dict[str, str | int]] = check(args.root, pids)
    if not pids:
        try:
            runs = one_shot_runs(args.root / worktrees_dir((args.root / "CLAUDE.md").read_text()))
        except OSError:  # no CLAUDE.md, so no trees dir to look in
            runs = []
        rows += [{"pid": pid, "task": task, "worktree": str(tree), "status": "running", "kind": "one-shot"}
                 for tree, pid, task in runs]
    print(toon_encode(rows) if rows else "[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
