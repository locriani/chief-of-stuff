#!/usr/bin/env python3
"""Compose the assignment a spawned session wakes up holding, from the tracker and nothing else.

The prompt is derived, never authored. A coordinator that wants to tell a worker something writes it
into the tracker first, where the user reads it before saying yes — so what was approved and what
the worker receives are the same text from the same place.

That is also what bans shell expansion. The six lines never travel as prose through a command line,
where a `$(...)` would be expanded by the shell before any script existed to check it. The only
variable argument is the lane name, and a name that is not a row in the tracker is refused, so an
expansion that got this far produces a refusal instead of a payload.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_board import ConfigError, parse_coordinator, parse_tracker  # noqa: E402


class RefusedError(ValueError):
    """Something did not survive its check against the tracker. Nothing was composed."""


def _config(root: Path):
    try:
        return parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())
    except (OSError, ConfigError) as exc:
        raise RefusedError(f"cannot read the ## Coordinator block: {exc}") from None


def compose(root: Path, day: str | None, lane: str) -> str:
    """The assignment for `lane`, read back off disk. A lane that is not a row is refused."""
    cfg = _config(root)
    relative = cfg.tracker_path(day or datetime.now(cfg.zone).date().isoformat())
    try:
        text = (root / relative).read_text()
    except OSError as exc:
        raise RefusedError(f"cannot read {relative}: {exc}") from None
    wanted = lane.strip()
    rows = [row for row in parse_tracker(text).lanes if row.item.strip() == wanted]
    if not wanted or not rows:
        raise RefusedError(f"no Lanes row {lane!r} in {relative}; a dispatch names a lane that is already there")
    return f"Lane: {rows[0].item.strip()}\nTracker: {relative}\n"
