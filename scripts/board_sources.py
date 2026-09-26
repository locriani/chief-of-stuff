"""What the board reads besides the tracker: forge issues and changes, and the workers.

Stub of the shared contract; the sources PR fills in `load` and `refresh`. Every consumer renders with `EMPTY`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Change:  # a PR or MR
    ref: str  # "!58" (GitLab) or "#58" (GitHub PR)
    url: str
    title: str
    state: str  # "open" | "merged" | "closed"
    draft: bool
    pipeline: str | None  # "passed" | "failed" | "running" | "pending" | None
    approved: bool
    approvals: int
    merged_at: datetime | None
    base: str
    files: tuple[str, ...]
    issues: tuple[str, ...]  # issue refs it closes or names, e.g. ("#111",)


@dataclass(frozen=True)
class Issue:
    ref: str
    url: str
    title: str
    state: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Worker:
    name: str
    kind: str  # "one-shot" | "session"
    runtime: str
    model: str
    task: str
    tree: str
    started: datetime | None
    last: datetime | None


@dataclass(frozen=True)
class Sources:
    fetched: dict[str, datetime]  # keys: "kanban", "merge requests", "workers"
    issues: dict[str, Issue]  # by ref
    changes: dict[str, Change]  # by ref
    workers: tuple[Worker, ...]
    errors: dict[str, str]  # source -> why it could not be read; the header shows it


EMPTY = Sources({}, {}, {}, (), {})


def load(pages_dir: Path) -> Sources:
    """The cache at `<pages dir>/.sources.json`; EMPTY when absent or unreadable."""
    return EMPTY


def refresh(root: Path, now: datetime) -> Sources:
    """Fetch everything, write the cache, return it."""
    return EMPTY
