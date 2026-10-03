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
    """#49: `audit --sha` answers in one line, and the exit code is the verdict. Pure: no git here."""

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

    def test_the_sha_is_printed_as_it_was_given(self) -> None:
        full = "54b7eb3" + "0" * 33
        self.assertEqual(git_trees.sha_verdict(full, "", True, True, self.T),
                         (0, f"sha {full}: on origin/main 9c790f3 (fetched)"))


class FetchBaseTest(Repo):
    """#49: the audit never fetched, so `origin/main` was whatever the last fetch left."""

    def push_from_another_clone(self) -> str:
        other = Path(tempfile.mkdtemp(dir=self.root, prefix="other-"))
        git("clone", "-q", str(self.root / "origin.git"), str(other), cwd=self.root)
        commit(other, f"theirs-{other.name}.txt")
        git("push", "-q", "origin", "main", cwd=other)
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
        self.assertEqual(seen["argv"], ["git", "-C", str(self.clone), "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main"])
        self.assertEqual(seen["kw"]["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(seen["kw"]["env"]["COS_FETCH_ENV_PROBE"], "kept")
        self.assertEqual(seen["kw"]["timeout"], 7)
        self.assertFalse(seen["kw"].get("shell"))


class CheckShaTest(Repo):
    def test_a_pushed_commit_is_on_origin_main(self) -> None:
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tip = git("rev-parse", "--short", "origin/main", cwd=self.clone)
        self.assertEqual(git_trees.check_sha(sha, self.clone), (0, f"sha {sha}: on origin/main {tip} (fetched)"))

    def test_a_commit_only_on_a_branch_is_not(self) -> None:
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        sha = git("rev-parse", "HEAD", cwd=tree)
        tip = git("rev-parse", "--short", "origin/main", cwd=self.clone)
        self.assertEqual(git_trees.check_sha(sha, tree), (1, f"sha {sha}: not on origin/main {tip} (fetched)"))

    def test_a_prefix_two_commits_share_is_unknown_not_a_pick(self) -> None:
        """Two commit ids start with it: no answer, whichever of them is on origin/main. `git` is patched, because two
        commits sharing a 7-hex prefix cannot be made cheaply."""
        calls = []

        def fake(args, cwd):
            calls.append(args)
            if args[0] == "rev-parse" and args[1].startswith("--disambiguate="):
                return 0, "9f3c1e7aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n9f3c1e7bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            return 0, "abc1234" if args[:2] == ["rev-parse", "--short"] else ""

        with patch.object(git_trees, "fetch_base", return_value=""), patch.object(git_trees, "git", fake):
            self.assertEqual(git_trees.check_sha("9f3c1e7", self.clone),
                             (2, "sha 9f3c1e7: unknown \u2014 more than one commit starts with it"))
        self.assertFalse([c for c in calls if c[:1] == ["merge-base"]], calls)


if __name__ == "__main__":
    unittest.main()
