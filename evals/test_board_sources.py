"""board_sources.py: the one place the pages get forge and worker facts, cached beside them."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import board_sources as bs  # noqa: E402

ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 26, 2, 10, tzinfo=ZONE)
DAY = NOW.date().isoformat()
HEAD = "a" * 40

TRACKER = f"""# Tracker {DAY}

## Tasks

| name | item | owner | state | since | due | size | issue | checklist |
|---|---|---|---|---|---|---|---|---|
| Rate limit | Rate limit headers, PR #58 | impl-1 | running 01:28 | 01:28 |  | M | #118 |  |
| Search | Search pagination | Robin | waiting | 01:00 |  | S | {{issue}} | {{mr}} |

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| abc123 | reviewer-2 | working | review of #58 | nothing |  |  | none | 01:58 reviewing |

## Log

- 01:28 one-shot impl-1 started: claude opus, worktree `rate-limit`, task Rate limit headers, PR #58
"""


def workspace(backlog: str, issue: str = "#115", mr: str = "") -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "CLAUDE.md").write_text(
        "## Coordinator\n- User: Robin\n- Timezone: America/Chicago\n- Daily log dir: `daily/`\n"
        "- Tracker: `daily/<date>-tracker.md`\n- Worktrees: `trees/`\n"
        "- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
        f"- Settings: `cos.toml`\n- Backlog: {backlog}\n")
    (root / "cos.toml").write_text('[workflow]\nmerge_owner = "approval"\napprover = "robin"\n')
    (root / "daily").mkdir()
    (root / "daily" / f"{DAY}-tracker.md").write_text(TRACKER.replace("{issue}", issue).replace("{mr}", mr))
    return root


def gh_pr(number, merged_at=None, state="OPEN"):
    return {"__typename": "PullRequest", "number": number, "url": f"https://github.com/o/app/pull/{number}",
            "title": f"PR {number}", "state": state, "isDraft": False, "mergedAt": merged_at,
            "baseRefName": "main", "body": "Closes #118", "headRefOid": HEAD, "reviewDecision": "APPROVED",
            "files": {"nodes": [{"path": "src/a.py"}]},
            "latestReviews": {"nodes": [{"author": {"login": "robin"}, "state": "APPROVED",
                                         "submittedAt": "2026-09-26T06:00:00Z", "commit": {"oid": HEAD}}]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}}}]}}


def gh_issue(number, labels=("stage:2-implement",)):
    return {"__typename": "Issue", "number": number, "url": f"https://github.com/o/app/issues/{number}",
            "title": f"Issue {number}", "state": "OPEN", "labels": {"nodes": [{"name": x} for x in labels]}}


class FakeGh:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, args, input_text=None):
        self.calls.append(args)
        if self.fail:
            return 1, "", "gh: not logged in"
        query = args[args.index("-f") + 1]
        repo = {"n118": gh_issue(118), "n115": gh_issue(115, ()), "n58": gh_pr(58)}
        data = {"r0": {n: v for n, v in repo.items() if f"{n}: " in query},
                "merged": {"nodes": [gh_pr(41, "2026-09-26T05:41:00Z", "MERGED")]}}
        return 0, json.dumps({"data": data}), ""


class GitHubTest(unittest.TestCase):
    def setUp(self):
        self.root = workspace("GitHub issues; repo o/app")
        self.gh = FakeGh()
        self.got = bs.refresh(self.root, NOW, gh=self.gh)

    def test_one_batched_graphql_call_for_what_the_tracker_names(self):
        self.assertEqual(len(self.gh.calls), 1)
        args = self.gh.calls[0]
        self.assertEqual(args[:2], ["api", "graphql"])
        query = args[args.index("-f") + 1]
        for asked in ("n118: issueOrPullRequest(number: 118)", "n115:", "n58:", "is:merged"):
            self.assertIn(asked, query)

    def test_issues_and_changes_by_ref(self):
        self.assertEqual(self.got.issues["#118"].labels, ("stage:2-implement",))
        pr = self.got.changes["#58"]
        self.assertEqual((pr.state, pr.pipeline, pr.approved, pr.approvals, pr.base, pr.files, pr.issues),
                         ("open", "passed", True, 1, "main", ("src/a.py",), ("#118",)))

    def test_merged_today_is_included(self):
        merged = self.got.changes["#41"]
        self.assertEqual((merged.state, merged.merged_at.astimezone(ZONE).strftime("%H:%M")), ("merged", "00:41"))

    def test_fetched_times_and_the_cache_round_trip(self):
        self.assertEqual(self.got.fetched, {"kanban": NOW, "merge requests": NOW, "workers": NOW})
        self.assertEqual(bs.load(self.root / "pages"), self.got)

    def test_a_failed_forge_is_an_error_and_keeps_the_last_good_facts(self):
        later = NOW.replace(hour=3)
        again = bs.refresh(self.root, later, gh=FakeGh(fail=True))
        self.assertIn("not logged in", again.errors["kanban"])
        self.assertIn("not logged in", again.errors["merge requests"])
        self.assertEqual((again.issues, again.changes), (self.got.issues, self.got.changes))
        self.assertEqual((again.fetched["kanban"], again.fetched["workers"]), (NOW, later))


class GitLabTest(unittest.TestCase):
    def test_one_graphql_call_with_the_backlog_token(self):
        root = workspace("GitLab issues; host https://labs.example.test; project team/app", mr="!54")
        calls = []
        mr = {"iid": "54", "webUrl": "https://labs.example.test/team/app/-/merge_requests/54", "title": "Search",
              "state": "opened", "draft": False, "mergedAt": None, "targetBranch": "main",
              "description": "Closes #115", "approved": False, "approvedBy": {"nodes": [{"username": "robin"}]},
              "headPipeline": {"status": "PENDING"}, "diffStats": [{"path": "src/search.py"}]}
        issue = {"iid": "115", "webUrl": "https://labs.example.test/team/app/-/issues/115", "title": "Search",
                 "state": "opened", "labels": {"nodes": [{"title": "stage:3-pr"}]}}

        def call(method, url, token, timeout, payload=None):
            calls.append((method, url, token, payload))
            return {"data": {"p0": {"issues": {"nodes": [issue]}, "mergeRequests": {"nodes": [mr]},
                                    "merged": {"nodes": []}}}}, {}, ""

        with mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}):
            got = bs.refresh(root, NOW, call=call)
        self.assertEqual([(m, u, t) for m, u, t, _ in calls], [("POST", "https://labs.example.test/api/graphql", "tok")])
        self.assertIn('mergeRequests(iids: ["54"])', calls[0][3]["query"])
        change = got.changes["!54"]
        self.assertEqual((change.state, change.pipeline, change.approved, change.approvals, change.files, change.issues),
                         ("open", "pending", True, 1, ("src/search.py",), ("#115",)))
        self.assertEqual(got.issues["#115"].labels, ("stage:3-pr",))
        self.assertEqual(got.errors, {})

    def test_no_token_is_an_error_not_a_raise(self):
        root = workspace("GitLab issues; host https://labs.example.test; project team/app")
        with mock.patch.object(bs.backlog, "token", return_value=""):
            got = bs.refresh(root, NOW, call=lambda *a, **k: self.fail("no call without a token"))
        self.assertIn("no token", got.errors["kanban"])


class WorkersTest(unittest.TestCase):
    def test_tracker_sessions_and_live_one_shots(self):
        root = workspace("GitHub issues; repo o/app")
        tree = root / "trees" / "rate-limit" / ".chief-of-stuff"
        tree.mkdir(parents=True)
        (tree / "one-shot.pid").write_text(f"{os.getpid()} Rate limit headers, PR #58\n")
        got = bs.refresh(root, NOW, gh=FakeGh())
        one = next(w for w in got.workers if w.kind == "one-shot")
        self.assertEqual((one.name, one.runtime, one.model, one.task, one.tree, one.started.strftime("%H:%M")),
                         ("impl-1", "claude", "opus", "Rate limit headers, PR #58", "rate-limit", "01:28"))
        session = next(w for w in got.workers if w.kind == "session")
        self.assertEqual((session.name, session.task, session.last.strftime("%H:%M")),
                         ("reviewer-2", "review of #58", "01:58"))


class LoadTest(unittest.TestCase):
    def test_absent_or_unreadable_cache_is_empty(self):
        pages = Path(tempfile.mkdtemp())
        self.assertIs(bs.load(pages), bs.EMPTY)
        (pages / ".sources.json").write_text("{not json")
        self.assertIs(bs.load(pages), bs.EMPTY)

    def test_no_coordinator_block_never_raises(self):
        got = bs.refresh(Path(tempfile.mkdtemp()), NOW)
        self.assertIn("config", got.errors)

    def test_cli_prints_a_summary(self):
        root = workspace("GitHub issues; repo o/app")
        bs.refresh(root, NOW, gh=FakeGh())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(bs.main(["--root", str(root)]), 0)
        self.assertIn("changes: 2", out.getvalue())


if __name__ == "__main__":
    unittest.main()
