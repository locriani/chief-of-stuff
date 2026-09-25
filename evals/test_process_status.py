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


if __name__ == "__main__":
    unittest.main()
