"""Read worker-written git data through private, launcher-authored metadata.

Callers supply the audit environment. Refs and objects remain untrusted data;
config, hooks, grafts and shallow boundaries never enter the view. This module
does not serve writes or inspect submodules.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class Unviewable(Exception):
    """A short, path-free reason why the repository cannot supply a safe view."""


@dataclass(frozen=True)
class Layout:
    tree: Path | None
    gitdir: Path
    common: Path


@dataclass(frozen=True)
class View:
    layout: Layout
    env: dict[str, str]


def read_regular(path: Path, limit: int, nofollow: bool = False) -> bytes | None:
    """At most `limit` bytes of `path`, or None when it is not a regular file (decided on the opened one, so a swap after
    the open cannot matter). For a file a worker can write: `O_NONBLOCK`, so opening a FIFO cannot block; `nofollow` refuses
    a symlink (an `OSError`, as is any open or read that fails)."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | (os.O_NOFOLLOW if nofollow else 0))
    try:
        return os.read(fd, limit) if stat.S_ISREG(os.fstat(fd).st_mode) else None
    finally:
        os.close(fd)


def _read(path: Path, limit: int) -> bytes:
    try:
        data = read_regular(path, limit + 1, nofollow=True)
    except OSError:
        raise Unviewable("metadata is not a readable regular file") from None
    if data is None or len(data) > limit:
        raise Unviewable("metadata is not a bounded regular file")
    return data


def _repository(path: Path) -> None:
    if not stat.S_ISREG((path / "HEAD").lstat().st_mode):
        raise Unviewable("HEAD is not a regular file")
    if not (path / "objects").is_dir() or not (path / "refs").is_dir():
        raise Unviewable("repository data directories are missing")


def locate(path: Path) -> Layout:
    """Resolve a clone, linked tree or bare git directory using files only."""
    try:
        tree = Path(path).resolve()
        dotgit = tree / ".git"
        try:
            mode = dotgit.lstat().st_mode
        except FileNotFoundError:
            _repository(tree)
            return Layout(None, tree, tree)
        if stat.S_ISDIR(mode):
            common = dotgit.resolve()
            _repository(common)
            return Layout(tree, common, common)
        if not stat.S_ISREG(mode):
            raise Unviewable(".git is neither a directory nor a regular file")
        line = _read(dotgit, 4096).decode()
        target = line.removeprefix("gitdir: ").rstrip("\r\n")
        if not line.startswith("gitdir: ") or not target or "\n" in target or "\r" in target:
            raise Unviewable(".git is not a gitfile")
        gitdir = (tree / target).resolve()
        common_name = _read(gitdir / "commondir", 4096).decode().rstrip("\r\n")
        back_name = _read(gitdir / "gitdir", 4096).decode().rstrip("\r\n")
        if not common_name or not back_name or any(c in common_name + back_name for c in "\r\n"):
            raise Unviewable("invalid worktree pointers")
        common = (gitdir / common_name).resolve()
        back = (gitdir / back_name).resolve()
        if gitdir.parent != common / "worktrees" or back != dotgit:
            raise Unviewable("not a linked worktree of its own repository")
        _repository(common)
        return Layout(tree, gitdir, common)
    except (OSError, ValueError, RuntimeError):
        raise Unviewable("repository layout is unreadable") from None


_OID = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
_NAME = r"[A-Za-z0-9._/-]+"
_BOOL_KEYS = ("core.filemode", "core.ignorecase", "core.precomposeunicode", "core.symlinks")
_INDEX_LIMIT = 512 * 1024 * 1024


def _head(layout: Layout) -> tuple[bytes, str | None]:
    data = _read(layout.gitdir / "HEAD", 1024)
    text = data.decode(errors="replace").rstrip("\r\n")
    if re.fullmatch(_OID, text):
        return data, None
    if text.startswith("ref: refs/"):
        ref = text.removeprefix("ref: ")
        if (re.fullmatch(_NAME, ref) and ".." not in ref and "//" not in ref
                and not any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))
                and not ref.endswith(("/", "."))):
            branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None
            return data, branch
    raise Unviewable("HEAD is neither a safe symbolic ref nor an object id")


def _config(layout: Layout, branch: str | None, env: dict[str, str], timeout: float) -> str:
    # Explicit-file parsing never follows includes or loads repository config.
    # Check the file type first so a planted FIFO is refused before starting git.
    _read(layout.common / "config", 1024 * 1024)
    out = subprocess.run(["git", "config", "--file", str(layout.common / "config"), "-z", "--list"],
                         cwd=Path("/"), env=env, capture_output=True, text=True, errors="backslashreplace", timeout=timeout)
    if out.returncode:
        raise Unviewable("config is unreadable")
    facts: dict[tuple[str, str], list[str]] = {}
    fmt = "sha1"
    for record in out.stdout.split("\0"):
        key, _, value = record.partition("\n")
        if key.lower() in _BOOL_KEYS and value in ("true", "false"):
            facts[("core", key.lower().split(".", 1)[1])] = [value]
        elif key.lower() == "extensions.objectformat":
            if value not in ("sha1", "sha256"):
                raise Unviewable("unsupported object format")
            fmt = value
        elif key.lower() == "extensions.refstorage" and value != "files":
            raise Unviewable("unsupported ref storage")
        elif branch and key in (f"branch.{branch}.remote", f"branch.{branch}.merge"):
            if re.fullmatch(_NAME, value) and ".." not in value:
                facts[(f'branch "{branch}"', key.rsplit(".", 1)[1])] = [value]
        else:
            remote = re.fullmatch(r"remote\.([A-Za-z0-9._-]+)\.fetch", key)
            if (remote and ".." not in remote[1] and ".." not in value
                    and re.fullmatch(r"\+?[A-Za-z0-9._/*-]+:[A-Za-z0-9._/*-]+", value)):
                section = f'remote "{remote[1]}"'
                fetches = facts.setdefault((section, "fetch"), [])
                if value not in fetches:
                    fetches.append(value)
                facts[(section, "url")] = ["/nonexistent"]
    lines = ["[core]", f"\trepositoryformatversion = {int(fmt == 'sha256')}",
             f"\tbare = {str(layout.tree is None).lower()}", "\tfsmonitor = false", "\tuntrackedCache = false",
             "\tcommitGraph = false", "\tmultiPackIndex = false", f"\thooksPath = {os.devnull}",
             f"\tattributesFile = {os.devnull}", f"\texcludesFile = {os.devnull}"]
    for (section, key), values in facts.items():
        if section == "core":
            lines.append(f"\t{key} = {values[0]}")
    if fmt == "sha256":
        lines.extend(["[extensions]", "\tobjectformat = sha256"])
    lines.extend(["[status]", "\tsubmoduleSummary = false", "[diff]", "\tignoreSubmodules = all",
                  "[pack]", "\tuseBitmaps = false"])
    for (section, key), values in facts.items():
        if section != "core":
            lines.append(f"[{section}]")
            lines.extend(f"\t{key} = {value}" for value in values)
    return "\n".join(lines) + "\n"


