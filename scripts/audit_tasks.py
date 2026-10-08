#!/usr/bin/env python3
"""Audit task completion against worktree and issue state.

The exit code counts tasks to reopen. Tasks without worktrees are excluded. Worktree paths must
remain inside the workspace root.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clock import HHMM, RAN, dur as _dur, hhmm as _hhmm  # noqa: E402
from findings import ISSUE, KANBAN, LANE, MERGED, NEXT, OVER, QUEUE, REOPEN, STOPPED  # noqa: E402
from md import cells as _cells, is_separator as _is_separator, section as _section, unmark as _unmark  # noqa: E402
from tracker import clip_name, parse_tracker  # noqa: E402
from workspace import ConfigError, parse_coordinator, read_config, worktrees_dir  # noqa: E402
from dispatch_prompt import PROMPT_DIR, STOP_FILE  # noqa: E402
from backlog import CLOSED, GITHUB, Backlog, BacklogError, GitHubBacklog, file_with, home_of, issue_ref, issue_states  # noqa: E402
import backlog  # noqa: E402
from settings import Kanban, SettingsError, load as load_settings  # noqa: E402
import board_sources  # noqa: E402
import kanban as kanban_tool  # noqa: E402
import ownership  # noqa: E402
import git_trees  # noqa: E402
from orphans import Claim, Orphan, Unclaimed, judge  # noqa: E402
from tree_claims import Claims, one_shot_claims, registry_claims  # noqa: E402

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
SHA = re.compile(r"^[0-9a-f]{7,64}$")
LANDED, NOT_LANDED, UNKNOWN_SHA = "landed", "not-landed", "unknown"
PAREN = re.compile(r"\((.*?)\)")
TOKEN = re.compile(r"[A-Za-z0-9]+")
STOP = NOT_A_NAME | {"done", "from", "to", "on", "by", "worktree", "worktrees", "off", "main", "new"}
DONE = "done"
# After a change's ref, the row records its merge: `!58 merged`, `PR #58 (merged)`.
ACKNOWLEDGED = r"(?i:\W+merged\b)"


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
        return f"{REOPEN} {clip_name(self.task)} — {self.worktree}: {self.why}"


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
        return f"{STOPPED} {clip_name(self.task)} — {self.worktree}: {kind}{who}, and the task still reads {self.state!r}"


@dataclass(frozen=True)
class QueueFault:
    """Duplicate, answered, or undecidable queue row."""

    row: str
    why: str

    def __str__(self) -> str:
        return f"{QUEUE} {self.row} — {self.why}"


@dataclass(frozen=True)
class IssueFault:
    """Missing, invalid, or incorrectly open issue."""

    task: str
    why: str

    def __str__(self) -> str:
        return f"{ISSUE} {self.task} — {self.why}"


def _closed_hhmm(closed_at: str, zone) -> str:
    """`closed_at`, an ISO timestamp (`gh`/GitLab's UTC, "Z" and all), read as HH:MM in the workspace
    zone. Empty when the forge gave no time — never a false clock."""
    if not closed_at or zone is None:
        return ""
    try:
        return datetime.fromisoformat(closed_at).astimezone(zone).strftime("%H:%M")
    except ValueError:
        return ""


def _tree_unlanded(root: Path | None, trees: str, owners: list, task, roster=(), refs=()) -> bool:
    """Does this task's File-ownership worktree carry a commit `main` never got? Reuses `_state`'s own
    merge-base check — the same signal `reopen` already reads off a done task's tree (#221). No
    resolvable tree (none owns the task, or it is missing) answers False: nothing is there to unland."""
    if root is None:
        return False
    rows, _ = rows_for(task.item, task.owner, owners or [], key_name(task, roster, refs))
    for row in rows:
        for name in row.worktrees:
            path, _, _ = _resolve(root, trees, name)
            if path is not None and "not on main" in _state(path)[1]:
                return True
    return False


def _config_for(home: Backlog | GitHubBacklog, host: str, repo: str) -> Backlog | GitHubBacklog:
    """The config that reads `repo` on `host`: the home itself, a GitHub repo, or another project on the home's
    GitLab host. The home is checked first. one_shot checks GitHub first; the two orders part only for a GitLab
    `Backlog:` line whose host is github.com."""
    # issue_ref admits a GitLab URL only on the Backlog's own host, so its token never leaves that host.
    return (home if (host, repo) == home_of(home) else GitHubBacklog(repo)
            if host == GITHUB else replace(home, project=repo))


def issue_faults(tasks, home: Backlog | GitHubBacklog, gh=None, lanes: dict | None = None,
                 root: Path | None = None, trees: str = "", owners: list | None = None,
                 zone=None, roster=(), session_refs=()) -> list[IssueFault]:
    """Compare task issues with one issue-list request per repository."""
    refs = {}
    faults: list[IssueFault] = []
    for task in tasks:
        if task.standing or task.workflow:
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
        cfg = _config_for(home, *want)
        try:
            states[want] = issue_states(cfg, gh=gh)
        except BacklogError as e:
            return faults + [IssueFault("unknown", str(e))]
    for task, ref in refs.items():
        name, found, tag = clip_name(task.label), states[(ref.host, ref.repo)].get(ref.number), ref.label(home)
        if task.needs_issue and found is None:
            faults.append(IssueFault(name, f"{tag} not found in {ref.repo}"))
        elif task.needs_issue and found.state == CLOSED:
            if _tree_unlanded(root, trees, owners, task, roster, session_refs):
                faults.append(IssueFault(
                    name, f"{tag} is closed but its tree has work not on main; ask the user: "
                    "reopen the issue or drop the work"))
            else:
                when = _closed_hhmm(found.closed_at, zone)
                at = f" {when}" if when else ""
                faults.append(IssueFault(name, f"{tag} is closed{at}; write the task done{f' at {when}' if when else ''}"))
        elif task.kind == "done" and found is not None and found.state != CLOSED and _ends_issue(task, lanes or {}):
            faults.append(IssueFault(name, f"done but {tag} is open; chief-of-stuff backlog --close {ref.number} --commit"))
    return faults


def _ends_issue(task, lanes: dict) -> bool:
    """A laned row closes its issue only at the lane's last stage; a review done mid-lane leaves it open."""
    lane = lanes.get(task.lane.strip())
    return lane is None or task.stage.strip() == lane.stages[-1]


@dataclass(frozen=True)
class MergedFault:
    """A task not done whose pull or merge request has merged (#45)."""

    task: str
    why: str

    def __str__(self) -> str:
        return f"{MERGED} {self.task} — {self.why}"


def _named(task, home: Backlog | GitHubBacklog) -> set[int]:
    """The changes a live task's row names and does not record as merged. A task is live when it is not
    standing, not done, and its issue is not in a repo other than the Backlog's, where `PR #12` is another #12."""
    ref = issue_ref(task.issue, home) if task.issue.strip() else None
    elsewhere = ref is not None and (ref.host.lower(), ref.repo.lower()) != tuple(part.lower() for part in home_of(home))
    if task.standing or task.kind == DONE or elsewhere:
        return set()
    return board_sources.change_numbers(task, home) - board_sources.change_numbers(task, home, ACKNOWLEDGED)


def merged_faults(tasks, changes: dict, home: Backlog | GitHubBacklog, answered: bool = True) -> list[MergedFault]:
    """One fault per live task for the changes its row names and does not record as merged, unless one is open:
    each merged one, and each the forge does not have. A forge that did not answer cleanly (`answered` False)
    may only have left a change out, so then none is `not found`."""
    faults: list[MergedFault] = []
    for task in tasks:
        refs = [board_sources.change_ref(home, n) for n in sorted(_named(task, home))]
        named = [changes[ref] for ref in refs if ref in changes]
        if any(c.state == "open" for c in named):
            continue
        said = [" ".join(filter(None, (board_sources.spelled(c.ref), "merged into", c.base, c.merge_sha[:7]))) for c in named if c.state == "merged"]
        how = '; write the task done once main is green with it, or write "merged" after the ref' if said else ""
        if answered:
            said += [f"{board_sources.spelled(ref)} not found" for ref in refs if ref not in changes]
        if said:
            faults.append(MergedFault(clip_name(task.label), ", ".join(said) + how))
    return faults


def _read_merged(tasks, home: Backlog | GitHubBacklog, gh, call) -> list[MergedFault] | None:
    """`merged_faults` over the forge's answer for the changes live rows name, or None when they name none and
    the forge is not read. A forge that cannot be read is one `unknown` fault."""
    numbers = {n for task in tasks for n in _named(task, home)}
    if not numbers:
        return None
    try:
        changes, error = board_sources.change_states(home, numbers, gh or backlog.run_gh, call or backlog._call)
    except Exception as e:  # a forge answer shaped unlike its schema, as board_sources.refresh reads it
        changes, error = {}, f"{type(e).__name__}: {e}"
    return merged_faults(tasks, changes, home, not error) + ([MergedFault("unknown", error)] if error else [])


@dataclass(frozen=True)
class LaneFault:
    """A task whose lane the settings file does not name, whose stage is not one of its lane's (#33), or that has a stage and no lane (#363)."""

    task: str
    why: str

    def __str__(self) -> str:
        return f"{LANE} {self.task} — {self.why}"


def lane_faults(tasks, lanes: dict, settings_path: str | None) -> list[LaneFault]:
    faults: list[LaneFault] = []
    for task in tasks:
        name, stage, lane = clip_name(task.label), task.stage.strip(), lanes.get(task.lane.strip())
        if not task.lane.strip():
            if stage:  # BUILD draws a card only for a task with a lane
                faults.append(LaneFault(name, f"stage {stage} but no lane"))
        elif lane is None:
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
        return f"{KANBAN} {self.task} — {self.why}"


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
        cfg = _config_for(home, host, repo)
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
        return f"{OVER} {self.task} running {_dur(self.ran)} ({self.size} {_dur(self.budget)})"


def over_budget(tasks, budgets: dict, day: str, now: datetime) -> list[OverBudget]:
    over: list[OverBudget] = []
    for task in tasks:
        budget, start = budgets.get(task.size.strip()), _hhmm(task.state_time or "", date.fromisoformat(day), now.tzinfo)
        if task.kind != "running" or budget is None or start is None:
            continue
        ran = now - start
        if ran > budget:
            over.append(OverBudget(clip_name(task.label), ran, task.size.strip(), budget))
    return over


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    reopen: list[Reopen] = field(default_factory=list)
    orphans: list[Orphan] = field(default_factory=list)
    unclaimed: list[Unclaimed] = field(default_factory=list)
    stopped: list[Stop] = field(default_factory=list)
    queue: list[QueueFault] = field(default_factory=list)
    issues: list[IssueFault] = field(default_factory=list)
    merged: list[MergedFault] = field(default_factory=list)
    lanes: list[LaneFault] = field(default_factory=list)
    kanban: list[KanbanFault] = field(default_factory=list)
    over: list[OverBudget] = field(default_factory=list)


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


def key_name(task, roster: set[str], refs: set[str]) -> str:
    """The Tasks-table name a File ownership row may be keyed on: none when it is a listed session's name, since
    that bare context is the session's own row, not the task's."""
    return "" if listed(task.name, roster, refs)[0] else task.name


def keyed_on(row: OwnerRow, item: str, name: str) -> bool:
    """A File ownership row keyed on a task's item, or whose whole context is its Tasks-table `name`, is keyed on that
    task. The name is compared exactly, as dispatch does: a session's `arch (deletion)` is not a task named `arch`."""
    return owns(row, item) or (bool(name.strip()) and row.context.strip() == name.strip())


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN.findall(text) if len(t) > 1 and not t.isdigit() and t.lower() not in STOP}


