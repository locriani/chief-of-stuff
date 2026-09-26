#!/usr/bin/env python3
"""Audit task completion against worktree and issue state.

The exit code counts tasks to reopen. Tasks without worktrees are excluded. Worktree paths must
remain inside the workspace root.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_board import BULLET, HHMM, RAN, ConfigError, _dur, _cells, _is_separator, _section, _unmark, _unquote, clip_name, parse_coordinator, parse_tracker  # noqa: E402
from dispatch_prompt import PROMPT_DIR, STOP_FILE  # noqa: E402
from backlog import CLOSED, GITHUB, Backlog, BacklogError, GitHubBacklog, file_with, home_of, issue_ref, issue_states  # noqa: E402
from settings import Kanban, SettingsError, load as load_settings  # noqa: E402
import kanban as kanban_tool  # noqa: E402
import ownership  # noqa: E402

# Preserve branch names from ownership rows when worktrees disappear.
WORKTREE = re.compile(r"\bworktrees?\s+(?P<tick>`)?(?P<name>[A-Za-z0-9._\-/]+)`?(?:\s*\((?P<branch>[^)]*)\))?")
BRANCHISH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,60}")
# Exclude prose from branch and worktree matches.
NOT_A_BRANCH = {"now", "was", "then", "renamed", "from", "to", "branch", "on", "off", "and", "or", "merged", "clean", "dirty", "at"}
NOT_A_NAME = {"only", "its", "it", "the", "that", "this", "those", "a", "an", "and", "in", "at", "for", "of", "is", "was", "with", "instead", "too", "here", "there"}
TREE_SHAPED = re.compile(r"[-/_.0-9]")
REF = re.compile(r"\s*\[[0-9a-f]{4,}\]\s*$")
# Context refs can appear anywhere in a cell.
ANY_REF = re.compile(r"\[([0-9a-f]{4,})\]")
# Accept abbreviated or full Git hashes in done states.
SHA = re.compile(r"^[0-9a-f]{7,40}$")
LANDED, NOT_LANDED, UNKNOWN_SHA = "landed", "not-landed", "unknown"
PAREN = re.compile(r"\((.*?)\)")
TOKEN = re.compile(r"[A-Za-z0-9]+")
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
    task: str
    owner: str
    worktree: str
    why: str

    def __str__(self) -> str:
        return f"reopen: {clip_name(self.task)} — {self.worktree}: {self.why}"


@dataclass(frozen=True)
class Orphan:
    """Uncommitted work whose owner is absent from Sessions."""

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
    """Stop file from a worker whose task still appears active."""

    task: str
    kind: str
    lands_on: str
    worktree: str
    state: str

    def __str__(self) -> str:
        who = f", lands on {self.lands_on}" if self.lands_on else ", lands on nobody it named"
        kind = self.kind or "kind unstated"
        if kind == UNREADABLE:
            kind = "a stop file that is unreadable"
        return f"stopped: {clip_name(self.task)} — {self.worktree}: {kind}{who}, and the task still reads {self.state!r}"


@dataclass(frozen=True)
class QueueFault:
    """Duplicate, answered, or undecidable queue row."""

    row: str
    why: str

    def __str__(self) -> str:
        return f"decision queue: {self.row} — {self.why}"


@dataclass(frozen=True)
class IssueFault:
    """Missing, invalid, or incorrectly open issue."""

    task: str
    why: str

    def __str__(self) -> str:
        return f"issue: {self.task} — {self.why}"


def issue_faults(tasks, home: Backlog | GitHubBacklog, gh=None) -> list[IssueFault]:
    """Compare task issues with one issue-list request per repository."""
    refs = {}
    faults: list[IssueFault] = []
    for task in tasks:
        if task.standing:
            continue
        cell = task.issue.strip()
        ref = issue_ref(cell, home) if cell else None
        name = clip_name(task.label)
        if not cell:
            if task.needs_issue:
                faults.append(IssueFault(name, f"no issue; file it with {file_with(home)}"))
        elif not ref:
            faults.append(IssueFault(name, f'"{cell}" is not an issue reference'))
        else:
            refs[task] = ref
    states: dict[tuple[str, str], dict] = {}
    for want in sorted({(r.host, r.repo) for r in refs.values()}):
        # issue_ref admits a GitLab URL only on the Backlog's own host, so its token never leaves that host.
        cfg = (home if want == home_of(home) else GitHubBacklog(repo=want[1]) if want[0] == GITHUB
               else replace(home, project=want[1]))
        try:
            states[want] = issue_states(cfg, gh=gh)
        except BacklogError as e:
            return faults + [IssueFault("unknown", str(e))]
    for task, ref in refs.items():
        name, found, tag = clip_name(task.label), states[(ref.host, ref.repo)].get(ref.number), ref.label(home)
        if task.needs_issue and found is None:
            faults.append(IssueFault(name, f"{tag} not found in {ref.repo}"))
        elif task.needs_issue and found.state == CLOSED:
            faults.append(IssueFault(name, f"{tag} is closed"))
        elif task.kind == "done" and found is not None and found.state != CLOSED:
            faults.append(IssueFault(name, f"done but {tag} is open; backlog.py --close {ref.number} --commit"))
    return faults


@dataclass(frozen=True)
class LaneFault:
    """A task whose lane the settings file does not name, or whose stage is not one of its lane's (#33)."""

    task: str
    why: str

    def __str__(self) -> str:
        return f"lane: {self.task} — {self.why}"


