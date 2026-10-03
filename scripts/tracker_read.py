#!/usr/bin/env python3
"""Read the tracker through the shared parser; writes nothing.

    chief-of-stuff tracker --root R [--date YYYY-MM-DD] header
    chief-of-stuff tracker --root R [--date YYYY-MM-DD] sections
    chief-of-stuff tracker --root R [--date YYYY-MM-DD] section <name>
    chief-of-stuff tracker --root R [--date YYYY-MM-DD] tasks [--not <state>]... [--state <state>]... [--issue <ref>]

`header` prints the lines above the first `## ` heading, `sections` the headings, `section` one section's body
as written, and `tasks` one TOON row per task: name, owner, state, stage, issue. The item cell is never printed.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import backlog
from _vendor.toon_format import encode as toon_encode
from md import section
from tracker import clip_name, parse_tracker
from workspace import ConfigError, read_config

# The first word of a state cell, as `Task.kind` reads it; the vocabulary is tracker.TASK_STATE's.
STATES = ("open", "waiting", "orphaned", "running", "done")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("."), help="workspace root holding CLAUDE.md")
    ap.add_argument("--date", help="tracker day, default today in the workspace zone")
    sub = ap.add_subparsers(dest="read", required=True)
    sub.add_parser("header", help="the lines above the first heading, as written")
    sub.add_parser("sections", help="the tracker's section names, one per line")
    sub.add_parser("section", help="one section's body as written").add_argument("name")
    tasks = sub.add_parser("tasks", help="one row per task: name, owner, state, stage, issue")
    tasks.add_argument("--not", dest="skip", action="append", default=[], choices=STATES, metavar="STATE",
                       help="leave out tasks in this state; repeatable")
    tasks.add_argument("--state", action="append", default=[], choices=STATES, metavar="STATE",
                       help="keep only tasks in this state; repeatable")
    tasks.add_argument("--issue", help="keep only the task with this issue reference")
    args = ap.parse_args(argv)
    try:
        cfg = read_config(args.root)
        text = (args.root / cfg.tracker_path(args.date or datetime.now(cfg.zone).date().isoformat())).read_text()
    except (OSError, ConfigError) as exc:
        print(f"tracker: {exc}", file=sys.stderr)
        return 2
    if args.read == "header":
        print(re.split(r"(?m)^## ", text, maxsplit=1)[0].strip("\n"))
        return 0
    if args.read == "sections":
        print("\n".join(line[3:].strip() for line in text.splitlines() if line.startswith("## ")))
        return 0
    if args.read == "section":
        heading = f"## {args.name}"
        if heading not in (line.strip() for line in text.splitlines()):
            print(f"tracker: no {heading} section", file=sys.stderr)
            return 1
        print("\n".join(section(text, heading)).strip("\n"))
        return 0
    wanted = backlog.parse_issue_arg(args.issue, cfg.backlog) if args.issue else None
    if args.issue and wanted is None:
        print(f"tracker: invalid issue reference {args.issue!r}", file=sys.stderr)
        return 2
    rows = [{"name": clip_name(task.label), "owner": task.owner.strip(), "state": task.state.strip(),
             "stage": task.stage.strip(), "issue": task.issue.strip()}
            for task in parse_tracker(text).tasks
            if task.kind not in args.skip and (not args.state or task.kind in args.state)
            and (wanted is None or backlog.issue_ref(task.issue, cfg.backlog) == wanted)]
    print(toon_encode(rows) if rows else "[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
