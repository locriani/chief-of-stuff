"""Read-only git, and the worktrees on disk to ask it in (#43).

`git()` is the one bounded call every audit question goes through. `read_state` fills a `TreeState`;
`discover` lists the trees under the `Worktrees:` dir, so the audit starts from what is on disk rather
than from the File ownership rows that happen to name a tree.
"""

from __future__ import annotations

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
