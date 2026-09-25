#!/usr/bin/env python3
"""Check worker PIDs in one call without exposing their command lines.

With no PIDs, check every registered worker. PIDs can be space or comma separated.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from _vendor.toon_format import encode as toon_encode


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
                binary = {"cursor": "agent"}.get(record["runtime"], record["runtime"])
                status = ("running" if binary in command and record["worktree"] in command
                          else "pid_reused")
        rows.append({"pid": pid, "name": record["name"] if record else "",
                     "runtime": record["runtime"] if record else "", "status": status})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="workspace containing the worker registry")
    ap.add_argument("pids", nargs="*", help="PIDs, separated by spaces or commas; omit for all workers")
    args = ap.parse_args(argv)
    try:
        pids = parse_pids(args.pids)
    except ValueError as exc:
        ap.error(str(exc))
    rows = check(args.root, pids)
    print(toon_encode(rows) if rows else "[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