def lane_faults(tasks, lanes: dict, settings_path: str | None) -> list[LaneFault]:
    faults: list[LaneFault] = []
    for task in tasks:
        if not task.lane.strip():
            continue
        name, stage, lane = clip_name(task.label), task.stage.strip(), lanes.get(task.lane.strip())
        if lane is None:
            faults.append(LaneFault(name, f"lane {task.lane.strip()} is not in {settings_path or 'the settings file'}"))
        elif not stage:
            faults.append(LaneFault(name, f"lane {task.lane.strip()} but no stage"))
        elif stage not in lane.stages:
            faults.append(LaneFault(name, f"stage {stage} is not a stage of {task.lane.strip()}"))
    return faults


@dataclass(frozen=True)
class KanbanFault:
    task: str
    why: str

    def __str__(self) -> str:
        return f"kanban: {self.task} — {self.why}"


def kanban_faults(tasks, home: Backlog | GitHubBacklog, config: Kanban, gh=None) -> list[KanbanFault]:
    """Report label or Project Status drift; never move a card during an audit."""
    selected = []
    for task in tasks:
        if task.standing or task.kind == "done" or not task.lane.strip() or not task.issue.strip():
            continue
        ref = issue_ref(task.issue, home)
        if ref is not None:
            selected.append((task, ref))
    states = {}
    for host, repo in sorted({(ref.host, ref.repo) for _, ref in selected}):
        cfg = (home if (host, repo) == home_of(home) else GitHubBacklog(repo)
               if host == GITHUB else replace(home, project=repo))
        try:
            states[(host, repo)] = issue_states(cfg, gh=gh)
        except BacklogError as exc:
            return [KanbanFault("unknown", str(exc))]
    faults = []
    project_items = None
    project_error = ""
    for task, ref in selected:
        found = states[(ref.host, ref.repo)].get(ref.number)
        if found is None:
            continue  # issue_faults already reports a missing issue
        status = None
        if ref.host == GITHUB and config.github_project:
            if project_items is None:
                project_items, project_error = kanban_tool._project_items(
                    gh or kanban_tool.backlog.run_gh, config.github_project)
            if project_error:
                faults.append(KanbanFault(clip_name(task.label), project_error))
                continue
            if ref.url not in project_items:
                faults.append(KanbanFault(clip_name(task.label), "issue is absent from the configured GitHub Project"))
                continue
            status = project_items[ref.url]
        issue_config = config if ref.host == GITHUB else replace(config, github_project=None)
        why = kanban_tool.drift(issue_config, found.labels, task.stage.strip(), project_status=status,
                                allow_extra_hold=task.kind == "waiting")
        if why:
            faults.append(KanbanFault(clip_name(task.label), why))
    return faults


