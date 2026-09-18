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


UNASSIGNED = "unassigned"


def compose(root: Path, day: str | None, lane: str, worktree: Path | None = None) -> str:
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
    owner = rows[0].owner.strip()
    # The rule has always said only an `unassigned` lane may be dispatched; nothing enforced it, and
    # two worktrees were handed the same lane and the same file to edit.
    if owner.lower() != UNASSIGNED:
        raise RefusedError(
            f"lane {rows[0].item.strip()!r} is already {owner}'s, not {UNASSIGNED}; it is not a lane to dispatch")
    # Absolute, because this file is read by a session whose cwd is its own worktree rather than the
    # root the tracker path is written against. The first real spawn went hunting for a relative path
    # that was never reachable from where it stood, and a session that hunts reads things nobody
    # pointed it at.
    lines = [f"Lane: {rows[0].item.strip()}"]
    if worktree is not None:
        # So a session can tell whether the assignment it is reading was addressed to it.
        lines.append(f"Worktree: {worktree}")
    lines += [f"Workspace: {root.resolve()}", f"Tracker: {(root / relative).resolve()}"]
    return "\n".join(lines) + "\n"
