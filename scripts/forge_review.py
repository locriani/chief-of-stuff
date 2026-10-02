"""How a forge's review state reads: whether the approver approved a pull or merge request's head, and how its
pipeline stands. merge_approved.py merges on it, review_threads.py replies on it, and board_sources.py caches it
for the pages.
"""

from __future__ import annotations

from pathlib import Path

import backlog
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


def forge(root: Path, repo: str | None) -> tuple[Workflow, backlog.Backlog, bool, str]:
    """The workspace's workflow settings, its forge, whether that is GitHub, and the repo to act on."""
    cfg = read_config(root)
    workflow = load_settings(root, cfg.settings_path).workflow
    home = cfg.backlog
    if home is None:
        raise ConfigError("no Backlog: line names the forge")
    github = isinstance(home, backlog.GitHubBacklog)
    return workflow, home, github, repo or (home.repo if github else home.project)