def _detail(context: str) -> str:
    """The parenthetical of a File ownership row: with one context to several tasks, it is the only thing saying which."""
    return " ".join(PAREN.findall(context))


def rows_for(item: str, owner: str, owners: list[OwnerRow], name: str = "") -> tuple[list[OwnerRow], str]:
    """The rows that speak for this task, and a note when nothing does.

    A context owns several tasks at once — `architecture` held both a deletion and a doc edit — so
    the bare name cannot attribute a tree. A row keyed by the task item, or by its Tasks-table `name`, is exact and wins. Otherwise
    the owner's rows are scored on how much of the task item their parenthetical repeats; a lone best
    row wins, and a tie or a blank draws nothing, because attributing the wrong tree reopens a task
    that is genuinely finished.
    """
    exact = [r for r in owners if keyed_on(r, item, name)]
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


def row_claim(row: OwnerRow, tasks, roster: set[str], refs: set[str], user: str) -> Claim:
    """A File ownership row's claim on its trees. Live when its context is in `## Sessions`, or when the
    task it is keyed on — a one-shot's row is keyed on its task, never a session — has a listed owner.
    An empty Sessions table cannot prove that workers are absent, so with no roster every row is live."""
    owner = _bare(row.context)
    if not roster:
        return Claim(owner, True)
    here, missing = listed(row.context, roster, refs, user)
    keyed = next((t for t in tasks if keyed_on(row, t.item, t.name)), None)
    here = here or (keyed is not None and listed(keyed.owner, roster, refs, user)[0])
    how = "names a ref no ## Sessions row carries" if missing else "is not in ## Sessions"
    return Claim(owner, here, how, missing)


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
        if git_trees.git(["rev-parse", "--verify", "--quiet", f"refs/heads/{cand}"], handle)[0] != 0:
            continue
        if git_trees.git(["merge-base", "--is-ancestor", cand, "main"], handle)[0] == 0:
            return f"{name}: no worktree — branch {cand} is on main; merged and cleaned up"
        return f"{name}: no worktree — branch {cand} is not on main, so the work is only on that branch"
    return f"{name}: no worktree and no branch by that name — the row names a tree that was never created"


