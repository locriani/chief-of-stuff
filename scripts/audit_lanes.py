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
from render_board import BULLET, HHMM, RAN, ConfigError, _cells, _is_separator, _section, _unmark, _unquote, clip_name, parse_coordinator, parse_tracker  # noqa: E402
from dispatch_prompt import PROMPT_DIR, STOP_FILE  # noqa: E402
from backlog import CLOSED, BacklogError, GitHubBacklog, issue_ref, issue_states  # noqa: E402

# The branch is written in the parenthetical right after the tree name — `worktree wt-x (feat/y)` —
# which is the only place it survives when the tree itself is gone. Finding 46 turns on that.
WORKTREE = re.compile(r"\bworktrees?\s+`?([A-Za-z0-9._\-/]+)`?(?:\s*\(([^)]*)\))?")
BRANCHISH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,60}")
# Prose that shows up inside a branch parenthetical and is never a ref.
NOT_A_BRANCH = {"now", "was", "then", "renamed", "from", "to", "branch", "on", "off", "and", "or", "merged", "clean", "dirty", "at"}
# "its worktree only", "that worktree instead": prose says the word without naming a tree.
NOT_A_NAME = {"only", "its", "it", "the", "that", "this", "those", "a", "an", "and", "in", "at", "for", "of", "is", "was", "with", "instead", "too", "here", "there"}
REF = re.compile(r"\s*\[[0-9a-f]{4,}\]\s*$")
# The same ref, anywhere in the cell rather than at the end of it: `coordinator (`gauntlet-b2`
# [028827])` carries it inside the parenthetical, and that cell is a context like any other.
ANY_REF = re.compile(r"\[([0-9a-f]{4,})\]")
# The commit a `done` state may name. Git's own shape, not a guess: abbreviated to seven or written
# in full, and nothing else is read as one.
SHA = re.compile(r"^[0-9a-f]{7,40}$")
LANDED, NOT_LANDED, UNKNOWN_SHA = "landed", "not-landed", "unknown"
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
        return f"reopen: {clip_name(self.lane)} — {self.worktree}: {self.why}"


@dataclass(frozen=True)
class Orphan:
    """Finding 47: work in a tree whose writer is not in `## Sessions`. A fact about owners, not trees."""

    worktree: str
    owner: str
    why: str
    ref: str = ""

    def __str__(self) -> str:
        who = f"{self.owner!r} [{self.ref}]" if self.ref else f"{self.owner!r}"
        how = "names a ref no ## Sessions row carries" if self.ref else "is not in ## Sessions"
        return f"orphaned work: {self.worktree} — {self.why}, and its owner {who} {how}"


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
        kind = self.kind or "kind unstated"
        if kind == UNREADABLE:
            kind = "a stop file that is unreadable"
        return f"stopped: {clip_name(self.lane)} — {self.worktree}: {kind}{who}, and the lane still reads {self.state!r}"


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


@dataclass(frozen=True)
class IssueFault:
    """A task the issue tracker does not back, or a done task whose issue is still open.

    Zach, 2026-09-22 22:20: "each entry in the task tracker is actually backed by an entry in github".
    """

    task: str
    why: str

    def __str__(self) -> str:
        return f"issue: {self.task} — {self.why}"


