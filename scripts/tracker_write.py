#!/usr/bin/env python3
"""Script-side tracker writes, under one lock, stamped in the workspace zone.

    chief-of-stuff log --root R [--date YYYY-MM-DD] <text>...

It appends `- HH:MM <text>` at the end of today's `## Log`. The launcher takes the same lock, so
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
    return path.with_name(f".{path.name}.lock")


def edit(path: Path, change: Callable[[str], str]) -> None:
    """Read, change and replace `path` while holding its lock; a raise from `change` writes nothing."""
    with open(lock_path(path), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        text = change(path.read_text())
        # A temporary sibling keeps readers from seeing a half-written tracker.
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as temp:
            temp.write(text)
        try:
            os.replace(temp.name, path)
        finally:
            Path(temp.name).unlink(missing_ok=True)


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--date", help="tracker day, default today in the workspace zone")
    ap.add_argument("text", nargs="+")
    args = ap.parse_args(argv)
    text = " ".join(args.text).strip()
    if not text or "\n" in text or "\r" in text:
        print("log: one non-empty line of text is required", file=sys.stderr)
        return 2
    from render_board import ConfigError, parse_coordinator
    root = Path(args.root)
    try:
        cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=datetime.now().date())
        path = root / cfg.tracker_path(args.date or datetime.now(cfg.zone).date().isoformat())
        at = stamp(cfg.zone)
        edit(path, lambda body: append_log(body, f"- {at} {text}"))
    except (OSError, ConfigError, ValueError) as e:
        print(f"log: nothing written — {e}", file=sys.stderr)
        return 1
    print(f"logged {at} in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
