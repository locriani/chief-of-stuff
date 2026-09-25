#!/usr/bin/env python3
"""The workspace's chief-of-stuff settings: a TOML file the `## Coordinator` block names with `Settings:`.

Zach, 2026-09-22 22:20, on the day open and close times: "Configurable. We're going to need to pull
settings into a config file. Default to 6 and 22". Notification, lane, budget, and Kanban settings
live here.

No `Settings:` line, no file, or no `[notify]` section is notify off, and nothing else changes. A value
that is there and cannot be read is refused: a guessed day-open time is a notification at the wrong hour.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

ADAPTERS = ("md-notify", "off")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
OFFSET = re.compile(r"^(\d+)([hm])$")
WORKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_WARNINGS = ("24h", "3h", "1h")


class SettingsError(ValueError):
    """A settings file that is there and cannot be read as written."""


def _offset(label: str, where: str = "[notify] deadline_warnings") -> timedelta:
    m = OFFSET.match(label) if isinstance(label, str) else None
    if not m:
        raise SettingsError(f"{where}: {label!r} is not Nh or Nm")
    n = int(m.group(1))
    return timedelta(hours=n) if m.group(2) == "h" else timedelta(minutes=n)


@dataclass(frozen=True)
class Notify:
    # Interim: md-notify is to be replaced by Zach's Todo tooling (Zach, 2026-09-22 22:20).
    adapter: str = "off"
    queue: str = "Areas/notifications/NOTIFICATIONS.md"
    day_open: str = "06:00"
    day_close: str = "22:00"
    warnings: tuple[tuple[str, timedelta], ...] = tuple((w, _offset(w)) for w in DEFAULT_WARNINGS)

    def queue_path(self, root: Path) -> Path:
        return (Path(root) / self.queue).resolve()


@dataclass(frozen=True)
class Lane:
    """A lane is the sequence of stages a task moves through (Zach, 2026-09-23, #33). A gate is a stage only
    the user's word passes."""
    stages: tuple[str, ...]
    gates: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitHubProject:
    owner: str
    number: int
    field: str = "Status"
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Workflow:
    architecture_reviewer: str | None = None


@dataclass(frozen=True)
class Kanban:
    """One visible board stage per tracker stage, with an independent human hold."""
    stages: tuple[str, ...]
    human_review_label: str
    stage_map: dict[str, int]
    hold_stages: tuple[str, ...] = ()
    terminal_stages: tuple[str, ...] = ()
    github_project: GitHubProject | None = None

    def label_for(self, stage: str) -> str | None:
        if stage in self.terminal_stages:
            return None
        return self.stages[self.stage_map[stage]]

    def holds(self, stage: str) -> bool:
        return stage in self.hold_stages


@dataclass(frozen=True)
class Settings:
    notify: Notify = field(default_factory=Notify)
    lanes: dict[str, Lane] = field(default_factory=dict)
    # #33 stage 3: how long a running task of each size goes before the coordinator polls its session. XL has none.
    budgets: dict[str, timedelta] = field(default_factory=dict)
    kanban: Kanban | None = None
    workflow: Workflow = field(default_factory=Workflow)


def _workflow(table) -> Workflow:
    if not isinstance(table, dict):
        raise SettingsError("[workflow] must be a table")
    reviewer = table.get("architecture_reviewer")
    if reviewer is not None and (not isinstance(reviewer, str) or not WORKER_NAME.fullmatch(reviewer)):
        raise SettingsError("[workflow] architecture_reviewer must be a session name")
    return Workflow(architecture_reviewer=reviewer)


def _lane(name: str, table) -> Lane:
    if not isinstance(table, dict):
        raise SettingsError(f"[lanes] {name}: a table, like {{ stages = [\"implement\", \"pr\"] }}")
    stages, gates = table.get("stages"), table.get("gates", [])
    if not (isinstance(stages, list) and stages and all(isinstance(x, str) and x for x in stages)):
        raise SettingsError(f"[lanes] {name}: stages is a non-empty list of names")
    if len(set(stages)) != len(stages):
        raise SettingsError(f"[lanes] {name}: a stage appears twice")
    if not isinstance(gates, list) or not set(gates) <= set(stages):
        raise SettingsError(f"[lanes] {name}: gates must be stages of the lane")
    return Lane(tuple(stages), tuple(gates))


def _kanban(table) -> Kanban:
    if not isinstance(table, dict):
        raise SettingsError("[kanban] must be a table")
    stages = table.get("stages")
    if not isinstance(stages, list) or not stages or any(not isinstance(x, str) or not x.strip() for x in stages):
        raise SettingsError("[kanban] stages must be a non-empty list of labels")
    if len(stages) != len(set(stages)) or any("," in label for label in stages):
        raise SettingsError("[kanban] stages must be unique")
    human = table.get("human_review_label")
    if not isinstance(human, str) or not human.strip() or human in stages or "," in human:
        raise SettingsError("[kanban] human_review_label must be distinct from stage labels")
    mapping = table.get("map")
    if not isinstance(mapping, dict) or not mapping:
        raise SettingsError("[kanban.map] must map tracker stages to label indexes")
    for name, index in mapping.items():
        if not name.strip() or type(index) is not int or not 0 <= index < len(stages):
            raise SettingsError(f"[kanban.map] {name!r} must be a stage index in 0..{len(stages)-1}")
    holds, terminal = table.get("hold_stages", []), table.get("terminal_stages", [])
    if not isinstance(holds, list) or not all(isinstance(x, str) for x in holds) or not set(holds) <= set(mapping):
        raise SettingsError("[kanban] hold_stages must be mapped stages")
    if not isinstance(terminal, list) or not all(isinstance(x, str) and x.strip() for x in terminal) or set(terminal) & set(mapping):
        raise SettingsError("[kanban] terminal_stages must be distinct from mapped stages")
    project = None
    if "github_project" in table:
        github = table["github_project"]
        if not isinstance(github, dict):
            raise SettingsError("[kanban.github_project] must be a table")
        owner, number, field_name = github.get("owner"), github.get("number"), github.get("field", "Status")
        options = github.get("options", stages)
        if not isinstance(owner, str) or not owner.strip() or type(number) is not int or number <= 0:
            raise SettingsError("[kanban.github_project] needs an owner and positive project number")
        if not isinstance(field_name, str) or not field_name.strip():
            raise SettingsError("[kanban.github_project] field must be a name")
        if not isinstance(options, list) or len(options) != len(stages) or any(not isinstance(x, str) or not x.strip() for x in options):
            raise SettingsError("[kanban.github_project] options must match stages in order")
        project = GitHubProject(owner, number, field_name, tuple(options))
    return Kanban(tuple(stages), human, mapping, tuple(holds), tuple(terminal), project)


def _notify(table: dict, root: Path) -> Notify:
    got = Notify(adapter="md-notify")
    adapter = table.get("adapter", got.adapter)
    if adapter not in ADAPTERS:
        raise SettingsError(f"[notify] adapter: {adapter!r} is not one of {', '.join(ADAPTERS)}")
    times = {}
    for key in ("day_open", "day_close"):
        value = table.get(key, getattr(got, key))
        if not isinstance(value, str) or not HHMM.match(value):
            raise SettingsError(f"[notify] {key}: {value!r} is not HH:MM")
        times[key] = value
    labels = table.get("deadline_warnings", list(DEFAULT_WARNINGS))
    if not isinstance(labels, list):
        raise SettingsError("[notify] deadline_warnings: a list, like [\"24h\", \"3h\"]")
    queue = table.get("queue", got.queue)
    if not isinstance(queue, str) or not queue.strip():
        raise SettingsError("[notify] queue: a path under the workspace root")
    resolved = (Path(root) / queue).resolve()
    if not resolved.is_relative_to(Path(root).resolve()):
        raise SettingsError(f"[notify] queue: {queue!r} is outside the workspace root")
    return Notify(adapter=adapter, queue=queue, warnings=tuple((w, _offset(w)) for w in labels), **times)


def load(root: Path, settings_path: str | None) -> Settings:
    """`settings_path` is the `Settings:` line's value, relative to `root`, or None when there is none."""
    if not settings_path:
        return Settings()
    path = Path(root) / settings_path
    if not path.is_file():
        return Settings()
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{settings_path}: {e}") from None
    lanes = data.get("lanes", {})
    if not isinstance(lanes, dict):
        raise SettingsError(f"{settings_path}: [lanes] is not a table")
    lanes = {name: _lane(name, t) for name, t in lanes.items()}
    kanban = _kanban(data["kanban"]) if "kanban" in data else None
    workflow = _workflow(data["workflow"]) if "workflow" in data else Workflow()
    budgets = data.get("budgets", {})
    if not isinstance(budgets, dict) or not set(budgets) <= {"S", "M", "L", "XL"}:
        raise SettingsError(f"{settings_path}: [budgets] is a table of S, M, L, XL")
    budgets = {size: _offset(v, f"[budgets] {size}") for size, v in budgets.items()}
    table = data.get("notify")
    if table is None:
        return Settings(lanes=lanes, budgets=budgets, kanban=kanban, workflow=workflow)
    if not isinstance(table, dict):
        raise SettingsError(f"{settings_path}: [notify] is not a table")
    return Settings(notify=_notify(table, Path(root)), lanes=lanes, budgets=budgets,
                    kanban=kanban, workflow=workflow)
