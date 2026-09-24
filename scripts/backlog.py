#!/usr/bin/env python3
"""Read the backlog out of GitLab or GitHub, so the tracker can hold what is being worked and nothing else.

Zach moved the tracker to GitHub on 2026-09-22 13:40: "for all future issue tracking, I want it to be
stored in https://github.com/locriani/GauntletIssues as github issues." A `- Backlog:` line whose
first part starts with `GitHub` names a `repo`, is read through `gh`, and is written only by
`gh-issue` (see GH_FILE_WITH). Every other line is the GitLab client below, unchanged.

The tracker grew to 158 rows in 22 hours because the ruleset says one row per item, so every
finding, defect and question became a row and the file became a second Log with a worse index.
The backlog belongs in an issue tracker; `labs.gauntletai.com` is the only authorized host for this
project (workspace CLAUDE.md house rule 2) and it is the one we have.

    python3 backlog.py --config /path/to/CLAUDE.md            # one count line
    python3 backlog.py --config /path/to/CLAUDE.md --list     # one line per open issue

Every failure returns a value. A token that is absent, a host that is down, a 401, a body that is
not JSON: each of those produces a line that says the answer is unknown and why. None of them
produces a zero, because a count nobody could take and a backlog that is empty are not the same
fact, and only one of them means there is nothing to do.

No secret is ever read from a file in a repo. The `- Backlog:` line carries the *name* of an
environment variable; the value comes from that variable or from the macOS Keychain.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

BACKLOG = re.compile(r"^\s*(?:[-*]\s*)?Backlog:\s*(.+?)\s*$", re.MULTILINE)
SCHEMES = ("http", "https")
TIMEOUT = 5.0
PER_PAGE = 100
MAX_PAGES = 50
ACCOUNT = "chief-of-stuff"
DEFAULT_ENV = "CHIEF_OF_STUFF_GITLAB_TOKEN"
OPEN, CLOSED = "opened", "closed"


class BacklogError(ValueError):
    """A `Backlog:` line that cannot be read as a backlog."""


@dataclass(frozen=True)
class Backlog:
    """Where the backlog is and what the token is *called*. Never what the token is."""

    host: str
    project: str
    env: str = DEFAULT_ENV
    account: str = ACCOUNT

    @property
    def service(self) -> str:
        """The Keychain service, derived from the host so one host is one entry and nothing drifts."""
        return f"gitlab-{urlsplit(self.host).hostname or ''}"

    @property
    def issues_url(self) -> str:
        return f"{self.host.rstrip('/')}/api/v4/projects/{quote(self.project, safe='')}/issues"


@dataclass(frozen=True)
class GitHubBacklog:
    """A GitHub repo's issues, read through `gh`. `gh` holds the login, so there is no token to name."""

    repo: str


# `gh issue list` has no pages to follow, only a limit. The same ceiling as GitLab's pages: a result
# that fills it may have been cut, and a cut list is refused rather than read as the whole backlog.
GH_LIMIT = PER_PAGE * MAX_PAGES
GH_FIELDS = "number,title,state,labels,url,updatedAt"
GH_STATE = {OPEN: "open", CLOSED: "closed", "all": "all"}
GH_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


def run_gh(args: list[str]) -> tuple[int, str, str]:
    """One `gh` call. Raises nothing: a missing `gh` is an answer like any other failure."""
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 127, "", "gh not found"
    return done.returncode, done.stdout, done.stderr