def _state(worktree: Path) -> tuple[str, str]:
    """(branch, why it is not done), where the why is empty when the work is on main and committed."""
    _, branch = git_trees.git(["rev-parse", "--abbrev-ref", "HEAD"], worktree)
    code, dirty = git_trees.git(["status", "--porcelain"], worktree)
    reasons = []
    if code != 0:
        reasons.append("uncommitted work unknown (status unreadable)")
    elif dirty:
        reasons.append(f"{len(dirty.splitlines())} uncommitted file(s)")
    merged, _ = git_trees.git(["merge-base", "--is-ancestor", "HEAD", "main"], worktree)
    if merged != 0:
        reasons.append("not on main")
    return branch, "; ".join(reasons)


def _tree_line(name: str, branch: str, why: str) -> str:
    return f"{name} ({branch}): {why or 'on main, committed'}"


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
        head = f"{REOPEN} {worktree}: {why} — {len(names)} done task{'' if len(names) == 1 else 's'}"
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
    if git_trees.git(["cat-file", "-e", f"{sha}^{{commit}}"], worktree)[0] != 0:
        return UNKNOWN_SHA, f"no commit {sha} here, so the citation cannot be checked"
    code, _ = git_trees.git(["merge-base", "--is-ancestor", sha, "main"], worktree)
    return (LANDED if code == 0 else NOT_LANDED), ""


