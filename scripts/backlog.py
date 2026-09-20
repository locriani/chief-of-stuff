#!/usr/bin/env python3
"""Read the backlog out of GitLab, so the tracker can hold what is being worked and nothing else.

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
            kind, _, name = value.partition(" ")
            if kind.lower() != "env" or not name.strip():
                raise BacklogError(
                    "a `Backlog:` line names an environment variable, never a token: write "
                    "`token env NAME`"
                )
            env = name.strip()
    if not host:
        raise BacklogError(f"backlog needs a host: {spec!r}")
    if urlsplit(host).scheme not in SCHEMES:
        raise BacklogError(f"backlog host must be http or https: {host!r}")
    if not project:
        raise BacklogError(f"backlog needs a project path: {spec!r}")
    return Backlog(host=host, project=project, env=env)


def backlog_from_config(path: Path) -> Backlog | None:
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


def _get(url: str, private_token: str, timeout: float) -> tuple[object, dict, str]:
    """One GET. Returns (parsed body, headers, error) and raises nothing the caller has to catch."""
    request = urllib.request.Request(
        url, method="GET",
        headers={"PRIVATE-TOKEN": private_token, "User-Agent": "chief-of-stuff/backlog",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (scheme checked in parse_backlog)
            raw = response.read()
            headers = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return None, {}, f"HTTP {exc.code} from GitLab"
    except Exception as exc:  # timeout, refused, DNS, TLS
        return None, {}, f"{type(exc).__name__}: {exc}"
    try:
        return json.loads(raw.decode("utf-8", "replace")), headers, ""
    except ValueError:
        return None, headers, "GitLab did not answer with JSON"


def _issue(row: dict) -> Issue:
    return Issue(
        iid=int(row.get("iid", 0)),
        title=str(row.get("title", "")),
        state=str(row.get("state", "")),
        labels=tuple(str(x) for x in row.get("labels", ())),
        web_url=str(row.get("web_url", "")),
        updated_at=str(row.get("updated_at", "")),
    )


def issues(cfg: Backlog, token: str | None = None, state: str = OPEN, labels: tuple[str, ...] = (),
           per_page: int = PER_PAGE, timeout: float = TIMEOUT) -> Fetch:
    """Every page of `state` issues, or the reason there are none. Never a partial list read as whole."""
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


def counts(cfg: Backlog, token: str | None = None, timeout: float = TIMEOUT) -> Counts:
    """Open and closed. One failure makes both unknown: half a count is a count nobody can act on."""
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


def line(got: Counts) -> str:
    """One line for the board. Unknown says unknown — it never borrows zero's wording."""
    if not got.ok or got.open is None:
        return f"backlog: unknown — {got.error or 'no answer'}"
    return f"backlog: {got.open} open, {got.closed} closed"


def issue_line(one: Issue) -> str:
    labels = f" [{', '.join(one.labels)}]" if one.labels else ""
    return f"#{one.iid} {one.title}{labels}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the GitLab backlog named in a ## Coordinator block.")
    parser.add_argument("--config", default="CLAUDE.md", help="workspace CLAUDE.md holding the block")
    parser.add_argument("--list", action="store_true", help="one line per issue instead of a count")
    parser.add_argument("--state", default=OPEN, choices=(OPEN, CLOSED, "all"), help="which issues")
    parser.add_argument("--labels", default="", help="comma-separated labels to narrow the ask")
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds per request")
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

    if not args.list:
        got = counts(cfg, timeout=args.timeout)
        print(line(got))
        return 0 if got.ok else 1

    labels = tuple(p.strip() for p in args.labels.split(",") if p.strip())
    fetched = issues(cfg, state=args.state, labels=labels, timeout=args.timeout)
    if not fetched.ok:
        print(f"backlog: unknown — {fetched.error}", file=sys.stderr)
        return 1
    for one in fetched.issues:
        print(issue_line(one))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
