"""Unit tests for scripts/spawn_session.py: the only file in the plugin that starts a process.

The launcher template never comes from the workspace. A working session can edit `CLAUDE.md`
freely — the coordinator's limits bind the coordinator and nobody else — so a launch template read
from there would be arbitrary argv, executed on the user's machine, with a config file as the
carrier. It lives in the plugin, and the eval overrides it through the environment.

Nothing here starts a real terminal: every test drives `--dry-run` or a recorder.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import spawn_session as ss  # noqa: E402

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Worktrees: `trees/`
- Timezone: America/Chicago
"""

TRACKER = """# Tracker 2026-09-18

## Lanes

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Security audit | unassigned | open | 09:00 |  | Checklist: Security audit of the upload handler |

## File ownership

| context | paths |
|---|---|
| Security audit | `src/a/` |

## Log

- 09:00 opened the day
"""



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
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "implementer", "--cwd", "/tmp", "--title", "t",
                 "--root", str(root), "--date", "2026-09-18", "--lane", "Security audit", "--dry-run"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("new tab in front window", out.stdout)
            self.assertIn("would run", out.stdout)
            self.assertIn("would write", out.stdout)

    def test_it_runs_the_launcher_and_reports_what_it_started(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            tree = root / "trees" / "wt-x"
            tree.mkdir(parents=True)
            log = root / "calls.jsonl"
            rec = root / "rec.py"
            rec.write_text(
                "#!/usr/bin/env python3\nimport json,sys\n"
                f"open({str(log)!r},'a').write(json.dumps({{'argv': sys.argv[1:]}})+chr(10))\n"
            )
            env = dict(os.environ, **{ss.ENV: json.dumps(["python3", str(rec), "{cwd}", "{title}", "{type}"])})
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "fixer", "--cwd", str(tree), "--title", "wt-x",
                 "--root", str(root), "--date", "2026-09-18", "--lane", "Security audit"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            recorded = json.loads(log.read_text().splitlines()[0])
            self.assertEqual(recorded["argv"], [str(tree), "wt-x", "fixer"])

    def test_the_dry_run_line_cannot_be_read_as_a_shell_line(self):
        """The argv is right, but a human reads the printed line — and may paste it into a shell.

        A path carrying a space and a semicolon must print with its boundaries visible, or the
        preview reads as `... ; rm -rf ~ ...` and is true when pasted. This used a hostile title until
        the title became the session's name, which is refused unless it is a bare session name.
        """
        cwd = "/tmp/--dangerous ; rm -rf ~"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "implementer", "--cwd", cwd, "--name", "t",
                 "--root", str(root), "--date", "2026-09-18", "--lane", "Security audit", "--dry-run"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("; rm -rf ~ -e", out.stdout, "the path's tokens run into the rest of the argv")
        self.assertIn(shlex.quote(cwd), out.stdout, "one path must print as one quoted word")

    def test_a_hostile_name_starts_nothing(self):
        """The name reaches `claude --name`, so it is a bare session name or it is refused."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "implementer", "--cwd", "/tmp",
                 "--name=--dangerous ; rm -rf ~", "--root", str(root), "--date", "2026-09-18",
                 "--lane", "Security audit", "--dry-run"],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(out.returncode, 1)
        self.assertIn("refused", out.stderr)
        self.assertNotIn("would run", out.stdout)

    def test_the_override_path_still_quotes_every_argv_entry(self):
        """The eval harness builds an argv, so the per-token invariant still has to hold there."""
        cwd = "/tmp/--dangerous ; rm -rf ~"
        env = dict(os.environ, **{ss.ENV: json.dumps(["ghostty", "--working-directory={cwd}", "-e", "claude"])})
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "implementer", "--cwd", cwd, "--name", "t",
                 "--root", str(root), "--date", "2026-09-18", "--lane", "Security audit", "--dry-run"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("'--working-directory=/tmp/--dangerous ; rm -rf ~'", out.stdout,
                      "one argv entry must print as one quoted word")

    def test_a_refusal_exits_nonzero_and_says_why(self):
        env = dict(os.environ, **{ss.ENV: json.dumps(["sh", "-c", "claude"])})
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "x", "--cwd", "/tmp", "--title", "t",
                 "--root", str(root), "--date", "2026-09-18", "--lane", "Security audit", "--dry-run"],
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
        """Spawning is the coordinator's; ending a session, removing a tree, deleting a branch is not.

        Call shapes, not substrings: `skills` contains `kill`, and a check that cannot tell those
        apart fails on a word rather than on a behaviour.
        """
        source = Path(ss.__file__).read_text()
        for forbidden in (r"\bos\.kill\b", r"\.kill\(", r"\.terminate\(", r"\brmtree\b",
                          r"worktree\s+remove", r"branch\s+-[dD]\b", r"\.unlink\(", r"os\.remove\b"):
            self.assertIsNone(re.search(forbidden, source), forbidden)


class BootstrapTest(unittest.TestCase):
    """The argv carries a constant. Nothing the coordinator composed travels on the command line."""

    def test_the_last_argv_token_is_the_bootstrap_and_points_at_the_dispatch_file(self):
        """`BOOTSTRAP` became a template when the paths went absolute, so the token is the filled
        form rather than the constant. The invariant it guards is unchanged: one token, naming the
        assignment, and nothing the coordinator composed."""
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/wt", title="wt-docs")
        self.assertIn(ss.PROMPT_FILE, argv[-1])
        self.assertIn("/tmp/wt", argv[-1])

    def test_the_argv_is_the_same_whatever_the_lane_is(self):
        """There is no per-dispatch text on the command line, so there is nothing to expand."""
        a = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/a", title="t")
        b = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/a", title="t")
        self.assertEqual(a, b)
        self.assertNotIn("Lane:", " ".join(a))

    def test_no_agent_type_drops_the_flag_and_its_value_together(self):
        """Removed as a pair, never substituted empty: a token must not expand to zero or two."""
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type=None, cwd="/tmp/wt", title="t")
        self.assertNotIn("--agent", argv)
        self.assertNotIn("", argv)
        self.assertIn("claude", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")

    def test_an_agent_type_is_passed_when_the_block_names_one(self):
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="fixer", cwd="/tmp/wt", title="t")
        self.assertEqual(argv[argv.index("--agent") + 1], "fixer")


class GhosttyTabTest(unittest.TestCase):
    """The real launcher: a tab in the window the user already has, not a window of its own.

    Design-input 44 said "a Ghostty tab running `claude`" in Zach's own words and 0.7.0 shipped a
    window. Ghostty's scripting dictionary has `new tab ... with configuration`, so the tab is asked
    for rather than simulated with keystrokes.
    """

    CLAUDE_BIN = ss.Path("/opt/homebrew/bin/claude")

    def script(self, agent_type="implementer", cwd="/tmp/trees/wt-audit", **kw):
        kw.setdefault("claude", self.CLAUDE_BIN)
        return ss.ghostty_script(cwd=cwd, agent_type=agent_type, **kw)

    def test_it_asks_for_a_tab(self):
        self.assertIn("new tab in front window with configuration", self.script())

    def test_it_makes_a_window_when_there_is_none_to_put_a_tab_in(self):
        s = self.script()
        self.assertIn("new window with configuration", s)
        self.assertIn("count of windows", s)

    def test_the_tab_takes_the_title_it_was_given(self):
        """A surface configuration has no title field; `set_tab_title:` is how a tab gets a name."""
        self.assertIn('set_tab_title:wt-audit', self.script(title="wt-audit"))

    def test_the_session_starts_in_its_worktree(self):
        self.assertIn('set initial working directory of cfg to "/tmp/trees/wt-audit"', self.script())

    def test_the_tab_outlives_a_command_that_dies(self):
        """Ghostty calls any sub-second exit a launch failure; without this the evidence closes itself."""
        self.assertIn("set wait after command of cfg to true", self.script())

    def test_the_command_is_quoted_per_token(self):
        """Ghostty parses `command` shell-style, so the bootstrap stays one argument or it is nonsense."""
        s = self.script()
        # The filled bootstrap, since it carries paths now; still exactly one shell token.
        filled = [t for t in shlex.split(s.split("set command of cfg to ")[1].split("\n")[0].strip('"').replace('\\"', '"'))
                  if t.startswith("Read the file ")]
        self.assertEqual(len(filled), 1, filled)

    def test_no_agent_type_drops_the_flag_and_its_value_together(self):
        self.assertNotIn("--agent", self.script(agent_type=None))
        self.assertIn("--agent implementer", self.script())

    def test_it_passes_no_environment_of_its_own(self):
        """The tab inherits Ghostty's environment, which is the user's login session and not a coordinator's.

        That is what the KEEP whitelist was approximating; asking Ghostty for a tab gets it exactly.
        """
        s = self.script()
        self.assertNotIn("environment variables", s)
        self.assertNotIn("CLAUDE", s)

    def test_the_tab_gets_the_launching_path(self):
        """A `command` tab skips the login shell, so Ghostty's PATH is launchd's: no /opt/homebrew/bin.

        Sessions spawned that way ran without rtk, gh or glab — every Bash call failed the rtk hook with
        `rtk: command not found`, and `gh pr create` would have too. The launching shell's PATH is
        what a terminal the user opened has.
        """
        with unittest.mock.patch.dict(os.environ, {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"}):
            s = self.script()
        self.assertIn(" PATH=/opt/homebrew/bin:/usr/bin:/bin ", s)

    def test_a_quote_cannot_end_the_applescript_string(self):
        s = self.script(cwd='/tmp/a"b', agent_type=None)
        self.assertIn('\\"', s)
        self.assertNotIn('"/tmp/a"b"', s)

    def test_a_backslash_is_escaped_before_anything_else(self):
        s = self.script(cwd="/tmp/a\\b", agent_type=None)
        self.assertIn("\\\\", s)

    def test_a_control_character_is_refused_rather_than_escaped(self):
        with self.assertRaises(ss.RefusedError):
            self.script(cwd="/tmp/a\x1b]0;x\x07", agent_type=None)

    def test_the_binary_is_resolved_absolutely(self):
        """Ghostty is launched from the GUI, so its children get launchd's PATH, not a shell's.

        The first real tab died in 38ms with `exec: claude: not found`, while `which claude` in the
        coordinator answered `/opt/homebrew/bin/claude`. Restoring a PATH would put the environment
        surface back; resolving the binary here does not.
        """
        s = self.script(cwd="/tmp", agent_type=None)
        self.assertIn("/opt/homebrew/bin/claude", s)
        self.assertNotIn("command:\"claude ", s)

    def test_a_claude_that_cannot_be_found_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(ss.RefusedError) as e:
            ss.ghostty_script(cwd="/tmp", agent_type=None, claude=None)
        self.assertIn("claude", str(e.exception))

    def test_it_never_reaches_a_shell(self):
        """The one file that starts processes gained a second quoting layer; it did not gain a shell."""
        s = self.script()
        for shellish in ("do shell script", "/bin/sh", "bash -c", "osascript -e"):
            self.assertNotIn(shellish, s)


class DispatchFileTest(unittest.TestCase):
    """The assignment lands in the tree before the session that reads it exists."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        (self.root / "daily").mkdir()
        (self.root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
        self.tree = self.root / "trees" / "wt-audit"
        self.tree.mkdir(parents=True)

    def spawn(self, lane="Security audit", **kw):
        log = self.root / "calls.jsonl"
        rec = self.root / "rec.py"
        rec.write_text(
            "#!/usr/bin/env python3\nimport json,os,sys\n"
            f"open({str(log)!r},'a').write(json.dumps({{'argv': sys.argv[1:], "
            f"'file_there': os.path.exists({str(self.tree / ss.PROMPT_FILE)!r})}})+chr(10))\n"
        )
        env = dict(os.environ, **{ss.ENV: json.dumps(["python3", str(rec), "{cwd}", "{title}"])})
        args = ["--cwd", str(self.tree), "--title", "wt-audit", "--root", str(self.root),
                "--date", "2026-09-18", "--lane", lane]
        out = subprocess.run([sys.executable, str(Path(ss.__file__)), *args, *kw.get("extra", [])],
                             capture_output=True, text=True, timeout=30, env=env)
        calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return out, calls

    def test_the_lane_lands_in_the_dispatch_file(self):
        out, _ = self.spawn()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Lane: Security audit", (self.tree / ss.PROMPT_FILE).read_text())

    def test_the_directory_ignores_itself_so_the_tree_stays_clean(self):
        """An untracked file here would read as uncommitted work in every spawned tree."""
        self.spawn()
        self.assertEqual((self.tree / ".chief-of-stuff" / ".gitignore").read_text().strip(), "*")

    def test_the_file_is_there_before_the_process_starts(self):
        _, calls = self.spawn()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["file_there"], "the session could read a file that is not there yet")

    def test_a_lane_that_is_not_a_row_writes_nothing_and_starts_nothing(self):
        out, calls = self.spawn(lane="Upload handler fix")
        self.assertEqual(out.returncode, 1)
        self.assertIn("refused", out.stderr)
        self.assertFalse((self.tree / ss.PROMPT_FILE).exists())
        self.assertEqual(calls, [])

    def test_an_expansion_that_reached_the_script_is_refused_as_an_unknown_row(self):
        out, calls = self.spawn(lane="locriani")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(calls, [])

    def test_a_tree_that_already_holds_a_dispatch_is_refused(self):
        (self.tree / ".chief-of-stuff").mkdir()
        (self.tree / ss.PROMPT_FILE).write_text("someone else's assignment\n")
        out, calls = self.spawn()
        self.assertEqual(out.returncode, 1)
        self.assertIn("already", out.stderr)
        self.assertEqual((self.tree / ss.PROMPT_FILE).read_text(), "someone else's assignment\n")
        self.assertEqual(calls, [])

    def test_the_coordinator_it_registers_with_reaches_the_assignment(self):
        """The ref arrives from the session rather than from the coordinator noticing it in a listing."""
        out, calls = self.spawn(extra=["--coordinator", "gauntlet-d3 [0d6cf4]"])
        body = (self.tree / ss.PROMPT_FILE).read_text()
        self.assertIn("gauntlet-d3 [0d6cf4]", body)
        self.assertIn("Register first", body)

    def test_it_still_says_to_register_when_no_coordinator_was_named(self):
        self.spawn()
        self.assertIn("chief-of-stuff coordinator", (self.tree / ss.PROMPT_FILE).read_text())

    def test_a_coordinator_that_is_not_a_session_name_writes_nothing_and_starts_nothing(self):
        out, calls = self.spawn(extra=["--coordinator", "$(whoami)"])
        self.assertEqual(out.returncode, 1)
        self.assertIn("refused", out.stderr)
        self.assertFalse((self.tree / ss.PROMPT_FILE).exists())
        self.assertEqual(calls, [])

    def test_the_assignment_carries_the_paths_the_lane_owns(self):
        """Finding 56: a lane name is not something a session can act on."""
        self.spawn()
        self.assertIn("Owns: `src/a/`", (self.tree / ss.PROMPT_FILE).read_text())

    def test_a_lane_with_no_ownership_row_writes_nothing_and_starts_nothing(self):
        (self.root / "daily" / "2026-09-18-tracker.md").write_text(
            TRACKER.replace("| Security audit | `src/a/` |", ""))
        out, calls = self.spawn()
        self.assertEqual(out.returncode, 1)
        self.assertIn("File ownership", out.stderr)
        self.assertFalse((self.tree / ss.PROMPT_FILE).exists())
        self.assertEqual(calls, [])

    def test_the_started_line_names_the_file_it_wrote(self):
        out, _ = self.spawn()
        self.assertIn(ss.PROMPT_FILE, out.stdout)


class LaunchEnvironmentTest(unittest.TestCase):
    """A spawned session is a new session, not a child of the coordinator's.

    The coordinator is itself a Claude Code session, so its environment carries the markers that say
    which session this is, which socket it speaks on, and that it is somebody's child. Inheriting
    those gave a spawned session the coordinator's messaging credentials, bound it to the
    coordinator's project rather than its own worktree, and turned its transcript off — one cause,
    three symptoms, found by watching a real spawn.
    """

    def test_no_claude_variable_reaches_the_new_session(self):
        parent = dict(os.environ, CLAUDE_CODE_CHILD_SESSION="1", CLAUDE_CODE_SESSION_ID="abc",
                      CLAUDE_CODE_MESSAGING_SOCKET="/tmp/cc-socks/1.sock",
                      CLAUDE_CODE_MESSAGING_TOKEN="secret", CLAUDECODE="1", CLAUDE_PID="99")
        env = ss.launch_env(parent)
        leaked = [k for k in env if k.upper().startswith("CLAUDE")]
        self.assertEqual(leaked, [], f"the new session would speak as the coordinator: {leaked}")

    def test_the_messaging_token_is_never_passed_on(self):
        env = ss.launch_env(dict(os.environ, CLAUDE_CODE_MESSAGING_TOKEN="secret"))
        self.assertNotIn("secret", "".join(env.values()))

    def test_what_a_terminal_actually_needs_survives(self):
        parent = dict(os.environ, PATH="/usr/bin:/bin", HOME="/Users/robin", TERM="xterm-256color")
        env = ss.launch_env(parent)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["HOME"], "/Users/robin")
        self.assertEqual(env["TERM"], "xterm-256color")

    def test_the_launcher_override_does_not_ride_along(self):
        """It is read here; a session that inherited it could relaunch itself."""
        env = ss.launch_env({ss.ENV: '["ghostty"]', "PATH": "/usr/bin"})
        self.assertNotIn(ss.ENV, env)

    def test_the_new_session_keeps_its_own_transcript(self):
        """Dropping the child marker is the fix; the lane audit covers a branch, not the reasoning."""
        env = ss.launch_env(dict(os.environ, CLAUDE_CODE_CHILD_SESSION="1"))
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", env)


