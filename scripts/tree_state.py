"""What a worktree holds that exists nowhere else (#43). Pure: no git, no disk.

`git_trees.read_state` fills a `TreeState` from git; `at_risk` is the rule over it. Work is at risk when
losing this tree loses it: uncommitted files, or commits that neither origin/main nor the branch's
upstream holds. Unknown is never read as safe.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TreeState:
    branch: str
    dirty: int  # uncommitted paths
    off_origin: int | None  # commits on HEAD not on origin/main; None when git could not say
    unpushed: int | None  # commits on HEAD not on its upstream; None when it has no upstream


def at_risk(state: TreeState) -> tuple[str, ...]:
    """What losing this tree would lose, one phrase per fact; empty when nothing."""
    out = [f"{state.dirty} uncommitted"] if state.dirty else []
    if state.unpushed == 0 or state.off_origin == 0:
        return tuple(out)  # every commit is on its upstream, or on origin/main
    out.append("origin/main unknown" if state.off_origin is None else f"{state.off_origin} not on origin/main")
    out.append("no upstream" if state.unpushed is None else f"{state.unpushed} unpushed")
    return tuple(out)
