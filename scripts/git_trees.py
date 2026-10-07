"""Git, and the worktrees on disk to ask it in (#43).

`git()` is the one bounded read-only call every audit question goes through; it ignores replace objects, so a
replace ref cannot rewrite ancestry. `read_state` fills a `TreeState`; `discover` lists the trees under the
`Worktrees:` dir, so the audit starts from what is on disk rather than from the File ownership rows that happen to
name a tree. The one write is `fetch_base`, which moves `refs/remotes/origin/main` (no other ref) and leaves `FETCH_HEAD` and the
fetched objects behind, for `check_sha` and `make_worktree`.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from shell_setup import clean_env
from tree_state import TreeState

# Replace refs and grafts both rewrite parents, so neither may make a yes.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
# What picks a repository over `-C`: the fetch must not inherit them, or it writes where the reads do not look.
REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
             "GIT_ALTERNATE_OBJECT_DIRECTORIES")
GIT_TIMEOUT = 15
BASE = "origin/main"
LOCAL_NOTE = "; local main holds it"
REF = f"refs/remotes/{BASE}"  # the full name: a branch, tag or `refs/origin/main` called `origin/main` shadows the short one


def git(args: list[str], cwd: Path) -> tuple[int, str]:
    """One git call that reads. Never a shell, always a list, always bounded."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            errors="backslashreplace",  # a byte that is not UTF-8 (a Linux ref name) reads as `\xff`, never raises
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
    finds (trees sharing a common git dir are one, as is a pruned
    worktree with its repository), else the root named `.` when it is a git tree, else none."""
    seen: set[Path] = set()
    found = []
    for tree in discover(root, trees):
        code, common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], tree)
        pruned = None if code == 0 else _pruned_common(tree)
        key = Path(common).resolve() if code == 0 else pruned.resolve() if pruned else tree  # unreadable: its pruned repository, else itself
        if key not in seen:
            seen.add(key)
            found.append((tree.name, tree))
    if not found and (root / ".git").exists():
        found.append((".", root.resolve()))
    return found


def fetch_base(tree: Path, timeout: int = 30) -> str:
    """`""` once `origin/main` is fetched, else one line saying why: the first `fatal:` line, else the last, with the
    tree's path blanked and every non-printable character escaped (`clean`), since it is printed. The refspec is
    explicit and forced, so the ref moves whatever `remote.origin.fetch` says and follows a rewritten main, and
    `--no-tags` keeps it the only ref written. Not through `git()`: a fetch needs the caller's credentials and ssh
    agent, which that sandboxed call strips. A fetch that loses a race for the ref (`cannot lock ref`) runs once more."""
    env = {k: v for k, v in os.environ.items() if k not in REPO_VARS}
    try:
        for _ in range(2):  # a concurrent fetch holding the ref has moved it by the second try
            out = subprocess.run(
                ["git", "-C", str(tree), "fetch", "-q", "--no-tags", "origin", f"+refs/heads/main:{REF}"],
                capture_output=True, text=True, errors="backslashreplace", timeout=timeout,  # stderr from ssh or a helper may hold any byte
                env={**env, "GIT_TERMINAL_PROMPT": "0"},
            )
            if out.returncode == 0 or "cannot lock ref" not in out.stderr:
                break
    except (OSError, subprocess.SubprocessError) as exc:  # not `exc`'s text: a timeout's repeats the tree's path
        return f"git fetch did not run ({type(exc).__name__})"
    if out.returncode == 0:
        return ""
    lines = [x for x in out.stderr.strip().split("\n") if x]  # not `splitlines()`: it also splits at `\r` and U+2028, text a remote controls
    line = next((x for x in lines if x.startswith("fatal:")), lines[-1] if lines else f"git fetch exited {out.returncode}")
    return clean(line.replace(str(tree.resolve()), ".").replace(str(tree), "."))  # resolved first: it contains the plain path


def clean(text: str) -> str:
    """`text` with every character `str.isprintable` rejects (a control character, DEL, a C1 control, a bidi or zero-width
    character, U+2028, U+2029) spelled as `repr` spells it, so what a remote or a tree name says cannot forge or split a
    line. Escapes only those: cleaning twice is a no-op."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def _local_note(local: bool) -> str:
    return LOCAL_NOTE if local else ""


def sha_verdict(sha: str, fetch_error: str, exists: bool, ancestor: bool, tip: str, local: bool = False) -> tuple[int, str]:
    """(exit code, one line): 0 only when fetched and on origin/main, 1 fetched and not, 2 unknown because the fetch
    failed: a stale ref cannot say yes, since main may have been rewritten since. `local` (local main holds the commit)
    is said on the unknown line, which would otherwise drop it (`LOCAL_NOTE`)."""
    if fetch_error:
        held = "holds" if ancestor else "does not hold"
        return 2, f"sha {sha}: unknown \u2014 fetch failed: {fetch_error}; {BASE} {tip} as last fetched {held} it{_local_note(local)}"
    if ancestor:
        return 0, f"sha {sha}: on {BASE} {tip} (fetched)"
    if not exists:
        return 1, f"sha {sha}: no such commit after fetch, so not on {BASE} {tip}"
    return 1, f"sha {sha}: not on {BASE} {tip} (fetched)"


