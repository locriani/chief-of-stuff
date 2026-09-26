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

from typing import TYPE_CHECKING

import backlog
import process_status
from _vendor.toon_format import encode as toon_encode
from settings import SettingsError, load as load_settings

if TYPE_CHECKING:
    from render_board import Config, Tracker
# render_board imports this module for the dataclasses, so render_board and what imports it load inside the functions.

CACHE = ".sources.json"
CLOSES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(#\d+)")
GH_PR = re.compile(r"(?i)\bPR\s*#(\d+)\b")
GL_MR = re.compile(r"(?<![\w&])!(\d+)\b")
HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\b")
STARTED = re.compile(r"^-\s+(\d{1,2}:\d{2}) one-shot (\S+) started: (.*?), worktree `([^`]*)`, task (.+?)\s*$")
GL_WAITING = {"pending", "created", "waiting_for_resource", "preparing", "scheduled", "manual"}

GH_ISSUE = "number url title state labels(first: 50) { nodes { name } }"
GH_CHANGE = ("number url title state isDraft mergedAt baseRefName body headRefOid reviewDecision "
             "files(first: 100) { nodes { path } } "
             "latestReviews(first: 50) { nodes { author { login } state submittedAt commit { oid } } } "
             "commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { __typename "
             "... on CheckRun { status conclusion } ... on StatusContext { state } } } } } } }")
GL_ISSUE = "iid webUrl title state labels { nodes { title } }"
GL_CHANGE = ("iid webUrl title state draft mergedAt targetBranch description approved "
             "approvedBy { nodes { username } } headPipeline { status } diffStats { path }")


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


def load(pages_dir: Path) -> Sources:
    """The cache, or EMPTY when it is absent or unreadable."""
    try:
        d = json.loads((pages_dir / CACHE).read_text())
        return Sources({k: datetime.fromisoformat(v) for k, v in d["fetched"].items()},
                       {k: Issue(**{**v, "labels": tuple(v["labels"])}) for k, v in d["issues"].items()},
                       {k: Change(**{**v, "merged_at": _when(v["merged_at"]), "files": tuple(v["files"]),
                                     "issues": tuple(v["issues"])}) for k, v in d["changes"].items()},
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


# --- GitHub ----------------------------------------------------------------------------------------

def _gh_change(key: str, p: dict, approver: str) -> Change:
    from merge_approved import _github_approval, _github_pipeline
    reviews = (p.get("latestReviews") or {}).get("nodes") or []
    commit = (((p.get("commits") or {}).get("nodes") or [{}])[0] or {}).get("commit") or {}
    checks = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
    pipeline = _github_pipeline(checks)
    approved = (_github_approval(reviews, approver, p.get("headRefOid", "")) == "approved" if approver
                else p.get("reviewDecision") == "APPROVED")
    return Change(key, p["url"], p["title"], p["state"].lower(), bool(p.get("isDraft")),
                  None if pipeline == "none" else pipeline, approved,
                  sum(r.get("state") == "APPROVED" for r in reviews), _when(p.get("mergedAt")),
                  p.get("baseRefName") or "", tuple(f["path"] for f in (p.get("files") or {}).get("nodes") or []),
                  _closes(p.get("body")))


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
                                    tuple(x["name"] for x in (node.get("labels") or {}).get("nodes") or []))
    for node in ((data.get("merged") or {}).get("nodes") or []):
        if node and _when(node.get("mergedAt")) and _when(node["mergedAt"]) >= since:
            changes.setdefault(f"#{node['number']}", _gh_change(f"#{node['number']}", node, approver))
    return issues, changes, _graphql_errors(body) or (err.strip() if code else "")


# --- GitLab ----------------------------------------------------------------------------------------

def _gl_pipeline(status: str | None) -> str | None:
    from merge_approved import GITLAB_PIPELINE
    if not status:
        return None
    s = status.lower()
    return GITLAB_PIPELINE.get(s) or ("pending" if s in GL_WAITING else "running")


def _gl_change(m: dict, approver: str) -> Change:
    by = {(u or {}).get("username") for u in (m.get("approvedBy") or {}).get("nodes") or []}
    state = {"opened": "open", "locked": "closed"}.get(m["state"], m["state"])
    return Change(f"!{m['iid']}", m["webUrl"], m["title"], state, bool(m.get("draft")),
                  _gl_pipeline((m.get("headPipeline") or {}).get("status")),
                  approver in by if approver else bool(m.get("approved")), len(by), _when(m.get("mergedAt")),
                  m.get("targetBranch") or "", tuple(d["path"] for d in m.get("diffStats") or []),
                  _closes(m.get("description")))


def gitlab(home: backlog.Backlog, wanted: dict[str, set[int]], mrs: set[int], since: datetime, approver: str, call):
    """(issues, changes, error) from one POST to the host's /api/graphql, with the Backlog's token."""
    secret = backlog.token(home)
    if not secret:
        return None, None, f"no token: set ${home.env} or add it to the Keychain as {home.service}"
    projects = sorted(set(wanted) | {home.project})
    parts = []
    for i, project in enumerate(projects):
        fields = []
        if wanted.get(project):
            fields.append(f"issues(iids: {json.dumps([str(n) for n in sorted(wanted[project])])}) {{ nodes {{ {GL_ISSUE} }} }}")
        if project == home.project:
            if mrs:
                fields.append(f"mergeRequests(iids: {json.dumps([str(n) for n in sorted(mrs)])}) {{ nodes {{ {GL_CHANGE} }} }}")
            fields.append(f"merged: mergeRequests(state: merged, mergedAfter: {json.dumps(since.isoformat())}) "
                          f"{{ nodes {{ {GL_CHANGE} }} }}")
        parts.append(f"p{i}: project(fullPath: {json.dumps(project)}) {{ {' '.join(fields)} }}")
    body, _, err = call("POST", f"{home.host.rstrip('/')}/api/graphql", secret, backlog.TIMEOUT,
                        {"query": "query { " + " ".join(parts) + " }"})
    if err or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        return None, None, err or _graphql_errors(body if isinstance(body, dict) else {}) or "GitLab did not answer"
    issues, changes = {}, {}
    host = backlog.home_of(home)[0]
    for i, project in enumerate(projects):
        p = body["data"].get(f"p{i}") or {}
        for node in (p.get("issues") or {}).get("nodes") or []:
            key = backlog.IssueRef(project, int(node["iid"]), host).label(home)
            issues[key] = Issue(key, node["webUrl"], node["title"], {"opened": "open"}.get(node["state"], node["state"]),
                                tuple(x["title"] for x in (node.get("labels") or {}).get("nodes") or []))
        for node in ((p.get("mergeRequests") or {}).get("nodes") or []) + ((p.get("merged") or {}).get("nodes") or []):
            changes.setdefault(f"!{node['iid']}", _gl_change(node, approver))
    return issues, changes, _graphql_errors(body)


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

def _at(hhmm: str, day: date, cfg: Config) -> datetime | None:
    m = HHMM.match(hhmm or "")
    return datetime.combine(day, time(int(m[1]), int(m[2])), cfg.zone) if m and int(m[1]) < 24 else None


def workers(root: Path, cfg: Config, tracker: Tracker, tracker_text: str, day: date) -> tuple[Worker, ...]:
    import one_shot
    from audit_tasks import worktrees_dir
    from render_board import _bare_name, _section
    rows = {_bare_name(s.name): s for s in tracker.sessions}
    found = []
    records = process_status.registrations(root)
    for row in process_status.check(root, []):
        if row["status"] != "running":
            continue
        record = records[row["pid"]]
        s = rows.pop(_bare_name(record["name"]), None)
        found.append(Worker(record["name"], "session", record["runtime"], "", s.doing if s else "",
                            record["worktree"], _when(record.get("started")), _at(s.last_reply, day, cfg) if s else None))
    found += [Worker(s.label, "session", "", "", s.doing, "", None, _at(s.last_reply, day, cfg)) for s in rows.values()]
    started = {m[5]: m for m in (STARTED.match(line) for line in _section(tracker_text, "## Log")) if m}
    trees = root / worktrees_dir((root / "CLAUDE.md").read_text())
    for tree, task in one_shot.running_trees(trees).items():
        m = started.get(task)
        runtime, _, model = (m[3] if m else "").partition(" ")
        found.append(Worker(m[2] if m else tree.name, "one-shot", runtime, model, task, tree.name,
                            _at(m[1], day, cfg) if m else None, None))
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
    from render_board import ConfigError, parse_coordinator, parse_tracker
    try:
        cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=now.date())
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
    from render_board import ConfigError, parse_coordinator
    now = datetime.now().astimezone()
    try:
        cfg = parse_coordinator((args.root / "CLAUDE.md").read_text(), today=date.today())
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