@dataclass(frozen=True)
class Issue:
    iid: int
    title: str
    state: str
    labels: tuple[str, ...] = ()
    web_url: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Fetch:
    """Issues, or the reason there are none. `error` empty means the list is the whole answer."""

    issues: tuple[Issue, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class Counts:
    """`None` is not zero. A count that could not be taken stays None all the way to the line."""

    open: int | None = None
    closed: int | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _section(text: str, heading: str) -> list[str]:
    """The lines under `heading`, up to the next `## `. Empty when the heading is absent."""
    out: list[str] = []
    seen = False
    for line_ in text.splitlines():
        if line_.strip() == heading:
            seen = True
            continue
        if seen and line_.startswith("## "):
            break
        if seen:
            out.append(line_)
    return out


def parse_backlog(spec: str) -> Backlog:
    """`<tool>; host <url>; project <group/name>; token env <NAME>` — the shape `Board:` already uses.

    A `token` part that is not `env <NAME>` is refused, and the refusal does not quote what it found:
    house rule 6 forbids a secret in a committed file, and an error message is a committed file's
    next stop.
    """
    parts = [p.strip() for p in spec.split(";") if p.strip()]
    if parts and parts[0].lower().startswith("github"):
        return _parse_github(parts)
    host = project = ""
    env = DEFAULT_ENV
    for part in parts[1:]:
        key, _, value = part.partition(" ")
        key, value = key.lower(), value.strip().strip("`").strip()
        if key == "host":
            host = value
        elif key == "project":
            project = value
        elif key == "token":
            kind, _, rest = value.partition(" ")
            # An environment variable's name has no spaces, so the name ends at the first one and
            # whatever follows is prose. The live line carries a parenthetical after it.
            name = rest.split()[0] if rest.split() else ""
            if kind.lower() != "env" or not name:
                raise BacklogError(
                    "a `Backlog:` line names an environment variable, never a token: write "
                    "`token env NAME`"
                )
            env = name
    if not host:
        raise BacklogError(f"backlog needs a host: {spec!r}")
    if urlsplit(host).scheme not in SCHEMES:
        raise BacklogError(f"backlog host must be http or https: {host!r}")
    if not project:
        raise BacklogError(f"backlog needs a project path: {spec!r}")
    return Backlog(host=host, project=project, env=env)


def _parse_github(parts: list[str]) -> GitHubBacklog:
    """`GitHub…; repo <url|owner/name>`. The repo ends at the first space; a parenthetical is prose."""
    repo = ""
    for part in parts[1:]:
        key, _, value = part.partition(" ")
        if key.lower() == "repo":
            words = value.split()
            repo = words[0].strip("`") if words else ""
    repo = re.sub(r"^https?://(?:www\.)?github\.com/", "", repo).removesuffix(".git").strip("/")
    if not GH_REPO.match(repo):
        raise BacklogError(f"backlog needs a repo owner/name: {repo or 'none given'}")
    return GitHubBacklog(repo=repo)


def backlog_from_config(path: Path) -> Backlog | GitHubBacklog | None:
    """The `Backlog:` line inside `## Coordinator`, or None. No line is a workspace without a backlog."""
    body = "\n".join(_section(Path(path).read_text(), "## Coordinator"))
    m = BACKLOG.search(body)
    return parse_backlog(m.group(1)) if m else None


def keychain_token(account: str, service: str) -> str:
    """The macOS Keychain entry, or "". Not found is the common case, not an incident."""
    found = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True, check=False,
    )
    return found.stdout.strip() if found.returncode == 0 else ""


def token(cfg: Backlog, environ: dict | None = None, keychain=keychain_token) -> str:
    """Environment, then Keychain, then absent. Absent is "" — a state to report, not an exception."""
    env = os.environ if environ is None else environ
    from_env = (env.get(cfg.env) or "").strip()
    if from_env:
        return from_env
    try:
        return (keychain(cfg.account, cfg.service) or "").strip()
    except Exception:  # a Mac without `security`, a locked Keychain, a denied prompt
        return ""