@dataclass(frozen=True)
class OverBudget:
    """A running task past its size's budget (#33 stage 3). The coordinator polls its session and tells the user;
    it never stops the session."""

    task: str
    ran: timedelta
    size: str
    budget: timedelta

    def __str__(self) -> str:
        return f"over budget: {self.task} running {_dur(self.ran)} ({self.size} {_dur(self.budget)})"


def over_budget(tasks, budgets: dict, day: str, now: datetime) -> list[OverBudget]:
    over: list[OverBudget] = []
    for task in tasks:
        budget, start = budgets.get(task.size.strip()), task.state_time
        if task.kind != "running" or budget is None or not start:
            continue
        ran = now - datetime.combine(date.fromisoformat(day), datetime.strptime(start, "%H:%M").time(), tzinfo=now.tzinfo)
        if ran > budget:
            over.append(OverBudget(clip_name(task.label), ran, task.size.strip(), budget))
    return over


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    reopen: list[Reopen] = field(default_factory=list)
    orphans: list[Orphan] = field(default_factory=list)
    stopped: list[Stop] = field(default_factory=list)
    queue: list[QueueFault] = field(default_factory=list)
    issues: list[IssueFault] = field(default_factory=list)
    lanes: list[LaneFault] = field(default_factory=list)
    kanban: list[KanbanFault] = field(default_factory=list)
    over: list[OverBudget] = field(default_factory=list)


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
        name = m["name"].rstrip("/.,;:")
        # A tree name is backticked or shaped like one; a bare word after "worktree" is prose (#72).
        if not name or name in names or not (m["tick"] or TREE_SHAPED.search(name)):
            continue
        names.append(name)
        if m["branch"]:
            branches[name] = m["branch"]
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
    return [OwnerRow(row.context, *_worktrees(row.paths)) for row in ownership.parse(text)]


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
    `architecture-review-setup [768194]` and `openemr-arch [768194]`, four of whose tasks matched no
    ownership row and so were attributed no tree, and a task with no tree is checked for nothing at
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
    """The parenthetical of a File ownership row: with one context to several tasks, it is the only thing saying which."""
    return " ".join(PAREN.findall(context))


def rows_for(item: str, owner: str, owners: list[OwnerRow]) -> tuple[list[OwnerRow], str]:
    """The rows that speak for this task, and a note when nothing does.

    A context owns several tasks at once — `architecture` held both a deletion and a doc edit — so
    the bare name cannot attribute a tree. A row keyed by the task item is exact and wins. Otherwise
    the owner's rows are scored on how much of the task item their parenthetical repeats; a lone best
    row wins, and a tie or a blank draws nothing, because attributing the wrong tree reopens a task
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
    """One line per fact, not per task. Finding 116(c).

    `visit()` asks git once per tree — "One tree, once" — and then fans that single answer across
    every task the tree owns, so a session that closes six tasks in one worktree and holds its merges
    to a cadence produces six identical lines. Thirteen lines carried three facts on the live tracker
    at 06:46, and the ratio is lines per tree, so it degraded as sessions worked well in a stable
    place.

    This changes the rendering and nothing else: the finding list is untouched, the exit code is
    untouched, and every task still appears by name on the line for its tree. It answers "how many
    things are true", where a per-task sha would answer "which task failed to merge" — different
    questions, and this one needs no new written field to answer.
    """
    order: list[tuple[str, str]] = []
    tasks: dict[tuple[str, str], list[str]] = {}
    for r in reopens:
        key = (r.worktree, r.why)
        if key not in tasks:
            order.append(key)
            tasks[key] = []
        tasks[key].append(clip_name(_unmark(r.task)))
    out = []
    for worktree, why in order:
        names = tasks[(worktree, why)]
        head = f"reopen: {worktree}: {why} — {len(names)} done task{'' if len(names) == 1 else 's'}"
        out.append(f"{head}: {', '.join(names)}")
    return out


def task_sha(state: str) -> str:
    """The token a `done` state carries beyond its clock: `done 21:16\u201322:05 54b7eb3` \u2192 `54b7eb3`.

    Returned sha or not — whether it is a commit id is `landed`'s question, because a task that wrote
    something unreadable there has still tried to cite evidence and the difference between that and
    citing none is worth a line.
    """
    parts = state.split()
    if not parts or parts[0].lower() != DONE:
        return ""
    rest = [p for p in parts[1:] if not HHMM.match(p) and not RAN.match(p)]
    return rest[0] if rest else ""


def landed(sha: str, worktree: Path) -> tuple[str, str]:
    """Did the change this task claimed actually reach main? Finding 116(a). Returns a verdict and,
    where the citation itself is the problem, the complaint to print.

    Three-valued, and only `LANDED` may clear a task. `reopen` asks whether the owner's *tree* is on
    main and `done` claims that *this task's change* did; a session working continuously in one
    worktree always has unmerged work, so every task it ever closed reopened — six of arch's did on
    the live tracker, all six already on main. Asking about the named commit is asking the question
    the state actually makes.

    Every other outcome is `UNKNOWN_SHA`, which means today's behaviour exactly: the tree decides,
    and a task reopens if the tree gives a reason. No sha, an unreadable one, one no repository can
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
    """Report main's commit and its distance from origin/main for verification."""
    # Use main, not this worktree's HEAD.
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


