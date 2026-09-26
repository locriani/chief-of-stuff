#!/usr/bin/env python3
"""Script-side tracker writes, under one lock, stamped in the workspace zone.

    chief-of-stuff log --root R [--date YYYY-MM-DD] <text>...
    chief-of-stuff log --root R [--date YYYY-MM-DD] --stage "<task name>" <stage>

It appends `- HH:MM <text>` at the end of today's `## Log`. With `--stage` it sets that task's `stage` cell
and appends `- HH:MM stage: <name> → <stage>`, the line the board's Flow charts read. The launcher takes the same lock, so
the coordinator's line and a one-shot's line never overwrite each other.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = "## Log"


def lock_path(path: Path) -> Path:
    # The tracker's own directory: a lock file beside it would show up untracked in the workspace's git.
    # ponytail: one lock for the whole daily dir; per-tracker lock files if two days ever write at once.
    return path.parent


def edit(path: Path, change: Callable[[str], str]) -> None:
    """Read, change and replace `path` while holding its lock; a raise from `change` writes nothing."""
    lock = os.open(lock_path(path), os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        text = change(path.read_text())
        # A temporary sibling keeps readers from seeing a half-written tracker.
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as temp:
            temp.write(text)
        try:
            os.replace(temp.name, path)
        finally:
            Path(temp.name).unlink(missing_ok=True)
    finally:
        os.close(lock)


def stamp(zone: ZoneInfo) -> str:
    return datetime.now(zone).strftime("%H:%M")


def append_log(text: str, line: str, create: bool = False) -> str:
    """`line` as the last line of the Log section. With no Log section, refuse, or add one when `create`."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, row in enumerate(lines) if row.rstrip() == LOG), None)
    if start is None and not create:
        raise ValueError(f"the tracker has no {LOG} section")
    if start is None:
        return text.rstrip("\n") + f"\n\n{LOG}\n\n{line.rstrip()}\n"
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    # Land after the last line of text, not after the blank line that closes the section.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    if not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    lines.insert(end, line.rstrip("\n") + "\n")
    return "".join(lines)


def set_stage(text: str, name: str, stage: str, lanes: dict) -> tuple[str, str]:
    """`text` with the task named `name` at `stage`, and the row's own name. Raises when nothing may be written."""
    from render_board import TASK_COLS_LANED, _cells, _is_separator
    lines = text.splitlines(keepends=True)
    start = next((i for i, row in enumerate(lines) if row.rstrip() == "## Tasks"), None)
    if start is None:
        raise ValueError("the tracker has no ## Tasks section")
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    rows = [i for i in range(start + 1, end) if lines[i].strip().startswith("|")]
    cols = list(TASK_COLS_LANED)
    if not rows or [c.lower() for c in _cells(lines[rows[0]])] != cols:
        raise ValueError("the Tasks table has no lane and stage columns")
    hits = [i for i in rows[1:] if not _is_separator(cells := _cells(lines[i])) and cells[0].casefold() == name.strip().casefold()]
    if len(hits) != 1:
        raise ValueError(f"{len(hits)} tasks are named {name!r}" if hits else f"no task named {name!r}")
    cells = _cells(lines[hits[0]])
    if len(cells) != len(cols):
        raise ValueError(f"the row for {name!r} has {len(cells)} cells, expected {len(cols)}")
    task = dict(zip(cols, cells))
    lane = lanes.get(task["lane"])
    if lane is None:
        raise ValueError(f"{task['name']!r} is in no lane the settings file names")
    if stage not in lane.stages:
        raise ValueError(f"{stage!r} is not a stage of {task['lane']}: {', '.join(lane.stages)}")
    cells[cols.index("stage")] = stage
    lines[hits[0]] = "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |\n"
    return "".join(lines), task["name"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--date", help="tracker day, default today in the workspace zone")
    ap.add_argument("--stage", metavar="TASK", help="move the named task to the stage given as the text")
    ap.add_argument("text", nargs="+")
    args = ap.parse_args(argv)
    text = " ".join(args.text).strip()
    if not text or "\n" in text or "\r" in text or (args.stage is not None and len(args.text) != 1):
        print("log: one non-empty line of text is required, or with --stage one stage name", file=sys.stderr)
        return 2
    from render_board import ConfigError, parse_coordinator
    from settings import SettingsError, load as load_settings
    root = Path(args.root)
    try:
        cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=datetime.now().date())
        path = root / cfg.tracker_path(args.date or datetime.now(cfg.zone).date().isoformat())
        lanes = load_settings(root, cfg.settings_path).lanes if args.stage is not None else {}
        at = stamp(cfg.zone)

        def change(body: str) -> str:
            if args.stage is None:
                return append_log(body, f"- {at} {text}")
            body, name = set_stage(body, args.stage, text, lanes)
            return append_log(body, f"- {at} stage: {name} → {text}")
        edit(path, change)
    except (OSError, ConfigError, SettingsError, ValueError) as e:
        print(f"log: nothing written — {e}", file=sys.stderr)
        return 1
    print(f"logged {at} in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
