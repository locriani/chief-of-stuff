"""How a forge's review state reads: whether the approver approved a pull or merge request's head, and how its
pipeline stands, and the request that reading yields. merge_approved.py merges on it, merge_ready.py prints its
verdict, review_threads.py replies on it, and board_sources.py caches it for the pages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import backlog
import backlog_ref
from settings import Workflow, load as load_settings
from workspace import ConfigError, read_config

FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
GITLAB_PIPELINE = {"success": "passed", "failed": "failed", "canceled": "failed"}


def github_approval(reviews: list[dict], approver: str, head: str) -> str:
    # A comment neither grants nor withdraws; the approver's latest approve or request-changes decides.
    mine = sorted((r for r in reviews if (r.get("author") or {}).get("login") == approver
                   and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")),
                  key=lambda r: r.get("submittedAt") or "")
    if not mine or mine[-1]["state"] != "APPROVED":
        return "none"
    return "approved" if (mine[-1].get("commit") or {}).get("oid") == head else "stale"


def github_pipeline(checks: list[dict]) -> str:
    if not checks:
        return "none"
    if any((c.get("conclusion") or c.get("state") or "").upper() in FAILED for c in checks):
        return "failed"
    running = [c for c in checks if (c.get("__typename") == "CheckRun" and c.get("status") != "COMPLETED")
               or (c.get("__typename") != "CheckRun" and c.get("state") in ("PENDING", "EXPECTED"))]
    return "running" if running else "passed"


def forge(root: Path, repo: str | None) -> tuple[Workflow, backlog_ref.Backlog, bool, str]:
    """The workspace's workflow settings, its forge, whether that is GitHub, and the repo to act on."""
    cfg = read_config(root)
    workflow = load_settings(root, cfg.settings_path).workflow
    home = cfg.backlog
    if home is None:
        raise ConfigError("no Backlog: line names the forge")
    github = isinstance(home, backlog_ref.GitHubBacklog)
    return workflow, home, github, repo or (home.repo if github else home.project)


NO_PIPELINE = "no pipeline"


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
    pipeline_id: str = ""   # the head pipeline's id; "" when none or unread
    pipeline_sha: str = ""  # the commit that pipeline ran on; "" when unread, which never counts as stale
    threads: int = 0        # unresolved review threads

    @property
    def verdict(self) -> str:
        """The one place that decides ready (#419), from the head pipeline and never the forge's own merge word."""
        if self.mergeable == "conflict":
            return "conflicts"
        if self.pipeline == "failed":
            return "red"
        if self.pipeline != "none" and self.pipeline_sha and self.pipeline_sha != self.head:
            return "stale pipeline"
        if self.pipeline == "running" or self.mergeable != "yes":
            return "running"
        if self.pipeline == "none":
            return NO_PIPELINE
        return "findings open" if self.threads else "ready"

    @property
    def blockers(self) -> list[str]:
        out = {"none": [f"no approval from {self.approver}"],
               "stale": [f"{self.approver} approved an earlier commit"]}.get(self.approval, [])
        if self.pipeline in ("failed", "running"):
            out.append(f"pipeline {self.pipeline}")
        if self.mergeable != "yes":
            out.append("conflict" if self.mergeable == "conflict" else f"not mergeable: {self.mergeable}")
        if self.verdict == "running" and "pipeline running" not in out:
            out.append("pipeline running")  # the forge has not settled the head's checks yet
        for verdict, why in (("stale pipeline", "pipeline ran on an earlier commit"), (NO_PIPELINE, NO_PIPELINE),
                             ("findings open", f"{self.threads} unresolved threads")):
            if self.verdict == verdict:
                out.append(why)
        return out


def github_requests(repo: str, approver: str, gh=backlog.run_gh) -> list[Request]:
    code, out, err = gh(["pr", "list", "-R", repo, "--state", "open", "--limit", "100", "--json",
                         "number,title,url,headRefOid,reviews,statusCheckRollup,mergeable"])
    if code != 0:
        raise RuntimeError(f"gh pr list: {err.strip() or code}")
    return [Request(p["number"], p["title"], p["url"], p["headRefOid"],
                    github_approval(p.get("reviews") or [], approver, p["headRefOid"]),
                    github_pipeline(p.get("statusCheckRollup") or []),
                    {"MERGEABLE": "yes", "CONFLICTING": "conflict"}.get(p.get("mergeable"), "unknown"), approver)
            for p in json.loads(out)]


def gitlab_requests(home: backlog.Backlog, project: str, approver: str, token: str, call=backlog._call) -> list[Request]:
    # ponytail: GitLab's approvals API names no commit, so an approval counts for the head only when the
    # project resets approvals on push. Check that project setting once; a per-note sha check if it is off.
    base = home.api(project)
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
        head = mr.get("head_pipeline") or {}
        pipeline = head.get("status")
        status = mr.get("detailed_merge_status") or ""
        out.append(Request(iid, mr.get("title", ""), mr.get("web_url", ""), mr.get("sha", ""),
                           "approved" if approver in names else "none",
                           "none" if pipeline is None else GITLAB_PIPELINE.get(pipeline, "running"),
                           "conflict" if mr.get("has_conflicts") else ("yes" if status == "mergeable" else status),
                           approver, str(head.get("id") or ""), head.get("sha") or ""))
    return out
