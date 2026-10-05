"""The tracker's `## Log` lines that scripts write and the pages read back: a task's stage move, and a one-shot's
start, end and relaunch.

`chief-of-stuff log --stage` and the one-shot launcher write them with the formatters here; the Flow charts, the
board's workers, the Workers page and the issue page read them with the patterns here. Both sides live in one
module that draws nothing, side by side; evals/test_tracker_log.py writes and reads back every line kind, so a
reworded formatter fails there until its pattern follows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

# A one-shot's end line says one of these. A relaunch line says RELAUNCHED instead, and the launcher looks for it so
# a task relaunches at most once a day.
COMPLETED = "completed; awaiting integration"
HUMAN_REVIEW = "HUMAN REVIEW NEEDED"
RELAUNCHED = ": relaunch requested for {task} — "

STAGE = re.compile(r"^- (\d{1,2}):(\d{2}) stage: (.+) → (\S+)\s*$")
STARTED = re.compile(r"^- (\d{1,2}):(\d{2}) one-shot (\S+) started: .*, task (.+?)\s*$")
ENDED = re.compile(r"^- (\d{1,2}):(\d{2}) one-shot (\S+): (completed; awaiting integration|HUMAN REVIEW NEEDED)\b")
# RELAUNCHED's Log line, grouped as ENDED is.
RELAUNCH = re.compile(r"^- (\d{1,2}):(\d{2}) one-shot (\S+): (relaunch) requested for ")
# A started line's runtime and model and its worktree, for the board's workers and the issue page. STARTED reads
# only the worker and the task.
STARTED_DETAIL = re.compile(r"^-\s+(\d{1,2}:\d{2}) one-shot (\S+) started: (.*?), worktree `([^`]*)`, task (.+?)\s*$")


def stage_line(at: str, name: str, stage: str) -> str:
    """`chief-of-stuff log --stage`'s line: the task row named `name` moved to `stage` at `at` (HH:MM)."""
    return f"- {at} stage: {name} → {stage}"


def started_line(at: str, name: str, runtime: str, model: str, tree: str, task: str, effort: str = "") -> str:
    """The launcher's line before worker `name` runs: `started: <runtime> <model> <effort>, worktree ...`, the model and
    effort words only when set. `tree` is how File ownership names the tree, worktree `<tree>` (<branch>); the line
    leaves the branch out."""
    return f"- {at} one-shot {name} started: {' '.join(filter(None, (runtime, model, effort)))}, {tree.split(' (')[0]}, task {task}"


def ended_line(at: str, name: str, status: str, reason: str, changes: str) -> str:
    """The launcher's line when worker `name` stops `done`, or with any other status for review. `reason` and
    `changes` come already on one line."""
    return (f"- {at} one-shot {name}: {COMPLETED if status == 'done' else HUMAN_REVIEW}"
            f" — {reason}. Changes: {changes}.")


def relaunch_line(at: str, name: str, task: str, reason: str) -> str:
    """The launcher's line when worker `name` asks for `task` to run again; `reason` comes already on one line."""
    return f"- {at} one-shot {name}{RELAUNCHED.format(task=task)}{reason}."


@dataclass(frozen=True)
class Move:
    at: datetime
    name: str
    stage: str
    launch: bool = False
    line: str = field(default="", compare=False)  # the Log line it was read from
    worker: str = field(default="", compare=False)  # a launcher move's worker


def moves(text: str, day: date, zone: ZoneInfo) -> list[Move]:
    """The `stage:` and one-shot launcher lines of the tracker's `## Log`, stamped on `day`. A launcher move names
    the task key of its worker's latest started line."""
    log = next((part for part in re.split(r"(?m)^## ", text) if part.split("\n", 1)[0].strip() == "Log"), "")
    out, task_of = [], {}
    for line in log.splitlines():
        line = line.strip()
        m = STAGE.match(line) or STARTED.match(line) or ENDED.match(line)
        if not m or int(m[1]) >= 24 or int(m[2]) >= 60:
            continue
        at = datetime.combine(day, time(int(m[1]), int(m[2])), tzinfo=zone)
        if m.re is STAGE:
            out.append(Move(at, m[3].strip(), m[4], line=line))
        elif m.re is STARTED:
            task_of[m[3]] = m[4]
            out.append(Move(at, m[4], "implement", True, line, m[3]))
        elif m[3] in task_of:
            out.append(Move(at, task_of[m[3]], "pr" if m[4].startswith("completed") else "review", True, line, m[3]))
    return out
