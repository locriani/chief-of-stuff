"""Unit tests for scripts/git_trees.py: the read-only git reader behind the orphan check (#43).

Real repos, as in test_audit_tasks: a bare origin, a clone, and worktrees off it. `rev-list` and `@{u}`
have no useful fake.
"""

import os
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

    def test_git_that_cannot_run_or_times_out_is_an_error_string(self) -> None:
        for exc in (OSError("no git"), subprocess.TimeoutExpired(["git"], 30)):
            with self.subTest(type(exc).__name__), patch.object(git_trees.subprocess, "run", side_effect=exc):
                err = git_trees.fetch_base(self.clone)
                self.assertIsInstance(err, str)
                self.assertTrue(err.strip())

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


class GitReaderTest(unittest.TestCase):
    def test_git_that_cannot_run_or_times_out_is_128_not_1(self) -> None:
        """`merge-base --is-ancestor` answers a no with exit 1, so a failed run must not look like one: 128 is git's own
        failure code. The text is what went wrong, for a caller that prints it."""
        for exc in (OSError("no git"), subprocess.TimeoutExpired(["git"], 15)):
            with self.subTest(type(exc).__name__), patch.object(git_trees.subprocess, "run", side_effect=exc):
                code, text = git_trees.git(["rev-parse", "HEAD"], Path("."))
                self.assertEqual(code, 128)
                self.assertTrue(text.strip())


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
        `held` is False: nothing was read. Not `cat-file -e <id>^{commit}`: that exits 128 for a blob or a missing
        object too (see the next test), so its 128 is an answer, not a fault."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        real = git_trees.git
        for what, broken in (("disambiguate", lambda a: a[0] == "rev-parse" and a[1].startswith("--disambiguate=")),
                             ("cat-file -t", lambda a: a[:2] == ["cat-file", "-t"])):
            def fake(args, cwd, broken=broken):
                return (128, "fatal: boom") if broken(args) else real(args, cwd)

            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, self.clone),
                                 (2, f"sha {sha}: unknown \u2014 git could not read this repository", False))

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

    def test_a_repository_git_cannot_read_does_not_hold_it(self) -> None:
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        broken = self.trees / "broken"
        broken.mkdir()
        (broken / ".git").write_text(f"gitdir: {self.root / 'no-such-gitdir'}\n")
        code, _, held = git_trees.check_sha(sha, broken)
        self.assertEqual((code, held), (2, False))

    def test_origin_main_missing_though_the_fetch_succeeded_is_unknown(self) -> None:
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        git("update-ref", "-d", "refs/remotes/origin/main", cwd=self.clone)
        with patch.object(git_trees, "fetch_base", return_value=""):
            self.assertEqual(git_trees.check_sha(sha, self.clone), (2, f"sha {sha}: unknown \u2014 no origin/main to check against", True))


if __name__ == "__main__":
    unittest.main()
