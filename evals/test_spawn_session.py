"""Unit tests for scripts/spawn_session.py: the only file in the plugin that starts a process.

The launcher template never comes from the workspace. A working session can edit `CLAUDE.md`
freely — the coordinator's limits bind the coordinator and nobody else — so a launch template read
from there would be arbitrary argv, executed on the user's machine, with a config file as the
carrier. It lives in the plugin, and the eval overrides it through the environment.

Nothing here starts a real terminal: every test drives `--dry-run` or a recorder.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import spawn_session as ss  # noqa: E402


class ArgvTest(unittest.TestCase):
    def test_the_default_launcher_opens_a_terminal_and_runs_claude_in_the_tree(self):
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/wt", title="wt-docs")
        self.assertEqual(argv[0], "ghostty")
        self.assertIn("--working-directory=/tmp/wt", argv)
        self.assertIn("claude", argv)
        self.assertIn("--agent", argv)
        self.assertIn("implementer", argv)

    def test_a_dispatched_session_starts_in_plan_mode(self):
        """The plan gate is enforced at launch, not asked for in the assignment text."""
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/wt", title="t")
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")

    def test_substitution_is_per_token_so_a_space_cannot_add_an_argument(self):
        argv = ss.argv(["run", "--title={title}", "--", "claude"], agent_type="x", cwd="/tmp/a b", title="two words")
        self.assertIn("--title=two words", argv)
        self.assertEqual(len(argv), 4)

    def test_a_value_that_looks_like_a_flag_stays_one_token(self):
        argv = ss.argv(["run", "{title}"], agent_type="x", cwd="/tmp", title="--dangerous")
        self.assertEqual(argv, ["run", "--dangerous"])
        self.assertEqual(len(argv), 2, "a value never splits into more argv entries than it occupies")

    def test_an_unknown_placeholder_is_refused_rather_than_left_in_the_argv(self):
        with self.assertRaises(ss.RefusedError):
            ss.argv(["run", "{whatever}"], agent_type="x", cwd="/tmp", title="t")


class LauncherSourceTest(unittest.TestCase):
    def test_a_shell_as_argv0_is_refused(self):
        for shell in ("sh", "bash", "zsh", "env", "eval", "/bin/sh"):
            with self.assertRaises(ss.RefusedError, msg=shell):
                ss.launcher([shell, "-c", "claude"])

    def test_a_metacharacter_anywhere_in_the_template_is_refused(self):
        for bad in (["ghostty", "-e", "claude; rm -rf /"], ["ghostty", "-e", "claude && x"], ["ghostty", "$(x)"], ["ghostty", "a|b"]):
            with self.assertRaises(ss.RefusedError, msg=str(bad)):
                ss.launcher(bad)

    def test_an_empty_template_is_refused(self):
        with self.assertRaises(ss.RefusedError):
            ss.launcher([])

    def test_the_environment_overrides_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Path(d) / "rec.py"
            rec.write_text("#!/usr/bin/env python3\n")
            env = json.dumps(["python3", str(rec), "{cwd}", "{title}"])
            with unittest.mock.patch.dict(os.environ, {ss.ENV: env}):
                self.assertEqual(ss.launcher()[0], "python3")

    def test_a_malformed_override_is_refused_not_ignored(self):
        with unittest.mock.patch.dict(os.environ, {ss.ENV: "ghostty -e claude"}):
            with self.assertRaises(ss.RefusedError):
                ss.launcher()


class RunTest(unittest.TestCase):
    def test_dry_run_prints_the_argv_and_starts_nothing(self):
        out = subprocess.run(
            [sys.executable, str(Path(ss.__file__)), "--type", "implementer", "--cwd", "/tmp", "--title", "t", "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ghostty", out.stdout)
        self.assertIn("would run", out.stdout)

    def test_it_runs_the_launcher_and_reports_what_it_started(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "calls.jsonl"
            rec = Path(d) / "rec.py"
            rec.write_text(
                "#!/usr/bin/env python3\nimport json,sys\n"
                f"open({str(log)!r},'a').write(json.dumps({{'argv': sys.argv[1:]}})+chr(10))\n"
            )
            env = dict(os.environ, **{ss.ENV: json.dumps(["python3", str(rec), "{cwd}", "{title}", "{type}"])})
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "fixer", "--cwd", d, "--title", "wt-x"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            recorded = json.loads(log.read_text().splitlines()[0])
            self.assertEqual(recorded["argv"], [d, "wt-x", "fixer"])

    def test_the_dry_run_line_cannot_be_read_as_a_shell_line(self):
        """The argv is right, but a human reads the printed line — and may paste it into a shell.

        A title carrying a space and a semicolon must print with its boundaries visible, or the
        preview of a harmless one-token title reads as `... ; rm -rf ~ ...` and is true when pasted.
        """
        title = "--dangerous ; rm -rf ~"
        out = subprocess.run(
            [sys.executable, str(Path(ss.__file__)), "--type", "implementer", "--cwd", "/tmp", "--title", title, "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("; rm -rf ~ -e", out.stdout, "the title's tokens run into the rest of the argv")
        self.assertIn("'--title=--dangerous ; rm -rf ~'", out.stdout, "one argv entry must print as one quoted word")

    def test_a_refusal_exits_nonzero_and_says_why(self):
        env = dict(os.environ, **{ss.ENV: json.dumps(["sh", "-c", "claude"])})
        out = subprocess.run(
            [sys.executable, str(Path(ss.__file__)), "--type", "x", "--cwd", "/tmp", "--title", "t", "--dry-run"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        self.assertEqual(out.returncode, 1)
        self.assertIn("refused", out.stderr)


class NoShellTest(unittest.TestCase):
    def test_the_source_never_reaches_for_a_shell(self):
        source = Path(ss.__file__).read_text()
        for forbidden in ("shell=True", "os.system", "shlex.split"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_it_never_closes_anything(self):
        """Spawning is the coordinator's; ending a session, removing a tree, deleting a branch is not."""
        source = Path(ss.__file__).read_text()
        for forbidden in ("kill", "terminate", "rmtree", "worktree remove", "branch -d", "branch -D"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
