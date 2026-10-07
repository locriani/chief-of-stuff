"""`chief-of-stuff merge-ready` prints one verdict per open request, from the head pipeline, never the forge's merge status (#419).

The coordinator wrote "mergeable" from the forge's detailed merge status, which says only that there are no conflicts, so a red
pipeline and a request with open findings were both called mergeable. The command reads the head pipeline (id, status, the sha it
ran on), conflicts and unresolved review threads, and prints one verdict: ready | red | running | conflicts | findings open |
stale pipeline | no pipeline. It never merges and needs no approver or merge_owner setting.
"""

from __future__ import annotations

import dataclasses
import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

import chief_of_stuff as cli  # noqa: E402
import merge_approved as ma  # noqa: E402
import merge_ready as mr  # noqa: E402
import run  # noqa: E402
import test_merge_approved as tma  # noqa: E402  (module-qualified: don't re-collect its TestCases)

HEAD, OLD, OK = tma.HEAD, tma.OLD, tma.OK
RED = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]
RUNNING = [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}]

# The one line per request, as merge-ready prints it. A request with no pipeline says `pipeline none`.
LINE = re.compile(
    r"^(?P<ref>[#!]\d+) (?P<verdict>ready|red|running|conflicts|findings open|stale pipeline|no pipeline)"
    r" · head (?P<head>[0-9a-f]{7})"
    r" · pipeline (?:(?P<pid>\S+) (?P<status>passed|failed|running) on (?P<psha>[0-9a-f]{7})|none)"
    r" · conflicts (?P<conflicts>yes|no)"
    r" · unresolved threads (?P<threads>\d+)"
    r" · (?P<url>\S+)$")


def gpr(number, checks=OK, mergeable="MERGEABLE"):
    return tma.pr(number, [], checks=checks, mergeable=mergeable)


# number -> (the pull request, the graphql node, the verdict)
GITHUB = {
    21: (gpr(21), tma.graphql_node(21), "ready"),
    22: (gpr(22, RED), tma.graphql_node(22), "red"),
    23: (gpr(23, RUNNING), tma.graphql_node(23), "running"),
    24: (gpr(24, mergeable="CONFLICTING"), tma.graphql_node(24), "conflicts"),
    25: (gpr(25, RED, "CONFLICTING"), tma.graphql_node(25, [False]), "conflicts"),
    26: (gpr(26), tma.graphql_node(26, sha=OLD), "stale pipeline"),
    27: (gpr(27, []), tma.graphql_node(27, run=False), "no pipeline"),
    28: (gpr(28), tma.graphql_node(28, [False, True, False]), "findings open"),
    29: (gpr(29, RED), tma.graphql_node(29, [False, False], sha=OLD), "red"),
    30: (gpr(30, RUNNING), tma.graphql_node(30, [False]), "running"),
    31: (gpr(31, []), tma.graphql_node(31, [False], run=False), "no pipeline"),
    32: (gpr(32), tma.graphql_node(32, [False], sha=OLD), "stale pipeline"),
    33: (gpr(33, mergeable="UNKNOWN"), tma.graphql_node(33), "running"),
    34: (gpr(34), tma.graphql_node(34, [True, True]), "ready"),
}


def github_gh(numbers=None):
    keep = [GITHUB[n] for n in (numbers or GITHUB)]
    return tma.FakeGhGraphql([p for p, _, _ in keep], [n for _, n, _ in keep])


class GitHubReadyTest(unittest.TestCase):
    def test_every_verdict_in_the_precedence_list(self):
        got = {r.number: r.verdict for r in mr.github_ready("o/app", github_gh())}
        self.assertEqual(got, {n: v for n, (_, _, v) in GITHUB.items()})

    def test_the_request_carries_the_head_pipeline_id_and_the_sha_it_ran_on(self):
        by = {r.number: r for r in mr.github_ready("o/app", github_gh())}
        self.assertEqual((by[21].head, by[21].pipeline_id, by[21].pipeline_sha, by[21].pipeline), (HEAD, "9021", HEAD, "passed"))
        self.assertEqual((by[22].pipeline_id, by[22].pipeline), ("9022", "failed"))
        self.assertEqual((by[26].head, by[26].pipeline_sha), (HEAD, OLD), "a green pipeline on an old sha is stale")
        self.assertEqual((by[27].pipeline, by[27].pipeline_id, by[27].pipeline_sha), ("none", "", ""))

    def test_only_unresolved_threads_count(self):
        by = {r.number: r.threads for r in mr.github_ready("o/app", github_gh())}
        self.assertEqual((by[21], by[28], by[34]), (0, 2, 0))

    def test_a_pipeline_that_passed_on_an_old_sha_is_never_ready(self):
        by = {r.number: r for r in mr.github_ready("o/app", github_gh([26]))}
        self.assertEqual(by[26].pipeline, "passed")
        self.assertEqual(by[26].verdict, "stale pipeline")

    def test_a_failed_read_raises_so_the_command_can_say_so(self):
        class Down:
            def __call__(self, args, input_text=None):
                return 1, "", "HTTP 502"
        with self.assertRaises(RuntimeError):
            mr.github_ready("o/app", Down())


