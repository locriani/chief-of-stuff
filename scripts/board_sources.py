#!/usr/bin/env python3
"""The forge and worker facts the pages draw, read in one place and cached beside the pages.

    chief-of-stuff sources --root R [--refresh]    # print what the cache holds; --refresh reads it all first

Only what today's tracker names is fetched: its tasks' issues, the pull or merge requests their rows
mention (`!58` on GitLab, `PR #58` on GitHub, or the request's URL), and the requests merged today, in
one GraphQL call per forge. Workers are the registered sessions process_status finds running, live
one-shot workers under the Worktrees dir, and the tracker's Sessions rows. The cache is
`<pages dir>/.sources.json`. A source that cannot be read keeps its last good facts, and `errors` says why.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time
from pathlib import Path

import backlog
import process_status
from _vendor.toon_format import encode as toon_encode
from clock import leading
from forge_review import GITLAB_PIPELINE, github_approval as _github_approval, github_pipeline as _github_pipeline
from md import section as _section
from settings import SettingsError, load as load_settings
from tracker import Tracker, bare_name as _bare_name, launcher, parse_tracker
from tracker_log import STARTED_DETAIL
from workspace import Config, ConfigError, read_config, worktrees_dir

CACHE = ".sources.json"
CLOSES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(#\d+)")
GH_PR = re.compile(r"(?i)\bPR\s*#(\d+)\b")
GL_MR = re.compile(r"(?<![\w&])!(\d+)\b")
GL_WAITING = {"pending", "created", "waiting_for_resource", "preparing", "scheduled", "manual"}

GH_ISSUE = "number url title state body labels(first: 50) { nodes { name } } closedAt"
# A review thread is its first comment, as review_threads.QUERY reads them; one comment keeps the node count low.
GH_CHANGE = ("number url title state isDraft mergedAt baseRefName body headRefOid reviewDecision mergeable "
             "files(first: 100) { nodes { path additions deletions } } "
             "latestReviews(first: 50) { nodes { author { login } state submittedAt commit { oid } } } "
             "reviewThreads(first: 100) { nodes { isResolved comments(first: 1) { nodes { author { login } body createdAt url } } } } "
             "commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { __typename "
             "... on CheckRun { name status conclusion startedAt completedAt } ... on StatusContext { state } } } } } } }")
GL_PAGE = 100  # the most nodes GitLab answers for one connection
GL_ISSUE = "iid webUrl title state description labels { nodes { title } } closedAt"
GL_CHANGE = ("iid webUrl title state draft mergedAt targetBranch description approved conflicts "
             "approvedBy { nodes { username } } headPipeline { status } diffStats { path additions deletions } diffHeadSha")
# Jobs and discussions only for open changes, in a second query: in GL_CHANGE they took it past GitLab's complexity cap of 250.
GL_DETAIL = ("iid headPipeline { jobs { nodes { name status duration } } } "
             "discussions { nodes { resolvable resolved notes(first: 1) { nodes { author { username } body createdAt url } } } }")


@dataclass(frozen=True)
class FileStat:
    path: str
    additions: int
    deletions: int


@dataclass(frozen=True)
class Job:            # one job of the head pipeline (GitLab) or one check run (GitHub)
    name: str
    status: str       # as Change.pipeline
    seconds: int | None


@dataclass(frozen=True)
class ReviewThread:   # its first comment
    resolved: bool
    author: str
    body: str
    at: datetime | None
    url: str


@dataclass(frozen=True)
class Change:          # a PR or MR
    ref: str           # "!58" (GitLab) or "#58" (GitHub PR)
    url: str
    title: str
    state: str         # "open" | "merged" | "closed"
    draft: bool
    pipeline: str | None   # "passed" | "failed" | "running" | "pending" | None
    approved: bool
    approvals: int
    merged_at: datetime | None
    base: str
    files: tuple[str, ...]
    issues: tuple[str, ...]   # issue refs it closes, e.g. ("#111",)
    head: str = ""            # the head commit's sha, which the decision page graphs
    stats: tuple[FileStat, ...] = ()
    jobs: tuple[Job, ...] = ()
    conflicts: bool = False
    threads: tuple[ReviewThread, ...] = ()


@dataclass(frozen=True)
class Issue:
    ref: str
    url: str
    title: str
    state: str
    labels: tuple[str, ...]
    body: str = ""
    closed_at: datetime | None = None  # (#220)


@dataclass(frozen=True)
class Worker:
    name: str
    kind: str          # "one-shot" | "session"
    runtime: str
    model: str
    task: str
    tree: str
    started: datetime | None
    last: datetime | None


@dataclass(frozen=True)
class Sources:
    fetched: dict[str, datetime]   # keys: "kanban", "merge requests", "workers"
    issues: dict[str, Issue]       # by ref
    changes: dict[str, Change]     # by ref
    workers: tuple[Worker, ...]
    errors: dict[str, str]         # source -> why it could not be read


EMPTY = Sources({}, {}, {}, (), {})


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _change_of(v: dict) -> Change:
    """A cached change; a cache from an older release lacks the later fields, which keep their defaults."""
    return Change(**{**v, "merged_at": _when(v["merged_at"]), "files": tuple(v["files"]), "issues": tuple(v["issues"]),
                     "stats": tuple(FileStat(**s) for s in v.get("stats") or ()),
                     "jobs": tuple(Job(**j) for j in v.get("jobs") or ()),
                     "threads": tuple(ReviewThread(**{**t, "at": _when(t["at"])}) for t in v.get("threads") or ())})


def load(pages_dir: Path) -> Sources:
    """The cache, or EMPTY when it is absent or unreadable."""
    try:
        d = json.loads((pages_dir / CACHE).read_text())
        return Sources({k: datetime.fromisoformat(v) for k, v in d["fetched"].items()},
                       {k: Issue(**{**v, "labels": tuple(v["labels"]), "closed_at": _when(v.get("closed_at"))})
                        for k, v in d["issues"].items()},
                       {k: _change_of(v) for k, v in d["changes"].items()},
                       tuple(Worker(**{**w, "started": _when(w["started"]), "last": _when(w["last"])})
                             for w in d["workers"]),
                       dict(d["errors"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return EMPTY


def pages_dir(root: Path, cfg: Config, day: str) -> Path:
    """Where the board is written: the Board line's dir, else beside the tracker (as render_board does)."""
    return root / cfg.pages_dir if cfg.pages_dir else (root / cfg.tracker_path(day)).parent