def _call(method: str, url: str, private_token: str, timeout: float,
          payload: dict | None = None) -> tuple[object, dict, str]:
    """One request. Returns (parsed body, headers, error) and raises nothing the caller has to catch."""
    headers = {"PRIVATE-TOKEN": private_token, "User-Agent": "chief-of-stuff/backlog",
               "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (scheme checked in parse_backlog)
            raw = response.read()
            got = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return None, {}, f"HTTP {exc.code} from GitLab"
    except Exception as exc:  # timeout, refused, DNS, TLS
        return None, {}, f"{type(exc).__name__}: {exc}"
    try:
        return json.loads(raw.decode("utf-8", "replace")), got, ""
    except ValueError:
        return None, got, "GitLab did not answer with JSON"


def _get(url: str, private_token: str, timeout: float) -> tuple[object, dict, str]:
    return _call("GET", url, private_token, timeout)


def _issue(row: dict) -> Issue:
    return Issue(
        iid=int(row.get("iid", 0)),
        title=str(row.get("title", "")),
        state=str(row.get("state", "")),
        labels=tuple(str(x) for x in row.get("labels", ())),
        web_url=str(row.get("web_url", "")),
        updated_at=str(row.get("updated_at", "")),
    )


def _gh_list(cfg: GitHubBacklog, state: str, fields: str, labels: tuple[str, ...], gh) -> tuple[list, str]:
    args = ["issue", "list", "-R", cfg.repo, "--state", GH_STATE[state], "--json", fields, "--limit", str(GH_LIMIT)]
    for label in labels:
        args += ["--label", label]
    rc, out, err = gh(args)
    if rc != 0:
        return [], f"gh: {(err.strip().splitlines() or ['exit ' + str(rc)])[-1]}"
    try:
        rows = json.loads(out)
    except ValueError:
        return [], "GitHub did not answer with JSON"
    if not isinstance(rows, list):
        return [], "GitHub answered with something that is not a list of issues"
    if len(rows) >= GH_LIMIT:
        return [], f"{GH_LIMIT} issues or more; refusing to read a partial backlog as whole"
    return rows, ""


def _gh_issue(row: dict) -> Issue:
    return Issue(
        iid=int(row.get("number", 0)),
        title=str(row.get("title", "")),
        state=CLOSED if str(row.get("state", "")).upper() == "CLOSED" else OPEN,
        labels=tuple(str(x.get("name", "")) for x in row.get("labels", ()) if isinstance(x, dict)),
        web_url=str(row.get("url", "")),
        updated_at=str(row.get("updatedAt", "")),
    )


def issues(cfg: Backlog | GitHubBacklog, token: str | None = None, state: str = OPEN,
           labels: tuple[str, ...] = (), per_page: int = PER_PAGE, timeout: float = TIMEOUT, gh=None) -> Fetch:
    """Every page of `state` issues, or the reason there are none. Never a partial list read as whole."""
    if isinstance(cfg, GitHubBacklog):
        rows, error = _gh_list(cfg, state, GH_FIELDS, labels, gh or run_gh)
        return Fetch(error=error) if error else Fetch(issues=tuple(_gh_issue(r) for r in rows if isinstance(r, dict)))
    secret = token if token is not None else globals()["token"](cfg)
    if not secret:
        return Fetch(error=f"no token: set ${cfg.env} or add it to the Keychain as {cfg.service}")
    out: list[Issue] = []
    page = "1"
    for _ in range(MAX_PAGES):
        query = {"state": state, "per_page": per_page, "page": page}
        if labels:
            query["labels"] = ",".join(labels)
        body, headers, error = _get(f"{cfg.issues_url}?{urlencode(query)}", secret, timeout)
        if error:
            return Fetch(error=error)
        if not isinstance(body, list):
            return Fetch(error="GitLab answered with something that is not a list of issues")
        out.extend(_issue(row) for row in body if isinstance(row, dict))
        page = (headers.get("x-next-page") or "").strip()
        if not page:
            return Fetch(issues=tuple(out))
    return Fetch(error=f"more than {MAX_PAGES} pages of issues; refusing to read a partial backlog as whole")


def issue_states(cfg: GitHubBacklog, gh=None) -> dict[int, Issue]:
    """Every issue in the repo, open and closed, by number: one list call, not one per tracker row.
    A failed or partial read raises. An empty map would read as "none of these issues exist"."""
    got = issues(cfg, state="all", gh=gh)
    if not got.ok:
        raise BacklogError(got.error)
    return {i.iid: i for i in got.issues}


@dataclass(frozen=True)
class IssueRef:
    """What a tracker's `issue` cell points at."""

    repo: str
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/issues/{self.number}"

    def label(self, home: str | None) -> str:
        """`#N` in the Backlog repo, `owner/repo#N` anywhere else."""
        return f"#{self.number}" if self.repo == home else f"{self.repo}#{self.number}"


_REF_SHORT = re.compile(r"^([\w.-]+/[\w.-]+)?#(\d+)$")
_REF_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)/?$")
_REF_LINK = re.compile(r"^\[[^\]]*\]\((https://[^)\s]+)\)$")


