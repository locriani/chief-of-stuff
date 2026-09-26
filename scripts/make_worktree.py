#!/usr/bin/env python3
"""Create a checked worktree and branch for a dispatch.

Branch and path are validated before Git writes. This module does not remove worktrees or branches.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_board import BULLET, _section, _unquote  # noqa: E402

AGENT = re.compile(r"^\s*(?:[-*]\s*)?Agent:\s*(\S+)\s+(\S+)\s*$", re.MULTILINE)
LIFETIMES = ("task", "standing")
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
GIT_TIMEOUT = 30
BASE = "main"


class RefusedError(ValueError):
    """Invalid worktree request; nothing created."""


@dataclass(frozen=True)
class Made:
    path: Path
    branch: str
    agent_type: str


def git(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run a bounded Git command without a shell."""
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


def worktrees_dir(claude_md: str) -> str:
    for line in _section(claude_md, "## Coordinator"):
        m = BULLET.match(line)
        if m and _unquote(m.group(2)).lower() == "worktrees":
            return _unquote(m.group(3))
    return ""


def agent_types(claude_md: str) -> dict[str, str]:
    """Parse Agent declarations from the Coordinator block."""
    body = "\n".join(_section(claude_md, "## Coordinator"))
    types: dict[str, str] = {}
    for name, lifetime in AGENT.findall(body):
        if lifetime not in LIFETIMES:
            raise RefusedError(f"agent type {name!r} has lifetime {lifetime!r}; expected one of {', '.join(LIFETIMES)}")
        types[name] = lifetime
    return types


def check_type(claude_md: str, agent_type: str) -> None:
    """Check a declared type; an empty list permits the default behavior."""
    types = agent_types(claude_md)
    if types and agent_type not in types:
        raise RefusedError(f"no agent type {agent_type!r} in the Coordinator block; it lists {', '.join(sorted(types))}")


def check_branch(branch: str, cwd: Path | None = None) -> None:
    """Validate a branch with Git after rejecting option-like names."""
    if not branch or branch.startswith("-"):
        raise RefusedError(f"branch name {branch!r} is empty or would be read as an option")
    code, detail = git(["check-ref-format", "--branch", branch], cwd or Path.cwd())
    if code != 0:
        raise RefusedError(f"git refuses the branch name {branch!r}: {detail or 'not a valid ref'}")


def resolve(root: Path, trees: str, name: str) -> Path:
    """Return an unused worktree path inside the workspace root."""
    if not name or name.startswith("-") or "/" in name or name in (".", ".."):
        raise RefusedError(f"worktree name {name!r} must be one path segment and not an option")
    base = (root / trees).resolve() if trees else root.resolve()
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise RefusedError(f"{name} resolves to {candidate}, outside the workspace root — refused") from None
    if candidate.exists():
        raise RefusedError(f"{candidate} already exists; a dispatch gets a tree of its own")
    return candidate


def build(root: Path, clone: Path, trees: str, name: str, branch: str, agent_type: str) -> Made:
    """Check everything that came out of a file, then make the tree. A refusal creates nothing."""
    check_type((root / "CLAUDE.md").read_text() if (root / "CLAUDE.md").is_file() else "", agent_type)
    check_branch(branch, clone)
    path = resolve(root, trees, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    code, detail = git(["worktree", "add", "-b", branch, "--", str(path), BASE], clone)
    if code != 0:
        raise RefusedError(f"git worktree add failed: {detail}")
    return Made(path, branch, agent_type)


def line(made: Made) -> str:
    return f"worktree {made.path} on {made.branch} off {BASE} for a {made.agent_type}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--type", dest="agent_type", required=True, help="an agent type from the Coordinator block")
    ap.add_argument("--name", required=True, help="the worktree directory name, one path segment")
    ap.add_argument("--branch", required=True, help="the new branch, cut from main")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--clone", required=True, help="the repository the worktree belongs to")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not (root / "CLAUDE.md").is_file():
        print(f"make_worktree: no CLAUDE.md at {root}", file=sys.stderr)
        return 2
    try:
        made = build(root, Path(args.clone), worktrees_dir((root / "CLAUDE.md").read_text()), args.name, args.branch, args.agent_type)
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(line(made))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
