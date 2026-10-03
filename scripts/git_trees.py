"""Git, and the worktrees on disk to ask it in (#43).

`git()` is the one bounded read-only call every audit question goes through; it ignores replace objects, so a
replace ref cannot rewrite ancestry. `read_state` fills a `TreeState`; `discover` lists the trees under the
`Worktrees:` dir, so the audit starts from what is on disk rather than from the File ownership rows that happen to
name a tree. The one write is `fetch_base`, which moves `refs/remotes/origin/main` (no other ref) and leaves `FETCH_HEAD` and the
fetched objects behind, for `check_sha`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tree_state import TreeState

# Replace refs and grafts both rewrite parents, so neither may make a yes.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
# What picks a repository over `-C`: the fetch must not inherit them, or it writes where the reads do not look.
REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
             "GIT_ALTERNATE_OBJECT_DIRECTORIES")
GIT_TIMEOUT = 15
BASE = "origin/main"
REF = f"refs/remotes/{BASE}"  # the full name: a branch, tag or `refs/origin/main` called `origin/main` shadows the short one


def git(args: list[str], cwd: Path) -> tuple[int, str]:
    """One git call that reads. Never a shell, always a list, always bounded."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            env={**GIT_ENV, "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 128, f"{type(exc).__name__}: {exc}"  # git's own failure code: 1 is a real answer to `--is-ancestor`
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


def sha_trees(root: Path, trees: str) -> list[tuple[str, Path]]:
    """(name, tree) for each repository `--sha` should ask: the first tree by name of each repository `discover`
    finds (trees sharing a common git dir are one), else the root named `.` when it is a git tree, else none."""
    seen: set[Path] = set()
    found = []
    for tree in discover(root, trees):
        code, common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], tree)
        key = Path(common).resolve() if code == 0 else tree  # a tree git cannot read is its own repository
        if key not in seen:
            seen.add(key)
            found.append((tree.name, tree))
    if not found and (root / ".git").exists():
        found.append((".", root.resolve()))
    return found


def fetch_base(tree: Path, timeout: int = 30) -> str:
    """`""` once `origin/main` is fetched, else one line saying why: the first `fatal:` line, else the last, with the
    tree's path blanked, since it is printed. The refspec is explicit and forced, so the ref moves whatever
    `remote.origin.fetch` says and follows a rewritten main, and `--no-tags` keeps it the only ref written. Not through
    `git()`: a fetch needs the caller's credentials and ssh agent, which that sandboxed call strips."""
    env = {k: v for k, v in os.environ.items() if k not in REPO_VARS}
    try:
        out = subprocess.run(
            ["git", "-C", str(tree), "fetch", "-q", "--no-tags", "origin", f"+refs/heads/main:{REF}"],
            capture_output=True, text=True, timeout=timeout, env={**env, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:  # not `exc`'s text: a timeout's repeats the tree's path
        return f"git fetch did not run ({type(exc).__name__})"
    if out.returncode == 0:
        return ""
    lines = out.stderr.strip().splitlines()
    line = next((x for x in lines if x.startswith("fatal:")), lines[-1] if lines else f"git fetch exited {out.returncode}")
    return line.replace(str(tree.resolve()), ".").replace(str(tree), ".")  # resolved first: it contains the plain path


def sha_verdict(sha: str, fetch_error: str, exists: bool, ancestor: bool, tip: str, local: bool = False) -> tuple[int, str]:
    """(exit code, one line): 0 only when fetched and on origin/main, 1 fetched and not, 2 unknown because the fetch
    failed: a stale ref cannot say yes, since main may have been rewritten since. `local` (local main holds the commit)
    is said on the unknown line, which would otherwise drop it."""
    if fetch_error:
        held = "holds" if ancestor else "does not hold"
        return 2, f"sha {sha}: unknown \u2014 fetch failed: {fetch_error}; {BASE} {tip} as last fetched {held} it" + ("; local main holds it" if local else "")
    if ancestor:
        return 0, f"sha {sha}: on {BASE} {tip} (fetched)"
    if not exists:
        return 1, f"sha {sha}: no such commit after fetch, so not on {BASE} {tip}"
    return 1, f"sha {sha}: not on {BASE} {tip} (fetched)"


def sha_exit(answers: list[tuple[int, bool]]) -> int:
    """The exit over every repository's `(code, held)`: 1 if one that has the commit says no (a clone or a fresh
    `git init` whose origin was pointed at the commit cannot outvote it), else 0 if any says yes, else 2 if any is
    unknown, else 1."""
    codes = [code for code, _ in answers]
    if any(code == 1 and held for code, held in answers):
        return 1
    return 0 if 0 in codes else 2 if 2 in codes else 1


def _commits(sha: str, tree: Path) -> list[str] | None:
    """The commit ids that start with `sha`, or None when git could not read the repository. `--disambiguate` lists
    ids of every type without reading refs, so a blob or tree is dropped and a tag object's id is not a commit."""
    code, ids = git(["rev-parse", f"--disambiguate={sha}"], tree)
    types = [(i, *git(["cat-file", "-t", i], tree)) for i in ids.split()] if code == 0 else []
    if code != 0 or any(c != 0 for _, c, _ in types):
        return None
    return [i for i, _, kind in types if kind == "commit"]


def check_sha(sha: str, tree: Path) -> tuple[int, str, bool]:
    """One repository's `(exit code, line, held)`; `held` is whether it has a commit with that id (False when it could
    not be read). The commit is the one whose object id starts with `sha`, never a tag or branch named like it, and
    the question is about `refs/remotes/origin/main` only. Unknown, never a no, when git cannot read the repository,
    ancestry cannot be trusted (grafts, checked after the fetch, which a hook may have written one in), git cannot
    compare, two commits share the prefix, or the clone is shallow and the commit may be in what was cut off."""
    fetch_error = fetch_base(tree)
    commits = _commits(sha, tree)
    if commits is None:
        return 2, f"sha {sha}: unknown \u2014 git could not read this repository", False
    held = bool(commits)
    code, grafts = git(["rev-parse", "--path-format=absolute", "--git-path", "info/grafts"], tree)
    if code == 0 and Path(grafts).is_file() and Path(grafts).stat().st_size:
        return 2, f"sha {sha}: unknown \u2014 this repository has grafts, so ancestry cannot be trusted", held
    code, tip = git(["rev-parse", "--short", REF], tree)
    if code != 0:
        return 2, f"sha {sha}: unknown \u2014 no {BASE} to check against", held
    if len(commits) > 1:
        return 2, f"sha {sha}: unknown \u2014 more than one commit starts with it", held
    compared = git(["merge-base", "--is-ancestor", commits[0], REF], tree)[0] if held else 1
    if compared not in (0, 1):
        return 2, f"sha {sha}: unknown \u2014 git could not compare it with {BASE}", held
    local = held and compared == 1 and git(["merge-base", "--is-ancestor", commits[0], "refs/heads/main"], tree)[0] == 0
    code, line = sha_verdict(sha, fetch_error, held, compared == 0, tip, local)
    if code != 1:
        return code, line, held
    if git(["rev-parse", "--is-shallow-repository"], tree)[1] == "true":
        return 2, f"sha {sha}: unknown \u2014 shallow clone, so history is cut off" + ("; local main holds it" if local else ""), held
    if local:
        return 1, f"sha {sha}: on local main only, not pushed to {BASE} {tip} (fetched)", held
    return code, line, held