def sha_exit(answers: list[tuple[int, bool]]) -> int:
    """The exit over every repository's `(code, held)`, `held` being that it has the commit, or may have it (git cannot
    read it): 1 if one that holds it says no (a clone or a fresh `git init` whose origin was
    pointed at the commit cannot outvote it); else 2 if there is none, or one is unknown and held (a repository that
    may hold the commit may be the project the claim is about, so it blocks a yes); else 0 if any says yes; else 2 if
    any is unknown; else 1. An unknown that does not hold the commit (no remote, offline,
    shallow) blocks no yes, so an unrelated repository cannot veto it forever."""
    if (1, True) in answers:
        return 1
    if not answers or (2, True) in answers:
        return 2
    codes = [code for code, _ in answers]
    return 0 if 0 in codes else 2 if 2 in codes else 1


def _commits(sha: str, tree: Path) -> list[str] | None:
    """The commit ids that start with `sha`, or None when git could not read the repository. `--disambiguate` lists
    ids of every type without reading refs, so a blob or tree is dropped and a tag object's id is not a commit."""
    code, ids = git(["rev-parse", f"--disambiguate={sha}"], tree)
    types = [(i, *git(["cat-file", "-t", i], tree)) for i in ids.split()] if code == 0 else []
    if code != 0 or any(c != 0 for _, c, _ in types):
        return None
    return [i for i, _, kind in types if kind == "commit"]


def _has_grafts(tree: Path) -> bool | None:
    """Whether `info/grafts` is a non-empty file; None when git cannot say where it is or it cannot be examined."""
    code, path = git(["rev-parse", "--path-format=absolute", "--git-path", "info/grafts"], tree)
    if code != 0:
        return None
    try:
        info = Path(path).stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


