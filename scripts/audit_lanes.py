#!/usr/bin/env python3
"""Audit the tracker's `done` lanes against the trees they were done in.

A lane is `done` when its change is on main. Work sitting on a branch, or uncommitted in a
worktree, is `waiting` however green its suites — so this reads Lanes and File ownership, asks git
about each tree once, and prints the rows that have to be reopened.

    python3 audit_lanes.py --date 2026-09-17

Exit code is the number of rows to reopen. Lanes with no tree — decisions, relays, deletions,
console actions — are never reopened: there is nothing to merge.

Worktrees are resolved under the workspace root and anything that escapes is refused, because the
paths come from a file the agent can write.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_board import BULLET, ConfigError, _cells, _is_separator, _section, _unquote, parse_coordinator, parse_tracker, short_name  # noqa: E402

WORKTREE = re.compile(r"\bworktrees?\s+`?([A-Za-z0-9._\-/]+)`?")
# "its worktree only", "that worktree instead": prose says the word without naming a tree.
NOT_A_NAME = {"only", "its", "it", "the", "that", "this", "those", "a", "an", "and", "in", "at", "for", "of", "is", "was", "with", "instead", "too", "here", "there"}
REF = re.compile(r"\s*\[[0-9a-f]{4,}\]\s*$")
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
GIT_TIMEOUT = 15
DONE = "done"


@dataclass(frozen=True)
class OwnerRow:
    context: str
    worktrees: list[str]


@dataclass(frozen=True)
class Reopen:
    lane: str
    owner: str
    worktree: str
    why: str

    def __str__(self) -> str:
        return f"reopen: {short_name(self.lane)} — {self.worktree}: {self.why}"


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    reopen: list[Reopen] = field(default_factory=list)


def worktrees_dir(claude_md: str) -> str:
    """The `Worktrees:` line of the `## Coordinator` block, or "" when there is none (trees sit at the root)."""
    for line in _section(claude_md, "## Coordinator"):
        m = BULLET.match(line)
        if m and _unquote(m.group(2)).lower() == "worktrees":
            return _unquote(m.group(3))
    return ""


def _worktrees(cell: str) -> list[str]:
    names = []
    for m in WORKTREE.finditer(cell):
        name = m.group(1).rstrip("/.,;:")
        if name and name.lower() not in NOT_A_NAME and name not in names:
            names.append(name)
    return names


def parse_ownership(text: str) -> list[OwnerRow]:
    """File ownership is prose, written as a table or as bullets; both name a context and its paths."""
    rows: list[OwnerRow] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")) and ":" in stripped:
            context, _, paths = stripped[2:].partition(":")
            rows.append(OwnerRow(context.strip(), _worktrees(paths)))
            continue
        if not stripped.startswith("|"):
            continue
        cells = _cells(line)
        if _is_separator(cells) or len(cells) < 2:
            continue
        if cells[0].lower() == "context":
            continue
        rows.append(OwnerRow(cells[0], _worktrees(cells[1])))
    return rows


def _bare(name: str) -> str:
    """`demo-fixes [b8fca1] (fix 5, from 11:13)` → `demo-fixes`."""
    head = name.split("(")[0].strip()
    return REF.sub("", head).strip().lower()


def owns(row: OwnerRow, owner: str) -> bool:
    return bool(owner.strip()) and _bare(row.context) == _bare(owner)


def git(args: list[str], cwd: Path) -> tuple[int, str]:
    """One read-only git call. Never a shell, always a list, always bounded."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            env={**GIT_ENV, "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return out.returncode, (out.stdout or out.stderr).strip()


def _resolve(root: Path, trees: str, name: str) -> tuple[Path | None, str]:
    """A worktree path inside the workspace root, or None and the reason it was refused."""
    base = (root / trees).resolve() if trees else root.resolve()
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None, f"{name} resolves outside the workspace root, refused"
    if not (candidate / ".git").exists():
        return None, f"{name}: no worktree at {candidate} — the tracker names a tree that is not there"
    return candidate, ""


def _state(worktree: Path) -> tuple[str, str]:
    """(branch, why it is not done), where the why is empty when the work is on main and committed."""
    _, branch = git(["rev-parse", "--abbrev-ref", "HEAD"], worktree)
    code, dirty = git(["status", "--porcelain"], worktree)
    reasons = []
    if code == 0 and dirty:
        reasons.append(f"{len(dirty.splitlines())} uncommitted file(s)")
    merged, _ = git(["merge-base", "--is-ancestor", "HEAD", "main"], worktree)
    if merged != 0:
        reasons.append("not on main")
    return branch, "; ".join(reasons)


def _push_gap(worktree: Path) -> str:
    """Local main against origin/main: a second gap, reported but never a lane state."""
    code, counts = git(["rev-list", "--left-right", "--count", "main...origin/main"], worktree)
    if code != 0 or not counts:
        return "origin/main unknown"
    ahead, behind = (counts.split() + ["0", "0"])[:2]
    if ahead == "0" and behind == "0":
        return "main even with origin/main"
    parts = []
    if ahead != "0":
        parts.append(f"{ahead} unpushed")
    if behind != "0":
        parts.append(f"{behind} behind")
    return "main " + ", ".join(parts) + " vs origin/main"


def audit(root: Path, day: str) -> Report:
    claude_md = (root / "CLAUDE.md").read_text()
    cfg = parse_coordinator(claude_md, today=date.today())
    trees = worktrees_dir(claude_md)
    tracker_text = (root / cfg.tracker_path(day)).read_text()
    lanes = parse_tracker(tracker_text).lanes
    owners = parse_ownership("\n".join(_section(tracker_text, "## File ownership")))

    report = Report()
    seen: set[Path] = set()
    refused: set[str] = set()
    for lane in lanes:
        names = [w for row in owners if owns(row, lane.owner) or owns(row, lane.item) for w in row.worktrees]
        for name in names:
            path, why_refused = _resolve(root, trees, name)
            if path is None:
                if name not in refused:
                    refused.add(name)
                    report.lines.append(why_refused)
                continue
            branch, why = _state(path)
            if path not in seen:
                seen.add(path)
                report.lines.append(f"{name} ({branch}): {why or 'on main, committed'}")
            if lane.kind == DONE and why:
                report.reopen.append(Reopen(lane.item, lane.owner, f"{name} ({branch})", why))
    for path in sorted(seen):
        report.lines.append(_push_gap(path))
        break  # one clone behind every worktree; the gap is a property of the repo, not the tree
    report.lines.extend(str(r) for r in report.reopen)
    report.lines.append(f"lanes={len(lanes)} trees={len(seen)} reopen={len(report.reopen)}")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not (root / "CLAUDE.md").is_file():
        print(f"audit_lanes: no CLAUDE.md at {root}", file=sys.stderr)
        return 2
    try:
        cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())
    except ConfigError as e:
        print(f"audit_lanes: {e}", file=sys.stderr)
        return 2
    day = args.date or datetime.now(cfg.zone).date().isoformat()
    tracker = root / cfg.tracker_path(day)
    if not tracker.is_file():
        print(f"audit_lanes: tracker not found at {tracker}", file=sys.stderr)
        return 2
    report = audit(root, day)
    for line in report.lines:
        print(line)
    return len(report.reopen)


if __name__ == "__main__":
    raise SystemExit(main())