def issue_ref(cell: str, repo: str | None) -> IssueRef | None:
    """`#N` (in `repo`), `owner/repo#N`, an issue URL, or a markdown link to one. Anything else is None,
    and a bare `#N` with no Backlog repo to resolve it against is None too."""
    text = cell.strip()
    link = _REF_LINK.match(text)
    if link:
        text = link.group(1)
    m = _REF_URL.match(text)
    if m:
        return IssueRef(m.group(1), int(m.group(2)))
    m = _REF_SHORT.match(text)
    if not m or not (m.group(1) or repo):
        return None
    return IssueRef(m.group(1) or repo, int(m.group(2)))


def _total(cfg: Backlog, secret: str, state: str, timeout: float) -> tuple[int | None, str]:
    """GitLab's `X-Total` for one state, or a full count when it withholds the header."""
    body, headers, error = _get(f"{cfg.issues_url}?{urlencode({'state': state, 'per_page': 1, 'page': 1})}",
                                secret, timeout)
    if error:
        return None, error
    total = (headers.get("x-total") or "").strip()
    if total.isdigit():
        return int(total), ""
    fetched = issues(cfg, token=secret, state=state, timeout=timeout)
    return (len(fetched.issues), "") if fetched.ok else (None, fetched.error)


def counts(cfg: Backlog | GitHubBacklog, token: str | None = None, timeout: float = TIMEOUT, gh=None) -> Counts:
    """Open and closed. One failure makes both unknown: half a count is a count nobody can act on."""
    if isinstance(cfg, GitHubBacklog):
        got = {}
        for state in (OPEN, CLOSED):
            rows, error = _gh_list(cfg, state, "number", (), gh or run_gh)
            if error:
                return Counts(error=error)
            got[state] = len(rows)
        return Counts(open=got[OPEN], closed=got[CLOSED])
    secret = token if token is not None else globals()["token"](cfg)
    if not secret:
        return Counts(error=f"no token: set ${cfg.env} or add it to the Keychain as {cfg.service}")
    opened, error = _total(cfg, secret, OPEN, timeout)
    if error:
        return Counts(error=error)
    closed, error = _total(cfg, secret, CLOSED, timeout)
    if error:
        return Counts(error=error)
    return Counts(open=opened, closed=closed)


# ---- the write side -------------------------------------------------------------------------
#
# `--dry-run` is the default and `--commit` is required, because a write is outward-facing and the
# GitLab *web UI* is on the coordinator's human-only list. The API is not the web UI, but the
# caution transfers: the default has to be the mode that cannot do damage.

CREATE, CLOSE, COMMENT, LABEL = "create", "close", "comment", "label"
LABEL_COLOR = "#428bca"
FUTURE = {CREATE: "create", CLOSE: "close", COMMENT: "comment", LABEL: "create label"}
PAST = {CREATE: "created", CLOSE: "closed", COMMENT: "commented on", LABEL: "created label"}


@dataclass(frozen=True)
class Written:
    """What a write did, or would do, or could not do. `done` is the only thing that means it happened."""

    action: str
    what: str
    done: bool = False
    iid: int | None = None
    url: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def write_line(one: Written) -> str:
    """A refusal never borrows a success's wording — 116(a), on the write side."""
    if one.error:
        return f"{one.action} failed: {one.what} — {one.error}"
    if not one.done:
        return f"would {FUTURE[one.action]}: {one.what}"
    if one.what == f"#{one.iid}":   # close and comment name the issue and nothing else
        return f"{PAST[one.action]} {one.what}"
    where = f" #{one.iid}" if one.iid else ""
    return f"{PAST[one.action]}{where}: {one.what}" if where else f"{PAST[one.action]} {one.what}"


def _secret(cfg: Backlog, token: str | None) -> str:
    return token if token is not None else globals()["token"](cfg)


