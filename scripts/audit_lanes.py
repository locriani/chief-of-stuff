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
from render_board import BULLET, ConfigError, _cells, _is_separator, _section, _unmark, _unquote, parse_coordinator, parse_tracker, short_name  # noqa: E402
from dispatch_prompt import PROMPT_DIR, STOP_FILE  # noqa: E402

# The branch is written in the parenthetical right after the tree name — `worktree wt-x (feat/y)` —
# which is the only place it survives when the tree itself is gone. Finding 46 turns on that.
WORKTREE = re.compile(r"\bworktrees?\s+`?([A-Za-z0-9._\-/]+)`?(?:\s*\(([^)]*)\))?")
BRANCHISH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,60}")
# Prose that shows up inside a branch parenthetical and is never a ref.
NOT_A_BRANCH = {"now", "was", "then", "renamed", "from", "to", "branch", "on", "off", "and", "or", "merged", "clean", "dirty", "at"}
# "its worktree only", "that worktree instead": prose says the word without naming a tree.
NOT_A_NAME = {"only", "its", "it", "the", "that", "this", "those", "a", "an", "and", "in", "at", "for", "of", "is", "was", "with", "instead", "too", "here", "there"}
REF = re.compile(r"\s*\[[0-9a-f]{4,}\]\s*$")
PAREN = re.compile(r"\((.*?)\)")
TOKEN = re.compile(r"[A-Za-z0-9]+")
# Words that say nothing about which lane a row is about.
STOP = NOT_A_NAME | {"done", "from", "to", "on", "by", "worktree", "worktrees", "off", "main", "new"}
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
GIT_TIMEOUT = 15
DONE = "done"


@dataclass(frozen=True)
class OwnerRow:
    context: str
    worktrees: list[str]
    branches: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Reopen:
    lane: str
    owner: str
    worktree: str
    why: str

    def __str__(self) -> str:
        return f"reopen: {short_name(self.lane)} — {self.worktree}: {self.why}"


@dataclass(frozen=True)
class Orphan:
    """Finding 47: work in a tree whose writer is not in `## Sessions`. A fact about owners, not trees."""

    worktree: str
    owner: str
    why: str

    def __str__(self) -> str:
        return f"orphaned work: {self.worktree} — {self.why}, and its owner {self.owner!r} is not in ## Sessions"


@dataclass(frozen=True)
class Stop:
    """A session put this lane down and said so in its tree. The lane never heard.

    A refusal and a session quietly dying are the same silence from the coordinator's side, which is
    how an hour of finished work sat at `running 02:20` against a session that had stopped. The stop
    is a file because a message is only as good as somebody reading it, and this walk happens anyway.
    """

    lane: str
    kind: str
    lands_on: str
    worktree: str
    state: str

    def __str__(self) -> str:
        who = f", lands on {self.lands_on}" if self.lands_on else ", lands on nobody it named"
        return f"stopped: {short_name(self.lane)} — {self.worktree}: {self.kind or 'kind unstated'}{who}, and the lane still reads {self.state!r}"


@dataclass(frozen=True)
class QueueFault:
    """A decision queue that cannot be worked through one row at a time.

    Zach, 2026-09-19 03:26: one at a time, in order, with the context to decide. A queue that repeats
    a number, keeps rows it has already answered, or carries a row nobody can act on is a list that
    looks like progress — which is the thing that was banned, in a new place.
    """

    row: str
    why: str

    def __str__(self) -> str:
        return f"decision queue: {self.row} — {self.why}"


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    reopen: list[Reopen] = field(default_factory=list)
    orphans: list[Orphan] = field(default_factory=list)
    stopped: list[Stop] = field(default_factory=list)
    queue: list[QueueFault] = field(default_factory=list)


def worktrees_dir(claude_md: str) -> str:
    """The `Worktrees:` line of the `## Coordinator` block, or "" when there is none (trees sit at the root)."""
    for line in _section(claude_md, "## Coordinator"):
        m = BULLET.match(line)
        if m and _unquote(m.group(2)).lower() == "worktrees":
            return _unquote(m.group(3))
    return ""