def _push_gap(worktree: Path) -> str:
    """Report main's commit and its distance from origin/main for verification."""
    # Use main, not this worktree's HEAD.
    code, sha = git_trees.git(["rev-parse", "--short", "main"], worktree)
    at = f"main {sha} " if code == 0 and sha and " " not in sha else "main "
    code, counts = git_trees.git(["rev-list", "--left-right", "--count", "main...origin/main"], worktree)
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
        lines.append(f"{NEXT} {num} — {clip_name(_unmark(decision))}")
    return faults, lines, len(open_rows)


def audit(root: Path, day: str, gh=None, check_issues: bool = True, now: datetime | None = None, call=None) -> Report:
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
    names: dict[Path, str] = {}
    stopped_at: set[str] = set()
    gone: dict[str, str] = {}
    def visit(row: OwnerRow, name: str, task) -> None:
        """One tree, once: its git state, and any task it reopens or stop it carries."""
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
            names.setdefault(path, name)
            report.lines.append(_tree_line(name, branch, why))
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
        stop = read_stop(path)
        if stop is not None and task is not None and task.kind not in SETTLED and name not in stopped_at:
            stopped_at.add(name)
            report.stopped.append(Stop(task.item, stop.get("stop", ""), stop.get("lands on", ""),
                                       f"{name} ({branch})", task.state.strip()))

    for task in tasks:
        rows, note = rows_for(task.item, task.owner, owners, key_name(task, roster, refs))
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

    # The trees on disk, not the rows that name them, are what the orphan check walks (#43).
    for path in git_trees.discover(root, trees):
        if path not in seen:
            branch, why = _state(path)
            seen.add(path)
            order.append(path)
            names[path] = path.name
            report.lines.append(_tree_line(path.name, branch, why))
    claims: Claims = {}
    for row in owners:
        for name in row.worktrees:
            path = _resolve(root, trees, name)[0]
            if path is not None:
                claims.setdefault(path, []).append(row_claim(row, tasks, roster, refs, cfg.user))
    for source in (registry_claims(root), one_shot_claims((root / trees).resolve())):
        for path, found in source.items():
            claims.setdefault(path, []).extend(found)
    for path in order:
        finding = judge(names[path], git_trees.read_state(path), claims.get(path, []))
        if isinstance(finding, Orphan):
            report.orphans.append(finding)
        elif isinstance(finding, Unclaimed):
            report.unclaimed.append(finding)
    handle = _handle(root, trees, order)
    for name, detail in gone.items():
        report.lines.append(missing_tree(handle, name, detail))
    if order:
        # All worktrees share the same main and origin/main refs.
        report.lines.append(_push_gap(order[0]))
    report.lines.extend(group_reopens(report.reopen))
    report.lines.extend(str(o) for o in report.orphans)
    report.lines.extend(str(u) for u in report.unclaimed)
    report.lines.extend(str(s) for s in report.stopped)
    faults, queue_lines, queued = queue_faults(tracker_text)
    report.queue.extend(faults)
    report.lines.extend(queue_lines)
    report.lines.extend(str(q) for q in report.queue)
    issues = merged = ""
    settings = load_settings(root, cfg.settings_path)
    if cfg.backlog:
        if check_issues:
            report.issues.extend(issue_faults(tasks, cfg.backlog, gh, settings.lanes,
                                              root, trees, owners, cfg.zone, roster, refs))
            report.lines.extend(str(f) for f in report.issues)
            issues = f" issues={len(report.issues)}"
            found = _read_merged(tasks, cfg.backlog, gh, call)
            if found is not None:
                report.merged.extend(found)
                report.lines.extend(str(f) for f in found)
                merged = f" merged={len(found)}"
        else:
            issues = " issues=off"
    report.lanes.extend(lane_faults(tasks, settings.lanes, cfg.settings_path))
    if settings.kanban and cfg.backlog and check_issues:
        report.kanban.extend(kanban_faults(tasks, cfg.backlog, settings.kanban, gh=gh))
        report.lines.extend(str(f) for f in report.kanban)
    report.over.extend(over_budget(tasks, settings.budgets, day, now or datetime.now(cfg.zone)))
    report.lines.extend(str(o) for o in report.over)
    budgeted = f" over={len(report.over)}" if settings.budgets else ""
    report.lines.extend(str(f) for f in report.lanes)
    laned = f" lanes={len(report.lanes)}" if report.lanes or any(t.lane.strip() for t in tasks) else ""
    kanban = f" kanban={len(report.kanban)}" if settings.kanban and check_issues else ""
    facts = len({(r.worktree, r.why) for r in report.reopen})
    reopen = f"{facts} tree{'' if facts == 1 else 's'}/{len(report.reopen)} task{'' if len(report.reopen) == 1 else 's'}" if report.reopen else "0"
    report.lines.append(
        f"tasks={len(tasks)} trees={len(seen)} reopen={reopen} orphaned={len(report.orphans)} "
        + (f"unclaimed={len(report.unclaimed)} " if report.unclaimed else "") + f"stopped={len(report.stopped)}" + (f" queued={queued}" if queue_lines or faults or queued else "") + issues + merged + laned + kanban + budgeted)
    return report