def _copy_index(source: Path, target: Path) -> None:
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError:
        raise Unviewable("index is not a readable regular file") from None
    with os.fdopen(fd, "rb") as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _INDEX_LIMIT:
            raise Unviewable("index is not a bounded regular file")
        remaining = _INDEX_LIMIT
        with target.open("wb") as dst:
            while chunk := src.read(min(1024 * 1024, remaining + 1)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise Unviewable("index is too large")
                dst.write(chunk)


def _base(layout: Layout, base: Path | None) -> Path:
    explicit = base is not None
    if base is None:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        base = cache / "chief-of-stuff/git-views"
    base = Path(base).resolve()
    forbidden = [layout.tree, layout.common, layout.gitdir]
    if not explicit:
        forbidden.append(Path(tempfile.gettempdir()).resolve())
    if any(root is not None and base.is_relative_to(root) for root in forbidden):
        raise Unviewable("the view directory would be worker-writable")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not explicit:
        base.chmod(0o700)
    cutoff = time.time() - 24 * 60 * 60
    with os.scandir(base) as entries:
        for entry in entries:
            if entry.name.startswith("view-"):
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and info.st_mtime < cutoff:
                        shutil.rmtree(entry.path, ignore_errors=True)
                except OSError:
                    pass
    return base


@contextmanager
def _opened(path: Path, env: dict[str, str], base: Path | None, deadline: float) -> Iterator[View]:
    layout = locate(path)
    if (layout.common / "reftable").exists():
        raise Unviewable("reftable ref storage")
    directory = Path(tempfile.mkdtemp(prefix="view-", dir=_base(layout, base)))
    try:
        directory.chmod(0o700)
        head, branch = _head(layout)
        (directory / "HEAD").write_bytes(head)
        (directory / "refs").symlink_to(layout.common / "refs", target_is_directory=True)
        if (layout.common / "packed-refs").exists():
            (directory / "packed-refs").symlink_to(layout.common / "packed-refs")
        (directory / "objects/info").mkdir(parents=True)
        objects = str(layout.common / "objects")
        if "\n" in objects or "\r" in objects:
            raise Unviewable("object directory cannot be represented")
        (directory / "objects/info/alternates").write_text(objects + "\n")
        if layout.tree is not None:
            _copy_index(layout.gitdir / "index", directory / "index")
            for shared in layout.gitdir.glob("sharedindex.*"):
                if re.fullmatch(r"sharedindex\." + _OID, shared.name):
                    _copy_index(shared, directory / shared.name)
        (directory / "config").write_text(_config(layout, branch, env, _remaining(deadline)))
        child_env = {**env, "GIT_DIR": str(directory)}
        if layout.tree is not None:
            child_env.update(GIT_WORK_TREE=str(layout.tree), GIT_INDEX_FILE=str(directory / "index"))
        view = View(layout, child_env)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    try:
        yield view
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("git view", 0)
    return remaining


@contextmanager
def opened(path: Path, env: dict[str, str], base: Path | None = None) -> Iterator[View]:
    """Build one view for several reads; always remove it when the body exits."""
    with _opened(path, env, base, time.monotonic() + 15) as view:
        yield view


def run(args: list[str], path: Path, *, env: dict[str, str], timeout: int = 15,
        base: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Build, read and remove a view within one subprocess time budget."""
    deadline = time.monotonic() + timeout
    with _opened(path, env, base, deadline) as view:
        cwd = view.layout.tree or Path(view.env["GIT_DIR"])
        if args and args[0] in ("status", "diff"):
            index = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=cwd, env=view.env,
                                   capture_output=True, timeout=_remaining(deadline))
            if index.returncode:
                raise Unviewable("index is unreadable")
            if any(entry.startswith(b"160000 ") for entry in index.stdout.split(b"\0")):
                raise Unviewable("submodules are not inspected")
        return subprocess.run(["git", *args], cwd=cwd, env=view.env, capture_output=True,
                              text=True, errors="backslashreplace", timeout=_remaining(deadline))


def signals(layout: Layout) -> frozenset[str]:
    """Conservative ancestry warnings from real metadata, never from the view."""
    found = set()
    for name, path in (("grafts", layout.common / "info/grafts"), ("shallow", layout.common / "shallow")):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            found.add(name)
        else:
            if name == "shallow" or not stat.S_ISREG(info.st_mode) or info.st_size > 0:
                found.add(name)
    return frozenset(found)
