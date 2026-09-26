"""One-shot workers exit once and leave a reviewable task outcome."""

import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import one_shot  # noqa: E402
from _vendor.toon_format import decode as toon_decode  # noqa: E402


CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Worktrees: `trees/`
- Timezone: America/Chicago
"""
TRACKER = """# Tracker

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Security audit | unassigned | open | 09:00 | | Inspect upload handler |

## File ownership

| context | paths |
|---|---|
| Security audit | `src/a/` |

## Log

- 09:00 opened
"""


class CommandTest(unittest.TestCase):
    def test_each_host_uses_one_noninteractive_write_run(self):
        cwd = Path("/tmp/one-shot-tree")
        dispatch = cwd / ".chief-of-stuff/dispatch.md"
        expected = {
            "claude": ("--print", "--permission-prompts", "none"),
            "codex": ("-a", "never", "exec", "--sandbox", "workspace-write", "--ephemeral"),
            "cursor": ("--print", "--force", "--trust"),
            "agy": ("--print", "--mode", "accept-edits"),
        }
        for runtime, flags in expected.items():
            with self.subTest(runtime=runtime):
                args = one_shot.command(runtime, "/bin/fake", cwd, dispatch,
                                        agent_type=None, model="", effort="")
                for flag in flags:
                    self.assertIn(flag, args)
                self.assertNotIn("plan", args)
                self.assertIn(str(dispatch), args[-1])
                if runtime == "claude":
                    release = Path(one_shot.__file__).resolve().parents[1]
                    self.assertEqual(args[args.index("--plugin-dir") + 1], str(release))
                    self.assertTrue((release / "hooks/hooks.json").is_file())

    def test_agy_print_takes_the_prompt_as_its_value(self):
        # agy's --print takes a value: a flag after it becomes the prompt, and agy exits 2.
        dispatch = Path("/tmp/one-shot-tree/.chief-of-stuff/dispatch.md")
        args = one_shot.command("agy", "/bin/fake", dispatch.parent.parent, dispatch,
                                agent_type=None, model="gemini-x", effort="")
        self.assertIn(str(dispatch), args[args.index("--print") + 1])

    def test_exit_zero_without_structured_result_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, reason, changes = one_shot.worker_result(Path(tmp) / "missing.toon", 0)
        self.assertEqual(status, "human_review")
        self.assertIn("did not write", reason)
        self.assertEqual(changes, "")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        (self.root / "daily").mkdir()
        self.tracker = self.root / "daily/2026-09-18-tracker.md"
        self.tracker.write_text(TRACKER)
        self.tree = self.root / "trees/worker"
        self.tree.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.tree)], check=True)
        subprocess.run(["git", "-C", str(self.tree), "-c", "user.name=Test", "-c",
                        "user.email=test@example.test", "commit", "--allow-empty", "-qm", "initial"], check=True)

    def _fake(self, result: str | None, exit_code: int = 0, write_partial: bool = True,
              commit_partial: bool = False) -> Path:
        path = self.root / "fake-agent"
        path.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
                        + "root = Path.cwd() / '.chief-of-stuff'\n"
                        # What the tracker says while the worker runs.
                        + f"Path({str(self.root / 'during.md')!r}).write_text(Path({str(self.tracker)!r}).read_text())\n"
                        + ("(Path.cwd() / 'partial.txt').write_text('partial work')\n" if write_partial else "")
                        + ("import subprocess\nsubprocess.run(['git', 'add', 'partial.txt'], check=True)\n"
                           "subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test', "
                           "'commit', '-qm', 'partial'], check=True)\n" if commit_partial else "")
                        + (f"(root / 'worker-result.toon').write_text({result!r})\n" if result is not None else "")
                        + f"sys.exit({exit_code})\n")
        path.chmod(0o755)
        return path

    def _run(self, fake: Path, tree: Path | None = None) -> int:
        with mock.patch.object(one_shot, "resolve", return_value=str(fake)), \
             mock.patch.object(one_shot, "login_argv", side_effect=lambda argv: argv):
            return one_shot.run(root=self.root, day="2026-09-18", task="Security audit",
                                cwd=tree or self.tree, name="worker01", runtime="codex", agent_type=None,
                                model="", effort="", dry_run=False)

    def test_done_exits_once_and_leaves_waiting_for_integration(self):
        fake = self._fake('status: done\nreason: completed audit\nchanges: committed handler and ran tests\n',
                          write_partial=False)
        self.assertEqual(self._run(fake), 0)
        self.assertIn("| Security audit | Robin | waiting |", self.tracker.read_text())
        self.assertIn("completed; awaiting integration", self.tracker.read_text())
        report = toon_decode((self.tree / one_shot.REPORT).read_text())
        self.assertEqual(report["status"], "done")
        self.assertIn("Working tree clean", report["changes"])
        self.assertNotIn("inbox.py", (self.tree / ".chief-of-stuff/dispatch.md").read_text().lower())

    def test_launch_records_the_worker_its_tree_and_a_log_line_before_it_runs(self):
        # The coordinator hand-edited all three after every launch, and a row still `open` could launch twice.
        fake = self._fake('status: done\nreason: done\nchanges: checked\n', write_partial=False)
        self.assertEqual(self._run(fake), 0)
        during = (self.root / "during.md").read_text()
        self.assertRegex(during, r"\| Security audit \| worker01 \| running \d\d:\d\d \|")
        branch = subprocess.run(["git", "-C", str(self.tree), "branch", "--show-current"],
                                capture_output=True, text=True, check=True).stdout.strip()
        self.assertIn(f"| Security audit | `src/a/`; worktree `worker` ({branch}) |", during)
        self.assertRegex(during, r"(?m)^- \d\d:\d\d one-shot worker01 started: codex, worktree `worker`, "
                                 r"task Security audit$")
        self.assertIn(f"worktree `worker` ({branch})", self.tracker.read_text())

    def test_launcher_stamps_in_the_workspace_zone(self):
        # The workspace is Chicago; a machine on UTC stamped UTC.
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(time.tzset)
        self.addCleanup(os.environ.pop, "TZ")
        fake = self._fake('status: done\nreason: done\nchanges: checked\n', write_partial=False)
        self._run(fake)
        now = datetime.now(ZoneInfo("America/Chicago"))
        near = {(now - timedelta(minutes=m)).strftime("%H:%M") for m in (0, 1)}
        stamps = re.findall(r"(?m)^- (\d\d:\d\d) one-shot", self.tracker.read_text())
        self.assertEqual(len(stamps), 2)
        self.assertLessEqual(set(stamps), near)

    def test_worker_inherits_the_workspace_for_the_board_guard(self):
        fake = self._fake('status: done\nreason: done\nchanges: checked\n', write_partial=False)
        with mock.patch.object(one_shot.subprocess, "run", wraps=subprocess.run) as execute:
            self.assertEqual(self._run(fake), 0)
        call = next(call for call in execute.call_args_list if "stdin" in call.kwargs)
        self.assertEqual(call.kwargs["env"]["CHIEF_OF_STUFF_WORKSPACE"], str(self.root.resolve()))

    def test_partial_failure_records_reason_and_changes(self):
        fake = self._fake('status: human_review\nreason: needs product decision\nchanges: changed parser\n')
        self.assertEqual(self._run(fake), 1)
        tracker = self.tracker.read_text()
        self.assertIn("| Security audit | Robin | waiting |", tracker)
        self.assertIn("HUMAN REVIEW NEEDED", tracker)
        self.assertIn("needs product decision", tracker)
        self.assertIn("partial.txt", tracker)

    def test_claimed_completion_with_uncommitted_edits_needs_review(self):
        fake = self._fake('status: done\nreason: complete\nchanges: changed parser\n')
        self.assertEqual(self._run(fake), 1)
        self.assertIn("worktree is not clean", self.tracker.read_text())
        self.assertIn("partial.txt", self.tracker.read_text())

    def test_relaunch_leaves_the_task_ready_and_the_user_out_of_it(self):
        # A stop a fresh run fixes is the coordinator's to relaunch, not the user's to review (#68).
        fake = self._fake('status: relaunch\nreason: target PR merged before work began\nchanges: none\n',
                          write_partial=False)
        with mock.patch.object(one_shot.kanban, "add_human_hold") as hold, \
             mock.patch.object(one_shot.backlog, "comment") as comment:
            self.assertEqual(self._run(fake), 1)
        hold.assert_not_called()
        comment.assert_not_called()
        tracker = self.tracker.read_text()
        self.assertIn("| Security audit | unassigned | open |", tracker)
        self.assertIn("one-shot worker01: relaunch requested for Security audit — target PR merged", tracker)
        self.assertNotIn("HUMAN REVIEW", tracker)
        self.assertEqual(toon_decode((self.tree / one_shot.REPORT).read_text())["status"], "relaunch")

    def test_the_assignment_offers_relaunch_for_a_moved_premise(self):
        fake = self._fake('status: done\nreason: done\nchanges: checked\n', write_partial=False)
        self._run(fake)
        dispatch = (self.tree / ".chief-of-stuff/dispatch.md").read_text()
        self.assertIn("status: relaunch", dispatch)
        self.assertRegex(dispatch, r"(?i)status: relaunch[^\n]*before you changed anything[^\n]*merged")

    def test_relaunch_with_changes_needs_review(self):
        fake = self._fake('status: relaunch\nreason: base moved\nchanges: none\n')
        self.assertEqual(self._run(fake), 1)
        tracker = self.tracker.read_text()
        self.assertIn("| Security audit | Robin | waiting |", tracker)
        self.assertIn("asked to be relaunched but left changes", tracker)

    def test_a_second_relaunch_of_one_task_needs_review(self):
        fake = self._fake('status: relaunch\nreason: base moved\nchanges: none\n', write_partial=False)
        self._run(fake)
        again = self.root / "trees/worker-2"
        subprocess.run(["git", "clone", "-q", str(self.tree), str(again)], check=True)
        self.assertEqual(self._run(fake, again), 1)
        tracker = self.tracker.read_text()
        self.assertIn("| Security audit | Robin | waiting |", tracker)
        self.assertIn("already relaunched once", tracker)

    def test_missing_result_reports_committed_partial_work(self):
        fake = self._fake(None, commit_partial=True)
        self.assertEqual(self._run(fake), 1)
        report = toon_decode((self.tree / one_shot.REPORT).read_text())
        self.assertIn("did not write", report["reason"])
        self.assertIn("partial.txt", report["changes"])
        self.assertIn("partial", report["changes"])

    def _cap(self, n: int) -> None:
        (self.root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `cos.toml`\n")
        (self.root / "cos.toml").write_text(f"[workers]\nmode = \"one-shot\"\nmax_concurrency = {n}\n")

    def _other(self, pid: int) -> None:
        other = self.root / "trees/other" / one_shot.PIDFILE
        other.parent.mkdir(parents=True)
        other.write_text(f"{pid} Export header\n")

    def test_a_launch_at_the_cap_is_refused_naming_the_running_ones(self):
        self._cap(1)
        self._other(os.getpid())
        before = self.tracker.read_text()
        with self.assertRaisesRegex(ValueError, r"Export header.*max_concurrency is 1"):
            self._run(self._fake('status: done\nreason: r\nchanges: c\n'))
        self.assertEqual(self.tracker.read_text(), before)
        self.assertFalse((self.root / "during.md").exists(), "the worker must not start")

    def test_a_launcher_that_has_exited_holds_no_slot(self):
        self._cap(1)
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self._other(gone.pid)
        fake = self._fake('status: done\nreason: r\nchanges: c\n', write_partial=False)
        self.assertEqual(self._run(fake), 0)

    def test_a_running_worker_is_counted_and_released_when_it_exits(self):
        probe = self.root / "pid-during.txt"
        fake = self._fake('status: done\nreason: r\nchanges: c\n', write_partial=False)
        fake.write_text(fake.read_text().replace(
            "sys.exit(", f"Path({str(probe)!r}).write_text((root / 'one-shot.pid').read_text())\nsys.exit(", 1))
        self.assertEqual(self._run(fake), 0)
        self.assertEqual(probe.read_text(), f"{os.getpid()} Security audit\n")
        self.assertEqual(one_shot.running_workers(self.root / "trees"), [])

    def test_dry_run_writes_nothing(self):
        fake = self._fake('status: done\nreason: done\nchanges: changed\n')
        with mock.patch.object(one_shot, "resolve", return_value=str(fake)):
            result = one_shot.run(root=self.root, day="2026-09-18", task="Security audit",
                                  cwd=self.tree, name="worker01", runtime="cursor", agent_type=None,
                                  model="", effort="", dry_run=True)
        self.assertEqual(result, 0)
        self.assertFalse((self.tree / ".chief-of-stuff").exists())
        self.assertEqual(self.tracker.read_text(), TRACKER)

    def test_review_adds_hold_and_comments_with_partial_changes(self):
        (self.root / "CLAUDE.md").write_text(CLAUDE +
            "- Settings: `chief-of-stuff.toml`\n"
            "- Backlog: GitHub issues; repo https://github.com/team/repo (private)\n")
        (self.root / "chief-of-stuff.toml").write_text(
            '[kanban]\nstages = ["00 - PLAN", "03 - BUILD"]\n'
            'human_review_label = "!! - HUMAN REVIEW REQUIRED"\n'
            '[kanban.map]\nimplement = 1\n')
        self.tracker.write_text(
            "## Tasks\n"
            "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| Security audit | Security audit | unassigned | open | 09:00 | | M | build | implement | #7 | Inspect upload handler |\n"
            "\n## File ownership\n| context | paths |\n|---|---|\n| Security audit | `src/a/` |\n")
        fake = self._fake('status: human_review\nreason: unclear policy\nchanges: changed parser\n')
        with mock.patch.object(one_shot, "resolve", return_value=str(fake)), \
             mock.patch.object(one_shot, "login_argv", side_effect=lambda argv: argv), \
             mock.patch.object(one_shot.kanban, "add_human_hold", return_value="") as hold, \
             mock.patch.object(one_shot.backlog, "comment", return_value=SimpleNamespace(done=True, error="")) as comment:
            code = one_shot.run(root=self.root, day="2026-09-18", task="Security audit",
                                cwd=self.tree, name="worker01", runtime="codex", agent_type=None,
                                model="", effort="", dry_run=False)
        self.assertEqual(code, 1)
        hold.assert_called_once()
        self.assertEqual(hold.call_args.args[0].number, 7)
        body = comment.call_args.args[2]
        self.assertIn("unclear policy", body)
        self.assertIn("changed parser", body)
        self.assertIn("partial.txt", body)
        self.assertIn("| Security audit | Security audit | Robin | waiting |", self.tracker.read_text())


if __name__ == "__main__":
    unittest.main()