def _closes(body: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(CLOSES.findall(body or "")))


def _graphql_errors(body) -> str:
    return "; ".join(str(e.get("message", e)) for e in body.get("errors") or [] if isinstance(e, dict))


def _stats(files: list[dict]) -> tuple[FileStat, ...]:
    return tuple(FileStat(f["path"], int(f.get("additions") or 0), int(f.get("deletions") or 0))
                 for f in files if "additions" in f)


# --- GitHub ----------------------------------------------------------------------------------------

def _gh_change(key: str, p: dict, approver: str) -> Change:
    reviews = (p.get("latestReviews") or {}).get("nodes") or []
    commit = (((p.get("commits") or {}).get("nodes") or [{}])[0] or {}).get("commit") or {}
    checks = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
    pipeline = _github_pipeline(checks)
    runs = [c for c in checks if c.get("__typename") == "CheckRun"]
    jobs = tuple(Job(c.get("name") or "", _github_pipeline([c]),
                     int((_when(c["completedAt"]) - _when(c["startedAt"])).total_seconds())
                     if c.get("startedAt") and c.get("completedAt") else None) for c in runs)
    threads = tuple(ReviewThread(bool(t.get("isResolved")), (c.get("author") or {}).get("login") or "", c.get("body") or "",
                                 _when(c.get("createdAt")), c.get("url") or "")
                    for t in (p.get("reviewThreads") or {}).get("nodes") or []
                    for c in ((t.get("comments") or {}).get("nodes") or [])[:1])
    files = (p.get("files") or {}).get("nodes") or []
    approved = (_github_approval(reviews, approver, p.get("headRefOid", "")) == "approved" if approver
                else p.get("reviewDecision") == "APPROVED")
    return Change(key, p["url"], p["title"], p["state"].lower(), bool(p.get("isDraft")),
                  None if pipeline == "none" else pipeline, approved,
                  sum(r.get("state") == "APPROVED" for r in reviews), _when(p.get("mergedAt")),
                  p.get("baseRefName") or "", tuple(f["path"] for f in files), _closes(p.get("body")),
                  p.get("headRefOid") or "", _stats(files), jobs, p.get("mergeable") == "CONFLICTING", threads)


