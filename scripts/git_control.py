"""The git control files a codex worker's git grant lets it write, recorded before its run and put back after it (#427).

git reads these files and acts on what they say, so a worker that rewrites one could make the launcher's own later git
call do something the worker chose. `snapshot` records them; `verify_and_restore` puts back every difference, with no git
call that reads the repository in between, and says which names differed. Only the config keys `git push -u` writes
survive a run; the rest of the config is restored whole.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import git_trees
from shell_setup import clean_env

MAX_BYTES = 4 * 1024 * 1024  # a snapshot of more than this is none: the run goes without the grant
# What a push or set-upstream writes to the config, and nothing else. `git config --list` lowercases the section and variable.
BENIGN = re.compile(rb"branch\..+\.(remote|merge|rebase|pushremote)", re.IGNORECASE)

# What is at a path: ("file", bytes, mode), ("link", target), ("other",), or None when nothing is.
Entry = tuple | None


@dataclass
class Snapshot:
    entries: dict[str, tuple[Path, Entry]]  # report name -> where it lives and what it held
    hooks: Path
    has_hooks: bool  # whether `hooks` was a real directory
    config_keys: list[bytes] | None  # the recorded config's entries, None when it could not be parsed


def _entry(path: Path) -> Entry:
    """What is at `path`, by lstat, so a symlink is itself and never what it points at."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return ("link", os.readlink(path))
    if stat.S_ISREG(info.st_mode):
        data = git_trees.read_regular(path, MAX_BYTES + 1, nofollow=True)
        return ("file", data, stat.S_IMODE(info.st_mode)) if data is not None else ("other",)
    return ("other",)


def _config_entries(path: Path) -> list[bytes] | None:
    """The config's `key\\nvalue` entries in order, read by git without running anything the file names, else None."""
    try:
        out = subprocess.run(["git", "config", "--file", str(path), "--null", "--list"], env=clean_env(),
                             capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.split(b"\0") if out.returncode == 0 else None


def _harmful(entries: list[bytes]) -> list[bytes]:
    return [e for e in entries if not BENIGN.fullmatch(e.split(b"\n", 1)[0])]


def snapshot(cwd: Path) -> Snapshot | None:
    """The control files of `cwd`'s validated git grant, or None: no grant to make, more than MAX_BYTES, or anything unreadable."""
    dirs = git_trees._own_git_dirs(cwd)
    if not dirs:
        return None
    common, own = Path(dirs[0]), Path(dirs[1]) if len(dirs) > 1 else None
    paths = {"config": common / "config"}
    if own:
        paths |= {"commondir": own / "commondir", "gitdir": own / "gitdir", "config.worktree": own / "config.worktree",
                  ".git": cwd.resolve() / ".git"}
    hooks = common / "hooks"
    try:
        has_hooks = _is_dir(hooks)
        if _entry(hooks) and not has_hooks:
            return None  # a link or a file where the hooks dir should be: not ours to put back
        if has_hooks:
            paths |= {f"hooks/{name}": hooks / name for name in os.listdir(hooks)}
        entries = {name: (path, _entry(path)) for name, path in paths.items()}
    except OSError:
        return None
    held = [e for _, e in entries.values() if e]
    if any(e[0] == "other" for e in held) or sum(len(e[1]) for e in held if e[0] == "file") > MAX_BYTES:
        return None
    config = entries["config"][1]
    return Snapshot(entries, hooks, has_hooks, _config_entries(paths["config"]) if config and config[0] == "file" else None)


def _is_dir(path: Path) -> bool:
    """A real directory: a link to one is not."""
    return os.path.lexists(path) and stat.S_ISDIR(os.lstat(path).st_mode)


def _remove(path: Path) -> None:
    if os.path.lexists(path):
        shutil.rmtree(path) if _is_dir(path) else os.unlink(path)


def _put(path: Path, entry: Entry) -> None:
    """Make `path` hold exactly `entry`: whatever is there goes first, and a file is created new and never through a link."""
    _remove(path)
    if entry and entry[0] == "link":
        os.symlink(entry[1], path)
    elif entry:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(entry[1])
            os.fchmod(f.fileno(), entry[2])


def verify_and_restore(snap: Snapshot) -> list[str]:
    """Put every recorded control file back as it was and return the sorted names that differed (`config`, `hooks/<name>`,
    `commondir`, `gitdir`, `config.worktree`, `.git`). A benign config change is kept and not reported. Nothing is read through a link."""
    changed = []
    if _is_dir(snap.hooks) != snap.has_hooks or (not snap.has_hooks and os.path.lexists(snap.hooks)):
        _remove(snap.hooks)  # swapped for a link or a file, or added where there was none
        if snap.has_hooks:
            snap.hooks.mkdir()
        changed.append("hooks")
    names = dict(snap.entries)
    if snap.has_hooks:
        for name in os.listdir(snap.hooks):
            names.setdefault(f"hooks/{name}", (snap.hooks / name, None))  # added
    for name, (path, recorded) in names.items():
        try:
            now = _entry(path)
        except OSError:
            now = ("unreadable",)
        if now == recorded:
            continue
        if name == "config" and _only_benign(path, now, recorded, snap.config_keys):
            continue
        _put(path, recorded)
        changed.append(name)
    return sorted(changed)


def _only_benign(path: Path, now: Entry, recorded: Entry, before: list[bytes] | None) -> bool:
    if before is None or not now or not recorded or now[0] != "file" or now[2] != recorded[2]:
        return False
    after = _config_entries(path)
    return after is not None and _harmful(after) == _harmful(before)
