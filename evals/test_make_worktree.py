"""Unit tests for scripts/make_worktree.py: the first script that writes to the user's repo.

Every git call the plugin made before this one was read-only, and that is the stated reason git
stays off the agent's Bash allowlist. So the refusals matter more than the happy path: the branch
and the path both come out of a file an agent wrote, and they are handed to git.

Repos are real. `git worktree add` against a fake proves nothing.
"""

import contextlib
import io
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_trees  # noqa: E402
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


def rev(cwd: Path, ref: str = "HEAD") -> str:
    return make_repo.git(["rev-parse", ref], cwd)


def advance_remote(root: Path) -> str:
    """Push a commit to the bare remote from a second clone, so the first clone's `main` and its
    `refs/remotes/origin/main` are both behind. Returns the remote's new head."""
    other = root / "other"
    make_repo.git(["clone", "-q", str(root / "origin.git"), str(other)], root)
    (other / "ahead.txt").write_text("ahead\n")
    make_repo.git(["add", "-A"], other)
    make_repo.git(["commit", "-q", "-m", "ahead"], other)
    make_repo.git(["push", "-q", "origin", "HEAD:main"], other)
    return rev(other)


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


class CloneCase(unittest.TestCase):
    """A workspace whose clone has a real local bare `origin` (make_repo.build makes one and pushes main)."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)
        self.clone = self.root / "repo"
        self.tree = self.root / "trees" / "wt-new"

    def build(self, **kw):
        args = {"root": self.root, "clone": self.clone, "trees": "trees", "name": "wt-new", "branch": "feat/new", "agent_type": "implementer"}
        args.update(kw)
        return mw.build(**args)

    def run_main(self, *extra: str) -> tuple[int, str, str]:
        argv = ["--type", "implementer", "--name", "wt-new", "--branch", "feat/new", "--root", str(self.root), "--clone", str(self.clone), *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mw.main(argv)
        return code, out.getvalue(), err.getvalue()

    def branches(self) -> str:
        return make_repo.git(["branch", "--list", "feat/new"], self.clone)


class BuildTest(CloneCase):
    def test_creates_the_tree_on_a_new_branch_off_main(self):
        made = self.build()
        self.assertTrue((made.path / ".git").exists())
        head = subprocess.run(["git", "-C", str(made.path), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
        self.assertEqual(head.stdout.strip(), "feat/new")

    def test_the_new_branch_starts_at_the_remotes_main(self):
        remote_head = advance_remote(self.root)
        made = self.build()
        self.assertEqual(rev(made.path), remote_head, "a dispatch starts from the remote's main, not from a stale local one")

    def test_an_unlisted_agent_type_is_refused_before_anything_is_created(self):
        with self.assertRaises(mw.RefusedError):
            self.build(agent_type="ghost")
        self.assertFalse(self.tree.exists())

    def test_no_agent_lines_means_any_type_is_allowed(self):
        (self.root / "CLAUDE.md").write_text("## Coordinator\n\n- Worktrees: `trees/`\n")
        made = self.build(agent_type="whatever")
        self.assertTrue(made.path.exists())

    def test_a_refused_branch_creates_nothing(self):
        with self.assertRaises(mw.RefusedError):
            self.build(branch="-rf")
        self.assertFalse(self.tree.exists())

    def test_it_prints_the_path_and_branch_it_actually_made(self):
        made = self.build()
        line = mw.line(made)
        self.assertIn(str(made.path), line)
        self.assertIn("feat/new", line)

    def assert_refused_before_any_fetch(self, about: str, **kw):
        with mock.patch.object(mw.git_trees, "fetch_base") as fetch:
            with self.assertRaises(mw.RefusedError) as e:
                self.build(**kw)
        fetch.assert_not_called()
        self.assertIn(about, str(e.exception), f"the refusal is about {about!r}, not a fetch")
        self.assertNotIn("fetch", str(e.exception).lower())

    def test_a_refused_type_is_refused_before_any_fetch(self):
        self.assert_refused_before_any_fetch("ghost", agent_type="ghost")

    def test_a_refused_branch_is_refused_before_any_fetch(self):
        self.assert_refused_before_any_fetch("a..b", branch="a..b")

    def test_a_bad_name_is_refused_before_any_fetch(self):
        self.assert_refused_before_any_fetch("../outside", name="../outside")

    def test_an_existing_name_is_refused_before_any_fetch(self):
        (self.root / "trees" / "taken").mkdir(parents=True)
        self.assert_refused_before_any_fetch("already exists", name="taken")

    def test_a_failed_worktree_add_is_a_refusal_and_leaves_no_tree(self):
        make_repo.git(["branch", "feat/new", "main"], self.clone)
        with self.assertRaises(mw.RefusedError) as e:
            self.build()
        self.assertIn("worktree add", str(e.exception))
        self.assertFalse(self.tree.exists())

    def test_main_exits_1_when_the_worktree_add_fails(self):
        make_repo.git(["branch", "feat/new", "main"], self.clone)
        code, out, err = self.run_main()
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("refused:"), err)
        self.assertFalse(self.tree.exists())

    def test_the_report_line_carries_the_short_sha_not_the_full_one(self):
        remote_head = advance_remote(self.root)
        self.assertEqual(len(remote_head), 40)
        line = mw.line(self.build())
        self.assertIn(remote_head[:7], line)
        self.assertNotIn(remote_head, line)

    def test_main_prints_the_short_sha_not_the_full_one(self):
        remote_head = advance_remote(self.root)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn(remote_head[:7], out)
        self.assertNotIn(remote_head, out)


class FreshBaseTest(CloneCase):
    """Rule: the branch is cut from the remote's main, fetched first; the clone's local main is left alone."""

    def test_the_tree_head_is_the_remotes_head_not_local_mains(self):
        local_main = rev(self.clone, "main")
        remote_head = advance_remote(self.root)
        self.assertNotEqual(local_main, remote_head)
        made = self.build()
        self.assertEqual(rev(made.path), remote_head)
        self.assertNotEqual(rev(made.path), local_main)

    def test_local_main_in_the_clone_is_not_moved(self):
        local_main = rev(self.clone, "main")
        advance_remote(self.root)
        self.build()
        self.assertEqual(rev(self.clone, "main"), local_main)

    def test_the_new_branch_has_no_upstream_so_a_bare_push_cannot_target_main(self):
        advance_remote(self.root)
        made = self.build()
        upstream = subprocess.run(["git", "-C", str(made.path), "rev-parse", "--abbrev-ref", "feat/new@{u}"], capture_output=True, text=True)
        self.assertNotEqual(upstream.returncode, 0, f"feat/new tracks {upstream.stdout.strip()!r}")
        merge = subprocess.run(["git", "-C", str(self.clone), "config", "--get", "branch.feat/new.merge"], capture_output=True, text=True)
        self.assertEqual(merge.stdout.strip(), "")

    def test_the_report_names_origin_main_and_its_sha_beside_path_branch_and_type(self):
        remote_head = advance_remote(self.root)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        for part in (str(self.tree.resolve()), "feat/new", "origin/main", remote_head[:7], "implementer"):
            self.assertIn(part, out)

    def test_line_carries_the_base_sha_too(self):
        remote_head = advance_remote(self.root)
        line = mw.line(self.build())
        self.assertIn("origin/main", line)
        self.assertIn(remote_head[:7], line)


class FetchFailureTest(CloneCase):
    """Rule: a failed fetch refuses and creates nothing; the message offers `--from-local` and names local main's sha."""

    def no_origin(self):
        make_repo.git(["remote", "remove", "origin"], self.clone)

    def dead_origin(self):
        make_repo.git(["remote", "set-url", "origin", str(self.root / "nowhere.git")], self.clone)

    def assert_refused_with_nothing_made(self, message: str):
        local_main = rev(self.clone, "main")
        self.assertIn("fetch", message.lower())
        self.assertIn(git_trees.fetch_base(self.clone), message, "the reason the fetch failed")
        self.assertIn(local_main[:7], message, "the sha local main would give")
        self.assertIn("--from-local", message)
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def refuse_build(self):
        with self.assertRaises(mw.RefusedError) as e:
            self.build()
        return str(e.exception)

    def test_no_origin_refuses_and_creates_nothing(self):
        self.no_origin()
        self.assert_refused_with_nothing_made(self.refuse_build())

    def test_an_origin_at_a_path_that_does_not_exist_refuses_and_creates_nothing(self):
        self.dead_origin()
        self.assert_refused_with_nothing_made(self.refuse_build())

    def test_main_exits_1_and_says_so_on_stderr_with_no_origin(self):
        self.no_origin()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assert_refused_with_nothing_made(err)

    def test_main_exits_1_and_says_so_on_stderr_with_a_dead_origin(self):
        self.dead_origin()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assert_refused_with_nothing_made(err)


