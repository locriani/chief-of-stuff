"""Stage-label walks preserve unrelated issue labels and never write on preview."""

from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backlog  # noqa: E402
import audit_tasks  # noqa: E402
import kanban  # noqa: E402
from settings import GitHubProject, Kanban  # noqa: E402


FLOW = Kanban(
    stages=("00 - PLAN", "01 - PLAN REVIEW", "02 - TEST", "03 - BUILD", "04 - REVIEW", "05 - FIX"),
    human_review_label="!! - HUMAN REVIEW REQUIRED",
    stage_map={"plan": 0, "plan review": 1, "test": 2, "implement": 3,
               "review": 4, "triage": 4, "fix": 5, "verify": 4, "merge": 4},
    hold_stages=("plan review", "triage", "merge"),
    terminal_stages=("main",),
)


class LabelDeltaTest(unittest.TestCase):
    def test_plan_review_adds_hold_and_replaces_numbered_stage(self):
        delta = kanban.label_delta(FLOW, ("00 - PLAN", "kind::code"), "plan review")
        self.assertEqual(delta.add, ("01 - PLAN REVIEW", "!! - HUMAN REVIEW REQUIRED"))
        self.assertEqual(delta.remove, ("00 - PLAN",))

    def test_review_fix_review_loop_clears_and_readds_hold(self):
        review = kanban.label_delta(FLOW, ("04 - REVIEW", "!! - HUMAN REVIEW REQUIRED"), "fix")
        self.assertEqual(review.add, ("05 - FIX",))
        self.assertEqual(review.remove, ("04 - REVIEW", "!! - HUMAN REVIEW REQUIRED"))
        verify = kanban.label_delta(FLOW, ("05 - FIX", "severity::high"), "verify")
        self.assertEqual(verify.add, ("04 - REVIEW",))
        self.assertEqual(verify.remove, ("05 - FIX",))

    def test_repeated_move_is_noop_and_unrelated_labels_survive(self):
        labels = ("04 - REVIEW", "kind::code", "severity::high")
        delta = kanban.label_delta(FLOW, labels, "review")
        self.assertEqual((delta.add, delta.remove), ((), ()))
        self.assertIn("severity::high", kanban.after(labels, delta))

    def test_duplicate_stage_labels_are_cleaned_in_one_move(self):
        delta = kanban.label_delta(FLOW, ("00 - PLAN", "03 - BUILD", "kind::code"), "implement")
        self.assertEqual(delta.remove, ("00 - PLAN",))
        self.assertEqual(delta.add, ())

    def test_drift_names_missing_or_duplicate_stage_without_changing_it(self):
        self.assertIn("expected 03 - BUILD", kanban.drift(FLOW, ("kind::code",), "implement"))
        self.assertIn("00 - PLAN", kanban.drift(FLOW, ("00 - PLAN", "03 - BUILD"), "implement"))
        self.assertEqual(kanban.drift(FLOW, ("03 - BUILD", "kind::code"), "implement"), "")

    def test_waiting_one_shot_may_hold_without_moving_its_stage(self):
        labels = ("03 - BUILD", "!! - HUMAN REVIEW REQUIRED", "kind::code")
        self.assertEqual(kanban.drift(FLOW, labels, "implement", allow_extra_hold=True), "")
        self.assertIn("!! - HUMAN REVIEW REQUIRED", kanban.drift(FLOW, labels, "implement"))


