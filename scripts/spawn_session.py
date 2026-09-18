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

ENV = "CHIEF_OF_STUFF_LAUNCHER"
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
]
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


def argv(template: list[str], *, agent_type: str, cwd: str, title: str) -> list[str]:
    """Substitute per whole token. A value never becomes more argv entries than the token it fills."""
    values = {"cwd": cwd, "title": title, "type": agent_type}
    out = []
    for token in template:
        unknown = [name for name in PLACEHOLDER.findall(token) if name not in values]
        if unknown:
            raise RefusedError(f"launcher token {token!r} uses {unknown[0]!r}; known fields are {', '.join(FIELDS)}")
        out.append(PLACEHOLDER.sub(lambda m: values[m.group(1)], token))
    return out


def main(argv_in: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--type", dest="agent_type", required=True, help="the agent type the session runs as")
    ap.add_argument("--cwd", required=True, help="the worktree the session starts in")
    ap.add_argument("--title", required=True, help="the terminal tab's title, so a human can find it")
    ap.add_argument("--dry-run", action="store_true", help="print the argv and start nothing")
    args = ap.parse_args(argv_in)

    try:
        command = argv(launcher(), agent_type=args.agent_type, cwd=args.cwd, title=args.title)
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        # Quoted per token: the printed line is read by a human and may be pasted into a shell,
        # where a title carrying a space or a semicolon would stop being one argument.
        print("would run: " + " ".join(shlex.quote(t) for t in command))
        return 0
    if not Path(args.cwd).is_dir():
        print(f"refused: no directory at {args.cwd}; make the worktree first", file=sys.stderr)
        return 1
    try:
        proc = subprocess.Popen(command, cwd=args.cwd, start_new_session=True)
    except (OSError, ValueError) as exc:
        print(f"refused: could not start {command[0]}: {exc}", file=sys.stderr)
        return 1
    print(f"started {args.agent_type} in {args.cwd} as {args.title} (pid {proc.pid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
