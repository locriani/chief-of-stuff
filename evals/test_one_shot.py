"""One-shot workers exit once and leave a reviewable task outcome."""

import os
import subprocess
import sys
import tempfile
import unittest
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
                        + ("(Path.cwd() / 'partial.txt').write_text('partial work')\n" if write_partial else "")
                        + ("import subprocess\nsubprocess.run(['git', 'add', 'partial.txt'], check=True)\n"
                           "subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test', "
                           "'commit', '-qm', 'partial'], check=True)\n" if commit_partial else "")
                        + (f"(root / 'worker-result.toon').write_text({result!r})\n" if result is not None else "")
                        + f"sys.exit({exit_code})\n")
        path.chmod(0o755)
        return path

    def _run(self, fake: Path) -> int:
        with mock.patch.object(one_shot, "resolve", return_value=str(fake)), \
             mock.patch.object(one_shot, "login_argv", side_effect=lambda argv: argv):
            return one_shot.run(root=self.root, day="2026-09-18", task="Security audit",
                                cwd=self.tree, name="worker01", runtime="codex", agent_type=None,
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

    def test_missing_result_reports_committed_partial_work(self):
        fake = self._fake(None, commit_partial=True)
        self.assertEqual(self._run(fake), 1)
        report = toon_decode((self.tree / one_shot.REPORT).read_text())
        self.assertIn("did not write", report["reason"])
        self.assertIn("partial.txt", report["changes"])
        self.assertIn("partial", report["changes"])

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