def gitlab_fake(mrs):
    """The merge request, approvals and discussions endpoints; `mrs` maps iid to its spec."""
    def call(method, url, token, timeout, payload=None):
        path = url.split("/api/v4/projects/team%2Fapp", 1)[1]
        if path.startswith("/merge_requests?"):
            return [{"iid": i} for i in mrs], {}, ""
        iid = int(path.split("/")[2])
        m = mrs[iid]
        if path.endswith("/approvals"):
            return {"approved_by": []}, {}, ""
        if "/discussions" in path:
            return m.get("discussions", []), {}, ""
        pipeline = m.get("pipeline", "success")
        return ({"iid": iid, "title": "MR", "web_url": f"https://labs.example.test/team/app/-/merge_requests/{iid}", "sha": HEAD,
                 "has_conflicts": m.get("conflict", False), "detailed_merge_status": m.get("status", "mergeable"),
                 "head_pipeline": None if pipeline is None else {"id": 7000 + iid, "status": pipeline, "sha": m.get("pipeline_sha", HEAD)}},
                {}, "")
    return call


def thread(resolved=False, resolvable=True, system=False):
    return {"id": f"d{id(object())}", "notes": [{"resolvable": resolvable, "resolved": resolved, "system": system}]}


GITLAB = {
    5: ({}, "ready"),
    6: ({"pipeline": "failed"}, "red"),
    7: ({"pipeline": "running"}, "running"),
    8: ({"conflict": True, "status": "conflict"}, "conflicts"),
    9: ({"pipeline_sha": OLD}, "stale pipeline"),
    10: ({"pipeline": None}, "no pipeline"),
    11: ({"discussions": [thread(), thread(), thread(resolved=True)]}, "findings open"),
    12: ({"status": "checking"}, "running"),
    13: ({"pipeline": "failed", "discussions": [thread()]}, "red"),
    14: ({"discussions": [thread(resolvable=False), thread(system=True)]}, "ready"),
}


class GitLabReadyTest(unittest.TestCase):
    HOME = ma.backlog.Backlog("https://labs.example.test", "team/app")

    def read(self, numbers=None):
        mrs = {n: GITLAB[n][0] for n in (numbers or GITLAB)}
        return {r.number: r for r in mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake(mrs))}

    def test_every_verdict_in_the_precedence_list(self):
        self.assertEqual({n: r.verdict for n, r in self.read().items()}, {n: v for n, (_, v) in GITLAB.items()})

    def test_the_request_carries_the_head_pipeline_id_and_the_sha_it_ran_on(self):
        got = self.read()
        self.assertEqual((got[5].head, got[5].pipeline_id, got[5].pipeline_sha, got[5].pipeline), (HEAD, "7005", HEAD, "passed"))
        self.assertEqual((got[9].pipeline_id, got[9].pipeline_sha), ("7009", OLD))
        self.assertEqual((got[10].pipeline, got[10].pipeline_id, got[10].pipeline_sha), ("none", "", ""))

    def test_only_unresolved_resolvable_threads_count(self):
        got = self.read()
        self.assertEqual((got[5].threads, got[11].threads, got[14].threads), (0, 2, 0))


class AgreementTest(unittest.TestCase):
    """One place decides ready: merge-approved's blockers and merge-ready's verdict agree on every request merge-ready reads."""

    def requests(self):
        gl = GitLabReadyTest()
        found = list(mr.github_ready("o/app", github_gh())) + list(gl.read().values())
        return [dataclasses.replace(r, approval="approved", approver="robin") for r in found]

    def test_an_approved_request_has_no_blockers_exactly_when_its_verdict_is_ready(self):
        for r in self.requests():
            with self.subTest(number=r.number, verdict=r.verdict, blockers=r.blockers):
                self.assertEqual(r.blockers == [], r.verdict == "ready")

    def test_the_words_merge_approved_already_says_stay(self):
        said = {"red": "pipeline failed", "running": "pipeline running", "conflicts": "conflict"}
        for r in self.requests():
            if r.verdict in said:
                with self.subTest(number=r.number, verdict=r.verdict):
                    self.assertIn(said[r.verdict], r.blockers)


class LineTest(unittest.TestCase):
    def test_a_line_per_request_in_the_pinned_format(self):
        by = {r.number: r for r in mr.github_ready("o/app", github_gh())}
        got = LINE.match(mr.line(by[28], "#"))
        self.assertIsNotNone(got, mr.line(by[28], "#"))
        self.assertEqual((got["ref"], got["verdict"], got["head"], got["pid"], got["status"], got["psha"], got["conflicts"], got["threads"]),
                         ("#28", "findings open", "aaaaaaa", "9028", "passed", "aaaaaaa", "no", "2"))
        self.assertEqual(got["url"], "https://github.com/o/app/pull/28")

    def test_the_stale_line_shows_both_shas_and_the_conflict_line_says_yes(self):
        by = {r.number: r for r in mr.github_ready("o/app", github_gh())}
        stale, conflict, none = (LINE.match(mr.line(by[n], "#")) for n in (26, 24, 27))
        self.assertEqual((stale["head"], stale["psha"], stale["verdict"]), ("aaaaaaa", "bbbbbbb", "stale pipeline"))
        self.assertEqual(conflict["conflicts"], "yes")
        self.assertIn("pipeline none", mr.line(by[27], "#"))
        self.assertEqual(none["verdict"], "no pipeline")


