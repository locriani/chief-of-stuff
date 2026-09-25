#!/usr/bin/env python3
"""The coordinator's notifications, written where a notifier reads them.

Zach, 2026-09-22 22:20: "consider integration with my md-notify repo for initial setup of notifications -
with a note that we will be plugging this out later and integrating with my todo tooling instead at a future
point." So md-notify is an adapter behind `adapter_for`, and a Todo adapter replaces it without any caller
changing. Todo's `compile` rewrites its whole queue file: the two must never share one.

    python3 notify.py --root . sync                                   # deadline warnings, day open and close
    python3 notify.py --root . add --kind awaiting --what "PR 0.23.0" [--message M] [--when now|YYYY-MM-DD HH:MM]

md-notify reads a markdown table every minute (MNCore `NotificationSchedule.parse`). What that parser does
decides what this writer does:

- A line containing `---` is a separator and one containing `When (CT)` is a header, wherever they occur,
  so no cell may carry either, and a `|` splits a cell. `clean()` removes all three.
- A one-shot row fires once its time is reached and then **resurfaces every 15 minutes until it is tapped**.
  A past row is never written, except `--when now`: a stale warning would nag for nothing.
- Snooze rewrites a row's `When`, and Complete and Ignore move it under `## Completed` / `## Ignored`. The
  Title is the only thing that survives all three, so a Title found anywhere in the file is never added
  again. Titles are therefore built here, from a kind and a clock read, never typed free.
- `When` is local time on the Mac; this writes the workspace timezone. They are the same here.
- The app rewrites the file when a banner is tapped. Each write here is one `os.replace` from a temp file
  beside the queue, so neither side reads half a file; a tap landing inside this read-modify-write can
  still lose one of the two edits.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_board import ConfigError, parse_coordinator, with_workspace_decision_deadlines  # noqa: E402
from settings import Settings, SettingsError, load  # noqa: E402

WHEN = "%Y-%m-%d %H:%M"
KINDS = {"awaiting": "🙋 Awaiting you", "stopped": "🛑 Session stopped", "gone": "👻 Session gone"}
WARNING = "⏰ "
OPEN_DAY, CLOSE_DAY = "☀️ Open the day", "🌙 Close the day"
TEMPLATE = """# chief-of-stuff notifications

Written by chief-of-stuff's `notify.py` for md-notify, the interim adapter: it is to be replaced by the Todo tooling (Zach, 2026-09-22). Tap Complete or Ignore on a banner to park its row. Never point Todo's `compile` at this file; it rewrites the whole file.

| When (CT) | Title | Message | Open |
|---|---|---|---|

## Recurring

| Every | Title | Message | Open |
|---|---|---|---|
"""
RECURRING_TABLE = ["", "## Recurring", "", "| Every | Title | Message | Open |", "|---|---|---|---|"]
PARKED = ("completed", "ignored")


def clean(text: str) -> str:
    """One line, safe for md-notify's row filter: no `|`, no `---`, no `When (CT)`."""
    t = " ".join(str(text).split()).replace("|", "/")
    return re.sub(r"-{3,}", "—", t).replace("When (CT)", "When CT")


def event_title(kind: str, what: str, now: datetime) -> str:
    """The clock read is in the title, so one event notifies once and the same ask later notifies again."""
    return f"{KINDS[kind]} — {clean(what)} ({now:%H:%M})"


@dataclass(frozen=True)
class Row:
    when: str
    title: str
    message: str = ""
    open: str = ""
    legacy_title: str = ""

    def line(self) -> str:
        return f"| {self.when} | {clean(self.title)} | {clean(self.message)} | {clean(self.open)} |"


@dataclass
class Changes:
    lines: list[str] = field(default_factory=list)


def warnings(cfg, notify, now: datetime, board: str) -> list[Row]:
    """One row per future warning. The full due date distinguishes deadlines a week apart."""
    rows = []
    for d in cfg.deadlines:
        at = d.at.astimezone(cfg.zone)
        for label, delta in notify.warnings:
            when = at - delta
            if when > now:
                head = f"{WARNING}{d.name} in {label}"
                rows.append(Row(when.strftime(WHEN), f"{head} ({at:%a %Y-%m-%d %H:%M})",
                                f"Due {at:%Y-%m-%d %H:%M}", board,
                                legacy_title=f"{head} ({at:%a %H:%M})"))
    return sorted(rows, key=lambda r: r.when)