def _worktrees(cell: str) -> tuple[list[str], dict[str, str]]:
    names: list[str] = []
    branches: dict[str, str] = {}
    for m in WORKTREE.finditer(cell):
        name = m.group(1).rstrip("/.,;:")
        if not name or name.lower() in NOT_A_NAME or name in names:
            continue
        names.append(name)
        if m.group(2):
            branches[name] = m.group(2)
    return names, branches


def branch_candidates(name: str, detail: str) -> list[str]:
    """Every ref the ownership row could be naming, best first, then the tree name itself.

    A live parenthetical reads `(now docs/local-toolchain, was fix/sample-data-stack-detect)`, so this
    guesses widely and cheaply and lets `git rev-parse` be the one that decides.
    """
    out = [t for t in BRANCHISH.findall(detail or "") if t.lower() not in NOT_A_BRANCH]
    if name not in out:
        out.append(name)
    return out[:6]


def parse_ownership(text: str) -> list[OwnerRow]:
    """File ownership is prose, written as a table or as bullets; both name a context and its paths."""
    rows: list[OwnerRow] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")) and ":" in stripped:
            context, _, paths = stripped[2:].partition(":")
            rows.append(OwnerRow(context.strip(), *_worktrees(paths)))
            continue
        if not stripped.startswith("|"):
            continue
        cells = _cells(line)
        if _is_separator(cells) or len(cells) < 2:
            continue
        if cells[0].lower() == "context":
            continue
        rows.append(OwnerRow(cells[0], *_worktrees(cells[1])))
    return rows


def _bare(name: str) -> str:
    """`demo-fixes [b8fca1] (fix 5, from 11:13)` → `demo-fixes`."""
    head = name.split("(")[0].strip()
    return REF.sub("", head).strip().lower()


def owns(row: OwnerRow, owner: str) -> bool:
    return bool(owner.strip()) and _bare(row.context) == _bare(owner)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN.findall(text) if len(t) > 1 and not t.isdigit() and t.lower() not in STOP}


def _detail(context: str) -> str:
    """The parenthetical of a File ownership row: with one context to several lanes, it is the only thing saying which."""
    return " ".join(PAREN.findall(context))


def rows_for(item: str, owner: str, owners: list[OwnerRow]) -> tuple[list[OwnerRow], str]:
    """The rows that speak for this lane, and a note when nothing does.

    A context owns several lanes at once — `architecture` held both a deletion and a doc edit — so
    the bare name cannot attribute a tree. A row keyed by the lane item is exact and wins. Otherwise
    the owner's rows are scored on how much of the lane item their parenthetical repeats; a lone best
    row wins, and a tie or a blank draws nothing, because attributing the wrong tree reopens a lane
    that is genuinely finished.
    """
    exact = [r for r in owners if owns(r, item)]
    if exact:
        return exact, ""
    mine = [r for r in owners if owns(r, owner)]
    if len(mine) <= 1:
        return mine, ""
    want = _tokens(item)
    scored = [(len(want & _tokens(_detail(r.context))), r) for r in mine]
    best = max(score for score, _ in scored)
    top = [r for score, r in scored if score == best]
    if best == 0 or len(top) > 1:
        return [], f"ambiguous: {short_name(item)} — {len(mine)} File ownership rows for {_bare(owner)} and none names it; no tree attributed"
    return top, ""


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


def _resolve(root: Path, trees: str, name: str) -> tuple[Path | None, str, str]:
    """A worktree path inside the workspace root, or None with why: `outside` or `missing`."""
    base = (root / trees).resolve() if trees else root.resolve()
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None, "outside", f"{name} resolves outside the workspace root, refused"
    if not (candidate / ".git").exists():
        return None, "missing", f"{name}: no worktree at {candidate} — the tracker names a tree that is not there"
    return candidate, "ok", ""


def _handle(root: Path, trees: str, seen: list[Path]) -> Path | None:
    """Somewhere to ask git about a branch whose tree is gone: any tree still on disk."""
    if seen:
        return seen[0]
    base = (root / trees) if trees else root
    for child in sorted(base.glob("*")) if base.is_dir() else []:
        if (child / ".git").exists():
            return child
    return root if (root / ".git").exists() else None


