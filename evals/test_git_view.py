"""Slice 1 contract: only launcher-authored git metadata may drive a read.

Controls run independently of the missing production module. Each vector test
also proves its control before importing git_view; no control is skipped or
xfail'ed. Compare return codes AND stdout, including empty/error answers.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import git_trees  # noqa: E402
import git_taint as taint  # noqa: E402


def raw(fx, args, env=None, path=None):
    return subprocess.run(["git", "-C", str(path or fx.tree), *args],
                          env=git_trees.audit_env() if env is None else env,
                          capture_output=True, text=True, timeout=5)


def answer(result):
    return result.returncode, result.stdout


def commands(fx):
    s = fx.sha
    return (
        ["status", "--short"],
        ["rev-parse", "HEAD"],
        ["log", "--format=%h %s", "--stat", f"{s['B']}..{s['D']}"],
        ["rev-list", "--count", f"{s['B']}..{s['D']}"],
        ["rev-list", "--count", "origin/main..HEAD"],
        ["rev-list", "--count", "@{u}..HEAD"],
        ["branch", "--show-current"],
        ["merge-base", "--is-ancestor", s["A"], s["D"]],
        ["merge-base", "--is-ancestor", s["D"], s["A"]],
        ["rev-parse", "--is-shallow-repository"],
    )


def prove_control(case, fx, vector):
    """Assert the precise bad behaviour, rather than just a changed answer."""
    fx.clear()
    env = git_trees.audit_env()
    markers = {
        "fsmonitor": "fsmonitor", "filter": "filter-clean",
        "hooks": "hook-post-index-change", "hooks_in_common": "hook-in-common",
        "include": "include-fsmonitor", "wtconfig": "wtconfig-fsmonitor",
        "commondir": "decoy-fsmonitor", "gitfile": "gitfile-fsmonitor",
        "gitlink": "nested-fsmonitor",
    }
    if vector in markers:
        if vector in ("hooks", "hooks_in_common"):
            # OPTIONAL_LOCKS=0 suppresses index-refresh hooks in the old audit
            # path; one_shot's old environment permits that refresh.
            env.pop("GIT_OPTIONAL_LOCKS")
        raw(fx, ["status", "--short"], env)
        case.assertIn(markers[vector], fx.fired())
    elif vector == "grafts":
        case.assertEqual(answer(raw(fx, ["rev-list", "--count", "origin/main..HEAD"])), (0, "2\n"))
    elif vector == "grafts_yes":
        case.assertEqual(raw(fx, ["merge-base", "--is-ancestor", fx.sha["D"], "main"]).returncode, 0)
    elif vector == "shallow":
        case.assertEqual(answer(raw(fx, ["rev-parse", "--is-shallow-repository"])), (0, "true\n"))
    elif vector == "replace":
        # The existing audit env ALREADY closes replace refs. Demonstrate the
        # hostile ref with replacement enabled, then retain the audit guard.
        env.pop("GIT_NO_REPLACE_OBJECTS")
        case.assertEqual(answer(raw(fx, ["rev-list", "--count", "origin/main..HEAD"], env)), (0, "1\n"))
        case.assertEqual(answer(raw(fx, ["rev-list", "--count", "origin/main..HEAD"])), (0, "3\n"))
    elif vector == "nested_untracked":
        # This vector is safe at the parent, but its nested command must be
        # live: a test that merely planted a dead hook would prove nothing.
        raw(fx, ["status", "--short"], path=fx.tree / "nested")
        case.assertIn("nested-fsmonitor", fx.fired())
        fx.clear()
        parent = raw(fx, ["status", "--short"])
        case.assertEqual(parent.returncode, 0, parent.stderr)
        case.assertIn("?? nested/\n", parent.stdout)
        case.assertEqual(fx.fired(), [])
    elif vector == "reftable_marker":
        # Plain files-backend git ignores this marker. The view must instead
        # refuse to interpret a possible reftable store as an empty ref set.
        case.assertTrue((fx.common / "reftable").is_dir())
        case.assertEqual(answer(raw(fx, ["rev-parse", "HEAD"])), (0, fx.sha["D"] + "\n"))
    else:
        case.fail(f"control missing for {vector}")
    fx.clear()


@contextmanager
def deadline(seconds=3):
    """A FIFO regression must fail promptly, without a stranded test thread."""
    def expired(signum, frame):
        raise AssertionError("worker-controlled file blocked the read")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.base = self.root / "private-views"
        self.base.mkdir()

    def fixture(self, vector=None, packed=False, sha256=False):
        root = Path(tempfile.mkdtemp(prefix="fixture-", dir=self.root))
        return (taint.build_sha256 if sha256 else taint.build)(root, vector, packed)

    @property
    def view(self):
        # Normal sibling-module import, delayed so real controls remain
        # runnable in the intended module-missing red state.
        import git_view
        return git_view

    def run_view(self, fx, args, **kwargs):
        return self.view.run(args, fx.tree, env=git_trees.audit_env(), base=self.base, **kwargs)

    def leftovers(self):
        return sorted(p.name for p in self.base.glob("view-*"))

    def compare_truth(self, fx, path=None):
        truth = [(args, answer(raw(fx, args, path=path))) for args in commands(fx)]
        for args, expected in truth:
            with self.subTest(command=args):
                got = self.view.run(args, path or fx.tree, env=git_trees.audit_env(), base=self.base)
                self.assertEqual(answer(got), expected, got.stderr)
        self.assertEqual(fx.fired(), [])
        self.assertEqual(self.leftovers(), [])


class FixtureControlsTest(FixtureCase):
    """These 15 green tests prove the planted hazards without git_view."""


def control_test(vector):
    def test(self):
        prove_control(self, self.fixture(vector), vector)
    test.__name__ = "test_control_" + vector
    return test


for _vector in taint.VECTORS:
    setattr(FixtureControlsTest, "test_control_" + _vector, control_test(_vector))


class LocateTest(FixtureCase):
    def assert_refused_without_git(self, path):
        view = self.view
        with patch.object(subprocess, "run", side_effect=AssertionError("locate started git")) as start:
            with deadline(), self.assertRaises(view.Unviewable) as caught:
                view.locate(path)
        start.assert_not_called()
        self.assertNotIn(str(path), str(caught.exception))
        self.assertTrue(str(caught.exception))

    def test_locate_linked_clone_symlink_and_bare_by_files_only(self):
        fx = self.fixture()
        linked = self.root / "tree-alias"
        linked.symlink_to(fx.tree, target_is_directory=True)
        bare = self.root / "bare.git"
        bare.mkdir()
        taint.git(["init", "--bare", "-q", "-b", "main"], bare)
        view = self.view
        with patch.object(subprocess, "run", side_effect=AssertionError("locate started git")) as start:
            for path, tree, gitdir, common in (
                (fx.tree, fx.tree, fx.gitdir, fx.common),
                (linked, fx.tree, fx.gitdir, fx.common),
                (fx.clone, fx.clone, fx.common, fx.common),
                (fx.common, None, fx.common, fx.common),
                (bare, None, bare, bare),
            ):
                with self.subTest(path=path):
                    layout = view.locate(path)
                    self.assertEqual((layout.tree, layout.gitdir, layout.common), (tree, gitdir, common))
        start.assert_not_called()

    def test_locate_relative_worktree_paths(self):
        fx = self.fixture()
        relative = fx.root / "relative"
        taint.git(["worktree", "add", "--relative-paths", "-q", "-b", "relative", str(relative)], fx.clone)
        self.assertFalse(Path((relative / ".git").read_text().removeprefix("gitdir: ").strip()).is_absolute())
        view = self.view
        with patch.object(subprocess, "run", side_effect=AssertionError("locate started git")):
            layout = view.locate(relative)
        self.assertEqual((layout.tree, layout.gitdir, layout.common),
                         (relative, fx.common / "worktrees" / "relative", fx.common))

    def test_locate_refuses_redirects_before_any_git(self):
        for vector in ("commondir", "gitfile"):
            with self.subTest(vector=vector):
                fx = self.fixture(vector)
                prove_control(self, fx, vector)
                self.assert_refused_without_git(fx.tree)
                view = self.view
                with patch.object(subprocess, "run", side_effect=AssertionError("run started git")) as start:
                    with self.assertRaises(view.Unviewable):
                        self.run_view(fx, ["rev-parse", "HEAD"])
                start.assert_not_called()
                self.assertEqual(fx.fired(), [])

    def test_locate_refuses_missing_symlink_fifo_and_oversize_gitfile(self):
        for kind in ("missing", "symlink", "fifo", "huge"):
            with self.subTest(kind=kind):
                fx = self.fixture()
                dotgit = fx.tree / ".git"
                content = dotgit.read_bytes()
                dotgit.unlink()
                if kind == "symlink":
                    target = fx.root / "gitfile"
                    target.write_bytes(content)
                    dotgit.symlink_to(target)
                elif kind == "fifo":
                    os.mkfifo(dotgit)
                elif kind == "huge":
                    # The pointer is otherwise valid even if a reader strips
                    # trailing newlines or silently truncates at its limit.
                    dotgit.write_bytes(content + b"\n" * 4096)
                self.assert_refused_without_git(fx.tree)

    def test_locate_accepts_gitfile_at_exactly_4096_bytes(self):
        fx = self.fixture()
        dotgit = fx.tree / ".git"
        content = dotgit.read_bytes()
        dotgit.write_bytes(content + b"\n" * (4096 - len(content)))
        self.assertEqual(dotgit.stat().st_size, 4096)
        view = self.view
        with patch.object(subprocess, "run", side_effect=AssertionError("locate started git")):
            self.assertEqual(view.locate(fx.tree).gitdir, fx.gitdir)

    def test_locate_refuses_submodule_gitfile_bad_backpointer_and_wrong_parent(self):
        for kind in ("submodule", "backpointer", "parent"):
            with self.subTest(kind=kind):
                fx = self.fixture()
                if kind == "backpointer":
                    (fx.gitdir / "gitdir").write_text(str(fx.clone / ".git") + "\n")
                else:
                    target = fx.common / "modules" / "x" if kind == "submodule" else fx.root / "outside"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(fx.gitdir, target)
                    (target / "commondir").unlink()
                    if kind == "parent":
                        (target / "commondir").write_text(str(fx.common) + "\n")
                    (fx.tree / ".git").write_text(f"gitdir: {target}\n")
                self.assert_refused_without_git(fx.tree)

    def test_locate_requires_head_objects_and_refs_in_common(self):
        for entry in ("HEAD", "objects", "refs"):
            with self.subTest(entry=entry):
                fx = self.fixture()
                target = fx.common / entry
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                self.assert_refused_without_git(fx.clone)
                self.assert_refused_without_git(fx.tree)

    def test_locate_bounds_commondir_and_backpointer_without_following_symlinks(self):
        for name in ("commondir", "gitdir"):
            for kind in ("fifo", "symlink", "huge"):
                with self.subTest(name=name, kind=kind):
                    fx = self.fixture()
                    target = fx.gitdir / name
                    original = target.read_bytes()
                    target.unlink()
                    if kind == "fifo":
                        os.mkfifo(target)
                    elif kind == "symlink":
                        other = fx.root / "redirect"
                        other.write_bytes(original)
                        target.symlink_to(other)
                    else:
                        target.write_bytes(original + b"\n" * 4096)
                    self.assert_refused_without_git(fx.tree)

    def test_locate_refuses_symlinked_clone_git_directory(self):
        fx = self.fixture()
        renamed = fx.root / "moved-gitdir"
        fx.common.rename(renamed)
        fx.common.symlink_to(renamed, target_is_directory=True)
        self.assert_refused_without_git(fx.clone)


class ContentsTest(FixtureCase):
    def config_values(self, directory):
        out = taint.git(["config", "--file", str(directory / "config"), "-z", "--list"], self.root)
        values = {}
        for record in out.stdout.split("\0"):
            if record:
                key, separator, value = record.partition("\n")
                self.assertEqual(separator, "\n")
                self.assertNotIn(key.lower(), values, "duplicate config fact")
                values[key.lower()] = value
        return values

    def expected_config(self, fx, *, bare=False, detached=False, upstream=True, sha256=False):
        expected = {
            "core.repositoryformatversion": "1" if sha256 else "0",
            "core.bare": str(bare).lower(), "core.fsmonitor": "false",
            "core.untrackedcache": "false", "core.commitgraph": "false",
            "core.multipackindex": "false", "core.hookspath": os.devnull,
            "core.attributesfile": os.devnull, "core.excludesfile": os.devnull,
            "status.submodulesummary": "false", "diff.ignoresubmodules": "all", "pack.usebitmaps": "false",
        }
        real = self.config_values(fx.common)
        for key in ("core.filemode", "core.ignorecase", "core.precomposeunicode", "core.symlinks"):
            if real.get(key) in ("true", "false"):
                expected[key] = real[key]
        if sha256:
            expected["extensions.objectformat"] = "sha256"
        if upstream and not detached and not bare:
            expected.update({"branch.feat.remote": "origin", "branch.feat.merge": "refs/heads/feat"})
        # Valid fetch facts may be retained for remotes other than the current
        # branch; their URL is always authored by the launcher.
        expected.update({"remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*",
                         "remote.origin.url": "/nonexistent"})
        return expected

    def test_contents_and_exact_allowlisted_config_facts(self):
        for shape in ("sha1", "sha256", "bare", "detached", "no-upstream", "packed", "split"):
            with self.subTest(shape=shape):
                fx = self.fixture(packed=shape == "packed", sha256=shape == "sha256")
                if shape == "detached":
                    taint.git(["checkout", "--detach", "-q"], fx.tree)
                if shape == "no-upstream":
                    taint.git(["config", "--unset", "branch.feat.remote"], fx.clone)
                    taint.git(["config", "--unset", "branch.feat.merge"], fx.clone)
                if shape == "split":
                    taint.git(["update-index", "--split-index"], fx.tree)
                path = fx.common if shape == "bare" else fx.tree
                with self.view.opened(path, git_trees.audit_env(), base=self.base) as v:
                    directory = Path(v.env["GIT_DIR"])
                    self.assertEqual(v.layout, self.view.locate(path))
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                    entries = {"HEAD", "refs", "objects", "config"}
                    if shape != "bare":
                        entries.add("index")
                        entries.update(p.name for p in fx.gitdir.glob("sharedindex.*"))
                    if shape == "packed":
                        entries.add("packed-refs")
                    self.assertEqual({p.name for p in directory.iterdir()}, entries)
                    self.assertEqual({p.relative_to(directory).as_posix() for p in (directory / "objects").rglob("*")},
                                     {"objects/info", "objects/info/alternates"})
                    self.assertTrue((directory / "refs").is_symlink())
                    self.assertEqual((directory / "refs").resolve(), fx.common / "refs")
                    if shape == "packed":
                        self.assertTrue((directory / "packed-refs").is_symlink())
                        self.assertEqual((directory / "packed-refs").resolve(), fx.common / "packed-refs")
                    self.assertEqual((directory / "HEAD").read_bytes(), (fx.common if shape == "bare" else fx.gitdir).joinpath("HEAD").read_bytes())
                    self.assertEqual((directory / "objects/info/alternates").read_text(), str(fx.common / "objects") + "\n")
                    self.assertNotIn("GIT_COMMON_DIR", v.env)
                    if shape == "bare":
                        self.assertNotIn("GIT_WORK_TREE", v.env)
                        self.assertNotIn("GIT_INDEX_FILE", v.env)
                    else:
                        self.assertEqual(v.env["GIT_WORK_TREE"], str(fx.tree))
                        self.assertEqual(v.env["GIT_INDEX_FILE"], str(directory / "index"))
                        self.assertFalse((directory / "index").is_symlink())
                        self.assertEqual((directory / "index").read_bytes(), (fx.gitdir / "index").read_bytes())
                        for shared in fx.gitdir.glob("sharedindex.*"):
                            self.assertEqual((directory / shared.name).read_bytes(), shared.read_bytes())
                    self.assertEqual(self.config_values(directory), self.expected_config(
                        fx, bare=shape == "bare", detached=shape == "detached",
                        upstream=shape != "no-upstream", sha256=shape == "sha256"))
                self.assertFalse(directory.exists())
                self.assertEqual(self.leftovers(), [])

    def test_all_hostile_config_keys_and_includes_are_dropped(self):
        fx = self.fixture()
        # Prove each config vector by itself before stacking their settings.
        for vector in ("fsmonitor", "filter", "hooks", "hooks_in_common", "include", "wtconfig"):
            control = self.fixture(vector)
            prove_control(self, control, vector)
            taint.apply(fx, vector)
        for key in ("core.sshCommand", "core.gitProxy", "credential.helper", "diff.external",
                    "core.pager", "core.worktree", "remote.origin.uploadpack", "remote.origin.proxy",
                    "url.worker.insteadOf", "status.showUntrackedFiles"):
            taint.config(fx, key, "worker-command")
        expected = self.expected_config(fx)
        with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            directory = Path(v.env["GIT_DIR"])
            self.assertEqual(self.config_values(directory), expected)
            text = (directory / "config").read_text()
            for hostile in ("worker-command", "worker.cfg", "worker-hooks", "include-fsmonitor",
                            "wtconfig-fsmonitor", "filter-clean", "textconv", str(fx.markers)):
                self.assertNotIn(hostile, text)
            self.assertNotIn("include", text.lower())
        self.assertEqual(fx.fired(), [])

    def test_bad_branch_names_never_become_config_sections(self):
        for name in ('bad"name', "bad\nname", "bad..name"):
            with self.subTest(name=name):
                fx = self.fixture()
                (fx.gitdir / "HEAD").write_text("ref: refs/heads/" + name + "\n")
                view = self.view
                try:
                    with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                        facts = self.config_values(Path(v.env["GIT_DIR"]))
                        self.assertFalse(any(k.startswith("branch.") for k in facts))
                except view.Unviewable:
                    pass  # rejecting invalid HEAD is also a closed answer
                self.assertEqual(self.leftovers(), [])

    def test_invalid_boolean_is_dropped(self):
        fx = self.fixture()
        taint.config(fx, "core.filemode", "maybe")
        with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            self.assertNotIn("core.filemode", self.config_values(Path(v.env["GIT_DIR"])))

    def test_invalid_upstream_and_fetch_values_are_dropped(self):
        for key, value in (("branch.feat.remote", 'bad"remote'),
                           ("branch.feat.remote", "bad..remote"),
                           ("branch.feat.merge", "refs/heads/bad\nname"),
                           ("branch.feat.merge", "refs/heads/bad..name"),
                           ("remote.origin.fetch", '+refs/heads/feat:refs/remotes/bad"name')):
            with self.subTest(key=key, value=value):
                fx = self.fixture()
                taint.config(fx, key, value)
                with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                    facts = self.config_values(Path(v.env["GIT_DIR"]))
                    self.assertNotIn(key.lower(), facts)
                    self.assertNotIn(value, facts.values())
                self.assertEqual(fx.fired(), [])

    def test_config_is_consulted_without_includes_and_command_uses_private_env(self):
        fx = self.fixture("include")
        prove_control(self, fx, "include")
        view = self.view
        with patch.object(subprocess, "run", wraps=subprocess.run) as started:
            result = self.run_view(fx, ["rev-parse", "HEAD"])
        self.assertEqual(answer(result), (0, fx.sha["D"] + "\n"))
        parsing = [call for call in started.call_args_list if "config" in call.args[0]]
        self.assertEqual(len(parsing), 1)
        argv = parsing[0].args[0]
        self.assertIn("--file", argv)
        self.assertIn(str(fx.common / "config"), argv)
        self.assertIn("-z", argv)
        self.assertIn("--list", argv)
        self.assertNotIn("--includes", argv)
        command = started.call_args_list[-1]
        self.assertEqual(command.args[0], ["git", "rev-parse", "HEAD"])
        self.assertEqual(Path(command.kwargs["cwd"]), fx.tree)
        env = command.kwargs["env"]
        directory = Path(env["GIT_DIR"])
        self.assertEqual(directory.parent, self.base)
        self.assertNotIn("GIT_COMMON_DIR", env)
        self.assertEqual(env["GIT_WORK_TREE"], str(fx.tree))
        self.assertEqual(env["GIT_INDEX_FILE"], str(directory / "index"))
        self.assertFalse(directory.exists())
        self.assertEqual(fx.fired(), [])

    def test_invalid_object_format_and_reftable_config_refuse(self):
        for key, value in (("extensions.objectformat", "md5"), ("extensions.refstorage", "reftable")):
            with self.subTest(key=key):
                fx = self.fixture()
                taint.config(fx, key, value)
                view = self.view
                with self.assertRaises(view.Unviewable):
                    self.run_view(fx, ["rev-parse", "HEAD"])
                self.assertEqual(self.leftovers(), [])

    def test_real_reftable_repository_is_refused(self):
        bare = self.root / "reftable.git"
        bare.mkdir()
        taint.git(["init", "--bare", "-q", "--ref-format=reftable", "-b", "main"], bare)
        self.assertTrue((bare / "reftable").is_dir())
        self.assertEqual(taint.git(["symbolic-ref", "HEAD"], bare).stdout, "refs/heads/main\n")
        view = self.view
        with self.assertRaises(view.Unviewable):
            view.run(["symbolic-ref", "HEAD"], bare, env=git_trees.audit_env(), base=self.base)
        self.assertEqual(self.leftovers(), [])

    def test_many_loose_refs_are_linked_in_under_half_a_second(self):
        fx = self.fixture()
        refs = fx.common / "refs/heads"
        for number in range(3000):
            (refs / f"branch-{number}").write_text(fx.sha["B"] + "\n")
        view = self.view
        start = time.monotonic()
        with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            self.assertTrue(Path(v.env["GIT_DIR"]).joinpath("refs").is_symlink())
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(len(list(refs.glob("branch-*"))), 3000)


class HostileFilesTest(FixtureCase):
    def test_fifo_head_index_and_sharedindex_return_promptly(self):
        for name in ("HEAD", "index", "sharedindex." + "a" * 40):
            with self.subTest(name=name):
                fx = self.fixture()
                target = fx.gitdir / name
                target.unlink(missing_ok=True)
                os.mkfifo(target)
                view = self.view
                with deadline():
                    try:
                        with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                            self.assertNotIn(name, {p.name for p in Path(v.env["GIT_DIR"]).iterdir()})
                    except view.Unviewable:
                        pass
                self.assertEqual(self.leftovers(), [])

    def test_index_over_512_mib_is_not_copied(self):
        fx = self.fixture()
        with (fx.gitdir / "index").open("wb") as file:
            file.truncate(512 * 1024 * 1024 + 1)  # sparse file; no giant allocation
        view = self.view
        with deadline(), self.assertRaises(view.Unviewable):
            with view.opened(fx.tree, git_trees.audit_env(), base=self.base):
                self.fail("oversize index accepted")
        self.assertEqual(self.leftovers(), [])

    def test_oversize_head_and_sharedindex_are_bounded(self):
        for name, size in (("HEAD", 1025), ("sharedindex." + "a" * 40, 512 * 1024 * 1024 + 1)):
            with self.subTest(name=name):
                fx = self.fixture()
                with (fx.gitdir / name).open("wb") as file:
                    file.truncate(size)
                view = self.view
                with deadline():
                    try:
                        with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                            self.assertFalse(Path(v.env["GIT_DIR"]).joinpath(name).exists())
                    except view.Unviewable:
                        pass
                self.assertEqual(self.leftovers(), [])

    def test_symlinked_worker_files_are_never_copied(self):
        for name in ("HEAD", "index", "sharedindex." + "a" * 40):
            with self.subTest(name=name):
                fx = self.fixture()
                target = fx.gitdir / name
                other = fx.root / "other-file"
                original = target.read_bytes() if target.exists() else b"worker data"
                other.write_bytes(original)
                target.unlink(missing_ok=True)
                target.symlink_to(other)
                view = self.view
                try:
                    with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                        self.assertFalse(Path(v.env["GIT_DIR"]).joinpath(name).exists())
                except view.Unviewable:
                    pass
                self.assertEqual(other.read_bytes(), original)
                self.assertEqual(self.leftovers(), [])

    def test_only_oid_named_sharedindex_files_are_copied(self):
        fx = self.fixture()
        for name in ("sharedindex.command", "sharedindex." + "g" * 40,
                     "sharedindex." + "a" * 39, "sharedindex." + "a" * 65):
            (fx.gitdir / name).write_text("worker data")
        with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            self.assertEqual(list(Path(v.env["GIT_DIR"]).glob("sharedindex.*")), [])

    def test_refs_swapped_to_symlink_after_locate_remain_data(self):
        fx = self.fixture()
        expected = answer(raw(fx, ["rev-parse", "HEAD"]))
        view = self.view
        layout = view.locate(fx.tree)
        original = fx.common / "refs"
        renamed = fx.root / "moved-refs"
        original.rename(renamed)
        original.symlink_to(renamed, target_is_directory=True)
        # Pin the race between locating and building, without private build APIs.
        with patch.object(view, "locate", return_value=layout):
            self.assertEqual(answer(self.run_view(fx, ["rev-parse", "HEAD"])), expected)
        self.assertTrue((renamed / "heads/feat").is_file())
        self.assertEqual(fx.fired(), [])

    def test_read_regular_moved_and_legacy_alias_is_identical(self):
        view = self.view
        self.assertIs(git_trees.read_regular, view.read_regular)
        self.assertEqual(view.read_regular.__module__, "git_view")
        regular = self.root / "regular"
        regular.write_bytes(b"abcdef")
        self.assertEqual(view.read_regular(regular, 3), b"abc")
        self.assertEqual(view.read_regular(self.root, 3), None)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with deadline():
            self.assertIsNone(view.read_regular(fifo, 3))
        link = self.root / "link"
        link.symlink_to(regular)
        self.assertEqual(view.read_regular(link, 3), b"abc")
        with self.assertRaises(OSError):
            view.read_regular(link, 3, nofollow=True)
        with self.assertRaises(FileNotFoundError):
            view.read_regular(self.root / "absent", 3)


class PrivacyAndCleanupTest(FixtureCase):
    def test_explicit_temporary_base_is_accepted_and_view_is_private(self):
        fx = self.fixture()
        env = git_trees.audit_env()
        before = dict(env)
        with self.view.opened(fx.tree, env, base=self.base) as v:
            directory = Path(v.env["GIT_DIR"])
            self.assertEqual(directory.parent, self.base)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for key, value in env.items():
                self.assertEqual(v.env[key], value)
        self.assertEqual(env, before)
        self.assertEqual(self.leftovers(), [])

    def test_explicit_base_inside_tree_common_or_gitdir_is_refused(self):
        fx = self.fixture()
        view = self.view
        for parent in (fx.tree, fx.common, fx.gitdir):
            for base in (parent, parent / "views"):
                with self.subTest(base=base), self.assertRaises(view.Unviewable):
                    view.run(["rev-parse", "HEAD"], fx.tree, env=git_trees.audit_env(), base=base)

    def test_default_base_inside_worker_roots_or_system_temp_is_refused(self):
        fx = self.fixture()
        view = self.view
        for cache in (fx.tree, fx.common, fx.gitdir, Path(tempfile.gettempdir())):
            with self.subTest(cache=cache), patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
                with self.assertRaises(view.Unviewable):
                    view.run(["rev-parse", "HEAD"], fx.tree, env=git_trees.audit_env())

    def test_default_base_uses_launcher_home_and_private_cache(self):
        fx = self.fixture()
        # A temporary launcher home in this writable worktree is outside the
        # system temp dir. No writes to the user's actual home or cache.
        temporary = tempfile.TemporaryDirectory(prefix="view-home-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        view = self.view
        for shape in ("home", "xdg"):
            cache = home / (".cache" if shape == "home" else "custom-cache")
            launcher_env = {"HOME": str(home)}
            if shape == "xdg":
                launcher_env["XDG_CACHE_HOME"] = str(cache)
            with self.subTest(shape=shape), patch.dict(os.environ, launcher_env, clear=True), \
                    patch.object(Path, "home", return_value=home):
                with view.opened(fx.tree, git_trees.audit_env()) as v:
                    directory = Path(v.env["GIT_DIR"])
                    self.assertEqual(directory.parent, cache / "chief-of-stuff/git-views")
                    self.assertEqual(stat.S_IMODE(directory.parent.stat().st_mode), 0o700)
                    self.assertEqual(v.env["HOME"], git_trees.SAFE_HOME)
                self.assertFalse(directory.exists())

    def test_real_index_and_refs_untouched_when_optional_locks_are_unset(self):
        for vector in ("hooks", "hooks_in_common"):
            with self.subTest(vector=vector):
                fx = self.fixture(vector, packed=True)
                prove_control(self, fx, vector)
                for path in fx.tree.glob("*.txt"):
                    os.utime(path, (1_900_000_000, 1_900_000_000))
                index = fx.gitdir / "index"
                before = index.read_bytes(), index.stat().st_mtime_ns
                # Packed refs alone leave no loose files to lose on cleanup.
                (fx.common / "refs/heads/keep").write_text(fx.sha["B"] + "\n")
                refs = {p.relative_to(fx.common): p.read_bytes() for p in (fx.common / "refs").rglob("*") if p.is_file()}
                packed = (fx.common / "packed-refs").read_bytes()
                env = git_trees.audit_env()
                env.pop("GIT_OPTIONAL_LOCKS")
                result = self.view.run(["status", "--short"], fx.tree, env=env, base=self.base)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(" M README.md\n", result.stdout)
                self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)
                self.assertTrue((fx.common / "refs").is_dir())
                self.assertEqual({p.relative_to(fx.common): p.read_bytes() for p in (fx.common / "refs").rglob("*") if p.is_file()}, refs)
                self.assertEqual((fx.common / "packed-refs").read_bytes(), packed)
                self.assertEqual(fx.fired(), [])
                self.assertEqual(self.leftovers(), [])

    def test_cleanup_after_success_and_nonzero_git_exit(self):
        fx = self.fixture()
        self.assertEqual(self.run_view(fx, ["rev-parse", "HEAD"]).returncode, 0)
        self.assertEqual(self.leftovers(), [])
        failed = self.run_view(fx, ["rev-parse", "--verify", "refs/heads/absent"])
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(failed.stderr)
        self.assertEqual(self.leftovers(), [])

    def test_cleanup_after_timeout_and_missing_git_during_command(self):
        fx = self.fixture()
        view = self.view
        real_run = subprocess.run
        for error in (subprocess.TimeoutExpired(["git", "rev-parse"], 1), FileNotFoundError("git missing")):
            seen = []
            def failing_command(argv, **kwargs):
                if "config" in argv:
                    return real_run(argv, **kwargs)
                seen.extend(self.leftovers())
                raise error
            with self.subTest(error=type(error).__name__), patch.object(subprocess, "run", side_effect=failing_command):
                with self.assertRaises(type(error)):
                    view.run(["rev-parse", "HEAD"], fx.tree, env=git_trees.audit_env(), timeout=1, base=self.base)
            self.assertTrue(seen, "exception must occur after the private view exists")
            self.assertEqual(self.leftovers(), [])

    def test_cleanup_after_missing_git_during_build(self):
        fx = self.fixture()
        view = self.view
        allocated = []
        real_mkdtemp = tempfile.mkdtemp
        def record_allocation(*args, **kwargs):
            result = real_mkdtemp(*args, **kwargs)
            allocated.append(Path(result))
            return result
        with patch.object(tempfile, "mkdtemp", side_effect=record_allocation), \
                patch.object(subprocess, "run", side_effect=FileNotFoundError("git missing")):
            with self.assertRaises(FileNotFoundError):
                self.run_view(fx, ["rev-parse", "HEAD"])
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(all(not path.exists() for path in allocated))

    def test_cleanup_after_unviewable_and_baseexception_mid_build(self):
        fx = self.fixture()
        view = self.view
        real_run = subprocess.run
        for error in (view.Unviewable("invalid config facts"), KeyboardInterrupt()):
            allocated = []
            real_mkdtemp = tempfile.mkdtemp
            def record_allocation(*args, **kwargs):
                result = real_mkdtemp(*args, **kwargs)
                allocated.append(Path(result))
                return result
            def failing_build(argv, **kwargs):
                if "config" in argv:
                    self.assertTrue(allocated, "exercise failure after mkdtemp, not before construction")
                    raise error
                return real_run(argv, **kwargs)
            with self.subTest(error=type(error).__name__), patch.object(tempfile, "mkdtemp", side_effect=record_allocation), \
                    patch.object(subprocess, "run", side_effect=failing_build):
                with self.assertRaises(type(error)):
                    self.run_view(fx, ["rev-parse", "HEAD"])
            self.assertEqual(self.leftovers(), [])
            self.assertTrue(all(not path.exists() for path in allocated))

    def test_cleanup_after_exception_in_opened_body(self):
        fx = self.fixture()
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                directory = Path(v.env["GIT_DIR"])
                self.assertTrue(directory.exists())
                raise RuntimeError("body failed")
        self.assertFalse(directory.exists())
        self.assertEqual(self.leftovers(), [])

    def test_opened_supports_several_reads_in_one_view_and_cleans_up(self):
        fx = self.fixture("fsmonitor")
        prove_control(self, fx, "fsmonitor")
        with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            directory = Path(v.env["GIT_DIR"])
            for args, expected in ((["rev-parse", "HEAD"], fx.sha["D"] + "\n"),
                                   (["rev-list", "--count", "origin/main..HEAD"], "3\n")):
                result = subprocess.run(["git", *args], cwd=v.layout.tree, env=v.env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(answer(result), (0, expected))
                self.assertTrue(directory.exists())
        self.assertFalse(directory.exists())
        self.assertEqual(fx.fired(), [])
        self.assertEqual(self.leftovers(), [])

    def test_stale_views_swept_fresh_views_and_symlink_targets_preserved(self):
        fx = self.fixture()
        stale = self.base / "view-stale"
        fresh = self.base / "view-fresh"
        other = self.base / "unrelated"
        for path in (stale, fresh, other):
            path.mkdir()
            (path / "keep").write_text("data")
        old = time.time() - 2 * 24 * 60 * 60
        os.utime(stale, (old, old))
        os.utime(other, (old, old))
        link = self.base / "view-old-link"
        link.symlink_to(other, target_is_directory=True)
        os.utime(link, (old, old), follow_symlinks=False)
        self.assertEqual(self.run_view(fx, ["rev-parse", "HEAD"]).returncode, 0)
        self.assertFalse(stale.exists())
        self.assertEqual((fresh / "keep").read_text(), "data")
        self.assertEqual((other / "keep").read_text(), "data")


class GitlinksTest(FixtureCase):
    def test_gitlink_refuses_status_and_diff_without_running_nested_command(self):
        fx = self.fixture("gitlink")
        prove_control(self, fx, "gitlink")
        self.assertFalse((fx.tree / ".gitmodules").exists())
        self.assertFalse((fx.common / "modules").exists())
        view = self.view
        for args in (["status", "--short"], ["diff"], ["diff", "--stat"]):
            with self.subTest(command=args), self.assertRaisesRegex(view.Unviewable, "submodules are not inspected"):
                self.run_view(fx, args)
            self.assertEqual(fx.fired(), [])
            self.assertEqual(self.leftovers(), [])
        self.assertEqual(answer(self.run_view(fx, ["rev-parse", "HEAD"])), (0, fx.sha["D"] + "\n"))

    def test_untracked_nested_repo_is_reported_without_entering_it(self):
        fx = self.fixture("nested_untracked")
        prove_control(self, fx, "nested_untracked")
        truth = answer(raw(fx, ["status", "--short"]))
        self.assertEqual(answer(self.run_view(fx, ["status", "--short"])), truth)
        self.assertEqual(fx.fired(), [])


class TruthTest(FixtureCase):
    """One named test per vector, with loose/packed x ten command subtests."""

    def test_untainted_truth_has_the_required_history_and_dirt(self):
        fx = self.fixture()
        self.assertEqual(answer(raw(fx, ["status", "--short"])),
                         (0, " M README.md\n?? .gitattributes\n?? new.txt\n"))
        self.assertEqual(answer(raw(fx, ["rev-list", "--count", "origin/main..HEAD"])), (0, "3\n"))
        self.assertEqual(answer(raw(fx, ["rev-list", "--count", "@{u}..HEAD"])), (0, "1\n"))
        self.assertEqual(raw(fx, ["merge-base", "--is-ancestor", fx.sha["D"], "main"]).returncode, 1)
        self.compare_truth(fx)

    def test_sha256_loose_and_packed_answers_equal_truth(self):
        for packed in (False, True):
            with self.subTest(packed=packed):
                fx = self.fixture(sha256=True, packed=packed)
                self.assertEqual(len(fx.sha["D"]), 64)
                self.compare_truth(fx)

    def test_normal_clone_answers_equal_truth(self):
        fx = self.fixture()
        self.compare_truth(fx, path=fx.clone)

    def test_split_index_answers_equal_truth(self):
        fx = self.fixture()
        taint.git(["update-index", "--split-index"], fx.tree)
        self.assertTrue(list(fx.gitdir.glob("sharedindex.*")))
        self.compare_truth(fx)

    def test_sparse_cone_and_sparse_index_answers_equal_truth(self):
        fx = self.fixture()
        (fx.tree / "kept").mkdir()
        (fx.tree / "omitted").mkdir()
        for directory in ("kept", "omitted"):
            (fx.tree / directory / "file.txt").write_text("data\n")
        taint.git(["add", "kept", "omitted"], fx.tree)
        taint.git(["commit", "-qm", "directories"], fx.tree)
        taint.git(["sparse-checkout", "init", "--cone", "--sparse-index"], fx.tree)
        taint.git(["sparse-checkout", "set", "kept"], fx.tree)
        self.assertFalse((fx.tree / "omitted").exists())
        sparse = taint.git(["ls-files", "--sparse", "--stage"], fx.tree).stdout
        self.assertIn("040000 ", sparse)
        self.compare_truth(fx)

    def test_detached_and_unborn_heads_equal_truth_including_nonzero_codes(self):
        for shape in ("detached", "unborn"):
            with self.subTest(shape=shape):
                fx = self.fixture()
                if shape == "detached":
                    taint.git(["checkout", "--detach", "-q"], fx.tree)
                else:
                    taint.git(["checkout", "--orphan", "unborn", "-q"], fx.tree)
                    self.assertNotEqual(raw(fx, ["rev-parse", "HEAD"]).returncode, 0)
                self.compare_truth(fx)

    def test_nondefault_fetch_refspec_and_dot_remote_upstream_equal_truth(self):
        for shape in ("single-branch", "dot-remote"):
            with self.subTest(shape=shape):
                fx = self.fixture()
                if shape == "single-branch":
                    taint.config(fx, "remote.origin.fetch", "+refs/heads/feat:refs/remotes/origin/feat")
                    count = "1\n"
                else:
                    taint.config(fx, "branch.feat.remote", ".")
                    taint.config(fx, "branch.feat.merge", "refs/heads/main")
                    count = "2\n"
                self.assertEqual(answer(raw(fx, ["rev-list", "--count", "@{u}..HEAD"])), (0, count))
                self.compare_truth(fx)

    def test_fsmonitor_lie_in_index_cannot_hide_modified_file(self):
        fx = self.fixture()
        # Start clean, let a real fsmonitor protocol response mark all entries
        # valid, then change a file while the monitor reports no changes.
        (fx.tree / "README.md").write_text("a\n")
        (fx.tree / "new.txt").unlink()
        (fx.tree / ".gitattributes").unlink()
        taint.git(["update-index", "--refresh"], fx.tree)
        code = (f"from pathlib import Path; import sys; "
                f"Path({str(fx.markers / 'fsmonitor-lie')!r}).write_text('fired'); "
                "sys.stdout.buffer.write(b'token\\0')")
        taint.config(fx, "core.fsmonitor", shlex.join([sys.executable, "-c", code]))
        taint.git(["update-index", "--fsmonitor"], fx.tree)
        env = git_trees.audit_env()
        env.pop("GIT_OPTIONAL_LOCKS")
        self.assertEqual(answer(raw(fx, ["status", "--short"], env)), (0, ""))
        (fx.tree / "README.md").write_text("z\n")
        self.assertEqual(answer(raw(fx, ["status", "--short"], env)), (0, ""))
        self.assertIn("fsmonitor-lie", fx.fired())
        fx.clear()
        result = self.run_view(fx, ["status", "--short"])
        self.assertEqual(answer(result), (0, " M README.md\n"))
        self.assertEqual(fx.fired(), [])


def vector_test(vector):
    def test(self):
        for packed in (False, True):
            with self.subTest(vector=vector, packed=packed):
                fx = self.fixture(packed=packed)
                truth = {tuple(args): answer(raw(fx, args)) for args in commands(fx)}
                taint.apply(fx, vector)
                prove_control(self, fx, vector)
                if vector == "nested_untracked":
                    truth[("status", "--short")] = answer(raw(fx, ["status", "--short"]))
                view = self.view
                for args in commands(fx):
                    with self.subTest(command=args):
                        if vector in ("commondir", "gitfile", "reftable_marker") or (vector == "gitlink" and args[0] == "status"):
                            with self.assertRaises(view.Unviewable):
                                self.run_view(fx, args)
                        else:
                            result = self.run_view(fx, args)
                            self.assertEqual(answer(result), truth[tuple(args)], result.stderr)
                        self.assertEqual(fx.fired(), [])
                        self.assertEqual(self.leftovers(), [])
                if vector == "grafts_yes":
                    # Pin the security consequence directly: an unmerged D
                    # must not become an ancestor of main because of grafts.
                    result = self.run_view(fx, ["merge-base", "--is-ancestor", fx.sha["D"], "main"])
                    self.assertEqual(answer(result), (1, ""))
                    self.assertEqual(fx.fired(), [])
    test.__name__ = "test_vector_" + vector
    return test


for _vector in taint.VECTORS:
    setattr(TruthTest, "test_vector_" + _vector, vector_test(_vector))


class SignalsTest(FixtureCase):
    def test_signals_read_real_grafts_and_shallow_not_view_metadata(self):
        for vector, expected in ((None, frozenset()), ("grafts", frozenset({"grafts"})),
                                 ("grafts_yes", frozenset({"grafts"})), ("shallow", frozenset({"shallow"}))):
            with self.subTest(vector=vector):
                fx = self.fixture(vector)
                if vector:
                    prove_control(self, fx, vector)
                view = self.view
                layout = view.locate(fx.tree)
                with patch.object(subprocess, "run", side_effect=AssertionError("signals started git")):
                    self.assertEqual(view.signals(layout), expected)
                with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                    private = Path(v.env["GIT_DIR"])
                    self.assertFalse((private / "shallow").exists())
                    self.assertFalse((private / "info/grafts").exists())
                self.assertEqual(view.signals(layout), expected)

    def test_signals_empty_grafts_present_empty_shallow_and_missing_files(self):
        fx = self.fixture()
        view = self.view
        layout = view.locate(fx.tree)
        grafts = fx.common / "info/grafts"
        shallow = fx.common / "shallow"
        grafts.write_bytes(b"")
        self.assertEqual(view.signals(layout), frozenset())
        shallow.write_bytes(b"")
        self.assertEqual(view.signals(layout), frozenset({"shallow"}))
        grafts.write_bytes(b"worker data")
        self.assertEqual(view.signals(layout), frozenset({"grafts", "shallow"}))
        grafts.unlink()
        shallow.unlink()
        self.assertEqual(view.signals(layout), frozenset())
        shutil.rmtree(fx.common / "info")
        self.assertEqual(view.signals(layout), frozenset())


if __name__ == "__main__":
    unittest.main()