def github(home: backlog.GitHubBacklog, wanted: dict[str, set[int]], since: datetime, approver: str, gh):
    """(issues, changes, error) from one `gh api graphql` call; issues and changes are None when it failed."""
    repos = sorted(wanted)
    parts = []
    for i, repo in enumerate(repos):
        owner, name = repo.split("/", 1)
        items = " ".join(f"n{n}: issueOrPullRequest(number: {n}) {{ __typename ... on Issue {{ {GH_ISSUE} }} "
                         f"... on PullRequest {{ {GH_CHANGE} }} }}" for n in sorted(wanted[repo]))
        parts.append(f"r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {items} }}")
    search = f"repo:{home.repo} is:pr is:merged merged:>={since.replace(microsecond=0).isoformat()}"
    parts.append(f"merged: search(query: {json.dumps(search)}, type: ISSUE, first: 50) "
                 f"{{ nodes {{ ... on PullRequest {{ {GH_CHANGE} }} }} }}")
    code, out, err = gh(["api", "graphql", "-f", "query=query { " + " ".join(parts) + " }"])
    try:
        body = json.loads(out)
        data = body["data"]
    except (ValueError, KeyError, TypeError):
        return None, None, err.strip() or f"gh exited {code}"
    issues, changes = {}, {}
    for i, repo in enumerate(repos):
        for node in ((data.get(f"r{i}") or {}).values()):
            if not isinstance(node, dict):
                continue
            key = backlog.IssueRef(repo, int(node["number"])).label(home)
            if node.get("__typename") == "PullRequest":
                changes[key] = _gh_change(key, node, approver)
            else:
                issues[key] = Issue(key, node["url"], node["title"], node["state"].lower(),
                                    tuple(x["name"] for x in (node.get("labels") or {}).get("nodes") or []),
                                    node.get("body") or "", _when(node.get("closedAt")))
    for node in ((data.get("merged") or {}).get("nodes") or []):
        if node and _when(node.get("mergedAt")) and _when(node["mergedAt"]) >= since:
            changes.setdefault(f"#{node['number']}", _gh_change(f"#{node['number']}", node, approver))
    return issues, changes, _graphql_errors(body) or (err.strip() if code else "")


# --- GitLab ----------------------------------------------------------------------------------------

def _gl_pipeline(status: str | None) -> str | None:
    if not status:
        return None
    s = status.lower()
    return GITLAB_PIPELINE.get(s) or ("pending" if s in GL_WAITING else "running")


def _gl_detail(m: dict) -> tuple[tuple[Job, ...], tuple[ReviewThread, ...]]:
    pipe = m.get("headPipeline") or {}
    jobs = tuple(Job(j.get("name") or "", _gl_pipeline(j.get("status")) or "pending",
                     None if j.get("duration") is None else int(j["duration"]))
                 for j in (pipe.get("jobs") or {}).get("nodes") or [])
    threads = tuple(ReviewThread(bool(d.get("resolved")), (n.get("author") or {}).get("username") or "", n.get("body") or "",
                                 _when(n.get("createdAt")), n.get("url") or "")
                    for d in (m.get("discussions") or {}).get("nodes") or [] if d.get("resolvable")
                    for n in ((d.get("notes") or {}).get("nodes") or [])[:1])
    return jobs, threads


