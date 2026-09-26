#!/usr/bin/env python3
"""List the open pull or merge requests the user approved, and merge one that is ready.

    chief-of-stuff merge-approved --root R [--repo owner/name]            list, ready ones first
    chief-of-stuff merge-approved --root R [--repo owner/name] --merge N  merge N, or say what blocks it

The user, 2026-09-26 01:57 (#97): "I want to be able to mark a PR as approved, and you handle merging
in what I mark approved." Only with `[workflow] merge_owner = "approval"`; `approver` is the user's forge
username, and no other account's approval counts. Ready is all three: the approver's approval on the
head commit, a pipeline that passed (or none), and no conflict. The merge names the head it checked,
so a push after the check fails the merge instead of landing unreviewed code.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode

import backlog
from render_board import ConfigError, parse_coordinator
from settings import SettingsError, Workflow, load as load_settings

FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
GITLAB_PIPELINE = {"success": "passed", "failed": "failed", "canceled": "failed"}


@dataclass(frozen=True)
class Request:
    number: int
    title: str
    url: str
    head: str
    approval: str   # approved | stale | none
    pipeline: str   # passed | failed | running | none
    mergeable: str  # yes | conflict | the forge's own word
    approver: str = ""

    @property
    def blockers(self) -> list[str]:
        out = {"none": [f"no approval from {self.approver}"],
               "stale": [f"{self.approver} approved an earlier commit"]}.get(self.approval, [])
        if self.pipeline in ("failed", "running"):
            out.append(f"pipeline {self.pipeline}")
        if self.mergeable != "yes":
            out.append("conflict" if self.mergeable == "conflict" else f"not mergeable: {self.mergeable}")
        return out


def _github_approval(reviews: list[dict], approver: str, head: str) -> str:
    # A comment neither grants nor withdraws; the approver's latest approve or request-changes decides.
    mine = sorted((r for r in reviews if (r.get("author") or {}).get("login") == approver
                   and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")),
                  key=lambda r: r.get("submittedAt") or "")
    if not mine or mine[-1]["state"] != "APPROVED":
        return "none"
    return "approved" if (mine[-1].get("commit") or {}).get("oid") == head else "stale"


def _github_pipeline(checks: list[dict]) -> str:
    if not checks:
        return "none"
    if any((c.get("conclusion") or c.get("state") or "").upper() in FAILED for c in checks):
        return "failed"
    running = [c for c in checks if (c.get("__typename") == "CheckRun" and c.get("status") != "COMPLETED")
               or (c.get("__typename") != "CheckRun" and c.get("state") in ("PENDING", "EXPECTED"))]
    return "running" if running else "passed"


def github_requests(repo: str, approver: str, gh=backlog.run_gh) -> list[Request]:
    code, out, err = gh(["pr", "list", "-R", repo, "--state", "open", "--limit", "100", "--json",
                         "number,title,url,headRefOid,reviews,statusCheckRollup,mergeable"])
    if code != 0:
        raise RuntimeError(f"gh pr list: {err.strip() or code}")
    return [Request(p["number"], p["title"], p["url"], p["headRefOid"],
                    _github_approval(p.get("reviews") or [], approver, p["headRefOid"]),
                    _github_pipeline(p.get("statusCheckRollup") or []),
                    {"MERGEABLE": "yes", "CONFLICTING": "conflict"}.get(p.get("mergeable"), "unknown"), approver)
            for p in json.loads(out)]


def _gitlab_base(home: backlog.Backlog, project: str) -> str:
    return f"{home.host.rstrip('/')}/api/v4/projects/{quote(project, safe='')}"


def gitlab_requests(home: backlog.Backlog, project: str, approver: str, token: str, call=backlog._call) -> list[Request]:
    # ponytail: GitLab's approvals API names no commit, so an approval counts for the head only when the
    # project resets approvals on push. Check that project setting once; a per-note sha check if it is off.
    base = _gitlab_base(home, project)
    listed, _, err = call("GET", f"{base}/merge_requests?" + urlencode({"state": "opened", "per_page": 100}), token, backlog.TIMEOUT)
    if err:
        raise RuntimeError(f"merge requests: {err}")
    out = []
    for row in listed:
        iid = int(row["iid"])
        mr, _, err = call("GET", f"{base}/merge_requests/{iid}", token, backlog.TIMEOUT)
        approvals, _, err2 = call("GET", f"{base}/merge_requests/{iid}/approvals", token, backlog.TIMEOUT)
        if err or err2:
            raise RuntimeError(f"!{iid}: {err or err2}")
        names = {(a.get("user") or {}).get("username") for a in approvals.get("approved_by") or []}
        pipeline = (mr.get("head_pipeline") or {}).get("status")
        status = mr.get("detailed_merge_status") or ""
        out.append(Request(iid, mr.get("title", ""), mr.get("web_url", ""), mr.get("sha", ""),
                           "approved" if approver in names else "none",
                           "none" if pipeline is None else GITLAB_PIPELINE.get(pipeline, "running"),
                           "conflict" if mr.get("has_conflicts") else ("yes" if status == "mergeable" else status),
                           approver))
    return out


def forge(root: Path, repo: str | None) -> tuple[Workflow, backlog.Backlog, bool, str]:
    """The workspace's workflow settings, its forge, whether that is GitHub, and the repo to act on."""
    cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())
    workflow = load_settings(root, cfg.settings_path).workflow
    home = cfg.backlog
    if home is None:
        raise ConfigError("no Backlog: line names the forge")
    github = isinstance(home, backlog.GitHubBacklog)
    return workflow, home, github, repo or (home.repo if github else home.project)