class FromLocalTest(CloneCase):
    """Rule: `--from-local` cuts from local main, as the flag says, with or without a remote."""

    def test_succeeds_with_no_origin_and_heads_at_local_main(self):
        make_repo.git(["remote", "remove", "origin"], self.clone)
        local_main = rev(self.clone, "main")
        made = self.build(from_local=True)
        self.assertEqual(rev(made.path), local_main)

    def test_the_line_says_local_main_and_its_sha_and_not_origin_main(self):
        make_repo.git(["remote", "remove", "origin"], self.clone)
        local_main = rev(self.clone, "main")
        line = mw.line(self.build(from_local=True))
        self.assertIn(local_main[:7], line)
        self.assertIn("local", line.lower())
        self.assertNotIn("origin/main", line)

    def test_main_accepts_the_flag_and_reports_local_main(self):
        make_repo.git(["remote", "remove", "origin"], self.clone)
        local_main = rev(self.clone, "main")
        code, out, err = self.run_main("--from-local")
        self.assertEqual(code, 0, err)
        self.assertIn(local_main[:7], out)
        self.assertNotIn("origin/main", out)
        self.assertEqual(rev(self.tree), local_main)

    def test_with_an_advanced_reachable_remote_head_is_still_local_main(self):
        local_main = rev(self.clone, "main")
        remote_head = advance_remote(self.root)
        made = self.build(from_local=True)
        self.assertEqual(rev(made.path), local_main)
        self.assertNotEqual(rev(made.path), remote_head)

    def test_it_does_not_fetch(self):
        stale = rev(self.clone, "refs/remotes/origin/main")
        advance_remote(self.root)
        with mock.patch.object(mw.git_trees, "fetch_base") as fetch:
            self.build(from_local=True)
        fetch.assert_not_called()
        self.assertEqual(rev(self.clone, "refs/remotes/origin/main"), stale, "the clone's remote-tracking ref did not move")

    def test_a_dead_origin_does_not_stop_it(self):
        make_repo.git(["remote", "set-url", "origin", str(self.root / "nowhere.git")], self.clone)
        local_main = rev(self.clone, "main")
        self.assertEqual(rev(self.build(from_local=True).path), local_main)