class HumanHoldTest(unittest.TestCase):
    def test_github_adds_only_hold_and_verifies(self):
        ref = backlog.IssueRef("team/repo", 9)
        calls = []

        def gh(args):
            calls.append(args)
            if args[:2] == ["issue", "view"]:
                held = any(cmd[:2] == ["issue", "edit"] for cmd in calls)
                labels = ["03 - BUILD", "kind::code"] + ([FLOW.human_review_label] if held else [])
                return 0, json.dumps({"labels": [{"name": x} for x in labels]}), ""
            if args[:2] == ["label", "list"]:
                return 0, json.dumps([{"name": FLOW.human_review_label}]), ""
            return 0, "", ""

        self.assertEqual(kanban.add_human_hold(ref, backlog.GitHubBacklog("team/repo"), FLOW, gh=gh), "")
        edits = [cmd for cmd in calls if cmd[:2] == ["issue", "edit"]]
        self.assertEqual(edits, [["issue", "edit", "9", "-R", "team/repo", "--add-label",
                                  FLOW.human_review_label]])

    def test_gitlab_adds_only_hold_and_verifies(self):
        home = backlog.Backlog("https://labs.example.test", "team/app")
        ref = backlog.IssueRef("team/app", 7, "labs.example.test")
        before = {"labels": ["03 - BUILD", "kind::code"]}
        after = {"labels": [*before["labels"], FLOW.human_review_label]}
        with mock.patch.object(kanban.backlog, "_get", side_effect=[(before, {}, ""), (after, {}, "")]), \
             mock.patch.object(kanban.backlog, "existing_labels", return_value=({FLOW.human_review_label}, "")), \
             mock.patch.object(kanban.backlog, "_call", return_value=(after, {}, "")) as write:
            self.assertEqual(kanban.add_human_hold(ref, home, FLOW, token="test"), "")
        self.assertEqual(write.call_args.args[-1], {"add_labels": FLOW.human_review_label})


class GitLabUpdaterTest(unittest.TestCase):
    def setUp(self):
        self.cfg = backlog.Backlog("https://labs.example.test", "team/app")
        self.ref = backlog.IssueRef("team/app", 7, "labs.example.test")
        self.before = {"iid": 7, "state": "opened", "labels": ["00 - PLAN", "kind::code"]}
        self.after = {"iid": 7, "state": "opened", "labels": ["01 - PLAN REVIEW", "!! - HUMAN REVIEW REQUIRED", "kind::code"]}

    def test_dry_run_reads_but_does_not_write(self):
        with mock.patch.object(kanban.backlog, "_get", return_value=(self.before, {}, "")), \
             mock.patch.object(kanban.backlog, "_call") as write:
            got = kanban.sync_gitlab(self.cfg, self.ref, FLOW, "plan review", token="test", commit=False)
        self.assertFalse(got.done)
        self.assertTrue(got.ok)
        self.assertEqual(got.delta.add, ("01 - PLAN REVIEW", "!! - HUMAN REVIEW REQUIRED"))
        write.assert_not_called()

    def test_commit_updates_only_managed_labels_and_verifies(self):
        with mock.patch.object(kanban.backlog, "_get", side_effect=[(self.before, {}, ""), (self.after, {}, "")]), \
             mock.patch.object(kanban.backlog, "existing_labels", return_value=(set(FLOW.stages) | {FLOW.human_review_label}, "")), \
             mock.patch.object(kanban.backlog, "_call", return_value=(self.after, {}, "")) as write:
            got = kanban.sync_gitlab(self.cfg, self.ref, FLOW, "plan review", token="test", commit=True,
                                     expected_stage="plan")
        self.assertTrue(got.done, got.error)
        method, _, _, _, payload = write.call_args.args
        self.assertEqual(method, "PUT")
        self.assertEqual(payload, {"add_labels": "01 - PLAN REVIEW,!! - HUMAN REVIEW REQUIRED",
                                   "remove_labels": "00 - PLAN"})

    def test_refuses_missing_configured_label_without_writing(self):
        with mock.patch.object(kanban.backlog, "_get", return_value=(self.before, {}, "")), \
             mock.patch.object(kanban.backlog, "existing_labels", return_value=({"00 - PLAN"}, "")), \
             mock.patch.object(kanban.backlog, "_call") as write:
            got = kanban.sync_gitlab(self.cfg, self.ref, FLOW, "plan review", token="test", commit=True,
                                     expected_stage="plan")
        self.assertIn("missing label", got.error)
        write.assert_not_called()

    def test_manual_move_is_reported_before_write(self):
        moved = dict(self.before, labels=["05 - FIX", "kind::code"])
        with mock.patch.object(kanban.backlog, "_get", return_value=(moved, {}, "")), \
             mock.patch.object(kanban.backlog, "_call") as write:
            got = kanban.sync_gitlab(self.cfg, self.ref, FLOW, "plan review", token="test", commit=True,
                                     expected_stage="plan")
        self.assertIn("changed outside the tracker", got.error)
        write.assert_not_called()

    def test_invalid_prior_stage_is_refused_without_writing(self):
        with mock.patch.object(kanban.backlog, "_get") as read, \
             mock.patch.object(kanban.backlog, "_call") as write:
            got = kanban.sync_gitlab(self.cfg, self.ref, FLOW, "plan review", token="test", commit=True,
                                     expected_stage="invented")
        self.assertIn("unmapped prior stage", got.error)
        read.assert_not_called()
        write.assert_not_called()


