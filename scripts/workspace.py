"""The workspace's `## Coordinator` block in CLAUDE.md: the user, where the trackers, logs and pages are, the zone,
the deadlines and the forge. A script that needs to know where something is reads it here.

Precise deadlines can also be decided in a tracker's `## Decisions` table; `with_decision_deadlines` folds them in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from backlog import Backlog, BacklogError, GitHubBacklog, parse_backlog
from clock import DATE, HHMM
from md import BULLET, cells as _cells, is_separator as _is_separator, section as _section, unmark as _unmark, unquote as _unquote

DECISION_DEADLINE = re.compile(r"^deadline:\s*(.+?)\s+(?:(\d{4}-\d{2}-\d{2})[ T])?(\d{1,2}):(\d{2})$", re.I)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Deadline:
    name: str
    at: datetime
    requirements: str | None = None
    # "all": unnamed tasks fall to it (the default). "named": only tasks whose `due` names it — an exam
    # governs no task on the table, and a derived end toward it would make it look like a plan (finding 73).
    scope: str = "all"


@dataclass(frozen=True)
class Config:
    user: str
    log_dir: str
    tracker_template: str
    tz: str
    deadlines: tuple[Deadline, ...]
    board_tool: str | None
    board_url: str | None
    # The `Board:` line's ``dir `X` ``: where the board is written for pages.py to serve. None writes it
    # beside the tracker.
    pages_dir: str | None = None
    # The parsed `Backlog:` line, GitLab or GitHub. None when there is no line: the issue rule binds only
    # where there is a tracker to back a task with.
    backlog: Backlog | GitHubBacklog | None = None
    # `Settings:` — the workspace's chief-of-stuff.toml, relative to the root (settings.py reads it).
    settings_path: str | None = None
    # An optional workspace identity rule for dispatched workers.
    identity_policy: str | None = None

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def tracker_path(self, day: str) -> str:
        return self.tracker_template.replace("<date>", day)

    def log_path(self, day: str) -> str:
        return f"{self.log_dir.rstrip('/')}/{day}.md"


def _first_path(value: str) -> str | None:
    """The path a line names: its first backtick span, else its first word. What follows is prose."""
    m = re.search(r"`([^`]+)`", value)
    words = value.split()
    return m.group(1).strip() if m else (words[0] if words else None)


def parse_coordinator(text: str, today: date) -> Config:
    body = _section(text, "## Coordinator")
    if not body:
        raise ConfigError("no `## Coordinator` block in CLAUDE.md")
    top: dict[str, str] = {}
    nested: dict[str, list[tuple[str, str]]] = {}
    current = ""
    for line in body:
        m = BULLET.match(line)
        if not m:
            continue
        indent, key, value = len(m.group(1)), _unquote(m.group(2)), m.group(3)
        if indent == 0:
            current = key
            top[key] = value
        else:
            nested.setdefault(current, []).append((key, value))
    for needed in ("User", "Daily log dir", "Tracker", "Timezone"):
        if needed not in top:
            raise ConfigError(f"`## Coordinator` block has no `{needed}:` line")
    tz = _unquote(top["Timezone"])
    zone = ZoneInfo(tz)
    identity_policy = _unquote(top.get("Identity", "")).lower() or None
    if identity_policy not in (None, "user-name-only"):
        raise ConfigError("`Identity:` must be `user-name-only` when present")
    deadlines = []
    for name, value in nested.get("Deadlines", []):
        when, *options = [part.strip() for part in value.split(";")]
        m = DATE.match(_unquote(when))
        if m:
            reqs = next((r.group(1) for o in options if (r := re.match(r"(?i)requirements\s+`?([^`]+?)`?\s*$", o))), None)
            scope = "named" if any(re.match(r"(?i)named tasks only\s*$", o) for o in options) else "all"
            deadlines.append(Deadline(name, datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 23), int(m[5] or 59), tzinfo=zone), reqs, scope))
    tracker = re.search(r"`([^`]+)`", top["Tracker"])
    board_tool = board_url = pages_dir = None
    if "Board" in top:
        parts = [p.strip() for p in top["Board"].split(";")]
        board_tool = _unquote(parts[0]) or None
        for p in parts[1:]:
            m = re.match(r"(?i)url\s+(\S+)", p)
            if m:
                board_url = m.group(1)
            m = re.match(r"(?i)dir\s+`([^`]+)`", p)
            if m:
                pages_dir = m.group(1)
    backlog = None
    if "Backlog" in top:
        try:
            backlog = parse_backlog(top["Backlog"])
        except BacklogError as e:
            raise ConfigError(f"`Backlog:` line: {e}") from None
    return Config(
        user=_unquote(top["User"]),
        log_dir=_unquote(top["Daily log dir"]),
        tracker_template=tracker.group(1) if tracker else _unquote(top["Tracker"]),
        tz=tz,
        deadlines=tuple(sorted(deadlines, key=lambda d: d.at)),
        board_tool=board_tool,
        board_url=board_url,
        pages_dir=pages_dir,
        backlog=backlog,
        settings_path=_first_path(top.get("Settings", "")),
        identity_policy=identity_policy,
    )


def read_config(root: Path) -> Config:
    """`root`'s `## Coordinator` block, read from its CLAUDE.md. Raises OSError or ConfigError."""
    return parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())