def day_rows(notify, board: str) -> list[Row]:
    return [Row(f"daily {notify.day_open}", OPEN_DAY, "Open the day with the coordinator.", board),
            Row(f"daily {notify.day_close}", CLOSE_DAY, "Close the day: log, tracker, what carries over.", board)]


def _cells(line: str) -> list[str]:
    body = line[1:]
    body = body[:-1] if body.endswith("|") else body
    return [c.strip() for c in body.split("|")]


def _is_data(line: str) -> bool:
    if not line.startswith("|") or "---" in line or "When (CT)" in line:
        return False
    cells = _cells(line)
    return len(cells) > 1 and cells[0] not in ("", "Every")


def _regions(lines: list[str]) -> list[str]:
    """Per line: `active`, `recurring` or `parked`, by the heading above it, the way md-notify reads it."""
    out, current = [], "active"
    for line in lines:
        if line.strip().startswith("#"):
            name = line.strip().lstrip("#").strip().lower()
            current = "parked" if name in PARKED else "recurring" if name == "recurring" else "active"
        out.append(current)
    return out


class Notifier(Protocol):
    def add(self, row: Row) -> Changes: ...
    def sync(self, one_shot: list[Row], recurring: list[Row], now: datetime) -> Changes: ...


class Off:
    def add(self, row: Row) -> Changes:
        return Changes()

    def sync(self, one_shot: list[Row], recurring: list[Row], now: datetime) -> Changes:
        return Changes()