def _gl_change(m: dict, approver: str) -> Change:
    by = {(u or {}).get("username") for u in (m.get("approvedBy") or {}).get("nodes") or []}
    state = {"opened": "open", "locked": "closed"}.get(m["state"], m["state"])
    pipe = m.get("headPipeline") or {}
    return Change(f"!{m['iid']}", m["webUrl"], m["title"], state, bool(m.get("draft")), _gl_pipeline(pipe.get("status")),
                  approver in by if approver else bool(m.get("approved")), len(by), _when(m.get("mergedAt")),
                  m.get("targetBranch") or "", tuple(d["path"] for d in m.get("diffStats") or []),
                  _closes(m.get("description")), m.get("diffHeadSha") or "", _stats(m.get("diffStats") or []), (),
                  bool(m.get("conflicts")))


def gitlab(home: backlog.Backlog, wanted: dict[str, set[int]], mrs: set[int], since: datetime, approver: str, call):
    """(issues, changes, error) from the host's /api/graphql, with the Backlog's token: one POST, and one more for open changes' jobs and threads.
    A connection answers at most GL_PAGE nodes, so past that the same query goes again for the next GL_PAGE iids and the next merged page."""
    secret = backlog.token(home)
    if not secret:
        return None, None, home.missing_token
    projects = sorted(set(wanted) | {home.project})
    left = {project: sorted(wanted.get(project) or ()) for project in projects}
    mrs, cursor = sorted(mrs), ""  # cursor: the merged page to ask for next, None once the last one is in
    issues, changes, error = None, None, ""
    host = backlog.home_of(home)[0]
    while True:
        parts = []
        for i, project in enumerate(projects):
            fields = []
            if left[project]:
                fields.append(f"issues(iids: {json.dumps([str(n) for n in left[project][:GL_PAGE]])}) {{ nodes {{ {GL_ISSUE} }} }}")
            if project == home.project:
                if mrs:
                    fields.append(f"mergeRequests(iids: {json.dumps([str(n) for n in mrs[:GL_PAGE]])}) {{ nodes {{ {GL_CHANGE} }} }}")
                if cursor is not None:
                    after = f", after: {json.dumps(cursor)}" if cursor else ""
                    fields.append(f"merged: mergeRequests(state: merged, mergedAfter: {json.dumps(since.isoformat())}{after}) "
                                  f"{{ nodes {{ {GL_CHANGE} }} pageInfo {{ hasNextPage endCursor }} }}")
            if fields:
                parts.append(f"p{i}: project(fullPath: {json.dumps(project)}) {{ {' '.join(fields)} }}")
        if not parts:
            break
        body, _, err = call("POST", f"{home.host.rstrip('/')}/api/graphql", secret, backlog.TIMEOUT,
                            {"query": "query { " + " ".join(parts) + " }"})
        if err or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            err = err or _graphql_errors(body if isinstance(body, dict) else {}) or "GitLab did not answer"
            if issues is None:
                return None, None, err
            return issues, changes, error or err  # a later page failed: the board keeps what the first ones gave
        issues, changes = issues or {}, changes or {}
        for i, project in enumerate(projects):
            p = body["data"].get(f"p{i}") or {}
            for node in (p.get("issues") or {}).get("nodes") or []:
                key = backlog.IssueRef(project, int(node["iid"]), host).label(home)
                issues[key] = Issue(key, node["webUrl"], node["title"], {"opened": "open"}.get(node["state"], node["state"]),
                                    tuple(x["title"] for x in (node.get("labels") or {}).get("nodes") or []),
                                    node.get("description") or "", _when(node.get("closedAt")))
            for node in ((p.get("mergeRequests") or {}).get("nodes") or []) + ((p.get("merged") or {}).get("nodes") or []):
                changes.setdefault(f"!{node['iid']}", _gl_change(node, approver))
            left[project] = left[project][GL_PAGE:]
        info = (((body["data"].get(f"p{projects.index(home.project)}") or {}).get("merged") or {}).get("pageInfo") or {})
        cursor = info.get("endCursor") if info.get("hasNextPage") else None
        mrs = mrs[GL_PAGE:]
        error = error or _graphql_errors(body)
    opened = sorted(int(k[1:]) for k, c in changes.items() if c.state == "open")
    for start in range(0, len(opened), GL_PAGE):
        query = (f"query {{ p0: project(fullPath: {json.dumps(home.project)}) {{ "
                 f"mergeRequests(iids: {json.dumps([str(n) for n in opened[start:start + GL_PAGE]])}) {{ nodes {{ {GL_DETAIL} }} }} }} }}")
        body, _, err = call("POST", f"{home.host.rstrip('/')}/api/graphql", secret, backlog.TIMEOUT, {"query": query})
        if err or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            # The board still renders the changes without their jobs and threads.
            return issues, changes, error or err or _graphql_errors(body if isinstance(body, dict) else {}) or "GitLab did not answer"
        for node in ((body["data"].get("p0") or {}).get("mergeRequests") or {}).get("nodes") or []:
            key = f"!{node['iid']}"
            if key in changes:
                jobs, threads = _gl_detail(node)
                changes[key] = replace(changes[key], jobs=jobs, threads=threads)
        error = error or _graphql_errors(body)
    return issues, changes, error


