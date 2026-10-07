"""Unit tests for scripts/git_control.py (#427): the git control files a granted worker can write.

`snapshot(cwd)` records them before a codex run; `verify_and_restore(snap)` puts back every difference after it and
returns the sorted relative names that differed. Names are relative to where the file lives: `config` and
`hooks/<name>` under the common dir, `commondir`, `gitdir` and `config.worktree` under a linked worktree's own gitdir,
`.git` for the tree's gitfile. Real repos and `git worktree add`, as in test_git_trees; nothing is mocked.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import git_control  # noqa: E402
import test_one_shot  # noqa: E402  (a module, not its classes, so unittest does not collect them twice)

KINDS = ("clone", "linked")  # the two shapes the grant covers
HOOK = "#!/bin/sh\nexit 0\n"
MiB = 1024 * 1024


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def entry(path: Path):
    """What is at `path`, by lstat: the bytes and mode of a regular file, a link's target, or just the kind."""
    if not os.path.lexists(path):
        return None
    mode = os.lstat(path).st_mode
    if stat.S_ISREG(mode):
        return ("file", path.read_bytes(), stat.S_IMODE(mode))
    if stat.S_ISLNK(mode):
        return ("link", os.readlink(path))
    return ("dir" if stat.S_ISDIR(mode) else "other",)


def state(p) -> dict:
    """Every control file `p` names, plus each direct entry of the hooks dir."""
    names = sorted(os.listdir(p.hooks)) if p.hooks.is_dir() else []
    return {"config": entry(p.config), "hooks": entry(p.hooks), "commondir": entry(p.commondir),
            "gitdir": entry(p.gitdir), "config.worktree": entry(p.cfgwt), ".git": entry(p.dotgit),
            **{f"hooks/{n}": entry(p.hooks / n) for n in names}}


class GitControlTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def fixture(self, kind: str) -> SimpleNamespace:
        """A fresh repo with one hook already in it, and the paths of its control files."""
        root = Path(tempfile.mkdtemp(dir=self.tmp)).resolve()
        if kind == "clone":
            tree = root / "tree"
            subprocess.run(["git", "init", "-q", str(tree)], check=True)
            git("commit", "-q", "--allow-empty", "-m", "init", cwd=tree)
        else:
            _, tree = test_one_shot.CommandTest._repo_with_linked_tree(self, root)
        out = lambda flag: Path(git("rev-parse", "--path-format=absolute", flag, cwd=tree)).resolve()  # noqa: E731
        common, gitdir = out("--git-common-dir"), out("--absolute-git-dir")
        hooks = common / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "pre-commit").write_text(HOOK)
        (hooks / "pre-commit").chmod(0o755)
        return SimpleNamespace(tree=tree, common=common, own=gitdir, config=common / "config", hooks=hooks,
                               hook=hooks / "pre-commit", commondir=gitdir / "commondir", gitdir=gitdir / "gitdir",
                               cfgwt=gitdir / "config.worktree", dotgit=tree / ".git")

    def hurt(self, mutate, names, kinds=KINDS):
        """After `mutate(paths)`, verify reports `names` (any non-empty list when None), leaves every control file as
        recorded, and a second verify finds nothing."""
        for kind in kinds:
            with self.subTest(kind=kind):
                p = self.fixture(kind)
                before = state(p)
                snap = git_control.snapshot(p.tree)
                self.assertIsNotNone(snap)
                mutate(p)
                found = git_control.verify_and_restore(snap)
                if names is None:
                    self.assertTrue(found)
                else:
                    self.assertEqual(found, names)
                self.assertEqual(state(p), before)
                self.assertEqual(git_control.verify_and_restore(snap), [])

    # --- snapshot ---

    def test_a_snapshot_is_taken_of_a_linked_worktree_and_of_a_normal_clone(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertIsNotNone(git_control.snapshot(self.fixture(kind).tree))

    def test_a_snapshot_of_a_plain_directory_is_none(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(git_control.snapshot(plain))

    def test_a_snapshot_of_a_subdirectory_of_a_repo_is_none(self):
        # The grant is made at the tree's own toplevel only, so a subdirectory has none to record.
        p = self.fixture("clone")
        (p.tree / "sub").mkdir()
        self.assertIsNone(git_control.snapshot(p.tree / "sub"))

    def test_a_snapshot_over_four_mib_is_none_and_under_it_is_taken(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                p = self.fixture(kind)
                (p.hooks / "big").write_bytes(b"x" * MiB)
                self.assertIsNotNone(git_control.snapshot(p.tree))
                (p.hooks / "big").write_bytes(b"x" * 5 * MiB)
                self.assertIsNone(git_control.snapshot(p.tree))

    @unittest.skipIf(os.geteuid() == 0, "root reads every file, so nothing is unreadable")
    def test_a_snapshot_with_an_unreadable_control_file_is_none(self):
        for kind in KINDS:
            for which in ("config", "hook"):
                with self.subTest(kind=kind, which=which):
                    p = self.fixture(kind)
                    path = getattr(p, which)
                    path.chmod(0)
                    self.addCleanup(path.chmod, 0o644)
                    self.assertIsNone(git_control.snapshot(p.tree))

    # --- verify_and_restore: nothing changed ---

    def test_verify_on_an_untouched_tree_reports_nothing_and_changes_nothing(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                p = self.fixture(kind)
                before = state(p)
                snap = git_control.snapshot(p.tree)
                self.assertEqual(git_control.verify_and_restore(snap), [])
                self.assertEqual(state(p), before)

    # --- verify_and_restore: each control file ---

    def test_a_rewritten_config_is_reported_and_restored(self):
        self.hurt(lambda p: p.config.write_text(p.config.read_text() + "[core]\n\tfsmonitor = touch x\n"), ["config"])

    def test_an_added_hook_is_reported_and_deleted(self):
        def add(p):
            (p.hooks / "evil").write_text("#!/bin/sh\ntouch x\n")
            (p.hooks / "evil").chmod(0o755)
        self.hurt(add, ["hooks/evil"])

    def test_a_modified_hook_is_reported_and_restored_with_its_bytes_and_mode(self):
        def modify(p):
            p.hook.write_text("#!/bin/sh\ntouch x\n")
            p.hook.chmod(0o644)
        self.hurt(modify, ["hooks/pre-commit"])

    def test_a_removed_hook_is_reported_and_recreated(self):
        self.hurt(lambda p: p.hook.unlink(), ["hooks/pre-commit"])

    def test_a_hook_replaced_by_a_symlink_is_reported_and_restored_as_a_regular_file(self):
        def link(p):
            p.hook.unlink()
            p.hook.symlink_to("/bin/sh")
        self.hurt(link, ["hooks/pre-commit"])

    def test_a_hook_replaced_by_a_directory_is_reported_and_restored_as_a_regular_file(self):
        def replace(p):
            p.hook.unlink()
            p.hook.mkdir()
            (p.hook / "inner").write_text("x")
        self.hurt(replace, ["hooks/pre-commit"])

    def test_a_hooks_dir_swapped_for_a_symlink_to_another_dir_is_restored(self):
        # Not in the brief's list: the same payload as an added hook, reached by swapping the directory itself.
        def swap(p):
            elsewhere = p.tree.parent / "elsewhere"
            shutil.copytree(p.hooks, elsewhere)
            (elsewhere / "evil").write_text("#!/bin/sh\ntouch x\n")
            shutil.rmtree(p.hooks)
            p.hooks.symlink_to(elsewhere)
        self.hurt(swap, None)

    def test_a_config_replaced_by_a_symlink_is_restored_without_writing_through_it(self):
        p = self.fixture("clone")
        target = self.tmp / "elsewhere-config"
        target.write_text("[user]\n\tname = untouched\n")
        snap = git_control.snapshot(p.tree)
        p.config.unlink()
        p.config.symlink_to(target)
        self.assertEqual(git_control.verify_and_restore(snap), ["config"])
        self.assertFalse(p.config.is_symlink())
        self.assertEqual(target.read_text(), "[user]\n\tname = untouched\n")

    def test_a_rewritten_commondir_is_reported_and_restored(self):
        self.hurt(lambda p: p.commondir.write_text("/elsewhere\n"), ["commondir"], kinds=("linked",))

    def test_a_rewritten_gitdir_pointer_is_reported_and_restored(self):
        self.hurt(lambda p: p.gitdir.write_text("/elsewhere/.git\n"), ["gitdir"], kinds=("linked",))

    def test_an_added_config_worktree_is_reported_and_deleted(self):
        self.hurt(lambda p: p.cfgwt.write_text("[core]\n\tfsmonitor = touch x\n"), ["config.worktree"], kinds=("linked",))

    def test_a_rewritten_gitfile_is_reported_and_restored(self):
        self.hurt(lambda p: p.dotgit.write_text("gitdir: /elsewhere\n"), [".git"], kinds=("linked",))

    def test_several_changes_are_reported_sorted(self):
        def many(p):
            p.config.write_text(p.config.read_text() + "[core]\n\tfsmonitor = touch x\n")
            (p.hooks / "b").write_text("x")
            (p.hooks / "a").write_text("x")
            if p.commondir.exists():
                p.commondir.write_text("/elsewhere\n")
        self.hurt(many, ["config", "hooks/a", "hooks/b"], kinds=("clone",))
        self.hurt(many, ["commondir", "config", "hooks/a", "hooks/b"], kinds=("linked",))

    # --- verify_and_restore: the config ---

    BENIGN = {
        "remote": '[branch "x"]\n\tremote = origin\n',
        "remote and merge": '[branch "x"]\n\tremote = origin\n\tmerge = refs/heads/x\n',
        "rebase": '[branch "x"]\n\trebase = true\n',
        "pushremote": '[branch "x"]\n\tpushremote = origin\n',
        "other case": '[Branch "x"]\n\tRemote = origin\n\tMERGE = refs/heads/x\n',
    }

    def test_a_branch_remote_merge_rebase_or_pushremote_write_is_kept_and_not_reported(self):
        # What `git push -u` and `git branch --set-upstream-to` write.
        for name, text in self.BENIGN.items():
            for kind in KINDS:
                with self.subTest(write=name, kind=kind):
                    p = self.fixture(kind)
                    snap = git_control.snapshot(p.tree)
                    p.config.write_text(p.config.read_text() + text)
                    kept = p.config.read_bytes()
                    self.assertEqual(git_control.verify_and_restore(snap), [])
                    self.assertEqual(p.config.read_bytes(), kept)

    def test_a_real_set_upstream_write_is_kept(self):
        p = self.fixture("clone")
        snap = git_control.snapshot(p.tree)
        git("config", "branch.topic.remote", "origin", cwd=p.tree)
        git("config", "branch.topic.merge", "refs/heads/topic", cwd=p.tree)
        self.assertEqual(git_control.verify_and_restore(snap), [])
        self.assertEqual(git("config", "branch.topic.merge", cwd=p.tree), "refs/heads/topic")

    def test_a_changed_or_removed_benign_key_is_kept_and_not_reported(self):
        p = self.fixture("clone")
        git("config", "branch.topic.remote", "origin", cwd=p.tree)
        git("config", "branch.topic.merge", "refs/heads/topic", cwd=p.tree)
        snap = git_control.snapshot(p.tree)
        git("config", "branch.topic.remote", "other", cwd=p.tree)
        git("config", "--unset", "branch.topic.merge", cwd=p.tree)
        self.assertEqual(git_control.verify_and_restore(snap), [])
        self.assertEqual(git("config", "branch.topic.remote", cwd=p.tree), "other")

    HARMFUL = {
        "include": "[include]\n\tpath = /elsewhere\n",
        "includeIf": '[includeIf "gitdir:/"]\n\tpath = /elsewhere\n',
        "fsmonitor": "[core]\n\tfsmonitor = touch x\n",
        "hooksPath": "[core]\n\thooksPath = /elsewhere\n",
        "alias": "[alias]\n\tx = !touch x\n",
        "another branch key": '[branch "x"]\n\tsomethingelse = 1\n',
        "uploadpack": '[remote "o"]\n\tuploadpack = touch x\n',
        "upper case": "[CORE]\n\tFSMonitor = touch x\n",
    }

    def test_any_other_config_change_restores_the_whole_config_and_reports_it(self):
        for name, text in self.HARMFUL.items():
            with self.subTest(write=name):
                self.hurt(lambda p, text=text: p.config.write_text(p.config.read_text() + text), ["config"])

    def test_a_changed_or_removed_ordinary_key_is_reported_and_restored(self):
        edit = lambda *args: (lambda p: git("config", "--file", str(p.config), *args, cwd=p.tree))  # noqa: E731
        self.hurt(edit("core.filemode", "false"), ["config"])
        self.hurt(edit("--unset", "core.filemode"), ["config"])

    def test_a_benign_write_beside_a_harmful_one_restores_the_whole_config(self):
        def both(p):
            p.config.write_text(p.config.read_text() + self.BENIGN["remote and merge"] + self.HARMFUL["fsmonitor"])
        self.hurt(both, ["config"])

    def test_an_unparseable_config_is_reported_and_restored(self):
        self.hurt(lambda p: p.config.write_text("[core\n\tfsmonitor = touch x\n"), ["config"])

    def test_verify_never_runs_what_the_worker_wrote_into_the_config(self):
        marker = self.tmp / "ran"
        p = self.fixture("clone")
        snap = git_control.snapshot(p.tree)
        p.config.write_text(p.config.read_text() + f"[core]\n\tfsmonitor = touch {marker} #\n"
                            f'[alias]\n\tx = !touch {marker}\n')
        self.assertEqual(git_control.verify_and_restore(snap), ["config"])
        self.assertFalse(marker.exists())
        git("status", "--short", cwd=p.tree)  # and git itself, afterwards, has nothing to run
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