# Read labeled stop-file fields.
STOP_FIELD = re.compile(r"(?im)^\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.*?)\s*$")
# Open and waiting tasks need no stop finding.
SETTLED = ("open", "waiting")


# Strip terminal control sequences from worker-written stop files.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
FIELD_CAP = 200
UNREADABLE = "unreadable"


def _field(value: str) -> str:
    """Sanitize and truncate a stop-file field."""
    clean = CONTROL.sub("", value).strip()
    return clean if len(clean) <= FIELD_CAP else clean[:FIELD_CAP] + "…"


def read_stop(worktree: Path) -> dict[str, str] | None:
    """Read a worker stop file; report unreadable files rather than ignoring them."""
    path = worktree / STOP_FILE
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return {"stop": UNREADABLE} if path.exists() else None
    return {m.group(1).strip().lower(): _field(m.group(2)) for m in STOP_FIELD.finditer(text)}


# Recognize both answered-row formats.
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
        # Strip Markdown before printing the decision name.
        lines.append(f"next decision: {num} — {clip_name(_unmark(decision))}")
    return faults, lines, len(open_rows)


def audit(root: Path, day: str, gh=None, check_issues: bool = True, now: datetime | None = None) -> Report:
    claude_md = (root / "CLAUDE.md").read_text()
    cfg = parse_coordinator(claude_md, today=date.today())
    trees = worktrees_dir(claude_md)
    tracker_text = (root / cfg.tracker_path(day)).read_text()
    tracker = parse_tracker(tracker_text)
    tasks = tracker.tasks
    owners = parse_ownership("\n".join(_section(tracker_text, "## File ownership")))
    # An empty Sessions table cannot prove that workers are absent.
    roster = {_bare(s.name) for s in tracker.sessions if s.name.strip()}
    # Match refs when available, then fall back to names.
    refs = {_ref(s.ref) or s.ref.strip().lower() for s in tracker.sessions if s.ref.strip()}
    if roster:
        # The workspace user may own a task without a session.
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
    def visit(row: OwnerRow, name: str, task) -> None:
        """One tree, once: its git state, any task it reopens, and whether its writer is still here."""
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
        if task is not None and task.kind == DONE:
            sha = task_sha(task.state)
            verdict, complaint = landed(sha, path)
            if complaint and complaint not in sha_said:
                sha_said.add(complaint)
                report.lines.append(f"sha: {clip_name(_unmark(task.item))} \u2014 {complaint}; read as it was before")
            if verdict == NOT_LANDED:
                report.reopen.append(Reopen(task.item, task.owner, f"{name} ({branch})", f"its sha {sha} is not on main"))
            elif verdict == UNKNOWN_SHA and why:
                report.reopen.append(Reopen(task.item, task.owner, f"{name} ({branch})", why))
        # Only dirty worktrees with absent owners count as orphans.
        stop = read_stop(path)
        if stop is not None and task is not None and task.kind not in SETTLED and name not in stopped_at:
            stopped_at.add(name)
            report.stopped.append(Stop(task.item, stop.get("stop", ""), stop.get("lands on", ""),
                                       f"{name} ({branch})", task.state.strip()))
        owner = row.context.strip() or (task.owner.strip() if task is not None else "")
        here, missing = listed(owner, roster, refs, cfg.user) if roster else (True, "")
        if roster and "uncommitted" in why and not here and name not in orphaned:
            orphaned.add(name)
            report.orphans.append(Orphan(f"{name} ({branch})", _bare(owner), why, missing))

    for task in tasks:
        rows, note = rows_for(task.item, task.owner, owners)
        if note and note not in noted:
            noted.add(note)
            report.lines.append(note)
        for row in rows:
            for name in row.worktrees:
                visit(row, name, task)

    # Visit ownership rows even when no current task names their worktree.
    for row in owners:
        for name in row.worktrees:
            visit(row, name, None)
    handle = _handle(root, trees, order)
    for name, detail in gone.items():
        report.lines.append(missing_tree(handle, name, detail))
    if order:
        # All worktrees share the same main and origin/main refs.
        report.lines.append(_push_gap(order[0]))
    report.lines.extend(group_reopens(report.reopen))
    report.lines.extend(str(o) for o in report.orphans)
    report.lines.extend(str(s) for s in report.stopped)
    faults, queue_lines, queued = queue_faults(tracker_text)
    report.queue.extend(faults)
    report.lines.extend(queue_lines)
    report.lines.extend(str(q) for q in report.queue)
    issues = ""
    if cfg.backlog:
        if check_issues:
            report.issues.extend(issue_faults(tasks, cfg.backlog, gh))
            report.lines.extend(str(f) for f in report.issues)
            issues = f" issues={len(report.issues)}"
        else:
            issues = " issues=off"
    settings = load_settings(root, cfg.settings_path)
    report.lanes.extend(lane_faults(tasks, settings.lanes, cfg.settings_path))
    if settings.kanban and cfg.backlog and check_issues:
        report.kanban.extend(kanban_faults(tasks, cfg.backlog, settings.kanban, gh=gh))
        report.lines.extend(str(f) for f in report.kanban)
    report.over.extend(over_budget(tasks, settings.budgets, day, now or datetime.now(cfg.zone)))
    report.lines.extend(str(o) for o in report.over)
    budgeted = f" over={len(report.over)}" if settings.budgets else ""
    report.lines.extend(str(f) for f in report.lanes)
    laned = f" lanes={len(report.lanes)}" if any(t.lane.strip() for t in tasks) else ""
    kanban = f" kanban={len(report.kanban)}" if settings.kanban and check_issues else ""
    facts = len({(r.worktree, r.why) for r in report.reopen})
    reopen = f"{facts} tree{'' if facts == 1 else 's'}/{len(report.reopen)} task{'' if len(report.reopen) == 1 else 's'}" if report.reopen else "0"
    report.lines.append(
        f"tasks={len(tasks)} trees={len(seen)} reopen={reopen} orphaned={len(report.orphans)} "
        f"stopped={len(report.stopped)}" + (f" queued={queued}" if queue_lines or faults or queued else "") + issues + laned + kanban + budgeted)
    return report


def main(argv: list[str] | None = None, gh=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--no-issues", action="store_true", help="skip the issue check (offline); the summary says issues=off")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not (root / "CLAUDE.md").is_file():
        print(f"audit_tasks: no CLAUDE.md at {root}", file=sys.stderr)
        return 2
    try:
        cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())
    except ConfigError as e:
        print(f"audit_tasks: {e}", file=sys.stderr)
        return 2
    day = args.date or datetime.now(cfg.zone).date().isoformat()
    tracker = root / cfg.tracker_path(day)
    if not tracker.is_file():
        print(f"audit_tasks: tracker not found at {tracker}", file=sys.stderr)
        return 2
    try:
        report = audit(root, day, gh=gh, check_issues=not args.no_issues)
    except SettingsError as e:
        print(f"audit_tasks: {e}", file=sys.stderr)
        return 2
    for line in report.lines:
        print(line)
    return len(report.reopen) + len(report.stopped) + len(report.queue) + len(report.issues) + len(report.lanes) + len(report.kanban)


if __name__ == "__main__":
    raise SystemExit(main())