def worktrees_dir(claude_md: str) -> str:
    """The `Worktrees:` line of the `## Coordinator` block, or "" when there is none (trees sit at the root)."""
    for line in _section(claude_md, "## Coordinator"):
        m = BULLET.match(line)
        if m and _unquote(m.group(2)).lower() == "worktrees":
            return _unquote(m.group(3))
    return ""



def with_decision_deadlines(cfg: Config, tracker_text: str, tracker_day: date,
                            *, min_day: date | None = None) -> Config:
    """Project precise Decisions-row deadlines into the board's configured deadline list.

    A time without a date belongs to the tracker day, even when an older board is rendered later.
    If a name is decided more than once, the decision recorded at the latest time wins. A decision
    that gives no exact time cannot supply an instant and leaves the existing configuration alone.
    """
    rows = [line for line in _section(tracker_text, "## Decisions") if line.strip().startswith("|")]
    if not rows:
        return cfg
    header = [cell.lower() for cell in _cells(rows[0])]
    if "item" not in header:
        return cfg
    item_col = header.index("item")
    time_col = header.index("time") if "time" in header else None
    base = {deadline.name.casefold(): deadline for deadline in cfg.deadlines}
    decided: dict[str, tuple[time | None, Deadline]] = {}
    for row in rows[1:]:
        cells = _cells(row)
        if _is_separator(cells) or item_col >= len(cells):
            continue
        item, *options = [part.strip() for part in _unmark(cells[item_col]).split(";")]
        match = DECISION_DEADLINE.fullmatch(item)
        if not match:
            continue
        name, day_text, hour_text, minute_text = match.groups()
        name = name.strip(" ,—–-")
        if not name:
            continue
        try:
            day = date.fromisoformat(day_text) if day_text else tracker_day
            at = datetime.combine(day, time(int(hour_text), int(minute_text)), tzinfo=cfg.zone)
        except ValueError:
            continue
        if min_day is not None and at.date() < min_day:
            continue
        key = name.casefold()
        old = base.get(key)
        requirements = next((r.group(1) for option in options
                             if (r := re.fullmatch(r"(?i)requirements\s+`?([^`]+?)`?", option))),
                            old.requirements if old else None)
        scope = ("named" if any(re.fullmatch(r"(?i)named tasks only", option) for option in options)
                 else old.scope if old else "all")
        recorded = None
        if time_col is not None and time_col < len(cells):
            stamp = HHMM.fullmatch(cells[time_col])
            if stamp:
                try:
                    recorded = time(int(stamp[1]), int(stamp[2]))
                except ValueError:
                    pass
        previous = decided.get(key)
        if previous is None or (recorded is not None and (previous[0] is None or recorded > previous[0])):
            decided[key] = (recorded, Deadline(old.name if old else name, at, requirements, scope))
    if not decided:
        return cfg
    base.update({name: deadline for name, (_, deadline) in decided.items()})
    return replace(cfg, deadlines=tuple(sorted(base.values(), key=lambda deadline: deadline.at)))


def daily_trackers(root: Path, cfg: Config) -> list[tuple[date, Path]]:
    """Every day's tracker in the workspace, oldest first."""
    template = cfg.tracker_template
    if template.count("<date>") != 1:
        return []
    prefix, suffix = template.split("<date>")
    found = []
    for path in root.glob(f"{prefix}*{suffix}"):
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            continue
        relative = path.relative_to(root).as_posix()
        if not relative.startswith(prefix) or not relative.endswith(suffix):
            continue
        token = relative[len(prefix):len(relative) - len(suffix) if suffix else None]
        try:
            day = date.fromisoformat(token)
        except ValueError:
            continue
        found.append((day, path))
    return sorted(found)


def with_workspace_decision_deadlines(root: Path, cfg: Config, tracker_text: str, tracker_day: date) -> Config:
    """Carry future deadlines forward from earlier daily trackers, then apply today's decisions."""
    for day, path in daily_trackers(root, cfg):
        if day < tracker_day:
            cfg = with_decision_deadlines(cfg, path.read_text(), day, min_day=tracker_day)
    return with_decision_deadlines(cfg, tracker_text, tracker_day)