def forge(root: Path, cfg: Config, tracker: Tracker, since: datetime, gh, call):
    """What the tracker's tasks name, from the Backlog's forge: (issues, changes, error)."""
    home = cfg.backlog
    if home is None:
        return {}, {}, ""
    try:
        approver = load_settings(root, cfg.settings_path).workflow.approver
    except SettingsError:
        approver = ""
    wanted: dict[str, set[int]] = {}
    changes: set[int] = set()
    github_home = isinstance(home, backlog.GitHubBacklog)
    url = (rf"github\.com/{re.escape(home.repo)}/pull/(\d+)" if github_home
           else rf"{re.escape(backlog.home_of(home)[0])}/{re.escape(home.project)}/-/merge_requests/(\d+)")
    for task in tracker.tasks:
        if task.standing:
            continue
        ref = backlog.issue_ref(task.issue, home) if task.issue.strip() else None
        if ref and (ref.host == backlog.GITHUB) == github_home:
            wanted.setdefault(ref.repo, set()).add(ref.number)
        row = " ".join((task.name, task.item, task.issue, task.checklist))
        changes |= {int(n) for n in (GH_PR if github_home else GL_MR).findall(row) + re.findall(url, row)}
    if github_home:
        if changes:
            wanted.setdefault(home.repo, set()).update(changes)
        return github(home, wanted, since, approver, gh)
    return gitlab(home, wanted, changes, since, approver, call)


# --- workers ---------------------------------------------------------------------------------------

def _owned_task(name: str, tasks: tuple) -> str:
    """(#199) The open task row `name`'s session owns: an open (not `done`) row whose owner cell is `name`'s
    bare name (`_bare_name`, as `Task.standing_for` matches owners). Several open rows owned: the last one,
    since later rows win elsewhere (`tracker.launcher`'s own tie-break). None owned: empty."""
    bare = _bare_name(name)
    owned = [t for t in tasks if bare and t.kind != "done" and _bare_name(t.owner) == bare]
    return owned[-1].label if owned else ""


