#!/usr/bin/env python3
"""Open a terminal running `claude` in a worktree, and print what was started.

    python3 spawn_session.py --type implementer --cwd <worktree> --title wt-docs

The launcher is here, in the plugin, and nowhere else. It is deliberately not read from the
workspace `CLAUDE.md`: a working session may edit that file freely — the coordinator's limits bind
the coordinator and nobody else — so a launch template living there would be arbitrary argv run on
the user's machine, with a config file as the carrier. The eval harness overrides it through
`CHIEF_OF_STUFF_LAUNCHER`, which is set by the runner and not by anything the agent can write.

Substitution is per whole token and the argv is never re-split, so a path or a title containing a
space, a quote or a leading dash occupies exactly the one argv entry it was given.

This file starts sessions. It never ends one, never removes a worktree and never deletes a branch:
a tree can hold hours of gitignored state that `git status` does not show.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_prompt  # noqa: E402

ENV = "CHIEF_OF_STUFF_LAUNCHER"
# The assignment lives in the tree, not on the command line. `.chief-of-stuff/` ignores itself, so a
# dispatch never reads as uncommitted work in the tree it was written into.
PROMPT_DIR = ".chief-of-stuff"
PROMPT_FILE = f"{PROMPT_DIR}/dispatch.md"
# The same string on every launch. There is no per-dispatch text in the argv, so there is nothing
# for a shell to expand on the way here.
# No punctuation the metacharacter guard rejects: the bootstrap is a launcher token like any other,
# and exempting it would be exempting the one token that reaches every session.
BOOTSTRAP = f"Read {PROMPT_FILE} in this directory and follow it. It is your assignment."
# `--permission-mode plan` is the plan-first gate, enforced at launch rather than asked for in the
# assignment text: a dispatched session cannot write before it has shown its plan.
DEFAULT_LAUNCHER = [
    "ghostty",
    "--working-directory={cwd}",
    "--title={title}",
    "-e",
    "claude",
    "--agent",
    "{type}",
    "--permission-mode",
    "plan",
    BOOTSTRAP,
]
# What a terminal and an editor actually need. Everything else is dropped, and the CLAUDE_* markers
# most of all: the coordinator is itself a Claude Code session, so passing its environment on gave a
# spawned session the coordinator's messaging socket and token, bound it to the coordinator's
# project instead of its own worktree, and turned its transcript off. A dispatched session that
# leaves no transcript is one whose account of its work dies when the tab closes.
# `SSH_AUTH_SOCK` stays deliberately. The dispatch says "do not commit or push", but that is a rule
# and not a missing capability: a session under the user's own rules may commit and push its lane
# when the user has said so, and a limit that binds the coordinator binds nobody else. A dispatched
# session gets what a terminal the user opened would have, minus the coordinator's identity.
KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TERM_PROGRAM", "TMPDIR",
        "LANG", "LC_ALL", "LC_CTYPE", "COLORTERM", "TZ", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "env", "eval", "exec", "xargs"}
METACHARACTERS = re.compile(r"[;&|`$<>\n]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
FIELDS = ("cwd", "title", "type")


class RefusedError(ValueError):
    """The launcher or one of its values did not survive its check. Nothing was started."""


def launcher(template: list[str] | None = None) -> list[str]:
    """The argv template: the environment override when set, otherwise the plugin's own."""
    if template is None:
        raw = os.environ.get(ENV)
        if raw:
            try:
                template = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RefusedError(f"{ENV} must be a JSON array of argv tokens: {exc}") from None
            if not isinstance(template, list) or not all(isinstance(t, str) for t in template):
                raise RefusedError(f"{ENV} must be a JSON array of strings")
        else:
            template = list(DEFAULT_LAUNCHER)
    if not template:
        raise RefusedError("launcher is empty: there is nothing to run")
    argv0 = Path(template[0]).name
    if argv0 in SHELLS:
        raise RefusedError(f"launcher runs {argv0!r}, which would interpret its arguments; give the program directly")
    for token in template:
        if METACHARACTERS.search(token):
            raise RefusedError(f"launcher token {token!r} carries a shell metacharacter")
    return template