def missing_tree(handle: Path | None, name: str, detail: str) -> str:
    """Finding 46. A missing tree means two opposite things and the script printed one line for both.

    A branch that is an ancestor of main is a session that tidied up after itself. A branch that is
    not is work that exists nowhere else. A name with no branch behind it never existed: the row
    carried a planned name and the tree was made under a different one.
    """
    if handle is None:
        return f"{name}: no worktree, and no tree left on disk to ask git in"
    for cand in branch_candidates(name, detail):
        if git(["rev-parse", "--verify", "--quiet", f"refs/heads/{cand}"], handle)[0] != 0:
            continue
        if git(["merge-base", "--is-ancestor", cand, "main"], handle)[0] == 0:
            return f"{name}: no worktree — branch {cand} is on main; merged and cleaned up"
        return f"{name}: no worktree — branch {cand} is not on main, so the work is only on that branch"
    return f"{name}: no worktree and no branch by that name — the row names a tree that was never created"


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
    """Local main against origin/main, and the commit main is at.

    House rule 5 (CIMP): the suite has to have been run on main after the merge, and no script can
    watch that happen. Naming main's commit is what makes the claim checkable rather than
    unfalsifiable — a `Verified` line pinned to a sha that is no longer main's is stale evidence.
    """
    _, sha = git(["rev-parse", "--short", "HEAD"], worktree)
    at = f"main {sha} " if sha and " " not in sha else "main "
    code, counts = git(["rev-list", "--left-right", "--count", "main...origin/main"], worktree)
    if code != 0 or not counts:
        return f"{at}vs origin/main unknown"
    ahead, behind = (counts.split() + ["0", "0"])[:2]
    if ahead == "0" and behind == "0":
        return f"{at}even with origin/main"
    parts = []
    if ahead != "0":
        parts.append(f"{ahead} unpushed")
    if behind != "0":
        parts.append(f"{behind} behind")
    return at + ", ".join(parts) + " vs origin/main"


# `Stop: permission`, `Lands on: Zach` — the assignment's own labelled lines, read back.
STOP_FIELD = re.compile(r"(?im)^\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.*?)\s*$")
# The two states a stopped lane is allowed to be in. Anything else means nobody moved it.
SETTLED = ("open", "waiting")


def read_stop(worktree: Path) -> dict[str, str] | None:
    """The stop a session left in its own tree, as its fields. None when there is none to read."""
    try:
        text = (worktree / STOP_FILE).read_text()
    except OSError:
        return None
    return {m.group(1).strip().lower(): m.group(2).strip() for m in STOP_FIELD.finditer(text)}


# `~~1~~` and `**ANSWERED 03:29**`: the two ways the live queue marked a row it had already settled
# while leaving it in the table.
ANSWERED = re.compile(r"(?i)\bANSWERED\b|^~~.*~~$")
DASH = {"", "-", "—", "–", "n/a", "none"}


def queue_faults(tracker_text: str) -> tuple[list[QueueFault], list[str], int]:
    """The decision queue read as a queue: unique numbers in order, answered rows gone, every row decidable."""
    rows = [line for line in _section(tracker_text, "## Decision queue") if line.strip().startswith("|")]
    if not rows:
        return [], [], 0
    faults: list[QueueFault] = []
    lines: list[str] = []
    seen: dict[str, str] = {}
    open_rows: list[tuple[str, str]] = []
    for row in rows[1:]:
        cells = _cells(row)
        if _is_separator(cells) or len(cells) < 2 or cells[0].strip().lower() == "#":
            continue
        num, decision = cells[0].strip(), cells[1].strip()
        if ANSWERED.search(decision) or ANSWERED.search(num):
            faults.append(QueueFault(f"row {num}", "answered and still in the table; an answered row moves to ## Decisions"))
            continue
        if num in seen:
            faults.append(QueueFault(f"row {num}", f"carries the number {num} twice, so there is no next one"))
        seen[num] = decision
        why, blocked = (cells[2].strip() if len(cells) > 2 else ""), (cells[3].strip() if len(cells) > 3 else "")
        if why.lower() in DASH and blocked.lower() in DASH:
            faults.append(QueueFault(f"row {num}", "names neither why it is next nor who is blocked, so it cannot be decided from the row"))
        open_rows.append((num, decision))
    if open_rows:
        num, decision = open_rows[0]
        # A decision cell opens with a bold headline, and `short_name` cutting inside the mark pair
        # returns a bare ellipsis: the line that says which one is next has to name it.
        lines.append(f"next decision: {num} — {short_name(_unmark(decision))}")
    return faults, lines, len(open_rows)