if __name__ == "__main__":
    unittest.main()


class BootstrapNamesItsPathsTest(unittest.TestCase):
    """Zach, 23:33-23:34: the bootstrap said "in this directory" and Ghostty does not reliably give
    the tab the directory it was asked for, so "this directory" was sometimes not the worktree and
    the session could not find its own assignment.

    Two fixes, because one of them is the cause and the other is the belt. `env -C` pins the working
    directory in the command itself rather than trusting a surface configuration field — shell-free,
    and it exits 125 loudly on a bad path instead of starting a session somewhere arbitrary. And the
    bootstrap names both paths absolutely, so a session that lands in the wrong place can still read
    its assignment and knows where it was supposed to be.
    """

    def argv(self, cwd="/tmp/wt"):
        return ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd=cwd, title="wt-docs")

    def test_the_bootstrap_names_the_dispatch_file_absolutely(self):
        self.assertIn(f"/tmp/wt/{ss.PROMPT_FILE}", self.argv()[-1])

    def test_the_bootstrap_names_the_working_directory_absolutely(self):
        self.assertIn("/tmp/wt", self.argv()[-1])

    def test_it_no_longer_says_in_this_directory(self):
        """The phrase that made the assignment unfindable whenever the cwd was not the worktree."""
        self.assertNotIn("in this directory", self.argv()[-1])

    def test_a_relative_cwd_is_resolved_before_it_reaches_the_session(self):
        """A session cannot resolve a relative path against a directory it may not be standing in."""
        token = self.argv(cwd=".")[-1]
        self.assertNotIn(" ./", token)
        self.assertIn(str(Path(".").resolve()), token)

    def test_the_bootstrap_carries_no_shell_metacharacter(self):
        """It is a launcher token like any other and the guard runs on the template."""
        self.assertIsNone(ss.METACHARACTERS.search(ss.BOOTSTRAP))

    def test_the_ghostty_command_pins_the_directory_with_env(self):
        s = ss.ghostty_script(cwd="/tmp/wt", agent_type="implementer", claude=Path("/opt/homebrew/bin/claude"))
        self.assertRegex(s, r"/usr/bin/env -C /tmp/wt (?:'PATH=[^']*'|PATH=\S*) /opt/homebrew/bin/claude")

    def test_the_ghostty_bootstrap_names_the_paths_too(self):
        s = ss.ghostty_script(cwd="/tmp/wt", agent_type="implementer", claude=Path("/opt/homebrew/bin/claude"))
        self.assertIn(shlex.quote(f"/tmp/wt/{ss.PROMPT_FILE}"), s.replace("\\", ""))