def workers(root: Path, cfg: Config, tracker: Tracker, tracker_text: str, day: date) -> tuple[Worker, ...]:
    zone = cfg.zone
    rows = {_bare_name(s.name): s for s in tracker.sessions}
    found = []
    records = process_status.registrations(root)
    for row in process_status.check(root, []):
        if row["status"] != "running":
            continue
        record = records[row["pid"]]
        s = rows.pop(_bare_name(record["name"]), None)
        found.append(Worker(record["name"], "session", record["runtime"], "", _owned_task(record["name"], tracker.tasks),
                            record["worktree"], _when(record.get("started")), leading(s.last_reply, day, zone) if s else None))
    found += [Worker(s.label, "session", "", "", _owned_task(s.label, tracker.tasks), "", None,
                     leading(s.last_reply, day, zone)) for s in rows.values()]
    started = {m[5]: m for m in (STARTED_DETAIL.match(line) for line in _section(tracker_text, "## Log")) if m}
    trees, row_of = root / worktrees_dir((root / "CLAUDE.md").read_text()), launcher(tracker.tasks)
    for tree, task in process_status.running_trees(trees).items():
        m = started.get(task)
        runtime, _, model = (m[3] if m else "").partition(" ")
        found.append(Worker(m[2] if m else tree.name, "one-shot", runtime, model, row_of(task)[1], tree.name,
                            leading(m[1], day, zone) if m else None, None))
    return tuple(found)


# --- refresh ---------------------------------------------------------------------------------------

def _write(path: Path, sources: Sources) -> None:
    """Atomically: the page server reads this file while the refresher writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".sources-", delete=False) as out:
        out.write(json.dumps(asdict(sources), default=lambda v: v.isoformat(), indent=1))
    os.replace(out.name, path)


def refresh(root: Path, now: datetime, gh=backlog.run_gh, call=backlog._call) -> Sources:
    """Fetch everything, write the cache, return it. Never raises: a failed source is in `errors`."""
    try:
        cfg = read_config(root)
    except (OSError, ConfigError) as e:
        return replace(EMPTY, errors={"config": str(e)})
    local = now.astimezone(cfg.zone)
    day = local.date()
    pages = pages_dir(root, cfg, day.isoformat())
    prev = load(pages)
    fetched, errors = dict(prev.fetched), {}
    try:
        text = (root / cfg.tracker_path(day.isoformat())).read_text()
    except OSError:
        text = ""
    tracker = parse_tracker(text)
    try:
        issues, changes, error = forge(root, cfg, tracker, datetime.combine(day, time(0), cfg.zone), gh, call)
    except Exception as e:  # a forge answer shaped unlike its schema; the page still renders
        issues, changes, error = None, None, f"{type(e).__name__}: {e}"
    if error:
        errors["kanban"] = errors["merge requests"] = error
    if issues is None:
        issues, changes = prev.issues, prev.changes
    elif cfg.backlog is not None:
        fetched["kanban"] = fetched["merge requests"] = now
    try:
        found = workers(root, cfg, tracker, text, day)
        fetched["workers"] = now
    except Exception as e:  # a registry or ps that cannot be read
        found, errors["workers"] = prev.workers, f"{type(e).__name__}: {e}"
    got = Sources(fetched, issues, changes, found, errors)
    try:
        _write(pages / CACHE, got)
    except OSError as e:
        got = replace(got, errors={**errors, "cache": str(e)})
    return got


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("."), help="workspace root holding CLAUDE.md")
    ap.add_argument("--refresh", action="store_true", help="read every source now and rewrite the cache")
    args = ap.parse_args(argv)
    now = datetime.now().astimezone()
    try:
        cfg = read_config(args.root)
    except (OSError, ConfigError) as e:
        print(f"sources: {e}", file=sys.stderr)
        return 2
    got = refresh(args.root, now) if args.refresh else load(pages_dir(args.root, cfg, now.astimezone(cfg.zone).date().isoformat()))
    print(toon_encode({"fetched": {k: v.astimezone(cfg.zone).strftime("%H:%M") for k, v in got.fetched.items()},
                       "issues": len(got.issues), "changes": len(got.changes), "workers": len(got.workers),
                       "errors": got.errors}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
