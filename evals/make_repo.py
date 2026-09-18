"""Build a real git fixture for a run: a bare origin, a clone that has pushed to it, and worktrees.

A case that audits `done` lanes needs git's own answer to "is this on main", and `merge-base
--is-ancestor` has no useful fake. Fixtures cannot carry a repo — `render_tree` reads every file as
text and git will not track a nested `.git` — so the repo is built inside the work dir per run.

    make_repo.build(root, {"trees": "trees", "files": {"src/a.py": "x = 1\n"},
                           "worktrees": [{"name": "wt", "branch": "b", "merged": False}]})
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.test",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.test",
}


def git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**ENV, "HOME": str(cwd)}, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {out.stderr.strip()}")
    return out.stdout.strip()


def build(root: Path, spec: dict[str, Any]) -> Path:
    """origin.git + repo/ (main pushed) + one worktree per entry. Returns the clone."""
    origin = root / spec.get("origin", "origin.git")
    origin.mkdir(parents=True, exist_ok=True)
    git(["init", "--bare", "--initial-branch=main", "."], origin)

    clone = root / spec.get("clone", "repo")
    clone.mkdir(parents=True, exist_ok=True)
    git(["init", "--initial-branch=main", "."], clone)
    (clone / "README.md").write_text("base\n")
    # A lane that names paths needs those paths to exist, or the case measures the fixture rather
    # than the rule: a coordinator that finds the file missing hands the lane back, correctly, and
    # never reaches the behaviour under test.
    for rel, body in spec.get("files", {}).items():
        path = clone / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    git(["add", "-A"], clone)
    git(["commit", "-m", "base"], clone)
    git(["remote", "add", "origin", str(origin)], clone)
    git(["push", "-u", "origin", "main"], clone)

    trees = root / spec.get("trees", "trees")
    trees.mkdir(parents=True, exist_ok=True)
    for entry in spec.get("worktrees", []):
        name, branch = entry["name"], entry["branch"]
        path = trees / name
        if entry.get("merged", True):
            git(["branch", branch], clone)
            git(["worktree", "add", str(path), branch], clone)
        else:
            git(["worktree", "add", "-b", branch, str(path), "main"], clone)
            (path / f"{name}.txt").write_text("work in progress\n")
            git(["add", f"{name}.txt"], path)
            git(["commit", "-m", f"{name} work"], path)
        if entry.get("dirty"):
            (path / "scratch.txt").write_text("unsaved\n")
    return clone
