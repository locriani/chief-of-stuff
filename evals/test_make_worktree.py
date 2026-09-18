"""Unit tests for scripts/make_worktree.py: the first script that writes to the user's repo.

Every git call the plugin made before this one was read-only, and that is the stated reason git
stays off the agent's Bash allowlist. So the refusals matter more than the happy path: the branch
and the path both come out of a file an agent wrote, and they are handed to git.

Repos are real. `git worktree add` against a fake proves nothing.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_repo  # noqa: E402
import make_worktree as mw  # noqa: E402

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Worktrees: `trees/`
- Agent: implementer task
- Agent: fixer standing
- Timezone: America/Chicago
"""


def workspace() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "CLAUDE.md").write_text(CLAUDE)
    make_repo.build(root, {"trees": "trees", "worktrees": []})
    return tmp, root


class AgentTypeTest(unittest.TestCase):
    def test_reads_the_types_and_their_lifetimes(self):
        self.assertEqual(mw.agent_types(CLAUDE), {"implementer": "task", "fixer": "standing"})

    def test_no_agent_lines_is_no_types_not_an_error(self):
        self.assertEqual(mw.agent_types("## Coordinator\n\n- User: Robin\n"), {})

    def test_a_lifetime_that_is_not_task_or_standing_is_refused(self):
        with self.assertRaises(mw.RefusedError):
            mw.agent_types("## Coordinator\n\n- Agent: ghost immortal\n")

    def test_a_line_outside_the_block_is_not_a_type(self):
        text = CLAUDE + "\n## Notes\n\n- Agent: smuggled standing\n"
        self.assertNotIn("smuggled", mw.agent_types(text))


class BranchNameTest(unittest.TestCase):
    """git's own answer, not a regex of mine."""

    def test_a_plain_branch_is_accepted(self):
        mw.check_branch("fix/docstring")

    def test_a_branch_that_is_an_option_is_refused(self):
        for name in ("-rf", "--upload-pack=touch /tmp/x"):
            with self.assertRaises(mw.RefusedError, msg=name):
                mw.check_branch(name)

    def test_gits_own_reserved_shapes_are_refused(self):
        for name in ("a..b", "a@{0}", "a.lock", "a b", "a~1", ""):
            with self.assertRaises(mw.RefusedError, msg=name):
                mw.check_branch(name)


class PathTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_a_name_resolves_directly_under_the_worktrees_dir(self):
        path = mw.resolve(self.root, "trees/", "wt-new")
        self.assertEqual(path, (self.root / "trees" / "wt-new").resolve())

    def test_a_name_that_escapes_the_root_is_refused(self):
        for name in ("../outside", "../../etc", "/tmp/elsewhere"):
            with self.assertRaises(mw.RefusedError, msg=name):
                mw.resolve(self.root, "trees/", name)

    def test_a_nested_name_is_refused_because_a_tree_sits_directly_under_the_dir(self):
        with self.assertRaises(mw.RefusedError):
            mw.resolve(self.root, "trees/", "a/b")

    def test_an_existing_path_is_refused_rather_than_reused(self):
        (self.root / "trees" / "taken").mkdir(parents=True)
        with self.assertRaises(mw.RefusedError) as e:
            mw.resolve(self.root, "trees/", "taken")
        self.assertIn("already", str(e.exception))

    def test_a_symlinked_worktrees_dir_that_points_outside_is_refused(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        link = self.root / "linked"
        link.symlink_to(outside)
        with self.assertRaises(mw.RefusedError):
            mw.resolve(self.root, "linked/", "wt-new")


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def build(self, **kw):
        args = {"root": self.root, "clone": self.root / "repo", "trees": "trees", "name": "wt-new", "branch": "feat/new", "agent_type": "implementer"}
        args.update(kw)
        return mw.build(**args)

    def test_creates_the_tree_on_a_new_branch_off_main(self):
        made = self.build()
        self.assertTrue((made.path / ".git").exists())
        head = subprocess.run(["git", "-C", str(made.path), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
        self.assertEqual(head.stdout.strip(), "feat/new")

    def test_the_new_branch_starts_at_main(self):
        made = self.build()
        base = subprocess.run(["git", "-C", str(made.path), "merge-base", "--is-ancestor", "HEAD", "main"], capture_output=True)
        self.assertEqual(base.returncode, 0, "a dispatch starts from main, not from whatever was checked out")

    def test_an_unlisted_agent_type_is_refused_before_anything_is_created(self):
        with self.assertRaises(mw.RefusedError):
            self.build(agent_type="ghost")
        self.assertFalse((self.root / "trees" / "wt-new").exists())

    def test_no_agent_lines_means_any_type_is_allowed(self):
        (self.root / "CLAUDE.md").write_text("## Coordinator\n\n- Worktrees: `trees/`\n")
        made = self.build(agent_type="whatever")
        self.assertTrue(made.path.exists())

    def test_a_refused_branch_creates_nothing(self):
        with self.assertRaises(mw.RefusedError):
            self.build(branch="-rf")
        self.assertFalse((self.root / "trees" / "wt-new").exists())

    def test_it_prints_the_path_and_branch_it_actually_made(self):
        made = self.build()
        line = mw.line(made)
        self.assertIn(str(made.path), line)
        self.assertIn("feat/new", line)


class ArgvTest(unittest.TestCase):
    def test_git_is_called_with_a_list_and_never_through_a_shell(self):
        source = (Path(mw.__file__)).read_text()
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)

    def test_positionals_are_separated_from_options(self):
        """A branch or path that starts with a dash must not become a flag."""
        self.assertIn('"--"', (Path(mw.__file__)).read_text())


if __name__ == "__main__":
    unittest.main()