def existing_labels(cfg: Backlog, token: str | None = None, timeout: float = TIMEOUT) -> tuple[set[str], str]:
    """Every label the project has, or the reason we do not know. Empty-and-unknown are different."""
    secret = _secret(cfg, token)
    if not secret:
        return set(), f"no token: set ${cfg.env} or add it to the Keychain as {cfg.service}"
    names: set[str] = set()
    page = "1"
    for _ in range(MAX_PAGES):
        url = f"{cfg.host.rstrip('/')}/api/v4/projects/{quote(cfg.project, safe='')}/labels"
        body, headers, error = _get(f"{url}?{urlencode({'per_page': PER_PAGE, 'page': page})}", secret, timeout)
        if error:
            return set(), error
        if not isinstance(body, list):
            return set(), "GitLab answered with something that is not a list of labels"
        names.update(str(row.get("name", "")) for row in body if isinstance(row, dict))
        page = (headers.get("x-next-page") or "").strip()
        if not page:
            return names, ""
    return set(), f"more than {MAX_PAGES} pages of labels"


def ensure_labels(cfg: Backlog, names: tuple[str, ...], token: str | None = None,
                  commit: bool = False, timeout: float = TIMEOUT) -> tuple[Written, ...]:
    """Create the labels a write needs and no others.

    No vocabulary is baked in. Zach, 2026-09-19 22:47 — "tasks are also going to be changeable over
    time" — so the caller names the labels and this creates whichever of them GitLab has not seen.
    """
    wanted = tuple(n for n in dict.fromkeys(n.strip() for n in names) if n)
    if not wanted:
        return ()
    secret = _secret(cfg, token)
    have, error = existing_labels(cfg, token=secret, timeout=timeout)
    if error:
        return (Written(LABEL, ", ".join(wanted), error=error),)
    out: list[Written] = []
    url = f"{cfg.host.rstrip('/')}/api/v4/projects/{quote(cfg.project, safe='')}/labels"
    for name in wanted:
        if name in have:
            continue
        if not commit:
            out.append(Written(LABEL, name))
            continue
        _body, _headers, failed = _call("POST", url, secret, timeout, {"name": name, "color": LABEL_COLOR})
        out.append(Written(LABEL, name, done=not failed, error=failed))
    return tuple(out)


# GitHub issue text is held to two rules — no issue cited in prose, and a body that is a filled
# template — by `gh-issue` and its PreToolUse guard in ai-additions' github-utilities. A second writer
# here would be a way around both, so on GitHub this module reads and closes, and names the tool
# that writes.
GH_FILE_WITH = ("GitHub issues are filed with gh-issue new (templates and native relationships); "
                "backlog.py does not write them")
GH_COMMENT_WITH = ("GitHub comments go through gh issue comment, which github-utilities checks; "
                   "backlog.py does not write them")


def create(cfg: Backlog | GitHubBacklog, title: str, body: str = "", labels: tuple[str, ...] = (),
           token: str | None = None, commit: bool = False, timeout: float = TIMEOUT, gh=None) -> Written:
    """One issue. Its labels are made first: GitLab drops an unknown label rather than refusing it."""
    if isinstance(cfg, GitHubBacklog):
        return Written(CREATE, title, error=GH_FILE_WITH)
    secret = _secret(cfg, token)
    if not secret:
        return Written(CREATE, title, error=f"no token: set ${cfg.env} or add it to the Keychain as {cfg.service}")
    if labels:
        for made in ensure_labels(cfg, labels, token=secret, commit=commit, timeout=timeout):
            if made.error:
                return Written(CREATE, title, error=f"label {made.what}: {made.error}")
    if not commit:
        return Written(CREATE, title)
    payload = {"title": title}
    if body:
        payload["description"] = body
    if labels:
        payload["labels"] = ",".join(labels)
    got, _headers, error = _call("POST", cfg.issues_url, secret, timeout, payload)
    if error:
        return Written(CREATE, title, error=error)
    row = got if isinstance(got, dict) else {}
    return Written(CREATE, title, done=True, iid=int(row.get("iid", 0)) or None, url=str(row.get("web_url", "")))