def read_regular(path: Path, limit: int, nofollow: bool = False) -> bytes | None:
    """At most `limit` bytes of `path`, or None when it is not a regular file (decided on the opened one, so a swap after
    the open cannot matter). For a file a worker can write: `O_NONBLOCK`, so opening a FIFO cannot block; `nofollow` refuses
    a symlink (an `OSError`, as is any open or read that fails)."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | (os.O_NOFOLLOW if nofollow else 0))
    try:
        return os.read(fd, limit) if stat.S_ISREG(os.fstat(fd).st_mode) else None
    finally:
        os.close(fd)


def _pruned_common(tree: Path) -> Path | None:
    """The repository (`<common>`) of a pruned worktree, else None. `tree` is pruned when its `.git` is a regular file (decided on the
    opened one) of at most 4096 bytes whose content is `gitdir: <path>` (that exact prefix, as git requires) with
    `<path>` = `<common>/worktrees/<name>`, KNOWN gone (`lstat` says not found; a permission error, any other `OSError` or a dangling
    symlink is present or unknown). Only the trailing run of `\r`/`\n` is trimmed, as git does; every other byte after `gitdir: ` is the
    path, and a path left with one is no path. Read from the file and the filesystem, never git (a probe can time out). Whether
    `<common>` is itself the git directory git uses there (rule A) is not asked here: `check_sha` asks git, once, and follows the
    redirect once (rule B)."""
    try:
        data = read_regular(tree / ".git", 4097)  # one past the bound: content beyond it is never ignored
        if data is None or len(data) > 4096:
            return None
        line = data.decode()
        path = line.removeprefix("gitdir: ").rstrip("\r\n")
        target = tree / path  # an absolute path replaces `tree`
        if line.startswith("gitdir: ") and not {"\r", "\n"} & set(path) and target.parent.name == "worktrees":
            try:
                target.lstat()
            except FileNotFoundError:
                return target.parent.parent
    except (OSError, ValueError):
        pass
    return None


def check_sha(sha: str, tree: Path) -> tuple[int, str, bool]:
    """One repository's `(exit code, line, held)`; `held` is whether it has a commit with that id, or may have one (git
    cannot read it). A pruned worktree (`_pruned_common`: its `.git` file names a `<common>/worktrees/<name>` known gone) is
    answered by its repository, checked only when the commit lookup failed, with two rules. A: only when `<common>` is itself
    the git directory git uses there (`rev-parse --git-dir` prints `.`), else a plain directory inside another repository would
    answer for that one. B: the redirect is followed once, and the answer is `_verdict`, which never redirects.
    Every other failed read (a refusal, a timeout, an unusable `.git` directory, a main clone that moved, a `<common>` git
    cannot use) may hold the commit and blocks a yes. The commit is the one whose object id starts with `sha`, never a tag or
    branch named like it, and the question is about `refs/remotes/origin/main` only. Exit 0 only when the fetch worked and
    origin/main holds it; 1 when it does not; 2 (unknown, never a no) for every cause: the fetch
    failed (a stale ref cannot say yes), git cannot read the repository (the commit lookup, the grafts lookup or file,
    the shallow check, or, when origin/main does not hold it, the local main lookup or ancestry), there are grafts
    (checked after the fetch, which a hook may have written one in), no origin/main, two commits share the prefix, git
    cannot compare, or the clone is shallow and the commit may be in what was cut off. A repository with no
    `refs/heads/main` is simply not on local main. The unknown lines say `; local main holds it` when it has the
    commit."""
    fetch_error = fetch_base(tree)
    commits = _commits(sha, tree)
    if commits is None and (common := _pruned_common(tree)) and git(["rev-parse", "--git-dir"], common) == (0, "."):  # A (#294)
        tree, fetch_error = common, fetch_base(common)  # the repository forgot this tree: it answers for it, once (B)
        commits = _commits(sha, tree)
    return _verdict(sha, tree, fetch_error, commits)


def _verdict(sha: str, tree: Path, fetch_error: str, commits: list[str] | None) -> tuple[int, str, bool]:
    """`check_sha`'s answer for one repository whose fetch and commit lookup are done. It never redirects."""
    held = commits is None or bool(commits)  # any other repository git could not read may hold it

    def unknown(why: str) -> tuple[int, str, bool]:
        return 2, f"sha {sha}: unknown \u2014 {why}", held

    grafted = None if commits is None else _has_grafts(tree)
    if grafted is None:
        return unknown("git could not read this repository")
    if grafted:
        return unknown("this repository has grafts, so ancestry cannot be trusted")
    ref = git(["rev-parse", "--verify", "-q", "refs/heads/main"], tree)[0] if len(commits) == 1 else 1  # 1: no such ref
    local_code = git(["merge-base", "--is-ancestor", commits[0], "refs/heads/main"], tree)[0] if ref == 0 else ref
    note = _local_note(local_code == 0)
    code, tip = git(["rev-parse", "--short", REF], tree)
    if code != 0:
        return unknown(f"no {BASE} to check against{note}")
    if len(commits) > 1:
        return unknown("more than one commit starts with it")
    compared = git(["merge-base", "--is-ancestor", commits[0], REF], tree)[0] if held else 1
    if compared not in (0, 1):
        return unknown(f"git could not compare it with {BASE}")
    if local_code not in (0, 1) and compared != 0:  # origin/main holding it needs no local fact
        return unknown("git could not read this repository")
    code, line = sha_verdict(sha, fetch_error, held, compared == 0, tip, local_code == 0 and compared == 1)
    if code == 1:
        shallow, out = git(["rev-parse", "--is-shallow-repository"], tree)
        if shallow != 0:
            return unknown("git could not read this repository")
        if out == "true":
            return unknown(f"shallow clone, so history is cut off{note}")
        if local_code == 0:
            line = f"sha {sha}: on local main only, not pushed to {BASE} {tip} (fetched)"
    return code, line, held


def _own_git_dirs(cwd: Path) -> list[str]:
    """The git directories codex may write: a linked worktree's common dir and own gitdir, or a clone's `.git`."""
    # The worker can rewrite `.git`, `commondir`, and gitfiles, so a path is granted only at cwd's own toplevel, where `.git` and git agree.
    def rev(flag: str) -> Path:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", flag], env=clean_env(),
                             capture_output=True, text=True, check=False, timeout=15)
        return Path(out.stdout.strip()).resolve() if out.returncode == 0 and out.stdout.strip() else Path()

    try:
        here, top, common, gitdir = cwd.resolve(), rev("--show-toplevel"), rev("--git-common-dir"), rev("--absolute-git-dir")
        dotgit = here / ".git"
        if top != here or dotgit.is_symlink() or Path() in (common, gitdir):
            return []
        if dotgit.is_dir():  # a normal clone
            return [str(common)] if dotgit.resolve() == common == gitdir else []
        # commondir must name `common` and the back-pointer file must name this `.git`; a damaged file raises and grants nothing.
        owns = (gitdir / (gitdir / "commondir").read_text().strip()).resolve() == common \
            and (gitdir / (gitdir / "gitdir").read_text().strip()).resolve() == dotgit
        return [str(common), str(gitdir)] if owns and gitdir.parent == common / "worktrees" else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def codex_add_dir_args(cwd: Path) -> list[str]:
    """The `--add-dir` argv that grants codex the validated git directories (#428). The grant is paired with git_control's snapshot and restore (#427)."""
    return [a for d in _own_git_dirs(cwd) for a in ("--add-dir", d)]