def main(argv: list[str] | None = None, gh=backlog.run_gh, call=backlog._call) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, required=True, help="workspace holding CLAUDE.md")
    ap.add_argument("--repo", help="owner/name or group/name; default the backlog's")
    ap.add_argument("--merge", type=int, metavar="N", help="merge N if it is ready")
    args = ap.parse_args(argv)
    try:
        workflow, home, github, repo = forge(args.root, args.repo)
    except (OSError, ConfigError, SettingsError) as exc:
        print(f"merge-approved: {exc}", file=sys.stderr)
        return 2
    if workflow.merge_owner != "approval":
        print(f"merge-approved: [workflow] merge_owner is {workflow.merge_owner}; merging on approval is "
              "off, and merging stays the user's", file=sys.stderr)
        return 2
    try:
        if github:
            found = github_requests(repo, workflow.approver, gh)
        else:
            secret = backlog.token(home)
            found = gitlab_requests(home, repo, workflow.approver, secret, call)
    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"merge-approved: {exc}", file=sys.stderr)
        return 1
    mark = "#" if github else "!"
    if args.merge is None:
        for r in sorted(found, key=lambda r: (bool(r.blockers), r.number)):
            print(f"{mark}{r.number} {'ready' if not r.blockers else '; '.join(r.blockers)} · {r.title} · {r.url}")
        return 0
    one = next((r for r in found if r.number == args.merge), None)
    if one is None:
        print(f"merge-approved: {mark}{args.merge} is not an open request in {repo}", file=sys.stderr)
        return 1
    if one.blockers:
        print(f"merge-approved: {mark}{one.number} not merged: {'; '.join(one.blockers)}", file=sys.stderr)
        return 1
    if github:
        # ponytail: squash, as this plugin's own repo merges; a [workflow] merge_method when a workspace differs.
        code, _, err = gh(["pr", "merge", str(one.number), "-R", repo, "--squash", "--match-head-commit", one.head])
        err = err.strip() if code else ""
    else:
        _, _, err = call("PUT", f"{_gitlab_base(home, repo)}/merge_requests/{one.number}/merge", secret,
                         backlog.TIMEOUT, {"sha": one.head})
    if err:
        print(f"merge-approved: {mark}{one.number} not merged: {err}", file=sys.stderr)
        return 1
    print(f"{mark}{one.number} merged at {one.head[:12]} · {one.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
