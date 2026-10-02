"""Unit tests for scripts/git_trees.py: the read-only git reader behind the orphan check (#43).

Real repos, as in test_audit_tasks: a bare origin, a clone, and worktrees off it. `rev-list` and `@{u}`
have no useful fake.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
