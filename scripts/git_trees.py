"""Read-only git, and the worktrees on disk to ask it in (#43).

`git()` is the one bounded call every audit question goes through. `read_state` fills a `TreeState`;
`discover` lists the trees under the `Worktrees:` dir, so the audit starts from what is on disk rather
than from the File ownership rows that happen to name a tree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tree_state import TreeState

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
GIT_TIMEOUT = 15
BASE = "origin/main"


def git(args: list[str], cwd: Path) -> tuple[int, str]:
    """One read-only git call. Never a shell, always a list, always bounded."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            env={**GIT_ENV, "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return out.returncode, (out.stdout or out.stderr).strip()


def _count(rev_range: str, tree: Path) -> int | None:
    code, out = git(["rev-list", "--count", rev_range], tree)
    return int(out) if code == 0 and out.isdecimal() else None


def read_state(tree: Path) -> TreeState:
    _, branch = git(["rev-parse", "--abbrev-ref", "HEAD"], tree)
    code, dirty = git(["status", "--porcelain"], tree)
    return TreeState(
        branch=branch,
        dirty=len(dirty.splitlines()) if code == 0 else 0,
        off_origin=_count(f"{BASE}..HEAD", tree),
        unpushed=_count("@{u}..HEAD", tree),
    )


def discover(root: Path, trees: str) -> list[Path]:
    """Every git tree directly under the Worktrees dir, resolved and inside the root. With no
    `Worktrees:` line there is no dir to walk: the root's own children include the main clone."""
    if not trees:
        return []
    base, top = (root / trees).resolve(), root.resolve()
    found = []
    for child in sorted(base.iterdir()) if base.is_dir() else []:
        path = child.resolve()
        if path.is_relative_to(top) and (path / ".git").exists():
            found.append(path)
    return found


def fetch_base(tree: Path, timeout: int = 30) -> str:
    """`""` once `origin/main` is fetched, else the last stderr line. Not through `git()`: a fetch writes refs
    and needs the caller's credentials and ssh agent, which that sandboxed read-only call strips."""
    try:
        out = subprocess.run(
            ["git", "-C", str(tree), "fetch", "-q", "origin", "main"],
            capture_output=True, text=True, timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}".splitlines()[0]
    return "" if out.returncode == 0 else (out.stderr.strip().splitlines() or [f"git fetch exited {out.returncode}"])[-1]


def sha_verdict(sha: str, fetch_error: str, exists: bool, ancestor: bool, tip: str) -> tuple[int, str]:
    """(exit code, one line): 0 on origin/main, 1 not, 2 unknown because the fetch failed and the last-fetched ref lacks it."""
    failed = f" \u2014 fetch failed: {fetch_error}" if fetch_error else ""
    if ancestor:
        return 0, f"sha {sha}: on {BASE} {tip}" + (f" as last fetched{failed}" if fetch_error else " (fetched)")
    if fetch_error:
        return 2, f"sha {sha}: unknown{failed}; {BASE} {tip} as last fetched does not hold it"
    if not exists:
        return 1, f"sha {sha}: no such commit after fetch, so not on {BASE} {tip}"
    return 1, f"sha {sha}: not on {BASE} {tip} (fetched)"


def check_sha(sha: str, tree: Path) -> tuple[int, str]:
    fetch_error = fetch_base(tree)
    code, tip = git(["rev-parse", "--short", BASE], tree)
    if code != 0:
        return 2, f"sha {sha}: unknown \u2014 no {BASE} in {tree}: {tip}".splitlines()[0]
    exists = git(["cat-file", "-e", f"{sha}^{{commit}}"], tree)[0] == 0
    ancestor = exists and git(["merge-base", "--is-ancestor", sha, BASE], tree)[0] == 0
    return sha_verdict(sha, fetch_error, exists, ancestor, tip)