class GitHubUpdaterTest(unittest.TestCase):
    def setUp(self):
        self.ref = backlog.IssueRef("example/app", 7)
        self.config = Kanban(FLOW.stages, FLOW.human_review_label, FLOW.stage_map,
                             FLOW.hold_stages, FLOW.terminal_stages,
                             GitHubProject("example", 3, "Status", ("Plan", "Plan Review", "Test", "Build", "Review", "Fix")))
        self.labels = ["kind::code"]
        self.status = "Plan"
        self.present = True
        self.calls = []

    def gh(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "view"]:
            return 0, json.dumps({"state": "OPEN", "url": self.ref.url,
                "labels": [{"name": x} for x in self.labels]}), ""
        if args[:2] == ["label", "list"]:
            return 0, json.dumps([{"name": x} for x in (*self.config.stages, self.config.human_review_label)]), ""
        if args[:2] == ["project", "field-list"]:
            return 0, json.dumps({"fields": [{"name": "Status", "options":
                [{"name": x} for x in self.config.github_project.options]}]}), ""
        if args[:2] == ["project", "item-list"]:
            items = ([{"content": {"url": self.ref.url}, "status": self.status}] if self.present else [])
            return 0, json.dumps({"items": items, "totalCount": len(items)}), ""
        if args[:2] == ["project", "item-add"]:
            self.present = True
            return 0, "{}", ""
        if args[:2] == ["project", "item-edit"]:
            self.status = args[args.index("--value") + 1]
            return 0, "{}", ""
        if args[:2] == ["issue", "edit"]:
            if "--add-label" in args:
                self.labels += args[args.index("--add-label") + 1].split(",")
            if "--remove-label" in args:
                self.labels = [x for x in self.labels if x not in args[args.index("--remove-label") + 1].split(",")]
            return 0, "", ""
        raise AssertionError(args)

    def test_project_preview_reads_status_without_writing(self):
        got = kanban.sync_github(self.ref, self.config, "plan review", gh=self.gh)
        self.assertTrue(got.ok)
        self.assertFalse(got.done)
        self.assertEqual(got.project_status, "Plan Review")
        self.assertEqual(got.delta.add, (FLOW.human_review_label,))
        self.assertFalse(any(args[1] in ("item-edit", "item-add", "edit") for args in self.calls))

    def test_project_commit_sets_status_and_hold_then_verifies(self):
        got = kanban.sync_github(self.ref, self.config, "plan review", gh=self.gh, commit=True,
                                 expected_stage="plan")
        self.assertTrue(got.done, got.error)
        self.assertEqual(self.status, "Plan Review")
        self.assertEqual(self.labels, ["kind::code", FLOW.human_review_label])
        self.assertLess(next(i for i, a in enumerate(self.calls) if a[:2] == ["issue", "edit"]),
                        next(i for i, a in enumerate(self.calls) if a[:2] == ["project", "item-edit"]))

    def test_project_can_add_issue_before_setting_status(self):
        self.present = False
        got = kanban.sync_github(self.ref, self.config, "implement", gh=self.gh, commit=True,
                                 initialize=True)
        self.assertTrue(got.done, got.error)
        self.assertEqual(self.status, "Build")
        self.assertTrue(got.project_add)
        self.assertTrue(any(a[:2] == ["project", "item-add"] for a in self.calls))

    def test_terminal_stage_does_not_add_missing_project_item(self):
        self.present = False
        got = kanban.sync_github(self.ref, self.config, "main", gh=self.gh, commit=True,
                                 expected_stage="merge")
        self.assertTrue(got.done, got.error)
        self.assertFalse(got.project_add)
        self.assertFalse(any(a[:2] == ["project", "item-add"] for a in self.calls))

    def test_configured_status_field_is_requested(self):
        kanban.sync_github(self.ref, self.config, "implement", gh=self.gh)
        call = next(a for a in self.calls if a[:2] == ["project", "item-list"])
        self.assertEqual(call[call.index("--field") + 1], "Status")

    def test_issue_label_mode_uses_add_remove_without_project(self):
        self.config = FLOW
        self.labels = ["00 - PLAN", "kind::code"]
        got = kanban.sync_github(self.ref, self.config, "plan review", gh=self.gh, commit=True,
                                 expected_stage="plan")
        self.assertTrue(got.done, got.error)
        self.assertEqual(set(self.labels), {"01 - PLAN REVIEW", FLOW.human_review_label, "kind::code"})
        self.assertFalse(any(a[:1] == ["project"] for a in self.calls))

    def test_project_manual_status_change_is_reported(self):
        self.status = "Fix"
        got = kanban.sync_github(self.ref, self.config, "plan review", gh=self.gh, commit=True,
                                 expected_stage="plan")
        self.assertIn("changed outside the tracker", got.error)
        self.assertFalse(any(args[1] in ("item-edit", "edit") for args in self.calls))

    def test_invalid_prior_stage_is_refused_without_writing(self):
        got = kanban.sync_github(self.ref, self.config, "plan review", gh=self.gh, commit=True,
                                 expected_stage="invented")
        self.assertIn("unmapped prior stage", got.error)
        self.assertFalse(self.calls)

    def test_project_drift_checks_status_and_independent_hold(self):
        self.assertIn("Plan Review", kanban.drift(self.config, (), "plan review", project_status="Plan"))
        self.assertIn("!! - HUMAN REVIEW REQUIRED", kanban.drift(
            self.config, (), "plan review", project_status="Plan Review"))
        self.assertEqual(kanban.drift(self.config, (FLOW.human_review_label,),
                                      "plan review", project_status="Plan Review"), "")


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Zach\n- Timezone: America/Chicago\n"
            "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            "- Settings: `chief-of-stuff.toml`\n"
            "- Backlog: GitLab issues; host https://labs.example.test; project team/app\n")
        (self.root / "chief-of-stuff.toml").write_text(
            '[kanban]\nstages = ["00 - PLAN", "01 - PLAN REVIEW"]\n'
            'human_review_label = "!! - HUMAN REVIEW REQUIRED"\n'
            'hold_stages = ["plan review"]\n[kanban.map]\nplan = 0\n"plan review" = 1\n')
        folder = self.root / "daily"
        folder.mkdir()
        (folder / "2026-09-25-tracker.md").write_text(
            "## Tasks\n| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| Build it | Build it | worker | running 09:00 | 09:00 | | M | build | plan review | #7 | |\n")

    def test_preview_one_issue_without_write(self):
        result = kanban.Sync(backlog.IssueRef("team/app", 7, "labs.example.test"), "plan review",
                             kanban.Delta(("01 - PLAN REVIEW",), ("00 - PLAN",)))
        output = io.StringIO()
        with mock.patch.object(kanban, "sync_gitlab", return_value=result) as sync, redirect_stdout(output):
            self.assertEqual(kanban.main(["--root", str(self.root), "--date", "2026-09-25", "--issue", "#7"]), 0)
        self.assertFalse(sync.call_args.kwargs["commit"])
        self.assertIn("would update", output.getvalue())

    def test_commit_requires_one_issue_and_prior_stage(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertNotEqual(kanban.main(["--root", str(self.root), "--date", "2026-09-25", "--commit"]), 0)
        with redirect_stdout(output):
            self.assertNotEqual(kanban.main(["--root", str(self.root), "--date", "2026-09-25",
                                             "--issue", "#7", "--commit"]), 0)

    def test_completed_task_can_be_selected_to_clear_final_stage(self):
        tracker = self.root / "daily" / "2026-09-25-tracker.md"
        tracker.write_text(tracker.read_text().replace("running 09:00", "done"))
        result = kanban.Sync(backlog.IssueRef("team/app", 7, "labs.example.test"), "plan review",
                             kanban.Delta(remove=("01 - PLAN REVIEW",)))
        output = io.StringIO()
        with mock.patch.object(kanban, "sync_gitlab", return_value=result) as sync, redirect_stdout(output):
            self.assertEqual(kanban.main(["--root", str(self.root), "--date", "2026-09-25"]), 0)
            self.assertEqual(output.getvalue().strip(), "[]")
            self.assertEqual(kanban.main(["--root", str(self.root), "--date", "2026-09-25",
                                          "--issue", "#7"]), 0)
        sync.assert_called_once()


class AuditTest(unittest.TestCase):
    def test_audit_reports_label_drift_without_writing(self):
        task = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                               label="Build it", kind="open")
        cfg = backlog.Backlog("https://labs.example.test", "team/app")
        issue = backlog.Issue(7, "Build it", "opened", ("00 - PLAN", "kind::code"))
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}), \
             mock.patch.object(kanban.backlog, "_call") as write:
            faults = audit_tasks.kanban_faults([task], cfg, FLOW)
        self.assertEqual(len(faults), 1)
        self.assertIn("expected 03 - BUILD", str(faults[0]))
        write.assert_not_called()

    def test_gitlab_audit_uses_labels_even_with_github_project_configured(self):
        task = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                               label="Build it", kind="open")
        cfg = backlog.Backlog("https://labs.example.test", "team/app")
        issue = backlog.Issue(7, "Build it", "opened", ("03 - BUILD",))
        mixed = Kanban(FLOW.stages, FLOW.human_review_label, FLOW.stage_map,
                       FLOW.hold_stages, FLOW.terminal_stages, GitHubProject("example", 3, options=FLOW.stages))
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}):
            self.assertEqual(audit_tasks.kanban_faults([task], cfg, mixed), [])

    def test_github_project_audit_lists_items_once_for_multiple_tasks(self):
        cfg = backlog.GitHubBacklog("example/app")
        mixed = Kanban(FLOW.stages, FLOW.human_review_label, FLOW.stage_map,
                       FLOW.hold_stages, FLOW.terminal_stages, GitHubProject("example", 3, options=FLOW.stages))
        tasks = [SimpleNamespace(standing=False, lane="build", stage="implement",
                                 issue=f"#{n}", label=f"Build {n}", kind="open") for n in (7, 8)]
        issues = {n: backlog.Issue(n, f"Build {n}", "opened", ()) for n in (7, 8)}
        project_items = {backlog.IssueRef("example/app", n).url: FLOW.stages[3] for n in (7, 8)}
        with mock.patch.object(audit_tasks, "issue_states", return_value=issues), \
             mock.patch.object(kanban, "_project_items", return_value=(project_items, "")) as items:
            self.assertEqual(audit_tasks.kanban_faults(tasks, cfg, mixed), [])
        items.assert_called_once()

if __name__ == "__main__":
    unittest.main()
