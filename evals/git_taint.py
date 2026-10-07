"""Generic, single-vector repositories for testing reads of worker-written git data.

All programs planted in a repository are Python. Dates enter git through its
environment, never through workspace data or a fixture file.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/var/empty",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
    "GIT_AUTHOR_DATE": "946684800 +0000", "GIT_COMMITTER_DATE": "946684800 +0000",
}

VECTORS = (
    "fsmonitor", "filter", "hooks", "hooks_in_common", "include", "wtconfig",
    "grafts", "grafts_yes", "shallow", "replace", "commondir", "gitfile",
    "gitlink", "nested_untracked", "reftable_marker",
)


def git(args: list[str], cwd: Path, *, env=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV if env is None else env,
                          capture_output=True, text=True, timeout=15, check=check)


@dataclass(frozen=True)
class Fx:
    root: Path
    tree: Path
    clone: Path
    common: Path
    gitdir: Path
    sha: dict[str, str]

    @property
    def markers(self) -> Path:
        return self.root / "markers"

    def fired(self) -> list[str]:
        return sorted(p.name for p in self.markers.iterdir())

    def clear(self) -> None:
        for p in self.markers.iterdir():
            p.unlink()

    def command(self, tag: str, exitcode=0) -> str:
        code = (f"from pathlib import Path; import sys; "
                f"Path({str(self.markers / tag)!r}).write_text('fired'); "
                f"sys.exit({exitcode})")
        return shlex.join([sys.executable, "-c", code])


def config(fx: Fx, key: str, value: str, *, file: Path | None = None) -> None:
    # Configure by explicit file outside any repository: a previously planted
    # core.worktree must not redirect the fixture builder's next command.
    git(["config", "--file", str(file or fx.common / "config"), key, value], fx.root)


def _hook(fx: Fx, path: Path, tag: str) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                    f"Path({str(fx.markers / tag)!r}).write_text('fired')\n")
    path.chmod(0o755)


def _fsmonitor(fx):
    config(fx, "core.fsmonitor", fx.command("fsmonitor", 1))


def _filter(fx):
    config(fx, "filter.x.clean", fx.command("filter-clean"))
    config(fx, "filter.x.smudge", fx.command("filter-smudge"))
    config(fx, "diff.x.textconv", fx.command("textconv"))


def _hooks(fx):
    directory = fx.root / "worker-hooks"
    directory.mkdir()
    for name in ("post-index-change", "reference-transaction", "post-checkout", "pre-commit"):
        _hook(fx, directory / name, "hook-" + name)
    config(fx, "core.hooksPath", str(directory))


def _hooks_in_common(fx):
    _hook(fx, fx.common / "hooks" / "post-index-change", "hook-in-common")


def _include(fx):
    included = fx.root / "worker.cfg"
    config(fx, "core.fsmonitor", fx.command("include-fsmonitor", 1), file=included)
    config(fx, "include.path", str(included))


def _wtconfig(fx):
    config(fx, "core.repositoryformatversion", "1")
    config(fx, "extensions.worktreeConfig", "true")
    config(fx, "core.fsmonitor", fx.command("wtconfig-fsmonitor", 1),
           file=fx.gitdir / "config.worktree")


def _grafts(fx):
    (fx.common / "info" / "grafts").write_text(f"{fx.sha['C']} {fx.sha['A']}\n")


def _grafts_yes(fx):
    (fx.common / "info" / "grafts").write_text(f"{fx.sha['B']} {fx.sha['A']} {fx.sha['D']}\n")


def _shallow(fx):
    (fx.common / "shallow").write_text(f"{fx.sha['C']}\n")


def _replace(fx):
    git(["replace", "-f", fx.sha["D"], fx.sha["B"]], fx.clone)


def _commondir(fx):
    decoy = fx.root / "decoy"
    decoy.mkdir()
    git(["init", "-q", "-b", "main"], decoy)
    config(fx, "core.fsmonitor", fx.command("decoy-fsmonitor", 1), file=decoy / ".git" / "config")
    (fx.gitdir / "commondir").write_text(str(decoy / ".git") + "\n")


def _gitfile(fx):
    forged = fx.root / "forged"
    shutil.copytree(fx.gitdir, forged)
    (forged / "commondir").write_text(str(fx.common) + "\n")
    config(fx, "core.repositoryformatversion", "1")
    config(fx, "extensions.worktreeConfig", "true")
    config(fx, "core.fsmonitor", fx.command("gitfile-fsmonitor", 1), file=forged / "config.worktree")
    (fx.tree / ".git").write_text(f"gitdir: {forged}\n")


def _nested(fx, tracked):
    nested = fx.tree / ("gl" if tracked else "nested")
    nested.mkdir()
    git(["init", "-q", "-b", "main"], nested)
    (nested / "file.txt").write_text("a\n")
    git(["add", "file.txt"], nested)
    git(["commit", "-qm", "nested"], nested)
    oid = git(["rev-parse", "HEAD"], nested).stdout.strip()
    config(fx, "core.fsmonitor", fx.command("nested-fsmonitor", 1), file=nested / ".git" / "config")
    (nested / "file.txt").write_text("z\n")
    if tracked:
        git(["update-index", "--add", "--cacheinfo", f"160000,{oid},gl"], fx.tree)


def _gitlink(fx):
    _nested(fx, True)


def _nested_untracked(fx):
    _nested(fx, False)


def _reftable_marker(fx):
    (fx.common / "reftable").mkdir()


def apply(fx: Fx, vector: str) -> None:
    if vector not in VECTORS:
        raise ValueError(f"unknown taint vector: {vector}")
    globals()["_" + vector](fx)


def _build(tmp: Path, vector, packed, object_format) -> Fx:
    root = Path(tmp).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "markers").mkdir()
    clone = root / "repo"
    clone.mkdir()
    git(["init", "-q", "-b", "main", f"--object-format={object_format}"], clone)
    (clone / "README.md").write_text("a\n")
    git(["add", "."], clone)
    git(["commit", "-qm", "A"], clone)
    (clone / "b.txt").write_text("b\n")
    git(["add", "."], clone)
    git(["commit", "-qm", "B"], clone)
    tree = root / "wt"
    git(["worktree", "add", "-q", "-b", "feat", str(tree)], clone)
    for label in ("C", "D"):
        (tree / f"{label.lower()}.txt").write_text(label.lower() + "\n")
        git(["add", "."], tree)
        git(["commit", "-qm", label], tree)
    sha = {label: git(["rev-parse", ref], clone).stdout.strip()
           for label, ref in (("A", "main~1"), ("B", "main"), ("C", "feat~1"), ("D", "feat"))}
    common = clone / ".git"
    fx = Fx(root, tree, clone, common, common / "worktrees" / "wt", sha)
    git(["update-ref", "refs/remotes/origin/main", sha["A"]], clone)
    git(["update-ref", "refs/remotes/origin/feat", sha["C"]], clone)
    for key, value in (("remote.origin.url", "/nonexistent"),
                       ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
                       ("branch.feat.remote", "origin"), ("branch.feat.merge", "refs/heads/feat")):
        config(fx, key, value)
    if packed:
        git(["pack-refs", "--all", "--prune"], clone)
    (tree / "README.md").write_text("z\n")  # same size, so status must compare content
    (tree / "new.txt").write_text("n\n")
    (tree / ".gitattributes").write_text("*.md filter=x diff=x\n*.txt diff=x\n")
    for name in ("README.md", "b.txt", "c.txt", "d.txt"):
        os.utime(tree / name, (2_000_000_000, 2_000_000_000))
    if vector is not None:
        apply(fx, vector)
    return fx


def build(tmp: Path, vector=None, packed=False) -> Fx:
    return _build(tmp, vector, packed, "sha1")


def build_sha256(tmp: Path, vector=None, packed=False) -> Fx:
    """The same fixture with 64-hex object ids, for the object-format allowlist."""
    return _build(tmp, vector, packed, "sha256")