def issue_faults(lanes, repo: str, gh=None) -> list[IssueFault]:
    """One `gh issue list --state all` per repo the tracker names, then each task against it. A read that
    fails is one `unknown` finding: an issue nobody could look up is not an issue known to be open."""
    refs = {}
    faults: list[IssueFault] = []
    for lane in lanes:
        if lane.standing:
            continue
        cell = lane.issue.strip()
        ref = issue_ref(cell, repo) if cell else None
        name = clip_name(lane.label)
        if not cell:
            if lane.needs_issue:
                faults.append(IssueFault(name, "no issue; file it with gh-issue new"))
        elif not ref:
            faults.append(IssueFault(name, f'"{cell}" is not an issue reference'))
        else:
            refs[lane] = ref
    states: dict[str, dict] = {}
    for want in sorted({r.repo for r in refs.values()}):
        try:
            states[want] = issue_states(GitHubBacklog(repo=want), gh=gh)
        except BacklogError as e:
            return faults + [IssueFault("unknown", str(e))]
    for lane, ref in refs.items():
        name, found, tag = clip_name(lane.label), states[ref.repo].get(ref.number), ref.label(repo)
        if lane.needs_issue and found is None:
            faults.append(IssueFault(name, f"{tag} not found in {ref.repo}"))
        elif lane.needs_issue and found.state == CLOSED:
            faults.append(IssueFault(name, f"{tag} is closed"))
        elif lane.kind == "done" and found is not None and found.state != CLOSED:
            faults.append(IssueFault(name, f"done but {tag} is open; backlog.py --close {ref.number} --commit"))
    return faults


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    reopen: list[Reopen] = field(default_factory=list)
    orphans: list[Orphan] = field(default_factory=list)
    stopped: list[Stop] = field(default_factory=list)
    queue: list[QueueFault] = field(default_factory=list)
    issues: list[IssueFault] = field(default_factory=list)


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
    """`**demo-fixes** [b8fca1] (fix 5, from 11:13)` → `demo-fixes`. Both sides of the join strip the
    same things, and emphasis is one of them: a coordinator bolds the name it most wants read, which
    is the name a join most needs to match."""
    head = _unmark(name.split("(")[0]).strip()
    return REF.sub("", head).strip().lower()


def _ref(cell: str) -> str:
    """The `[768194]` a context cell carries, or nothing."""
    m = ANY_REF.search(cell)
    return m.group(1).lower() if m else ""


def listed(owner: str, roster: set[str], refs: set[str], user: str = "") -> tuple[bool, str]:
    """Is this context still in `## Sessions`, and if not, what did the asking turn on?

    A ref is minted once and survives every rename; a name is a tab title, and here they change —
    `768194`'s was rewritten at 03:21 on Zach's word that the worktree is the name, and the join,
    reading names, called a live session's tree orphaned. So where the cell carries a ref and the
    roster has refs to compare it against, the ref decides and the name is decoration. A ref the
    roster does not carry is a real absence even under a familiar name: the context that took the
    paths is gone whatever now wears its label.
    """
    bare = _bare(owner)
    if user and bare == _bare(user):
        return True, ""
    ref = _ref(owner) if refs else ""
    if ref:
        return ref in refs, "" if ref in refs else ref
    return bare in roster, ""


def owns(row: OwnerRow, owner: str) -> bool:
    """Same context? The ref decides where both sides carry one — finding 112's rule, which reaches
    here too and was left out of it. This tracker runs one ref under three names: `arch [768194]`,
    `architecture-review-setup [768194]` and `openemr-arch [768194]`, four of whose lanes matched no
    ownership row and so were attributed no tree, and a lane with no tree is checked for nothing at
    all. The name decides only where one side or the other has no ref to ask."""
    if not owner.strip():
        return False
    theirs, mine = _ref(row.context), _ref(owner)
    if theirs and mine:
        return theirs == mine
    return _bare(row.context) == _bare(owner)


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
        return [], f"ambiguous: {clip_name(item)} — {len(mine)} File ownership rows for {_bare(owner)} and none names it; no tree attributed"
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


def group_reopens(reopens: list[Reopen]) -> list[str]:
    """One line per fact, not per lane. Finding 116(c).

    `visit()` asks git once per tree — "One tree, once" — and then fans that single answer across
    every lane the tree owns, so a session that closes six lanes in one worktree and holds its merges
    to a cadence produces six identical lines. Thirteen lines carried three facts on the live tracker
    at 06:46, and the ratio is lines per tree, so it degraded as sessions worked well in a stable
    place.

    This changes the rendering and nothing else: the finding list is untouched, the exit code is
    untouched, and every lane still appears by name on the line for its tree. It answers "how many
    things are true", where a per-lane sha would answer "which lane failed to merge" — different
    questions, and this one needs no new written field to answer.
    """
    order: list[tuple[str, str]] = []
    lanes: dict[tuple[str, str], list[str]] = {}
    for r in reopens:
        key = (r.worktree, r.why)
        if key not in lanes:
            order.append(key)
            lanes[key] = []
        lanes[key].append(clip_name(_unmark(r.lane)))
    out = []
    for worktree, why in order:
        names = lanes[(worktree, why)]
        head = f"reopen: {worktree}: {why} — {len(names)} done lane{'' if len(names) == 1 else 's'}"
        out.append(f"{head}: {', '.join(names)}")
    return out