def _without_agent(template: list[str]) -> list[str]:
    """Drop `--agent {type}` as a pair, so the session gets the default agent and skills.

    Removal rather than an empty substitution: a token that expands to zero or two argv entries is
    the one thing per-token substitution exists to prevent.
    """
    out, skip = [], False
    for i, token in enumerate(template):
        if skip:
            skip = False
            continue
        if "{type}" in token:
            continue
        if token == "--agent" and i + 1 < len(template) and "{type}" in template[i + 1]:
            skip = True
            continue
        out.append(token)
    return out


def argv(template: list[str], *, agent_type: str | None, cwd: str, title: str) -> list[str]:
    """Substitute per whole token. A value never becomes more argv entries than the token it fills."""
    if agent_type is None:
        template = _without_agent(template)
    values = {"cwd": cwd, "title": title, "type": agent_type or ""}
    out = []
    for token in template:
        unknown = [name for name in PLACEHOLDER.findall(token) if name not in values]
        if unknown:
            raise RefusedError(f"launcher token {token!r} uses {unknown[0]!r}; known fields are {', '.join(FIELDS)}")
        out.append(PLACEHOLDER.sub(lambda m: values[m.group(1)], token))
    return out


def launch_env(parent: dict[str, str] | None = None) -> dict[str, str]:
    """The environment the new session starts in: a whitelist, never the coordinator's own.

    The same discipline `audit_lanes.git()` applies to a read-only git call, applied to the one call
    that starts a session — and it matters more here, because what leaks is an identity rather than
    a config.
    """
    source = os.environ if parent is None else parent
    return {k: v for k, v in source.items() if k in KEEP and not k.upper().startswith("CLAUDE")}


def write_dispatch(cwd: Path, body: str) -> Path:
    """The assignment as a file, created not overwritten, beside a gitignore that hides them both."""
    path = cwd / PROMPT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / ".gitignore").write_text("*\n")
    try:
        with path.open("x") as fh:
            fh.write(body if body.endswith("\n") else body + "\n")
    except FileExistsError:
        raise RefusedError(f"{path} already holds a dispatch; a dispatch gets a tree of its own") from None
    except OSError as exc:
        raise RefusedError(f"could not write {path}: {exc}") from None
    return path


def main(argv_in: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--type", dest="agent_type", help="an agent type from the Coordinator block; omitted runs the default agent")
    ap.add_argument("--cwd", required=True, help="the worktree the session starts in")
    ap.add_argument("--title", required=True, help="the terminal tab's title, so a human can find it")
    ap.add_argument("--lane", required=True, help="the Lanes row this dispatch owns; the assignment is read back from it")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--dry-run", action="store_true", help="print the argv and start nothing")
    args = ap.parse_args(argv_in)

    try:
        body = dispatch_prompt.compose(Path(args.root), args.date, args.lane, worktree=Path(args.cwd).resolve())
        command = argv(launcher(), agent_type=args.agent_type, cwd=args.cwd, title=args.title)
    except (RefusedError, dispatch_prompt.RefusedError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        # Quoted per token: the printed line is read by a human and may be pasted into a shell,
        # where a title carrying a space or a semicolon would stop being one argument.
        print("would run: " + " ".join(shlex.quote(t) for t in command))
        print(f"would write: {Path(args.cwd) / PROMPT_FILE}")
        return 0
    if not Path(args.cwd).is_dir():
        print(f"refused: no directory at {args.cwd}; make the worktree first", file=sys.stderr)
        return 1
    try:
        written = write_dispatch(Path(args.cwd), body)
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    try:
        proc = subprocess.Popen(command, cwd=args.cwd, env=launch_env(), start_new_session=True)
    except (OSError, ValueError) as exc:
        print(f"refused: could not start {command[0]}: {exc}", file=sys.stderr)
        return 1
    print(f"started {args.agent_type or 'the default agent'} in {args.cwd} as {args.title} "
          f"(pid {proc.pid}), assignment in {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