def audit(root: Path, day: str) -> Report:
    claude_md = (root / "CLAUDE.md").read_text()
    cfg = parse_coordinator(claude_md, today=date.today())
    trees = worktrees_dir(claude_md)
    tracker_text = (root / cfg.tracker_path(day)).read_text()
    tracker = parse_tracker(tracker_text)
    lanes = tracker.lanes
    owners = parse_ownership("\n".join(_section(tracker_text, "## File ownership")))
    # The roster, for the join. An empty `## Sessions` is not a roster of nobody: with nothing to
    # join against, the script says nothing about owners rather than calling every tree orphaned.
    roster = {_bare(s.name) for s in tracker.sessions if s.name.strip()}
    if roster:
        # The user is not a session and never was; they are exempt from the join, not absent from it.
        roster.add(_bare(cfg.user))

    report = Report()
    handled: set[str] = set()
    seen: set[Path] = set()
    order: list[Path] = []
    refused: set[str] = set()
    noted: set[str] = set()
    orphaned: set[str] = set()
    stopped_at: set[str] = set()
    gone: dict[str, str] = {}
    def visit(row: OwnerRow, name: str, lane) -> None:
        """One tree, once: its git state, any lane it reopens, and whether its writer is still here."""
        path, kind, message = _resolve(root, trees, name)
        if path is None:
            if kind == "missing":
                gone.setdefault(name, row.branches.get(name, ""))
            elif name not in refused:
                refused.add(name)
                report.lines.append(message)
            return
        branch, why = _state(path)
        if name not in handled:
            handled.add(name)
            seen.add(path)
            order.append(path)
            report.lines.append(f"{name} ({branch}): {why or 'on main, committed'}")
        if lane is not None and lane.kind == DONE and why:
            report.reopen.append(Reopen(lane.item, lane.owner, f"{name} ({branch})", why))
        # Finding 47: the audit reports per tree, and this is a fact about owners. Only uncommitted
        # work counts — an unmerged branch is recoverable by name, a dirty tree nobody is writing in
        # is not.
        stop = read_stop(path)
        if stop is not None and lane is not None and lane.kind not in SETTLED and name not in stopped_at:
            stopped_at.add(name)
            report.stopped.append(Stop(lane.item, stop.get("stop", ""), stop.get("lands on", ""),
                                       f"{name} ({branch})", lane.state.strip()))
        owner = row.context.strip() or (lane.owner.strip() if lane is not None else "")
        if roster and "uncommitted" in why and _bare(owner) not in roster and name not in orphaned:
            orphaned.add(name)
            report.orphans.append(Orphan(f"{name} ({branch})", _bare(owner), why))

    for lane in lanes:
        rows, note = rows_for(lane.item, lane.owner, owners)
        if note and note not in noted:
            noted.add(note)
            report.lines.append(note)
        for row in rows:
            for name in row.worktrees:
                visit(row, name, lane)

    # Every remaining File ownership row that names a tree. A tree reached only through a lane is a
    # tree nobody can reach once the lane lets go of it — and the three orphaned trees this join was
    # written for are keyed `nobody`, which no lane owner ever matches.
    for row in owners:
        for name in row.worktrees:
            visit(row, name, None)
    handle = _handle(root, trees, order)
    for name, detail in gone.items():
        report.lines.append(missing_tree(handle, name, detail))
    if order:
        # One clone behind every worktree; the gap is a property of the repo, not the tree.
        report.lines.append(_push_gap(order[0]))
    report.lines.extend(str(r) for r in report.reopen)
    report.lines.extend(str(o) for o in report.orphans)
    report.lines.extend(str(s) for s in report.stopped)
    faults, queue_lines, queued = queue_faults(tracker_text)
    report.queue.extend(faults)
    report.lines.extend(queue_lines)
    report.lines.extend(str(q) for q in report.queue)
    report.lines.append(
        f"lanes={len(lanes)} trees={len(seen)} reopen={len(report.reopen)} orphaned={len(report.orphans)} "
        f"stopped={len(report.stopped)}" + (f" queued={queued}" if queue_lines or faults or queued else ""))
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
    return len(report.reopen) + len(report.stopped) + len(report.queue)


if __name__ == "__main__":
    raise SystemExit(main())