def lane_sha(state: str) -> str:
    """The token a `done` state carries beyond its clock: `done 21:16\u201322:05 54b7eb3` \u2192 `54b7eb3`.

    Returned sha or not — whether it is a commit id is `landed`'s question, because a lane that wrote
    something unreadable there has still tried to cite evidence and the difference between that and
    citing none is worth a line.
    """
    parts = state.split()
    if not parts or parts[0].lower() != DONE:
        return ""
    rest = [p for p in parts[1:] if not HHMM.match(p) and not RAN.match(p)]
    return rest[0] if rest else ""


def landed(sha: str, worktree: Path) -> tuple[str, str]:
    """Did the change this lane claimed actually reach main? Finding 116(a). Returns a verdict and,
    where the citation itself is the problem, the complaint to print.

    Three-valued, and only `LANDED` may clear a lane. `reopen` asks whether the owner's *tree* is on
    main and `done` claims that *this lane's change* did; a session working continuously in one
    worktree always has unmerged work, so every lane it ever closed reopened — six of arch's did on
    the live tracker, all six already on main. Asking about the named commit is asking the question
    the state actually makes.

    Every other outcome is `UNKNOWN_SHA`, which means today's behaviour exactly: the tree decides,
    and a lane reopens if the tree gives a reason. No sha, an unreadable one, one no repository can
    resolve, git failing or timing out \u2014 none of them is proof, and none of them clears anything. That
    is the whole safety argument. The instrument's failures today are false *reopens*: noisy,
    self-clearing, visible. A false *clear* is silent and permanent, so it is reachable only from a
    `merge-base` that answered yes.

    `main`, never `HEAD` \u2014 finding 109: this runs inside a worktree whose HEAD is its own branch.
    """
    if not sha:
        return UNKNOWN_SHA, ""
    if not SHA.match(sha.lower()):
        return UNKNOWN_SHA, f"{sha!r} is not a commit id"
    if git(["cat-file", "-e", f"{sha}^{{commit}}"], worktree)[0] != 0:
        return UNKNOWN_SHA, f"no commit {sha} here, so the citation cannot be checked"
    code, _ = git(["merge-base", "--is-ancestor", sha, "main"], worktree)
    return (LANDED if code == 0 else NOT_LANDED), ""


def _push_gap(worktree: Path) -> str:
    """Local main against origin/main, and the commit main is at.

    House rule 5 (pull requests): the suite has to have been run on main after the merge, and no script can
    watch that happen. Naming main's commit is what makes the claim checkable rather than
    unfalsifiable — a `Verified` line pinned to a sha that is no longer main's is stale evidence.
    """
    # `main`, not `HEAD`: this runs in whichever worktree the scan reached first, and a worktree's
    # HEAD is its own branch. The count below always resolved the ref correctly, so a right number
    # vouched for a wrong name — the failure mode a reader cannot catch, on the one line the post-merge
    # gate rests on.
    code, sha = git(["rev-parse", "--short", "main"], worktree)
    at = f"main {sha} " if code == 0 and sha and " " not in sha else "main "
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


# The same guard `dispatch_prompt._clean` applies on the way in, applied on the way out: this file is
# written by a session and printed into somebody's terminal, so an ANSI or OSC payload in it would be
# a payload in the report. C0 without tab or newline, DEL, and C1.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
FIELD_CAP = 200
UNREADABLE = "unreadable"


def _field(value: str) -> str:
    """One field off the stop, made safe to print: no escapes, and short enough not to bury the report."""
    clean = CONTROL.sub("", value).strip()
    return clean if len(clean) <= FIELD_CAP else clean[:FIELD_CAP] + "…"