def main(argv: list[str] | None = None, gh=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--no-issues", action="store_true", help="skip the issue check (offline); the summary says issues=off")
    ap.add_argument("--sha", help=(
        "fetch origin main (writing refs/remotes/origin/main, FETCH_HEAD and objects) and say, per repository under the worktrees dir, "
        "whether the commit with this id is on origin/main (a ref of that name is not a commit); takes no flag but --root. "
        "Exit 1 if any repository that has the commit says it is not on origin/main (a tree a worker creates with its own origin, "
        "holding a commit the project never fetched, can still say on, since the veto needs the commit object; a second clone whose "
        "origin/main lags, such as a fork beside the upstream, says no until it catches up); else 2 if none was asked or usage, or "
        "any repository that has, or may have (git cannot read it), the commit is unknown (fetch failed, repository or grafts "
        "unreadable, grafts, no origin/main, shallow clone, ambiguous prefix, git could not compare); else 0 if any says on; else 2 "
        "if any is unknown; else 1. An unknown repository that does not hold the commit (no remote, offline, shallow) blocks no yes; "
        "a pruned worktree (its .git file names a worktrees/<name> that is gone) is answered by its repository; "
        "any other repository git cannot read may hold it and blocks a yes. A repository with no local "
        "main is simply not on local main. With more than one repository the last line is the overall answer"))
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not (root / "CLAUDE.md").is_file():
        print(f"audit_tasks: no CLAUDE.md at {root}", file=sys.stderr)
        return 2
    try:
        cfg = read_config(root)
    except ConfigError as e:
        print(f"audit_tasks: {e}", file=sys.stderr)
        return 2
    if args.sha is not None:
        if args.date is not None or args.no_issues:
            print("audit_tasks: --sha takes no other flag but --root", file=sys.stderr)
            return 2
        if not SHA.fullmatch(args.sha):
            print("audit_tasks: --sha takes 7-64 lowercase hex digits", file=sys.stderr)
            return 2
        trees = worktrees_dir((root / "CLAUDE.md").read_text())
        asked = git_trees.sha_trees(root, trees)
        if not asked:
            print(f"audit_tasks: --sha found no git tree under {trees or 'the workspace root'}, so nothing was checked", file=sys.stderr)
            return 2
        answers = []
        for name, tree in asked:
            code, line, held = git_trees.check_sha(args.sha, tree)
            print(git_trees.clean(f"{line} [{name}]"))  # escaped: neither a name nor a remote's error can forge or split a line
            answers.append((code, held))
        code = git_trees.sha_exit(answers)
        if len(asked) > 1:
            print(f"sha {args.sha}: overall {('on origin/main', 'not on origin/main', 'unknown')[code]}")
        return code
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
    return len(report.reopen) + len(report.stopped) + len(report.queue) + len(report.issues) + len(report.merged) + len(report.lanes) + len(report.kanban)


if __name__ == "__main__":
    raise SystemExit(main())
