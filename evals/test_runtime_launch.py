"""Focused checks for versioned coordinator launches and non-Claude workers."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import start_coordinator as start  # noqa: E402
import dispatch_prompt as dispatch  # noqa: E402
import session_exec  # noqa: E402
import process_status  # noqa: E402
import spawn_session as spawn  # noqa: E402
from evals.test_spawn_session import CLAUDE, TRACKER  # noqa: E402


class CoordinatorLaunchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        (self.root / "CLAUDE.md").write_text(CLAUDE + "\n- Calendars: none\n")
        self.install_dir = Path(self.tmp.name) / "install"

    def test_install_is_content_addressed_and_keeps_prior_release(self):
        first = start.install(ROOT, self.install_dir)
        again = start.install(ROOT, self.install_dir)
        self.assertEqual(first, again)
        self.assertTrue((first / "scripts" / "spawn_session.py").is_file())
        self.assertTrue((first / "scripts" / "process_status.py").is_file())
        self.assertTrue((first / "scripts" / "_vendor" / "toon_format" / "decoder.py").is_file())
        self.assertTrue((first / "agents" / "chief-of-stuff.md").is_file())
        self.assertIn("0.36.10-", first.name)

    def test_concurrent_installs_share_one_complete_release(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            paths = list(pool.map(lambda _: start.install(ROOT, self.install_dir), range(3)))
        self.assertEqual(paths, [paths[0]] * 3)
        self.assertTrue((paths[0] / "scripts" / "inbox.py").is_file())

    def test_commands_pin_rules_and_use_host_flags(self):
        release = start.install(ROOT, self.install_dir)
        for runtime, flag in (("claude", "--plugin-dir"), ("codex", "-C"),
                              ("cursor", "--workspace"), ("agy", "-i")):
            with self.subTest(runtime=runtime):
                cmd = start.command(runtime, "/bin/fake", release, self.root)
                self.assertIn(flag, cmd)
                self.assertIn(str(release), " ".join(cmd))
        codex = start.command("codex", "/bin/fake", release, self.root)
        self.assertIn("session-scoped watcher", codex[-1])
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", codex[-1])

    def test_coordinator_prompts_use_host_mailbox_checks(self):
        release = start.install(ROOT, self.install_dir)
        expected = {"claude": "[Mailbox check] chief-of-stuff",
                    "agy": "Antigravity `Schedule` at `*/5 * * * *`",
                    "cursor": "Cursor in-session `/loop 5m`",
                    "codex": "Codex CLI has no in-session schedule"}
        for runtime, marker in expected.items():
            with self.subTest(runtime=runtime):
                rendered = start.prompt(runtime, release, self.root)
                self.assertIn(marker, rendered)
                self.assertIn("list --recipient coordinator --unread", rendered)
                self.assertIn("process_status.py --root", rendered)
                self.assertIn("kanban.py --root", rendered)
        self.assertEqual(start.WATCH_INTERVAL, 5 * 60)

    def test_missing_calendar_server_does_not_block_launch(self):
        (self.root / "CLAUDE.md").write_text(CLAUDE +
            "\n- Calendar tool: `mcp__claude_ai_Google_Calendar__list_events`\n"
            "- Calendars:\n  - Work: work-id\n")
        output = io.StringIO()
        with mock.patch.object(start.shutil, "which", return_value="/bin/fake"), \
             mock.patch.object(start.subprocess, "run") as probe, \
             contextlib.redirect_stdout(output):
            code = start.main(["--runtime", "codex", "--root", str(self.root),
                               "--install-dir", str(self.install_dir), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["runtime"], "codex")
        probe.assert_not_called()
        self.assertTrue((self.install_dir / "versions").exists())

    def test_missing_runtime_binary_refuses_before_install(self):
        with mock.patch.object(start.shutil, "which", return_value=None):
            code = start.main(["--runtime", "cursor", "--root", str(self.root),
                               "--install-dir", str(self.install_dir), "--dry-run"])
        self.assertEqual(code, 1)
        self.assertFalse(self.install_dir.exists())

    def test_watcher_stops_with_its_session_and_notifies_once_per_change(self):
        release = start.install(ROOT, self.install_dir)
        stop = threading.Event()
        calls = []
        with mock.patch.object(start, "check_once", return_value="one finding"), \
             mock.patch.object(start.subprocess, "run", side_effect=lambda *a, **k: calls.append(a[0])):
            worker = threading.Thread(target=start.watcher, args=(self.root, release, stop, 0.01))
            worker.start()
            time.sleep(0.06)
            stop.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), 1)

    def test_watcher_ignores_clean_audit_summary_and_counts_unread(self):
        release = start.install(ROOT, self.install_dir)
        clean = [mock.Mock(returncode=0, stdout="[]"),
                 mock.Mock(returncode=0, stdout="tasks=1 trees=0 orphaned=0 stopped=0\n"),
                 mock.Mock(returncode=0, stdout="[]")]
        with mock.patch.object(start.subprocess, "run", side_effect=clean):
            self.assertEqual(start.check_once(self.root, release), "")
        unread = [mock.Mock(returncode=0, stdout='[1]{type}:\n  register'),
                  mock.Mock(returncode=0, stdout="tasks=1 trees=0 orphaned=0 stopped=0\n"),
                  mock.Mock(returncode=0, stdout="[]")]
        with mock.patch.object(start.subprocess, "run", side_effect=unread):
            self.assertIn("1 unread", start.check_once(self.root, release))

    def test_watcher_notifies_on_kanban_drift(self):
        release = start.install(ROOT, self.install_dir)
        outputs = [mock.Mock(returncode=0, stdout="[]"),
                   mock.Mock(returncode=1, stdout="kanban: Build it — expected 03, found 00\n"
                                                      "tasks=1 trees=0 kanban=1\n"),
                   mock.Mock(returncode=0, stdout="[]")]
        with mock.patch.object(start.subprocess, "run", side_effect=outputs):
            self.assertIn("kanban: Build it", start.check_once(self.root, release))

    def test_watcher_uses_batch_process_helper_and_reports_reused_pid(self):
        release = start.install(ROOT, self.install_dir)
        rows = [{"pid": 123, "name": "impl01", "runtime": "cursor", "status": "pid_reused"}]
        outputs = [mock.Mock(returncode=0, stdout="[]"),
                   mock.Mock(returncode=0, stdout="tasks=1 trees=0 orphaned=0 stopped=0\n"),
                   mock.Mock(returncode=0, stdout=process_status.toon_encode(rows))]
        with mock.patch.object(start.subprocess, "run", side_effect=outputs) as run:
            self.assertIn("worker impl01 process is pid_reused", start.check_once(self.root, release))
        self.assertEqual(Path(run.call_args_list[2].args[0][1]).name, "process_status.py")

    def test_cursor_launch_with_fake_cli(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "agent"
        record = Path(self.tmp.name) / "cursor-argv.json"
        fake.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
        fake.chmod(0o755)
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        proc = subprocess.run([sys.executable, str(ROOT / "start_coordinator.py"), "--runtime", "cursor",
                               "--root", str(self.root), "--install-dir", str(self.install_dir)],
                              env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = json.loads(record.read_text())
        self.assertEqual(argv[:2], ["--workspace", str(self.root.resolve())])
        self.assertIn("## Host adapter", argv[-1])


class WorkerRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        (self.root / "daily").mkdir()
        (self.root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)

    def test_codex_and_cursor_launch_in_planning_state(self):
        codex = spawn.ghostty_script(cwd="/tmp/wt", agent_type=None, claude=Path("/bin/codex"),
            title="impl01", runtime="codex", workspace=str(self.root))
        cursor = spawn.ghostty_script(cwd="/tmp/wt", agent_type=None, claude=Path("/bin/agent"),
            title="impl02", runtime="cursor", workspace=str(self.root))
        self.assertIn("/bin/codex -C /tmp/wt --sandbox read-only", codex)
        self.assertIn("/bin/agent --workspace /tmp/wt --mode plan", cursor)
        for text in (codex, cursor):
            self.assertIn("session_exec.py", text)
            self.assertIn("--registry", text)
            self.assertIn("--login-shell", text)

    def test_assignment_names_real_host_tools_and_mailbox(self):
        for runtime in ("claude", "agy", "codex", "cursor"):
            body = dispatch.compose(self.root, "2026-09-18", "Security audit",
                                    worktree=Path("/tmp/wt"), name="impl01", runtime=runtime)
            self.assertIn('wait --recipient "impl01" --timeout 300', body)
            self.assertIn("do not create polling scripts", body)
            if runtime in ("codex", "cursor"):
                self.assertIn("--from \"impl01\" --type register", body)
                self.assertNotIn("List your sessions", body)
                self.assertIn("/plan", body)
            if runtime == "codex":
                self.assertIn("resume-codex", body)
                self.assertIn("--session-id <session-id>", body)

    def test_registration_and_liveness_use_the_assigned_identity(self):
        path = self.root / ".chief-of-stuff" / "sessions" / "impl01.json"
        session_exec.register(path, runtime="cursor", name="impl01", worktree="/tmp/wt", pid=123)
        record = json.loads(path.read_text())
        self.assertEqual((record["name"], record["runtime"], record["worktree"]),
                         ("impl01", "cursor", "/tmp/wt"))
        with mock.patch.object(session_exec.os, "kill") as kill, \
             mock.patch.object(session_exec.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="/bin/agent --workspace /tmp/wt")):
            self.assertTrue(session_exec.alive(path))
            kill.assert_called_once_with(123, 0)
        with mock.patch.object(session_exec.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(session_exec.alive(path))

    def test_wrapper_registers_before_exec_of_fake_cursor(self):
        fake = self.root / "fake-agent"
        marker = self.root / "seen.json"
        fake.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                        f"open({str(marker)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
        fake.chmod(0o755)
        registry = self.root / ".chief-of-stuff" / "sessions" / "impl01.json"
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "session_exec.py"),
            "--registry", str(registry), "--runtime", "cursor", "--name", "impl01",
            "--worktree", str(self.root), "--", str(fake), "--mode", "plan"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(marker.read_text()), ["--mode", "plan"])
        self.assertEqual(json.loads(registry.read_text())["name"], "impl01")
        self.assertFalse(session_exec.alive(registry))

    def test_wrapper_loads_login_zsh_setup_when_requested(self):
        home = self.root / "home"
        home.mkdir()
        (home / ".zshrc").write_text("export CHIEF_SHELL_MARKER=from-zshrc\n")
        fake = self.root / "fake-agent"
        marker = self.root / "shell-marker.txt"
        fake.write_text("#!/usr/bin/env python3\nimport os,sys\n"
                        f"open({str(marker)!r}, 'w').write(os.environ.get('CHIEF_SHELL_MARKER', 'missing'))\n")
        fake.chmod(0o755)
        registry = self.root / ".chief-of-stuff" / "sessions" / "impl02.json"
        env = dict(os.environ, HOME=str(home))
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "session_exec.py"),
            "--registry", str(registry), "--runtime", "cursor", "--name", "impl02",
            "--worktree", str(self.root), "--login-shell", "--", str(fake)],
            env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(marker.read_text(), "from-zshrc")


if __name__ == "__main__":
    unittest.main()
