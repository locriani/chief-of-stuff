"""Unit tests for scripts/git_trees.py: the read-only git reader behind the orphan check (#43).

Real repos, as in test_audit_tasks: a bare origin, a clone, and worktrees off it. `rev-list` and `@{u}`
have no useful fake.
"""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import git_trees  # noqa: E402

ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
       "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={**ENV, "HOME": str(cwd)}).stdout.strip()


def commit(tree: Path, name: str) -> None:
    (tree / name).write_text("work\n")
    git("add", name, cwd=tree)
    git("commit", "-m", name, cwd=tree)


class Repo(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        origin, self.clone, self.trees = self.root / "origin.git", self.root / "repo", self.root / "trees"
        origin.mkdir()
        git("init", "--bare", "--initial-branch=main", ".", cwd=origin)
        self.clone.mkdir()
        git("init", "--initial-branch=main", ".", cwd=self.clone)
        commit(self.clone, "README.md")
        git("remote", "add", "origin", str(origin), cwd=self.clone)
        git("push", "-u", "origin", "main", cwd=self.clone)
        self.trees.mkdir()

    def tree(self, name: str, branch: str) -> Path:
        git("worktree", "add", "-b", branch, str(self.trees / name), "main", cwd=self.clone)
        return self.trees / name


class ReadStateTest(Repo):
    def test_a_fresh_branch_at_main_holds_nothing(self) -> None:
        s = git_trees.read_state(self.tree("wt-a", "feat/a"))
        self.assertEqual((s.branch, s.dirty, s.off_origin, s.unpushed), ("feat/a", 0, 0, None))

    def test_uncommitted_files_are_counted(self) -> None:
        t = self.tree("wt-a", "feat/a")
        (t / "one.txt").write_text("x\n")
        (t / "two.txt").write_text("x\n")
        self.assertEqual(git_trees.read_state(t).dirty, 2)

    def test_a_local_commit_has_no_upstream(self) -> None:
        t = self.tree("wt-a", "feat/a")
        commit(t, "a.txt")
        s = git_trees.read_state(t)
        self.assertEqual((s.off_origin, s.unpushed), (1, None))

    def test_a_pushed_branch_has_nothing_unpushed(self) -> None:
        t = self.tree("wt-a", "feat/a")
        commit(t, "a.txt")
        git("push", "-u", "origin", "feat/a", cwd=t)
        s = git_trees.read_state(t)
        self.assertEqual((s.off_origin, s.unpushed), (1, 0))

    def test_a_commit_after_the_push_is_unpushed(self) -> None:
        t = self.tree("wt-a", "feat/a")
        commit(t, "a.txt")
        git("push", "-u", "origin", "feat/a", cwd=t)
        commit(t, "b.txt")
        s = git_trees.read_state(t)
        self.assertEqual((s.off_origin, s.unpushed), (2, 1))

    def test_with_no_origin_main_the_count_is_unknown(self) -> None:
        t = self.tree("wt-a", "feat/a")
        git("update-ref", "-d", "refs/remotes/origin/main", cwd=self.clone)
        self.assertIsNone(git_trees.read_state(t).off_origin)


class DiscoverTest(Repo):
    def test_every_tree_under_the_worktrees_dir_is_found(self) -> None:
        """No File ownership row is needed to be seen: the field report's tree had none."""
        self.tree("wt-b", "feat/b")
        self.tree("wt-a", "feat/a")
        (self.trees / "notes").mkdir()
        found = git_trees.discover(self.root, "trees/")
        self.assertEqual([p.name for p in found], ["wt-a", "wt-b"])

    def test_no_worktrees_line_discovers_nothing(self) -> None:
        """The root's own children include the main clone, which is nobody's worktree."""
        self.tree("wt-a", "feat/a")
        self.assertEqual(git_trees.discover(self.root, ""), [])

    def test_a_link_out_of_the_workspace_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as away:
            (Path(away) / ".git").mkdir()
            (self.trees / "escape").symlink_to(away)
            self.assertEqual(git_trees.discover(self.root, "trees/"), [])


class ShaVerdictTest(unittest.TestCase):
    """#49: `audit --sha` answers in one line, and the exit code is the verdict. Pure: no git here.

    The lines here carry no ` [<tree>]` suffix: the caller (`audit_tasks`, which asks every repository under the
    worktrees directory) appends the tree's name, so `sha_verdict` and `check_sha` answer about one repository only.
    """

    S, T = "54b7eb3", "9c790f3"

    def test_the_table(self) -> None:
        err = "fatal: unable to access the remote"
        table = [
            # what, fetch_error, exists, ancestor -> code, line
            ("fetched, ancestor", "", True, True,
             0, "sha 54b7eb3: on origin/main 9c790f3 (fetched)"),
            ("fetch failed, ancestor of the last-fetched ref: never a yes", err, True, True,
             2, f"sha 54b7eb3: unknown \u2014 fetch failed: {err}; origin/main 9c790f3 as last fetched holds it"),
            ("fetched, not an ancestor", "", True, False,
             1, "sha 54b7eb3: not on origin/main 9c790f3 (fetched)"),
            ("fetched, no such commit", "", False, False,
             1, "sha 54b7eb3: no such commit after fetch, so not on origin/main 9c790f3"),
            ("fetch failed, not an ancestor", err, True, False,
             2, f"sha 54b7eb3: unknown \u2014 fetch failed: {err}; origin/main 9c790f3 as last fetched does not hold it"),
            ("fetch failed, no such commit", err, False, False,
             2, f"sha 54b7eb3: unknown \u2014 fetch failed: {err}; origin/main 9c790f3 as last fetched does not hold it"),
        ]
        for what, fetch_error, exists, ancestor, code, line in table:
            with self.subTest(what):
                self.assertEqual(git_trees.sha_verdict(self.S, fetch_error, exists, ancestor, self.T), (code, line))

    def test_a_fetch_failure_still_says_local_main_holds_it(self) -> None:
        """#49 round 7, 5: the sixth argument, `local`, is True when local main holds the commit. The answer stays
        unknown (exit 2), and the fact is said instead of dropped; False reads as the table above."""
        err = "fatal: unable to access the remote"
        self.assertEqual(git_trees.sha_verdict(self.S, err, True, False, self.T, True),
                         (2, f"sha 54b7eb3: unknown \u2014 fetch failed: {err}; origin/main 9c790f3 as last fetched does not hold it; local main holds it"))
        self.assertEqual(git_trees.sha_verdict(self.S, err, True, False, self.T, False),
                         git_trees.sha_verdict(self.S, err, True, False, self.T))

    def test_the_sha_is_printed_as_it_was_given(self) -> None:
        full = "54b7eb3" + "0" * 33
        self.assertEqual(git_trees.sha_verdict(full, "", True, True, self.T),
                         (0, f"sha {full}: on origin/main 9c790f3 (fetched)"))


class FetchBaseTest(Repo):
    """#49: the audit never fetched, so `origin/main` was whatever the last fetch left."""

    def push_from_another_clone(self, tag: str | None = None) -> str:
        other = Path(tempfile.mkdtemp(dir=self.root, prefix="other-"))
        git("clone", "-q", str(self.root / "origin.git"), str(other), cwd=self.root)
        commit(other, f"theirs-{other.name}.txt")
        if tag:
            git("tag", tag, cwd=other)
        git("push", "-q", "origin", "main", *([tag] if tag else []), cwd=other)
        return git("rev-parse", "HEAD", cwd=other)

    def test_success_is_the_empty_string_and_origin_main_has_moved(self) -> None:
        new = self.push_from_another_clone()
        self.assertNotEqual(git("rev-parse", "origin/main", cwd=self.clone), new)
        self.assertEqual(git_trees.fetch_base(self.clone), "")
        self.assertEqual(git("rev-parse", "origin/main", cwd=self.clone), new)

    def test_origin_main_moves_whatever_the_fetch_refspec_is(self) -> None:
        """`git fetch origin main` only updates `refs/remotes/origin/main` when `remote.origin.fetch` maps it."""
        for what, refspec in (("no refspec", None), ("maps only another branch", "+refs/heads/other:refs/remotes/origin/other")):
            with self.subTest(what):
                if refspec is None:
                    git("config", "--unset-all", "remote.origin.fetch", cwd=self.clone)
                else:
                    git("config", "--replace-all", "remote.origin.fetch", refspec, cwd=self.clone)
                new = self.push_from_another_clone()
                self.assertEqual(git_trees.fetch_base(self.clone), "")
                self.assertEqual(git("rev-parse", "origin/main", cwd=self.clone), new)

    def test_it_fetches_from_a_worktree_too(self) -> None:
        tree = self.tree("wt-a", "feat/a")
        new = self.push_from_another_clone()
        self.assertEqual(git_trees.fetch_base(tree), "")
        self.assertEqual(git("rev-parse", "origin/main", cwd=tree), new)

    def test_a_missing_remote_is_an_error_string_not_an_exception(self) -> None:
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        err = git_trees.fetch_base(self.clone)
        self.assertIsInstance(err, str)
        self.assertTrue(err.strip())
        self.assertNotIn("\n", err)  # one line: it is printed inside the verdict line

    def test_a_non_utf8_byte_on_stderr_is_an_unknown_line_not_a_traceback(self) -> None:
        """#49 round 11: git, ssh or a credential helper can write any byte to stderr; decoding it as text raised
        `UnicodeDecodeError`, which is neither `OSError` nor `SubprocessError`. A REAL subprocess, not a fake `run` (a fake
        returning a str would hide it): `core.sshCommand` in this clone is a Python one-liner that writes `fatal: boom \xff`
        to stderr and exits 128, and origin is an ssh-style URL, so the real `git fetch` runs it and relays the bytes."""
        helper = "import sys; sys.stderr.buffer.write(b'fatal: boom \\xff\\n'); sys.exit(128)"
        git("remote", "set-url", "origin", "ssh://example.invalid/x.git", cwd=self.clone)
        git("config", "core.sshCommand", f"{shlex.quote(sys.executable)} -c {shlex.quote(helper)}", cwd=self.clone)
        err = git_trees.fetch_base(self.clone)
        self.assertIsInstance(err, str)
        self.assertTrue(err.strip())
        self.assertEqual([c for c in err if not c.isprintable()], [], repr(err))
        self.assertIn("fatal: boom", err)

    def test_git_that_cannot_run_or_times_out_is_an_error_string(self) -> None:
        for exc in (OSError("no git"), subprocess.TimeoutExpired(["git"], 30)):
            with self.subTest(type(exc).__name__), patch.object(git_trees.subprocess, "run", side_effect=exc):
                err = git_trees.fetch_base(self.clone)
                self.assertIsInstance(err, str)
                self.assertTrue(err.strip())

    def test_the_error_never_carries_the_exceptions_text(self) -> None:
        """#49 round 9, 5: a timeout's text repeats its command, which holds the tree's path; the error is printed."""
        cmd = ["git", "-C", str(self.clone), "fetch", "-q", "origin"]
        for exc in (subprocess.TimeoutExpired(cmd=cmd, timeout=30), OSError(2, "No such file", str(self.clone))):
            with self.subTest(type(exc).__name__), patch.object(git_trees.subprocess, "run", side_effect=exc):
                err = git_trees.fetch_base(self.clone)
                self.assertTrue(err.strip())
                for path in (self.clone, self.clone.resolve()):
                    self.assertNotIn(str(path), err)

    def test_the_call_is_the_callers_environment_with_prompts_off(self) -> None:
        """Not through the sandboxed `git()`: a fetch needs the caller's credentials and ssh agent."""
        seen = {}

        def fake(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.dict(os.environ, {"COS_FETCH_ENV_PROBE": "kept"}), patch.object(git_trees.subprocess, "run", fake):
            self.assertEqual(git_trees.fetch_base(self.clone, timeout=7), "")
        self.assertEqual(seen["argv"], ["git", "-C", str(self.clone), "fetch", "-q", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"])
        self.assertEqual(seen["kw"]["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(seen["kw"]["env"]["COS_FETCH_ENV_PROBE"], "kept")
        self.assertEqual(seen["kw"]["timeout"], 7)
        self.assertFalse(seen["kw"].get("shell"))

    def test_the_fetch_writes_only_origin_main_no_tags(self) -> None:
        """#49 D: upstream gains a tag on main; the default tag auto-follow would copy it into the clone."""
        new = self.push_from_another_clone(tag="upstream-tag")
        self.assertEqual(git_trees.fetch_base(self.clone), "")
        self.assertEqual(git("rev-parse", "origin/main", cwd=self.clone), new)
        self.assertEqual(git("tag", "--list", cwd=self.clone), "")

    def test_the_fetch_does_not_carry_the_variables_that_pick_a_repository(self) -> None:
        """#49 C: a caller's `GIT_DIR` beats `-C`, so the fetch would write into another repository while the reads
        look at this one. Credentials and ssh settings are still the caller's."""
        seen = {}

        def fake(argv, **kw):
            seen["env"] = kw["env"]
            return subprocess.CompletedProcess(argv, 0, "", "")

        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            with self.subTest(var), patch.dict(os.environ, {var: "/elsewhere"}), patch.object(git_trees.subprocess, "run", fake):
                git_trees.fetch_base(self.clone)
                self.assertNotIn(var, list(seen["env"]), f"{var} reached the fetch")
        with patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -i key"}), patch.object(git_trees.subprocess, "run", fake):
            git_trees.fetch_base(self.clone)
            self.assertEqual(seen["env"]["GIT_SSH_COMMAND"], "ssh -i key")

    def test_a_callers_git_dir_does_not_move_the_fetch_to_another_repository(self) -> None:
        decoy = Path(tempfile.mkdtemp(dir=self.root, prefix="decoy-"))
        git("clone", "-q", str(self.root / "origin.git"), str(decoy), cwd=self.root)
        new = self.push_from_another_clone()
        with patch.dict(os.environ, {"GIT_DIR": str(decoy / ".git")}):
            self.assertEqual(git_trees.fetch_base(self.clone), "")
        self.assertEqual(git("rev-parse", "origin/main", cwd=self.clone), new)

    def test_git_failing_with_no_stderr_is_still_an_error_string(self) -> None:
        fake = subprocess.CompletedProcess(["git"], 1, "", "")
        with patch.object(git_trees.subprocess, "run", return_value=fake):
            self.assertTrue(git_trees.fetch_base(self.clone).strip())

    def test_the_error_is_the_first_fatal_line_and_never_holds_the_trees_path(self) -> None:
        """A missing remote prints four lines; the last, `and the repository exists.`, says nothing. The first `fatal:`
        line names the remote, and a remote under the tree would put the tree's path in a line that is printed."""
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        err = git_trees.fetch_base(self.clone)
        self.assertTrue(err.startswith("fatal: "), err)
        self.assertNotIn("the repository exists", err)
        git("remote", "set-url", "origin", str(self.clone / "gone.git"), cwd=self.clone)
        err = git_trees.fetch_base(self.clone)
        self.assertTrue(err.startswith("fatal: "), err)
        self.assertNotIn(str(self.clone), err)
        self.assertNotIn(str(self.clone.resolve()), err)


    def test_the_error_text_a_remote_controls_carries_no_raw_control_character(self) -> None:
        """#49 round 8, 3b; round 10, 3: the line is printed, and git's stderr is whatever the remote makes it.
        `subprocess.run` is faked. Pinned: no character that is not printable (a control character, DEL, a C1 control such
        as CSI `\\x9b`, a bidi override, U+2028/9), `fatal: a` survives, and a to f stay in order (so a carriage return
        does not cut the text). The escape's spelling is not pinned."""
        hostile = subprocess.CompletedProcess([], 128, "", "fatal: a\x1b[31mb\rc\x7fd\x9be\u202ef\n")
        with patch.object(git_trees.subprocess, "run", return_value=hostile):
            line = git_trees.fetch_base(self.clone)
        self.assertEqual([c for c in line if not c.isprintable()], [], repr(line))
        self.assertTrue(line.startswith("fatal: a"), repr(line))
        self.assertRegex(line[len("fatal: "):], "a.*b.*c.*d.*e.*f")


class GitReaderTest(unittest.TestCase):
    def test_git_that_cannot_run_or_times_out_is_128_not_1(self) -> None:
        """`merge-base --is-ancestor` answers a no with exit 1, so a failed run must not look like one: 128 is git's own
        failure code. The text is what went wrong, for a caller that prints it."""
        for exc in (OSError("no git"), subprocess.TimeoutExpired(["git"], 15)):
            with self.subTest(type(exc).__name__), patch.object(git_trees.subprocess, "run", side_effect=exc):
                code, text = git_trees.git(["rev-parse", "HEAD"], Path("."))
                self.assertEqual(code, 128)
                self.assertTrue(text.strip())


class NonUtf8OutputTest(Repo):
    def test_a_ref_name_with_a_non_utf8_byte_is_read_not_a_traceback(self) -> None:
        """#49 round 11: `git()` also decodes output as text. A checkout on a filesystem that allows any byte in a name (Linux)
        can be on a branch like `b\\xff`; macOS refuses to create one, so it is written as git would store it: a packed ref
        and a HEAD that names it. `git rev-parse --abbrev-ref HEAD` then prints the byte. Real repository, no fake."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        git_dir = self.clone / ".git"
        (git_dir / "packed-refs").write_bytes(b"# pack-refs with: peeled fully-peeled sorted \n" + sha.encode() + b" refs/heads/b\xff\n")
        (git_dir / "HEAD").write_bytes(b"ref: refs/heads/b\xff\n")
        raw = subprocess.run(["git", "-C", str(self.clone), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True)
        self.assertEqual(raw.stdout, b"b\xff\n")  # the premise: git itself prints the byte
        code, text = git_trees.git(["rev-parse", "--abbrev-ref", "HEAD"], self.clone)
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("b"), repr(text))
        self.assertEqual(git_trees.read_state(self.clone).branch[:1], "b")


class ShaExitTest(unittest.TestCase):
    def test_an_unknown_blocks_a_yes_only_when_that_repository_may_hold_the_commit(self) -> None:
        """#49 round 10, 2: over `(code, held)`: 1 if any repository holds the commit and says no; else 2 if the list is
        empty or any repository is unknown AND held (it may hold the commit, or be the project the claim is about); else
        0 if any says yes; else 2 if any is unknown; else 1. An unrelated repository (no remote, a `master` default, a
        shallow clone, offline) does not hold the commit, so it cannot veto a yes forever."""
        yes, no_held, no_absent = (0, True), (1, True), (1, False)
        unknown_held, unknown_absent = (2, True), (2, False)
        table = [
            ("yes alone", [yes], 0),
            ("yes, no such commit elsewhere", [yes, no_absent], 0),
            ("yes, unknown that holds it", [yes, unknown_held], 2),
            ("yes, unknown that does not", [yes, unknown_absent], 0),
            ("yes, a holder says no", [yes, no_held], 1),
            ("a holder says no, unknown that does not hold it", [no_held, unknown_absent], 1),
            ("a holder says no, unknown that holds it", [unknown_held, no_held], 1),
            ("unknown that does not hold it, alone", [unknown_absent], 2),
            ("unknown that holds it, alone", [unknown_held], 2),
            ("no such commit, unknown that does not hold it", [no_absent, unknown_absent], 2),
            ("no such commit alone", [no_absent], 1),
            ("a holder says no alone", [no_held], 1),
            ("empty", [], 2),
        ]
        for what, answers, want in table:
            with self.subTest(what):
                self.assertEqual(git_trees.sha_exit(answers), want)


def git_makes_sha256() -> bool:
    with tempfile.TemporaryDirectory() as d:
        return subprocess.run(["git", "init", "-q", "--object-format=sha256", d], capture_output=True, env=ENV).returncode == 0


@unittest.skipUnless(git_makes_sha256(), "this git cannot `init --object-format=sha256`")
class Sha256CheckTest(unittest.TestCase):
    def test_a_full_sha256_id_pushed_to_main_is_on_origin_main(self) -> None:
        """#49 round 11: a repository made with `--object-format=sha256` has 64-digit ids; `--sha` refused them as usage."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, clone = root / "origin.git", root / "repo"
            origin.mkdir()
            clone.mkdir()
            git("init", "--bare", "--object-format=sha256", "--initial-branch=main", ".", cwd=origin)
            git("init", "--object-format=sha256", "--initial-branch=main", ".", cwd=clone)
            commit(clone, "README.md")
            git("remote", "add", "origin", str(origin), cwd=clone)
            git("push", "-u", "origin", "main", cwd=clone)
            sha = git("rev-parse", "HEAD", cwd=clone)
            tip = git("rev-parse", "--short", "origin/main", cwd=clone)
            self.assertEqual(len(sha), 64)
            self.assertEqual(git_trees.check_sha(sha, clone), (0, f"sha {sha}: on origin/main {tip} (fetched)", True))


class CheckShaTest(Repo):
    def test_a_pushed_commit_is_on_origin_main(self) -> None:
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tip = git("rev-parse", "--short", "origin/main", cwd=self.clone)
        self.assertEqual(git_trees.check_sha(sha, self.clone), (0, f"sha {sha}: on origin/main {tip} (fetched)", True))

    def test_a_commit_only_on_a_branch_is_not(self) -> None:
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        sha = git("rev-parse", "HEAD", cwd=tree)
        tip = git("rev-parse", "--short", "origin/main", cwd=self.clone)
        self.assertEqual(git_trees.check_sha(sha, tree), (1, f"sha {sha}: not on origin/main {tip} (fetched)", True))

    def test_a_prefix_two_commits_share_is_unknown_not_a_pick(self) -> None:
        """Two commit ids start with it: no answer, whichever of them is on origin/main. `git` is patched, because two
        commits sharing a 7-hex prefix cannot be made cheaply."""
        calls = []

        def fake(args, cwd):
            calls.append(args)
            if args[0] == "rev-parse" and args[1].startswith("--disambiguate="):
                return 0, "9f3c1e7aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n9f3c1e7bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            if args[:2] == ["cat-file", "-t"]:
                return 0, "commit"
            return 0, "abc1234" if args[:2] == ["rev-parse", "--short"] else ""

        with patch.object(git_trees, "fetch_base", return_value=""), patch.object(git_trees, "git", fake):
            self.assertEqual(git_trees.check_sha("9f3c1e7", self.clone),
                             (2, "sha 9f3c1e7: unknown \u2014 more than one commit starts with it", True))
        self.assertFalse([c for c in calls if c[:1] == ["merge-base"]], calls)

    def tip(self) -> str:
        return git("rev-parse", "--short", "refs/remotes/origin/main", cwd=self.clone)

    def test_a_shallow_clone_never_gives_a_definite_no(self) -> None:
        """#49 F: the commit may be in the history the clone cut off."""
        commit(self.clone, "second.txt")
        git("push", "-q", "origin", "main", cwd=self.clone)
        older = git("rev-parse", "HEAD~1", cwd=self.clone)
        head = git("rev-parse", "HEAD", cwd=self.clone)
        shallow = self.root / "shallow"
        git("clone", "-q", "--depth", "1", f"file://{self.root / 'origin.git'}", str(shallow), cwd=self.root)
        self.assertEqual(git("rev-parse", "--is-shallow-repository", cwd=shallow), "true")
        self.assertEqual(git_trees.check_sha(older, shallow),
                         (2, f"sha {older}: unknown \u2014 shallow clone, so history is cut off", False))
        # a yes is still a yes: the commit is reachable from the tip whatever was cut off
        tip = git("rev-parse", "--short", "origin/main", cwd=shallow)
        self.assertEqual(git_trees.check_sha(head, shallow), (0, f"sha {head}: on origin/main {tip} (fetched)", True))

    def test_a_commit_on_local_main_not_pushed_is_said_so(self) -> None:
        """#49 G: distinct from a commit on no main at all (`not on origin/main`), since the row audit reports an
        unpushed local main as a fact."""
        commit(self.clone, "local.txt")
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        want = (1, f"sha {sha}: on local main only, not pushed to origin/main {self.tip()} (fetched)", True)
        self.assertEqual(git_trees.check_sha(sha, self.clone), want)
        self.assertEqual(git_trees.check_sha(sha, self.tree("wt-a", "feat/a")), want)

    def test_git_that_cannot_compare_is_unknown_not_a_no(self) -> None:
        """#49 H: `merge-base --is-ancestor` exits 1 for a no. Any other exit is git failing."""
        on = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        off = git("rev-parse", "HEAD", cwd=tree)
        real = git_trees.git

        def fake(args, cwd):
            return (128, "fatal: boom") if args[:2] == ["merge-base", "--is-ancestor"] else real(args, cwd)

        for what, sha in (("on origin/main", on), ("not on it", off)):
            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, tree),
                                 (2, f"sha {sha}: unknown \u2014 git could not compare it with origin/main", True))

    def test_an_annotated_tags_object_id_is_not_a_commit(self) -> None:
        """#49 H: `cat-file -e <id>^{commit}` peels a tag, so the tag's id passes for the commit it points at."""
        head = git("rev-parse", "HEAD", cwd=self.clone)
        git("tag", "-a", "v1", "-m", "release", cwd=self.clone)
        tag = git("rev-parse", "v1", cwd=self.clone)
        self.assertNotEqual(tag, head)
        for given in (tag, tag[:7]):
            with self.subTest(given):
                self.assertEqual(git_trees.check_sha(given, self.clone),
                                 (1, f"sha {given}: no such commit after fetch, so not on origin/main {self.tip()}", False))

    def test_a_read_that_fails_is_unknown_never_no_such_commit(self) -> None:
        """#49 round 7, 2: exit 128 from the read that lists the commits (`rev-parse --disambiguate`) or from `cat-file -t`
        on an id it listed means git could not read the repository, which says nothing about whether the commit exists.
        `held` is True (round 10, 2): git could not read the repository, so it may hold the commit. Not
        `cat-file -e <id>^{commit}`: that exits 128 for a blob or a missing object too (see the next test), so its 128
        is an answer, not a fault."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        real = git_trees.git
        for what, broken in (("disambiguate", lambda a: a[0] == "rev-parse" and a[1].startswith("--disambiguate=")),
                             ("cat-file -t", lambda a: a[:2] == ["cat-file", "-t"])):
            def fake(args, cwd, broken=broken):
                return (128, "fatal: boom") if broken(args) else real(args, cwd)

            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, self.clone),
                                 (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))

    def test_a_prefix_that_names_a_blob_is_no_such_commit_not_a_read_fault(self) -> None:
        """`rev-parse --disambiguate` lists objects of every type, and `cat-file -e <blob>^{commit}` exits 128. That is
        git saying it is not a commit: no such commit (exit 1), not `could not read this repository`."""
        blob = git("rev-parse", "HEAD:README.md", cwd=self.clone)
        self.assertEqual(git("cat-file", "-t", blob[:7], cwd=self.clone), "blob")
        self.assertEqual(git_trees.check_sha(blob[:7], self.clone),
                         (1, f"sha {blob[:7]}: no such commit after fetch, so not on origin/main {self.tip()}", False))

    def test_a_tag_named_main_is_not_local_main(self) -> None:
        """#49 round 7, 4: `main` alone names the tag before the branch. The commit is on a feature branch, so the answer
        is the plain `not on origin/main`, never `on local main only`. Green when written: the code reads `refs/heads/main`."""
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        sha = git("rev-parse", "HEAD", cwd=tree)
        git("tag", "main", sha, cwd=self.clone)
        self.assertEqual(git_trees.check_sha(sha, tree), (1, f"sha {sha}: not on origin/main {self.tip()} (fetched)", True))

    def test_the_fetch_failure_line_still_says_local_main_holds_it(self) -> None:
        """#49 round 7, 5: unknown stays unknown, and the fact the old line dropped is said. A commit on a branch only
        keeps the line as it was."""
        commit(self.clone, "local.txt")
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        branch = self.tree("wt-a", "feat/a")
        commit(branch, "a.txt")
        on_branch = git("rev-parse", "HEAD", cwd=branch)
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        err = git_trees.fetch_base(self.clone)
        self.assertTrue(err)
        head = f"unknown \u2014 fetch failed: {err}; origin/main {self.tip()} as last fetched does not hold it"
        self.assertEqual(git_trees.check_sha(sha, self.clone), (2, f"sha {sha}: {head}; local main holds it", True))
        self.assertEqual(git_trees.check_sha(on_branch, self.clone), (2, f"sha {on_branch}: {head}", True))

    def test_a_shallow_clone_still_says_local_main_holds_it(self) -> None:
        commit(self.clone, "second.txt")
        git("push", "-q", "origin", "main", cwd=self.clone)
        shallow = self.root / "shallow"
        git("clone", "-q", "--depth", "1", f"file://{self.root / 'origin.git'}", str(shallow), cwd=self.root)
        commit(shallow, "local.txt")
        sha = git("rev-parse", "HEAD", cwd=shallow)
        self.assertEqual(git_trees.check_sha(sha, shallow),
                         (2, f"sha {sha}: unknown \u2014 shallow clone, so history is cut off; local main holds it", True))

    def test_a_grafts_file_written_by_the_fetch_is_seen(self) -> None:
        """#49 round 7, 6: the grafts check ran before the fetch, so a file the fetch left was not seen. `fetch_base` is
        patched to write one and report success."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.clone))

        def fetch_leaves_grafts(tree, timeout=30):
            (common / "info").mkdir(exist_ok=True)
            (common / "info" / "grafts").write_text(f"{sha} {sha}\n")
            return ""

        with patch.object(git_trees, "fetch_base", fetch_leaves_grafts):
            self.assertEqual(git_trees.check_sha(sha, self.clone),
                             (2, f"sha {sha}: unknown \u2014 this repository has grafts, so ancestry cannot be trusted", True))

    def test_a_grafts_lookup_that_fails_is_unknown_not_no_grafts(self) -> None:
        """#49 round 8, 1: only the `--git-path` call fails (128); every other git call is real, on a commit that is on
        origin/main. A repository whose grafts cannot be looked up is unknown, with `held` as already known."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        real = git_trees.git

        def lookup_fails(args, cwd, *rest, **kw):
            return (128, "") if "--git-path" in args else real(args, cwd, *rest, **kw)

        with patch.object(git_trees, "git", lookup_fails):
            self.assertEqual(git_trees.check_sha(sha, self.clone),
                             (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))

    def test_a_grafts_file_that_cannot_be_examined_is_unknown_not_no_grafts(self) -> None:
        """#49 round 8, 1: `os.stat` (which `Path.stat`, `is_file` and `exists` all go through) raises PermissionError,
        for the grafts path only; every other stat is real. The file exists and is empty, so a reader that can examine
        it finds no grafts (exit 0)."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        grafts = self.clone / ".git" / "info" / "grafts"
        grafts.parent.mkdir(exist_ok=True)
        grafts.write_text("")
        real = os.stat

        def denied(path, *args, **kw):
            if isinstance(path, (str, os.PathLike)) and os.fspath(path).endswith(os.path.join("info", "grafts")):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real(path, *args, **kw)

        self.assertEqual(git_trees.check_sha(sha, self.clone)[0], 0)
        with patch.object(os, "stat", denied):
            self.assertEqual(git_trees.check_sha(sha, self.clone),
                             (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))

    NOT_A_REPOSITORY = "unknown \u2014 not a git repository"
    COULD_NOT_READ = "unknown \u2014 git could not read this repository"

    def pruned(self, name: str = "wt-dead") -> tuple[str, Path]:
        """`(sha, tree)`: a real `git worktree add`, then `<common>/worktrees/<name>` deleted. The repository is still
        there and no longer knows this tree."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree(name, f"feat/{name}")
        shutil.rmtree(self.clone / ".git" / "worktrees" / name)
        return sha, tree

    def assert_could_not_read(self, sha: str, tree: Path) -> None:
        self.assertEqual(git_trees.check_sha(sha, tree), (2, f"sha {sha}: {self.COULD_NOT_READ}", True))

    def test_a_pruned_worktree_is_not_a_repository_and_holds_nothing(self) -> None:
        """#294: `git worktree add`, then `<common>/worktrees/<name>` is deleted (`git worktree prune` after the directory
        went): the `.git` file names `<common>/worktrees/<name>`, that is gone, `<common>` is still there. The repository
        no longer knows this tree, so it cannot hold the commit: `held` is False and it blocks no yes."""
        sha, tree = self.pruned()
        self.assertEqual(git_trees.git(["rev-parse", "--git-dir"], tree)[0], 128)
        self.assertEqual(git_trees.check_sha(sha, tree), (2, f"sha {sha}: {self.NOT_A_REPOSITORY}", False))

    def test_a_pruned_worktree_is_decided_from_the_dot_git_file_not_from_a_git_probe(self) -> None:
        """#294: git timing out or not running must not turn a pruned worktree into one that may hold the commit, so the
        decision is read from the `.git` file and the filesystem. Every `git` call answers `(128, "")`."""
        sha, tree = self.pruned()
        with patch.object(git_trees, "git", lambda *a, **kw: (128, "")):
            self.assertEqual(git_trees.check_sha(sha, tree), (2, f"sha {sha}: {self.NOT_A_REPOSITORY}", False))

    def test_a_dot_git_file_pointing_at_a_repository_that_is_gone_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 2: `gitdir: /nonexistent/path` (and the next test) has no repository behind it at all, so nothing says
        the repository forgot this tree; it may be the project's, on a volume that is not mounted. It blocks a yes."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        broken = self.trees / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/path\n")
        self.assertEqual(git_trees.git(["rev-parse", "--git-dir"], broken)[0], 128)
        self.assert_could_not_read(sha, broken)

    def test_a_linked_worktree_whose_main_clone_was_moved_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 2: `<common>/worktrees/<name>` is named by the `.git` file and `<common>` does not exist (the clone was
        renamed, or its volume is gone). The repository is not known to have forgotten the tree: unknown, `held` True."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree("wt-a", "feat/a")
        self.clone.rename(self.root / "repo-moved")
        self.assertEqual(git_trees.git(["rev-parse", "--git-dir"], tree)[0], 128)
        self.assert_could_not_read(sha, tree)

    def test_git_refusing_or_not_running_on_a_healthy_repository_could_not_read_it_and_it_may_hold_it(self) -> None:
        """#294 round 2: the repository is real and healthy, but every `git` call fails: `fatal: detected dubious
        ownership` (git refuses a directory another user owns), or `(128, "")` (a timeout, git not on PATH). That is not
        evidence the repository is not one, so `held` stays True."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree("wt-a", "feat/a")
        dubious = f"fatal: detected dubious ownership in repository at '{tree}'"
        for what, answer in (("dubious ownership", (128, dubious)), ("git timed out or did not run", (128, ""))):
            with self.subTest(what), patch.object(git_trees, "git", lambda *a, answer=answer, **kw: answer):
                self.assert_could_not_read(sha, tree)
                self.assert_could_not_read(sha, self.clone)

    def test_a_dot_git_directory_git_cannot_use_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 2: a `.git` DIRECTORY with `HEAD` removed, in a temp dir inside no other repository. Observed: git
        walks up, finds none, and fails `rev-parse --git-dir` with `fatal: not a git repository (or any of the parent
        directories): .git`. Same probe result as a pruned worktree, but the evidence is the directory is there:
        `could not read`, `held` True."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        broken = self.trees / "broken"
        broken.mkdir()
        git("init", "--initial-branch=main", ".", cwd=broken)
        (broken / ".git" / "HEAD").unlink()
        code, out = git_trees.git(["rev-parse", "--git-dir"], broken)
        self.assertEqual(code, 128, out)
        self.assertIn("not a git repository", out)
        self.assert_could_not_read(sha, broken)

    def test_a_repository_git_can_open_but_not_read_may_hold_it(self) -> None:
        """#294 (was #49 round 10, 2): git finds the git directory (`rev-parse --git-dir` is real and succeeds) but the commit
        lookup (`rev-parse --disambiguate`, or `cat-file -t` on an id it listed) faults. That is a repository that may hold
        the commit: `git could not read this repository`, `held` True, so its unknown blocks a yes. Only the named call
        fails; the fake must not also fail `--git-dir`, or this would describe a repository that is not one."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree("wt-a", "feat/a")
        real = git_trees.git
        self.assertEqual(real(["rev-parse", "--git-dir"], tree)[0], 0)
        for what, broken in (("disambiguate", lambda a: a[0] == "rev-parse" and a[1].startswith("--disambiguate=")),
                             ("cat-file -t", lambda a: a[:2] == ["cat-file", "-t"])):
            def fake(args, cwd, *rest, broken=broken, **kw):
                return (128, "fatal: boom") if broken(args) else real(args, cwd, *rest, **kw)

            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, tree),
                                 (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))

    def test_origin_main_missing_though_the_fetch_succeeded_is_unknown(self) -> None:
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        git("update-ref", "-d", "refs/remotes/origin/main", cwd=self.clone)
        with patch.object(git_trees, "fetch_base", return_value=""):  # `sha` is on local main, so the line says so (round 9, 1)
            self.assertEqual(git_trees.check_sha(sha, self.clone),
                             (2, f"sha {sha}: unknown \u2014 no origin/main to check against; local main holds it", True))

    def test_no_origin_main_still_says_local_main_holds_it(self) -> None:
        """#49 round 9, 1: a clone whose `origin` was removed has no `refs/remotes/origin/main`. A commit on local main
        gets the `; local main holds it` suffix on the unknown line; a commit on a branch only, and one nobody has, keep
        the line without it."""
        on_main = git("rev-parse", "HEAD", cwd=self.clone)
        branch = self.tree("wt-a", "feat/a")
        commit(branch, "a.txt")
        on_branch = git("rev-parse", "HEAD", cwd=branch)
        nobody = "0123456789abcdef0123456789abcdef01234567"
        git("remote", "remove", "origin", cwd=self.clone)
        self.assertEqual(git("for-each-ref", "refs/remotes", cwd=self.clone), "")
        line = "unknown \u2014 no origin/main to check against"
        for what, sha, suffix, held in (("on local main", on_main, "; local main holds it", True),
                                        ("on a branch only", on_branch, "", True),
                                        ("nobody has it", nobody, "", False)):
            with self.subTest(what):
                self.assertEqual(git_trees.check_sha(sha, self.clone), (2, f"sha {sha}: {line}{suffix}", held))

    def test_a_single_call_that_fails_is_unknown_never_a_plain_no(self) -> None:
        """#49 round 9, 2; round 10, 1: only the named git call returns `(128, "")`; every other call is real. The commit is
        on a branch and not on origin/main, so a plain answer would be exit 1 `not on origin/main`. This tree has a local
        main, so the ref-existence lookup (`rev-parse --verify -q refs/heads/main`) says so (exit 0) and a failing
        merge-base is a real fault; when that lookup itself returns 128 (a missing ref is exit 1, a fault is 128) it is
        a fault too."""
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        sha = git("rev-parse", "HEAD", cwd=tree)
        real = git_trees.git
        is_lookup = lambda args: args[:1] == ["rev-parse"] and "--verify" in args and "refs/heads/main" in args
        self.assertEqual(real(["rev-parse", "--verify", "-q", "refs/heads/main"], tree)[0], 0)
        fails = {
            "the shallow check": lambda args: args[:2] == ["rev-parse", "--is-shallow-repository"],
            "the local main ancestry check": lambda args: args[:2] == ["merge-base", "--is-ancestor"] and args[-1] == "refs/heads/main",
            "the local main ref lookup": is_lookup,
        }
        for what, named in fails.items():
            def fake(args, cwd, *rest, **kw):
                return (128, "") if named(args) else real(args, cwd, *rest, **kw)

            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, tree),
                                 (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))

    def test_origin_main_holding_it_needs_no_local_main_fact(self) -> None:
        """A commit origin/main holds (the fetch worked) is a yes even when the local main lookup, or the ancestry check
        against local main, faults: local main only feeds the note and the `local main only` wording."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        real = git_trees.git
        faults = {
            "the local main ref lookup": lambda a: a[:1] == ["rev-parse"] and "--verify" in a and "refs/heads/main" in a,
            "the local main ancestry check": lambda a: a[:2] == ["merge-base", "--is-ancestor"] and a[-1] == "refs/heads/main",
        }
        for what, named in faults.items():
            hits = []

            def fake(args, cwd, *rest, named=named, hits=hits, **kw):
                if named(args):
                    hits.append(args)
                    return 128, ""
                return real(args, cwd, *rest, **kw)

            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, self.clone),
                                 (0, f"sha {sha}: on origin/main {self.tip()} (fetched)", True))
            self.assertTrue(hits, f"{what} was never reached")

    def test_a_tag_named_main_is_not_local_main_in_a_clone_with_no_local_main(self) -> None:
        """The sibling of `test_a_tag_named_main_is_not_local_main`, whose repository has a local main branch. Here there
        is no `refs/heads/main` at all, only a tag `main` on the unmerged branch commit: plain `not on origin/main`."""
        sha = self.pushed_branch_commit()
        for what, clone in self.clones_without_local_main().items():
            git("tag", "main", sha, cwd=clone)
            self.assertEqual(git("for-each-ref", "refs/heads/main", cwd=clone), "")
            with self.subTest(what):
                tip = git("rev-parse", "--short", "refs/remotes/origin/main", cwd=clone)
                self.assertEqual(git_trees.check_sha(sha, clone), (1, f"sha {sha}: not on origin/main {tip} (fetched)", True))

    def pushed_branch_commit(self) -> str:
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        git("push", "-q", "origin", "feat/a", cwd=tree)
        return git("rev-parse", "HEAD", cwd=tree)

    def clones_without_local_main(self) -> dict[str, Path]:
        """Two real clones of origin with no `refs/heads/main`: `clone -b feat/a`, and a clone whose main was deleted."""
        origin = f"file://{self.root / 'origin.git'}"
        only_branch = self.root / "only-branch"
        git("clone", "-q", "-b", "feat/a", origin, str(only_branch), cwd=self.root)
        deleted = self.root / "deleted"
        git("clone", "-q", origin, str(deleted), cwd=self.root)
        git("checkout", "-q", "feat/a", cwd=deleted)
        git("branch", "-D", "main", cwd=deleted)
        clones = {"clone -b feat/a": only_branch, "after branch -D main": deleted}
        for clone in clones.values():
            self.assertEqual(git("for-each-ref", "refs/heads/main", cwd=clone), "")
        return clones

    def test_a_clone_with_no_local_main_says_no_not_unknown(self) -> None:
        """#49 round 10, 1: `git clone -b feat <origin>` has no `refs/heads/main`, and `merge-base ... refs/heads/main`
        exits 128 on the missing ref. A pushed branch commit that is not on origin/main is the plain no, with no local-main
        suffix, whether the clone never had a main or had it deleted. Real repositories."""
        sha = self.pushed_branch_commit()
        for what, clone in self.clones_without_local_main().items():
            with self.subTest(what):
                tip = git("rev-parse", "--short", "refs/remotes/origin/main", cwd=clone)
                self.assertEqual(git_trees.check_sha(sha, clone), (1, f"sha {sha}: not on origin/main {tip} (fetched)", True))

    def test_a_clone_with_no_local_main_still_gives_a_yes_for_what_origin_main_holds(self) -> None:
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        self.pushed_branch_commit()
        for what, clone in self.clones_without_local_main().items():
            with self.subTest(what):
                tip = git("rev-parse", "--short", "refs/remotes/origin/main", cwd=clone)
                self.assertEqual(git_trees.check_sha(sha, clone), (0, f"sha {sha}: on origin/main {tip} (fetched)", True))

    def test_a_failed_fetch_in_a_clone_with_no_local_main_keeps_the_fetch_reason(self) -> None:
        """#49 round 10, 1: the line is the fetch-failed one, with no `; local main holds it` suffix (there is no local
        main), not `git could not read this repository`."""
        sha = self.pushed_branch_commit()
        for what, clone in self.clones_without_local_main().items():
            with self.subTest(what):
                git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=clone)
                err = git_trees.fetch_base(clone)
                self.assertTrue(err)
                tip = git("rev-parse", "--short", "refs/remotes/origin/main", cwd=clone)
                self.assertEqual(git_trees.check_sha(sha, clone),
                                 (2, f"sha {sha}: unknown \u2014 fetch failed: {err}; origin/main {tip} as last fetched does not hold it", True))


if __name__ == "__main__":
    unittest.main()
