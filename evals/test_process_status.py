"""Batch PID checks for coordinator worker sessions."""

from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import process_status  # noqa: E402
import session_exec  # noqa: E402
from _vendor.toon_format import decode as toon_decode  # noqa: E402


class ProcessStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        folder = self.root / ".chief-of-stuff" / "sessions"
        session_exec.register(folder / "worker.json", runtime="cursor", name="worker",
                              worktree="/tmp/worker-tree", pid=123)
        session_exec.register(folder / "old.json", runtime="codex", name="old",
                              worktree="/tmp/old-tree", pid=456)

    def test_batch_check_verifies_identity_and_reports_missing_and_unregistered(self):
        def exists(pid):
            return pid != 789

        probe = mock.Mock(returncode=0, stdout=(" 123 /bin/agent --workspace /tmp/worker-tree\n"
                                                   " 456 /bin/other --workspace /tmp/elsewhere\n"
                                                   " 999 /bin/python\n"))
        with mock.patch.object(process_status, "process_exists", side_effect=exists), \
             mock.patch.object(process_status.subprocess, "run", return_value=probe) as ps:
            rows = process_status.check(self.root, [123, 456, 789, 999])
        ps.assert_called_once()
        self.assertEqual(ps.call_args.args[0][0:4], ["ps", "-ww", "-p", "123,456,999"])
        self.assertEqual([(row["pid"], row["name"], row["status"]) for row in rows],
                         [(123, "worker", "running"), (456, "old", "pid_reused"),
                          (789, "", "gone"), (999, "", "running")])

    def test_unverified_is_not_reported_gone_when_ps_cannot_inspect(self):
        with mock.patch.object(process_status.os, "kill", side_effect=PermissionError), \
             mock.patch.object(process_status.subprocess, "run", side_effect=OSError):
            self.assertEqual(process_status.check(self.root, [123])[0]["status"], "unverified")

    def test_cli_accepts_comma_batch_and_defaults_to_registered_workers(self):
        with mock.patch.object(process_status, "process_exists", return_value=False):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(process_status.main(["--root", str(self.root), "123,456"]), 0)
            self.assertEqual([row["pid"] for row in toon_decode(output.getvalue())], [123, 456])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(process_status.main(["--root", str(self.root)]), 0)
            self.assertEqual([row["pid"] for row in toon_decode(output.getvalue())], [456, 123])


CLAUDE = "# Workspace\n\n## Coordinator\n\n- User: Robin\n- Worktrees: `trees/`\n"
LIVE, DEAD = 4242, 999999  # the launcher pids the fake process table knows


class OneShotRunsTests(unittest.TestCase):
    """#53: `processes` is the entry point that says what is running; a live one-shot run is part of that."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        self.alive = {LIVE}

    def pidfile(self, tree: str, content: str) -> None:
        path = self.root / "trees" / tree / ".chief-of-stuff" / "one-shot.pid"
        path.parent.mkdir(parents=True)
        path.write_text(content)

    def register(self, pid: int) -> None:
        session_exec.register(self.root / ".chief-of-stuff" / "sessions" / "worker.json", runtime="cursor",
                              name="worker", worktree="/tmp/worker-tree", pid=pid)

    def processes(self, *pids: str) -> tuple[str, list[dict]]:
        probe = mock.Mock(returncode=0, stdout="".join(f" {pid} /bin/agent --workspace /tmp/worker-tree\n"
                                                     for pid in self.alive))
        out = io.StringIO()
        with mock.patch.object(process_status, "process_exists", side_effect=lambda pid: pid in self.alive), \
             mock.patch.object(process_status.subprocess, "run", return_value=probe), redirect_stdout(out):
            self.assertEqual(process_status.main(["--root", str(self.root), *pids]), 0)
        return out.getvalue(), toon_decode(out.getvalue())

    def test_each_live_one_shot_is_a_row_and_a_dead_or_garbled_pid_file_is_not(self):
        self.register(123)
        self.alive.add(123)
        self.pidfile("rate-limit", f"{LIVE} Rate limit headers\n")
        self.pidfile("died", f"{DEAD} Export header\n")
        self.pidfile("garbled", "not-a-pid Search pagination\n")
        _, rows = self.processes()
        runs = [row for row in rows if row.get("kind") == "one-shot"]
        # Asserted: what the pid file holds (launcher pid, task) and where it sits (the tree), marked one-shot,
        # with the status the other rows use. Not asserted: `name` and `runtime` -- the pid file holds neither.
        self.assertEqual([(row["pid"], row["task"], Path(row["worktree"]).name, row["status"]) for row in runs],
                         [(LIVE, "Rate limit headers", "rate-limit", "running")])
        self.assertEqual([row["pid"] for row in rows if row not in runs], [123])

    def test_a_live_one_shot_with_no_registered_workers_is_not_an_empty_list(self):
        self.pidfile("rate-limit", f"{LIVE} Rate limit headers\n")
        text, rows = self.processes()
        self.assertNotEqual(text.strip(), "[]")
        self.assertEqual([(row["pid"], row["task"]) for row in rows], [(LIVE, "Rate limit headers")])

    def test_explicit_pids_answer_for_those_pids_only(self):
        self.register(123)
        self.alive.add(123)
        self.pidfile("rate-limit", f"{LIVE} Rate limit headers\n")
        _, rows = self.processes("123")
        self.assertEqual(rows, [{"pid": 123, "name": "worker", "runtime": "cursor", "status": "running"}])


if __name__ == "__main__":
    unittest.main()