class ConcurrentDispatchTest(CloneCase):
    """Rule: dispatches that start together against one clone do not refuse each other over the fetch's ref lock."""

    BUILDERS = 4

    def test_simultaneous_builds_all_succeed_and_all_head_at_the_remote(self):
        remote_head = advance_remote(self.root)
        start = threading.Barrier(self.BUILDERS)
        results: dict[int, object] = {}

        def dispatch(i: int):
            start.wait()
            try:
                results[i] = self.build(name=f"wt-{i}", branch=f"feat/c{i}")
            except Exception as exc:  # the test reports it, whatever it is
                results[i] = exc

        threads = [threading.Thread(target=dispatch, args=(i,)) for i in range(self.BUILDERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        refused = {i: str(r) for i, r in results.items() if not isinstance(r, mw.Made)}
        self.assertEqual(refused, {}, "no dispatch is refused by another")
        for made in results.values():
            self.assertEqual(rev(made.path), remote_head)


class LocalMainIsTheBranchTest(CloneCase):
    """Rule: local main is `refs/heads/main`, never a tag that happens to be called `main`."""

    def tag_behind(self) -> tuple[str, str]:
        """A tag `main` at the commit the branch has now, then one more commit on the branch. (tag sha, branch sha)"""
        tag = rev(self.clone, "refs/heads/main")
        make_repo.git(["tag", "main", tag], self.clone)
        (self.clone / "local.txt").write_text("local\n")
        make_repo.git(["add", "-A"], self.clone)
        make_repo.git(["commit", "-q", "-m", "local"], self.clone)
        branch = rev(self.clone, "refs/heads/main")
        self.assertNotEqual(tag, branch)
        return tag, branch

    def test_from_local_cuts_from_the_branch_not_the_tag(self):
        _, branch = self.tag_behind()
        made = self.build(from_local=True)
        self.assertEqual(rev(made.path), branch)

    def test_a_failed_fetch_names_the_branchs_sha_not_the_tags(self):
        tag, branch = self.tag_behind()
        make_repo.git(["remote", "remove", "origin"], self.clone)
        with self.assertRaises(mw.RefusedError) as e:
            self.build()
        self.assertIn(branch[:7], str(e.exception))
        self.assertNotIn(tag[:7], str(e.exception))


class NoLocalMainTest(CloneCase):
    """Rule: a clone with no local main branch says so in one line, and does not offer what cannot work."""

    def drop_local_main(self):
        make_repo.git(["checkout", "-q", "-b", "elsewhere"], self.clone)
        make_repo.git(["branch", "-D", "main"], self.clone)
        self.assertEqual(make_repo.git(["branch", "--list", "main"], self.clone), "")

    def test_a_failed_fetch_says_there_is_no_local_main_in_one_line_and_offers_no_flag(self):
        self.drop_local_main()
        make_repo.git(["remote", "remove", "origin"], self.clone)
        with self.assertRaises(mw.RefusedError) as e:
            self.build()
        message = str(e.exception)
        self.assertNotIn("\n", message)
        self.assertIn("no local main", message)
        self.assertNotIn("--from-local", message)
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def test_from_local_is_refused_in_one_line_and_creates_nothing(self):
        self.drop_local_main()
        with self.assertRaises(mw.RefusedError) as e:
            self.build(from_local=True)
        self.assertNotIn("\n", str(e.exception))
        self.assertIn("no local main", str(e.exception))
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def test_a_reachable_origin_still_builds_from_the_remotes_head(self):
        self.drop_local_main()
        remote_head = advance_remote(self.root)
        self.assertEqual(rev(self.build().path), remote_head)


class BaseRefTest(unittest.TestCase):
    """The choice of base, with no repo."""

    def test_a_clean_fetch_cuts_from_the_fetched_remote_ref(self):
        self.assertEqual(mw.base_ref("", False), "refs/remotes/origin/main")

    def test_from_local_cuts_from_local_main_whatever_the_fetch_said(self):
        """Not the bare `main`: a tag called `main` would shadow it. The full ref names the branch alone."""
        self.assertEqual(mw.base_ref("", True), "refs/heads/main")
        self.assertEqual(mw.base_ref("fatal: could not read from remote", True), "refs/heads/main")

    def test_a_failed_fetch_without_from_local_is_refused(self):
        with self.assertRaises(mw.RefusedError):
            mw.base_ref("fatal: could not read from remote", False)


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
