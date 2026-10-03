"""What the forge's cached sources say about the tracker's tasks: the changes naming a task's issue, the stage a
merged change has moved it to, whether the issue's labels drift from that stage, and when its Flow row ends. Beside
them, who the workers are and whether a stage holds. The board and the task page read these; nothing here draws.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import board_sources
import kanban as kanban_tool
from settings import Kanban
from tracker import Task, issue_key


def held(task: Task, kanban: Kanban | None, stage: str | None = None) -> bool:
    return kanban is not None and kanban.holds(task.stage.strip() if stage is None else stage)


def worker_names(sources: board_sources.Sources) -> set[str]:
    return {w.name for w in sources.workers}


def task_changes(task: Task, sources: board_sources.Sources) -> tuple[board_sources.Change, ...]:
    """The changes naming the task's issue: the open ones, or else the merged ones."""
    # ponytail: keyed by `#N` alone, so two projects' #N collide; key by host and project when a board spans them.
    key = issue_key(task.issue) if task.issue.strip() else None
    named = [c for c in sources.changes.values() if key and key in c.issues and c.state != "closed"]
    return tuple(c for c in named if c.state == "open") or tuple(named)


def effective_stage(task: Task, sources: board_sources.Sources, lanes: dict) -> str:
    """A task's real stage for drawing, drift and hold: the tracker's own cell, unless its change already
    merged and the tracker hasn't caught up — then the lane's last stage. The forge already answered the only
    question a stale `merge` cell was tracking; a stalled coordinator can otherwise leave it stuck for hours (#240)."""
    stage = task.stage.strip()
    changes = task_changes(task, sources)
    lane = lanes.get(task.lane.strip())
    if changes and changes[0].state == "merged" and lane and lane.stages:
        return lane.stages[-1]
    return stage


def forge_ends(tasks: list[Task], sources: board_sources.Sources, zone: ZoneInfo) -> dict[str, tuple[datetime, str]]:
    """Each task's name to what ends its Flow row, in `zone`: its change's latest forge merge time and "merged", or,
    failing that, its closed issue's close time and "closed". A merge wins. The board and the task page both end a
    row here (#200, #220)."""
    out = {}
    for t in tasks:
        name = t.name.strip()
        if not name:
            continue
        if merges := [c.merged_at for c in task_changes(t, sources) if c.state == "merged" and c.merged_at]:
            out[name] = (max(merges).astimezone(zone), "merged")
        elif (issue := sources.issues.get(issue_key(t.issue)) if t.issue.strip() else None) and issue.state == "closed" and issue.closed_at:
            out[name] = (issue.closed_at.astimezone(zone), "closed")
    return out


def drifts(task: Task, kanban: Kanban | None, sources: board_sources.Sources, stage: str | None = None) -> bool:
    """The forge's labels disagree with the task's stage, by the same check the audit runs (kanban.drift).
    `stage` overrides the tracker's own cell — pass `effective_stage()` so a merged change's card is judged at
    the stage it now draws in, not the one it's stuck reading (#240)."""
    issue = sources.issues.get(issue_key(task.issue)) if task.issue.strip() else None
    stage = task.stage.strip() if stage is None else stage
    # ponytail: labels only; a GitHub Project's Status is not in the sources, so a project board never drifts here.
    if kanban is None or kanban.github_project or issue is None or not stage or task.kind == "done":
        return False
    return bool(kanban_tool.drift(kanban, issue.labels, stage, allow_extra_hold=task.kind == "waiting"))