def close(cfg: Backlog | GitHubBacklog, iid: int, token: str | None = None, commit: bool = False,
          timeout: float = TIMEOUT, gh=None) -> Written:
    what = f"#{iid}"
    if isinstance(cfg, GitHubBacklog):
        if not commit:
            return Written(CLOSE, what)
        rc, _out, err = (gh or run_gh)(["issue", "close", str(iid), "-R", cfg.repo])
        return Written(CLOSE, what, done=rc == 0, iid=iid, error="" if rc == 0 else f"gh: {err.strip()}")
    secret = _secret(cfg, token)
    if not secret:
        return Written(CLOSE, what, error=f"no token: set ${cfg.env} or add it to the Keychain as {cfg.service}")
    if not commit:
        return Written(CLOSE, what)
    _got, _headers, error = _call("PUT", f"{cfg.issues_url}/{iid}", secret, timeout, {"state_event": "close"})
    return Written(CLOSE, what, done=not error, iid=iid, error=error)


def comment(cfg: Backlog | GitHubBacklog, iid: int, body: str, token: str | None = None, commit: bool = False,
            timeout: float = TIMEOUT, gh=None) -> Written:
    what = f"#{iid}"
    if isinstance(cfg, GitHubBacklog):
        return Written(COMMENT, what, error=GH_COMMENT_WITH)
    secret = _secret(cfg, token)
    if not secret:
        return Written(COMMENT, what, error=f"no token: set ${cfg.env} or add it to the Keychain as {cfg.service}")
    if not commit:
        return Written(COMMENT, what)
    _got, _headers, error = _call("POST", f"{cfg.issues_url}/{iid}/notes", secret, timeout, {"body": body})
    return Written(COMMENT, what, done=not error, iid=iid, error=error)


def line(got: Counts) -> str:
    """One line for the board. Unknown says unknown — it never borrows zero's wording."""
    if not got.ok or got.open is None:
        return f"backlog: unknown — {got.error or 'no answer'}"
    return f"backlog: {got.open} open, {got.closed} closed"


def issue_line(one: Issue) -> str:
    labels = f" [{', '.join(one.labels)}]" if one.labels else ""
    return f"#{one.iid} {one.title}{labels}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the GitLab or GitHub backlog named in a ## Coordinator block.")
    parser.add_argument("--config", default="CLAUDE.md", help="workspace CLAUDE.md holding the block")
    parser.add_argument("--list", action="store_true", help="one line per issue instead of a count")
    parser.add_argument("--state", default=OPEN, choices=(OPEN, CLOSED, "all"), help="which issues")
    parser.add_argument("--labels", default="", help="comma-separated labels to narrow the ask")
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds per request")
    parser.add_argument("--create", metavar="TITLE", help="file one issue")
    parser.add_argument("--body", default="", help="the issue body, or the comment's text")
    parser.add_argument("--close", type=int, metavar="IID", help="close one issue")
    parser.add_argument("--comment", type=int, metavar="IID", help="comment on one issue")
    # No `--token`: argv is readable by `ps`, so the token comes from the environment or the
    # Keychain and from nowhere a shell history can keep it.
    parser.add_argument("--commit", action="store_true",
                        help="actually write. Without it every write is printed and not made.")
    args = parser.parse_args(argv)

    config = Path(args.config)
    if not config.is_file():
        print(f"no config at {config}", file=sys.stderr)
        return 2
    try:
        cfg = backlog_from_config(config)
    except BacklogError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if cfg is None:
        print("backlog: none configured", file=sys.stderr)
        return 2

    labels = tuple(p.strip() for p in args.labels.split(",") if p.strip())
    writes: list[Written] = []
    if args.create:
        writes.append(create(cfg, args.create, body=args.body, labels=labels,
                             commit=args.commit, timeout=args.timeout))
    if args.close:
        writes.append(close(cfg, args.close, commit=args.commit, timeout=args.timeout))
    if args.comment:
        writes.append(comment(cfg, args.comment, args.body, commit=args.commit, timeout=args.timeout))
    if writes:
        for one in writes:
            print(write_line(one))
        if not args.commit:
            print("nothing was written: add --commit to make these real", file=sys.stderr)
        return 0 if all(w.ok for w in writes) else 1

    if not args.list:
        got = counts(cfg, timeout=args.timeout)
        print(line(got))
        return 0 if got.ok else 1

    fetched = issues(cfg, state=args.state, labels=labels, timeout=args.timeout)
    if not fetched.ok:
        print(f"backlog: unknown — {fetched.error}", file=sys.stderr)
        return 1
    for one in fetched.issues:
        print(issue_line(one))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