class MainTest(unittest.TestCase):
    GITHUB_ROOT = ("## Coordinator\n- User: Robin\n- Timezone: America/Chicago\n- Daily log dir: `daily/`\n"
                   "- Tracker: `daily/<date>-tracker.md`\n- Settings: `chief-of-stuff.toml`\n- Backlog: GitHub issues; repo o/app\n")
    GITLAB_ROOT = GITHUB_ROOT.replace("GitHub issues; repo o/app", "GitLab; host https://labs.example.test; project team/app")

    def workspace(self, toml="", claude=GITHUB_ROOT):
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(claude)
        (root / "chief-of-stuff.toml").write_text(toml)
        return root

    def run_main(self, root, *args, gh=None, call=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mr.main(["--root", str(root), *args], gh=gh or github_gh(), call=call)
        return code, out.getvalue(), err.getvalue()

    def test_it_prints_one_line_per_open_request_by_number(self):
        gh = github_gh()
        code, out, err = self.run_main(self.workspace(), gh=gh)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), len(GITHUB))
        for line in lines:
            self.assertRegex(line, LINE)
        self.assertEqual([l.split()[0] for l in lines], [f"#{n}" for n in sorted(GITHUB)])
        self.assertEqual({l.split()[0]: LINE.match(l)["verdict"] for l in lines}, {f"#{n}": v for n, (_, _, v) in GITHUB.items()})

    def test_it_never_merges(self):
        gh = github_gh()
        self.run_main(self.workspace(), gh=gh)
        self.assertEqual(gh.merges, [])
        with self.assertRaises(SystemExit):  # no --merge flag: the tool only reports
            self.run_main(self.workspace(), "--merge", "21", gh=gh)
        self.assertEqual(gh.merges, [])

    def test_it_needs_no_merge_owner_or_approver_setting(self):
        for toml in ("", '[workflow]\nmerge_owner = "user"\n', '[workflow]\nmerge_owner = "worker"\n',
                     '[workflow]\nmerge_owner = "approval"\napprover = "robin"\n'):
            with self.subTest(toml=toml):
                code, out, err = self.run_main(self.workspace(toml), gh=github_gh([21, 22]))
                self.assertEqual((code, len(out.splitlines())), (0, 2), err)

    def test_repo_names_the_repository_it_reads(self):
        gh = github_gh([21])
        self.run_main(self.workspace(), "--repo", "o/other", gh=gh)
        listed = [c for c in gh.calls if c[:2] == ["pr", "list"]]
        self.assertTrue(listed and all("o/other" in c for c in listed), gh.calls)
        self.assertFalse([c for c in gh.calls if "name=app" in c], "the graphql read follows --repo too")

    def test_a_forge_that_cannot_be_read_is_an_error_not_a_verdict(self):
        class Down:
            def __call__(self, args, input_text=None):
                return 1, "", "HTTP 502"
        code, out, err = self.run_main(self.workspace(), gh=Down())
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("merge-ready:"), err)

    def test_a_gitlab_workspace_prints_merge_requests_with_a_bang(self):
        call = gitlab_fake({n: GITLAB[n][0] for n in (5, 6, 9, 11)})
        with mock.patch.object(ma.backlog, "token", return_value="t"):
            code, out, err = self.run_main(self.workspace(claude=self.GITLAB_ROOT), call=call)
        self.assertEqual(code, 0, err)
        got = {l.split()[0]: LINE.match(l) for l in out.splitlines()}
        self.assertEqual({k: m["verdict"] for k, m in got.items()}, {"!5": "ready", "!6": "red", "!9": "stale pipeline", "!11": "findings open"})
        self.assertEqual((got["!9"]["head"], got["!9"]["psha"], got["!6"]["pid"], got["!11"]["threads"]), ("aaaaaaa", "bbbbbbb", "7006", "2"))


class RegisteredLikeMergeApprovedTest(unittest.TestCase):
    def test_the_installed_command_runs_the_script(self):
        self.assertEqual(cli.COMMANDS["merge-ready"], "scripts/merge_ready.py")

    def test_the_eval_harness_allows_the_command_and_the_script(self):
        self.assertIn("merge-ready", run.OPERATIONS)
        self.assertIn("merge_ready.py", run.SCRIPTS)
        self.assertIn("Bash(chief-of-stuff merge-ready:*)", run.allowed_tools(ROOT))


if __name__ == "__main__":
    unittest.main()
