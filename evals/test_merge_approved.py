"""The coordinator merges only what the configured approver approved, on a passing, mergeable head."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_approved as ma  # noqa: E402
import settings as st  # noqa: E402

HEAD = "a" * 40
OLD = "b" * 40
OK = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]


def review(login, state, oid=HEAD, at="2026-09-26T01:00:00Z"):
    return {"author": {"login": login}, "state": state, "commit": {"oid": oid}, "submittedAt": at}


def pr(number, reviews, checks=OK, mergeable="MERGEABLE", head=HEAD):
    return {"number": number, "title": f"PR {number}", "url": f"https://github.com/o/app/pull/{number}",
            "headRefOid": head, "reviews": reviews, "statusCheckRollup": checks, "mergeable": mergeable}


PRS = [
    pr(12, [review("robin", "APPROVED")]),
    pr(13, [review("peer-bot", "APPROVED")]),
    pr(14, [review("robin", "APPROVED", oid=OLD)]),
    pr(15, [review("robin", "APPROVED", at="2026-09-26T01:00:00Z"),
            review("robin", "CHANGES_REQUESTED", at="2026-09-26T02:00:00Z")]),
    pr(16, [review("robin", "APPROVED")], checks=[{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]),
    pr(17, [review("robin", "APPROVED")], mergeable="CONFLICTING"),
    pr(18, [review("robin", "APPROVED")], checks=[{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}]),
    pr(19, [review("robin", "APPROVED"), review("robin", "COMMENTED", at="2026-09-26T03:00:00Z")]),
]


class FakeGh:
    def __init__(self, prs):
        self.prs, self.calls = prs, []

    def __call__(self, args, input_text=None):
        self.calls.append(args)
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps(self.prs), ""
        if args[:2] == ["pr", "merge"]:
            return 0, "", ""
        return 1, "", "unexpected"

    @property
    def merges(self):
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]


class GitHubReadinessTest(unittest.TestCase):
    def test_only_the_approvers_approval_on_the_head_with_passing_checks_and_no_conflict_is_ready(self):
        got = {r.number: r.blockers for r in ma.github_requests("o/app", "robin", FakeGh(PRS))}
        self.assertEqual(got[12], [])
        self.assertEqual(got[19], [], "a later comment does not withdraw an approval")
        self.assertEqual(got[13], ["no approval from robin"])
        self.assertEqual(got[14], ["robin approved an earlier commit"])
        self.assertEqual(got[15], ["no approval from robin"])
        self.assertEqual(got[16], ["pipeline failed"])
        self.assertEqual(got[17], ["conflict"])
        self.assertEqual(got[18], ["pipeline running"])


class GitLabReadinessTest(unittest.TestCase):
    def fake(self, mrs):
        calls = []

        def call(method, url, token, timeout, payload=None):
            calls.append((method, url, payload))
            path = url.split("/api/v4/projects/team%2Fapp", 1)[1]
            if method == "GET" and path.startswith("/merge_requests?"):
                return [{"iid": m["iid"]} for m in mrs], {}, ""
            iid = int(path.split("/")[2])
            m = next(x for x in mrs if x["iid"] == iid)
            if path.endswith("/approvals"):
                return {"approved_by": [{"user": {"username": u}} for u in m["approved_by"]]}, {}, ""
            if method == "PUT":
                return {"state": "merged"}, {}, ""
            return {"iid": iid, "title": "MR", "web_url": "u", "sha": HEAD, "has_conflicts": m.get("conflict", False),
                    "detailed_merge_status": m.get("status", "mergeable"),
                    "head_pipeline": {"status": m.get("pipeline", "success")}}, {}, ""
        return call, calls

    def test_a_gitlab_mr_needs_the_approver_a_passing_pipeline_and_mergeable(self):
        call, _ = self.fake([{"iid": 5, "approved_by": ["robin"]}, {"iid": 6, "approved_by": ["peer-bot"]},
                             {"iid": 7, "approved_by": ["robin"], "pipeline": "running"},
                             {"iid": 8, "approved_by": ["robin"], "conflict": True, "status": "conflict"}])
        home = ma.backlog.Backlog("https://labs.example.test", "team/app")
        got = {r.number: r.blockers for r in ma.gitlab_requests(home, "team/app", "robin", "t", call)}
        self.assertEqual(got, {5: [], 6: ["no approval from robin"], 7: ["pipeline running"], 8: ["conflict"]})


class MainTest(unittest.TestCase):
    def workspace(self, toml):
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Timezone: America/Chicago\n"
            "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            "- Settings: `chief-of-stuff.toml`\n- Backlog: GitHub issues; repo o/app\n")
        (root / "chief-of-stuff.toml").write_text(toml)
        return root

    def run_main(self, root, *args, gh):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ma.main(["--root", str(root), *args], gh=gh)
        return code, out.getvalue(), err.getvalue()

    OPT_IN = '[workflow]\nmerge_owner = "approval"\napprover = "robin"\n'

    def test_merge_merges_a_ready_pr_at_its_checked_head(self):
        gh = FakeGh(PRS)
        code, out, _ = self.run_main(self.workspace(self.OPT_IN), "--merge", "12", gh=gh)
        self.assertEqual(code, 0)
        self.assertEqual(len(gh.merges), 1)
        self.assertIn("12", gh.merges[0])
        self.assertEqual(gh.merges[0][gh.merges[0].index("--match-head-commit") + 1], HEAD)
        self.assertIn("merged", out)

    def test_merge_refuses_a_peer_approval_and_says_why(self):
        gh = FakeGh(PRS)
        code, _, err = self.run_main(self.workspace(self.OPT_IN), "--merge", "13", gh=gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.merges, [])
        self.assertIn("no approval from robin", err)

    def test_the_listing_marks_each_ready_or_blocked(self):
        code, out, _ = self.run_main(self.workspace(self.OPT_IN), gh=FakeGh(PRS))
        self.assertEqual(code, 0)
        line = {l.split()[0]: l for l in out.splitlines() if l.startswith("#")}
        self.assertIn("ready", line["#12"])
        self.assertIn("pipeline failed", line["#16"])

    def test_without_the_opt_in_merging_stays_the_users(self):
        gh = FakeGh(PRS)
        code, _, err = self.run_main(self.workspace('[workflow]\nmerge_owner = "user"\n'), "--merge", "12", gh=gh)
        self.assertEqual(code, 2)
        self.assertEqual(gh.calls, [])
        self.assertIn("merge_owner", err)


class SettingsTest(unittest.TestCase):
    def load(self, text):
        root = Path(tempfile.mkdtemp())
        (root / "s.toml").write_text(text)
        return st.load(root, "s.toml").workflow

    def test_approval_needs_an_approver_and_pull_requests(self):
        wf = self.load('[workflow]\nmerge_owner = "approval"\napprover = "robin"\n')
        self.assertEqual((wf.merge_owner, wf.approver), ("approval", "robin"))
        for bad in ('merge_owner = "approval"', 'merge_owner = "approval"\napprover = ""',
                    'delivery = "branch"\nmerge_owner = "approval"\napprover = "robin"'):
            with self.assertRaises(st.SettingsError, msg=bad):
                self.load("[workflow]\n" + bad + "\n")


if __name__ == "__main__":
    unittest.main()