class SessionNameTest(unittest.TestCase):
    """Zach, 2026-09-22 16:18: "the NUMERIC NAMES I ASSIGN ARE THE ONLY NAMES THEY ARE ALLOWED TO KEEP".

    The tab title was all `--title` ever set. `claude` itself got no name, so the harness named the
    session after its directory and then retitled it from its first task: impl07 was listed as
    `lab-write-patient-check-merge` by its second turn. A name given at launch registers as the
    user's, and the harness never retitles one of those.
    """

    setUp = DispatchFileTest.setUp
    spawn = DispatchFileTest.spawn

    def test_the_launch_names_the_session(self):
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/wt", title="impl07")
        self.assertEqual(argv[argv.index("--name") + 1], "impl07")

    def test_the_ghostty_command_names_the_session(self):
        s = ss.ghostty_script(cwd="/tmp/wt", agent_type="implementer", claude=Path("/opt/homebrew/bin/claude"),
                              title="impl07")
        self.assertIn(" --name impl07 ", s)

    def test_no_name_drops_the_flag_and_its_value_together(self):
        """Removed as a pair, never substituted empty: `--name ''` would be a session named nothing."""
        s = ss.ghostty_script(cwd="/tmp/wt", agent_type="implementer", claude=Path("/opt/homebrew/bin/claude"))
        self.assertNotIn("--name", s)

    def test_name_is_the_flag_and_title_still_means_it(self):
        out, calls = self.spawn(extra=["--name", "impl07"])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(calls[0]["argv"][1], "impl07")

    def test_the_assignment_carries_the_name_the_launch_gave(self):
        self.spawn(extra=["--name", "impl07"])
        self.assertIn("You are `impl07`.", (self.tree / ss.PROMPT_FILE).read_text())

    def test_a_name_that_is_not_a_session_name_writes_nothing_and_starts_nothing(self):
        for bad in ("impl 07", "-x", "a;b", "impl07 [abc123]", "$(whoami)"):
            with self.subTest(bad=bad):
                out, calls = self.spawn(extra=[f"--name={bad}"])
                self.assertEqual(out.returncode, 1, bad)
                self.assertIn("refused", out.stderr)
                self.assertFalse((self.tree / ss.PROMPT_FILE).exists())
                self.assertEqual(calls, [])