def read_stop(worktree: Path) -> dict[str, str] | None:
    """The stop a session left in its own tree, as its fields. None only when there is no stop at all.

    A stop that exists and cannot be read is still a stop. Returning None for it would fail open on
    exactly the state this exists to surface, so the unreadable case reports itself as one.
    """
    path = worktree / STOP_FILE
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return {"stop": UNREADABLE} if path.exists() else None
    return {m.group(1).strip().lower(): _field(m.group(2)) for m in STOP_FIELD.finditer(text)}


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
        # A decision cell opens with a bold headline. `clip_name` handles the mark pair itself since
        # the short_name/clip_name split, so the `_unmark` here is no longer load-bearing for that —
        # it stays because it also strips backticks, and a name is read here in a terminal line.
        lines.append(f"next decision: {num} — {clip_name(_unmark(decision))}")
    return faults, lines, len(open_rows)


def audit(root: Path, day: str, gh=None, check_issues: bool = True) -> Report:
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
    # The ref column, which is the join proper wherever both sides have one. Empty is not a roster of
    # nobody here either: a tracker whose refs are blank falls back to the names it does have.
    refs = {_ref(s.ref) or s.ref.strip().lower() for s in tracker.sessions if s.ref.strip()}
    if roster:
        # The user is not a session and never was; they are exempt from the join, not absent from it.
        roster.add(_bare(cfg.user))

    report = Report()
    handled: set[str] = set()
    seen: set[Path] = set()
    order: list[Path] = []
    refused: set[str] = set()
    noted: set[str] = set()
    sha_said: set[str] = set()
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
        if lane is not None and lane.kind == DONE:
            sha = lane_sha(lane.state)
            verdict, complaint = landed(sha, path)
            if complaint and complaint not in sha_said:
                sha_said.add(complaint)
                report.lines.append(f"sha: {clip_name(_unmark(lane.item))} \u2014 {complaint}; read as it was before")
            if verdict == NOT_LANDED:
                report.reopen.append(Reopen(lane.item, lane.owner, f"{name} ({branch})", f"its sha {sha} is not on main"))
            elif verdict == UNKNOWN_SHA and why:
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
        here, missing = listed(owner, roster, refs, cfg.user) if roster else (True, "")
        if roster and "uncommitted" in why and not here and name not in orphaned:
            orphaned.add(name)
            report.orphans.append(Orphan(f"{name} ({branch})", _bare(owner), why, missing))

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
    report.lines.extend(group_reopens(report.reopen))
    report.lines.extend(str(o) for o in report.orphans)
    report.lines.extend(str(s) for s in report.stopped)
    faults, queue_lines, queued = queue_faults(tracker_text)
    report.queue.extend(faults)
    report.lines.extend(queue_lines)
    report.lines.extend(str(q) for q in report.queue)
    issues = ""
    if cfg.backlog_repo:
        if check_issues:
            report.issues.extend(issue_faults(lanes, cfg.backlog_repo, gh))
            report.lines.extend(str(f) for f in report.issues)
            issues = f" issues={len(report.issues)}"
        else:
            issues = " issues=off"
    facts = len({(r.worktree, r.why) for r in report.reopen})
    reopen = f"{facts} tree{'' if facts == 1 else 's'}/{len(report.reopen)} lane{'' if len(report.reopen) == 1 else 's'}" if report.reopen else "0"
    report.lines.append(
        f"lanes={len(lanes)} trees={len(seen)} reopen={reopen} orphaned={len(report.orphans)} "
        f"stopped={len(report.stopped)}" + (f" queued={queued}" if queue_lines or faults or queued else "") + issues)
    return report


def main(argv: list[str] | None = None, gh=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--no-issues", action="store_true", help="skip the issue check (offline); the summary says issues=off")
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
    report = audit(root, day, gh=gh, check_issues=not args.no_issues)
    for line in report.lines:
        print(line)
    return len(report.reopen) + len(report.stopped) + len(report.queue) + len(report.issues)


if __name__ == "__main__":
    raise SystemExit(main())
