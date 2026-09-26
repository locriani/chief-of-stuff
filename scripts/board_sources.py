"""What the board pages read from outside the tracker: the forge and the workers, cached in `<pages dir>/.sources.json`.

A stand-in with the shared contract's names only: `load` returns EMPTY until the fetcher lands and replaces this file.
Every page renders correctly from EMPTY.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Change:
    """A PR or MR."""
    ref: str
    url: str
    title: str
    state: str
    draft: bool
    pipeline: str | None
    approved: bool
    approvals: int
    merged_at: datetime | None
    base: str
    files: tuple[str, ...]
    issues: tuple[str, ...]


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
    kind: str
    runtime: str
    model: str
    task: str
    tree: str
    started: datetime | None
    last: datetime | None


@dataclass(frozen=True)
class Sources:
    fetched: dict[str, datetime]
    issues: dict[str, Issue]
    changes: dict[str, Change]
    workers: tuple[Worker, ...]
    errors: dict[str, str]


EMPTY = Sources({}, {}, {}, (), {})


def load(pages_dir: Path) -> Sources:
    return EMPTY