class MdNotifyQueue:
    """md-notify's queue file. Interim: to be replaced by the Todo tooling (Zach, 2026-09-22 22:20)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> list[str]:
        return (self.path.read_text() if self.path.is_file() else TEMPLATE).split("\n")

    def _write(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = self.path.stat().st_mode & 0o777 if self.path.exists() else 0o644
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(lines))
            os.chmod(tmp, mode)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @staticmethod
    def _titles(lines: list[str]) -> set[str]:
        return {_cells(x)[1] for x in lines if _is_data(x)}

    @staticmethod
    def _table_end(lines: list[str], region: str) -> int | None:
        """The index after the last row of the first table in `region`, or None when it has none."""
        regions, start = _regions(lines), None
        for i, line in enumerate(lines):
            if regions[i] == region and line.startswith("|") and not line.strip().startswith("#"):
                start = i
                break
        if start is None:
            return None
        end = start
        while end + 1 < len(lines) and lines[end + 1].startswith("|"):
            end += 1
        return end + 1

    def _insert(self, lines: list[str], row: Row, region: str) -> None:
        at = self._table_end(lines, region)
        if at is None and region == "recurring":
            tail = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
            lines[tail:tail] = RECURRING_TABLE
            at = self._table_end(lines, region)
        elif at is None:
            first_heading = next((i for i, x in enumerate(lines) if x.startswith("## ")), len(lines))
            lines[first_heading:first_heading] = ["| When (CT) | Title | Message | Open |", "|---|---|---|---|", ""]
            at = first_heading + 2
        lines.insert(at, row.line())

    def add(self, row: Row) -> Changes:
        lines = self._read()
        if clean(row.title) in self._titles(lines):
            return Changes([f"notify: already there {clean(row.title)}"])
        self._insert(lines, row, "active")
        self._write(lines)
        return Changes([f"notify: added {clean(row.title)}"])

    def sync(self, one_shot: list[Row], recurring: list[Row], now: datetime) -> Changes:
        """Reconcile only what this script owns: `⏰ ` rows in the active table, and the two day rows.
        A future owned row that is no longer wanted goes; a past one stays for its owner to tap; a snoozed
        one keeps its new time, because it is still wanted by title."""
        lines, said = self._read(), []
        created = not self.path.is_file()
        wanted = {clean(r.title) for r in one_shot}
        # Existing queues used weekday and time in the title. Match the full due instant in Message
        # before renaming: a parked warning from a previous week must not suppress this week's one.
        legacy = {(clean(r.legacy_title), clean(r.message)): clean(r.title)
                  for r in one_shot if r.legacy_title}
        for i, line in enumerate(lines):
            if not _is_data(line):
                continue
            cells = _cells(line)
            if len(cells) < 3:
                continue
            replacement = legacy.get((cells[1], cells[2]))
            if replacement and replacement != cells[1]:
                parts = line.split("|")
                parts[2] = parts[2].replace(cells[1], replacement, 1)
                lines[i] = "|".join(parts)
                said.append(f"notify: updated {replacement}")
        regions, keep = _regions(lines), []
        for line, region in zip(lines, regions):
            if region == "active" and _is_data(line):
                when, title = _cells(line)[:2]
                try:
                    at = datetime.strptime(when, WHEN).replace(tzinfo=now.tzinfo)
                except ValueError:
                    at = None
                if title.startswith(WARNING) and title not in wanted and at is not None and at > now:
                    said.append(f"notify: removed {title}")
                    continue
            keep.append(line)
        lines = keep
        for row in one_shot:
            if clean(row.title) not in self._titles(lines):
                self._insert(lines, row, "active")
                said.append(f"notify: added {clean(row.title)}")
        for row in recurring:
            regions = _regions(lines)
            hit = next((i for i, x in enumerate(lines) if regions[i] == "recurring" and _is_data(x)
                        and _cells(x)[1] == clean(row.title)), None)
            if hit is None:
                self._insert(lines, row, "recurring")
                said.append(f"notify: added {clean(row.title)}")
            elif _cells(lines[hit])[0] != row.when:
                lines[hit] = row.line()
                said.append(f"notify: retimed {clean(row.title)} to {row.when}")
        if said or created:
            self._write(lines)
        return Changes(said)


def adapter_for(settings: Settings, root: Path) -> Notifier:
    if settings.notify.adapter == "md-notify":
        return MdNotifyQueue(settings.notify.queue_path(root))
    return Off()


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="one event notification")
    add.add_argument("--kind", choices=sorted(KINDS), required=True)
    add.add_argument("--what", required=True, help="what it is about; goes in the title")
    add.add_argument("--message", default="", help="the banner's body; defaults to --what")
    add.add_argument("--when", default="now", help="now, or YYYY-MM-DD HH:MM in the workspace timezone")
    sub.add_parser("sync", help="deadline warnings and the day open and close rows")
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=datetime.now().date())
        settings = load(root, cfg.settings_path)
    except (OSError, ConfigError, SettingsError) as e:
        print(f"notify: {e}", file=sys.stderr)
        return 2
    if settings.notify.adapter == "off":
        print("notify: off")
        return 0
    now = (now or datetime.now(cfg.zone)).astimezone(cfg.zone).replace(second=0, microsecond=0)
    queue, board = adapter_for(settings, root), cfg.board_url or ""
    if args.cmd == "sync":
        try:
            tracker = root / cfg.tracker_path(now.date().isoformat())
            tracker_text = tracker.read_text() if tracker.is_file() else ""
            cfg = with_workspace_decision_deadlines(root, cfg, tracker_text, now.date())
        except OSError as e:
            print(f"notify: {e}", file=sys.stderr)
            return 2
        got = queue.sync(warnings(cfg, settings.notify, now, board), day_rows(settings.notify, board), now)
    else:
        when = now
        if args.when != "now":
            try:
                when = datetime.strptime(args.when, WHEN).replace(tzinfo=cfg.zone)
            except ValueError:
                print(f"notify: --when {args.when!r} is not now or YYYY-MM-DD HH:MM", file=sys.stderr)
                return 2
            if when < now:
                print(f"notify: --when {args.when} has passed; a past row resurfaces until tapped, so use --when now",
                      file=sys.stderr)
                return 2
        got = queue.add(Row(when.strftime(WHEN), event_title(args.kind, args.what, now), args.message or args.what, board))
    print("\n".join(got.lines) or "notify: nothing to change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
