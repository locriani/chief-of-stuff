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
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_prompt  # noqa: E402

ENV = "CHIEF_OF_STUFF_LAUNCHER"
# The assignment lives in the tree, not on the command line. `.chief-of-stuff/` ignores itself, so a
# dispatch never reads as uncommitted work in the tree it was written into. The directory is defined
# beside the assignment it carries, because the stop that travels back out of the tree shares it.
PROMPT_DIR = dispatch_prompt.PROMPT_DIR
PROMPT_FILE = f"{PROMPT_DIR}/dispatch.md"
# The same string on every launch. There is no per-dispatch text in the argv, so there is nothing
# for a shell to expand on the way here.
# No punctuation the metacharacter guard rejects: the bootstrap is a launcher token like any other,
# and exempting it would be exempting the one token that reaches every session.
BOOTSTRAP = f"Read {PROMPT_FILE} in this directory and follow it. It is your assignment."
# What the tab runs. `--permission-mode plan` is the plan-first gate, enforced at launch rather than
# asked for in the assignment text: a dispatched session cannot write before it has shown its plan.
CLAUDE_ARGV = ["claude", "--agent", "{type}", "--permission-mode", "plan", BOOTSTRAP]
# The override the eval harness sets, and the only path that still builds an argv. Nothing else uses
# it: the real launcher asks Ghostty for a tab.
DEFAULT_LAUNCHER = ["ghostty", "--working-directory={cwd}", "--title={title}", "-e", *CLAUDE_ARGV]
# Ghostty's scripting dictionary, rather than keystrokes into the front window. `new tab` takes a
# `surface configuration` record with an initial working directory and a command, so the tab is asked
# for by name. Two things it does not take: a title (the tab is named by the process, which in a
# worktree is the tree) and, deliberately, an environment.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
AS_ESCAPE = str.maketrans({'"': '\\"', "\\": "\\\\"})
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
OSASCRIPT = "/usr/bin/osascript"
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


def _as_string(value: str) -> str:
    """One AppleScript string literal. The outer of the two quoting layers this launcher now has.

    A control character is refused rather than escaped: there is no reading of an escape sequence in a
    path or an agent type that is a path or an agent type.
    """
    if CONTROL.search(value):
        raise RefusedError(f"{value!r} carries a control character; nothing was started")
    return '"' + value.translate(AS_ESCAPE) + '"'


def ghostty_script(*, cwd: str, agent_type: str | None, claude: Path | None, title: str | None = None) -> str:
    """Ask Ghostty for a tab running `claude` in `cwd`. The inner layer is Ghostty's own shell-style parse.

    `claude` is an absolute path, resolved by the caller. Ghostty is launched from the GUI, so its
    children inherit launchd's environment and not a login shell's: the first real tab died in 38ms
    with `exec: claude: not found` while `which claude` answered `/opt/homebrew/bin/claude`. Upstream
    hits the same thing with tmux, and the answer there is the same — name the binary absolutely.

    No environment is passed either way. The tab inherits Ghostty's, which is what a terminal the user
    opened themselves would have and never a coordinator's; that is what the `KEEP` whitelist was
    approximating, and asking for a tab gets it exactly.
    """
    if claude is None:
        raise RefusedError("cannot find `claude` on PATH; a GUI-launched terminal cannot look it up either")
    template = CLAUDE_ARGV if agent_type else _without_agent(CLAUDE_ARGV)
    tokens = [str(claude)] + [t.replace("{type}", agent_type or "") for t in template[1:]]
    command = " ".join(shlex.quote(tok) for tok in tokens)
    # `set_tab_title:` is how a tab gets a name — a surface configuration has no title field, and
    # without this the tab is called after whatever the process last wrote.
    named = (f'  perform action {_as_string("set_tab_title:" + title)} on focused terminal of tb\n'
             if title else "")
    return (
        'tell application "Ghostty"\n'
        "  activate\n"
        "  set cfg to new surface configuration\n"
        f"  set initial working directory of cfg to {_as_string(cwd)}\n"
        f"  set command of cfg to {_as_string(command)}\n"
        # Ghostty calls any sub-second exit a launch failure and closes the tab on it. A session that
        # dies on startup should leave its reason on screen, which is how the PATH fault above was read.
        "  set wait after command of cfg to true\n"
        "  if (count of windows) is 0 then\n"
        "    set tb to selected tab of (new window with configuration cfg)\n"
        "  else\n"
        # `in front window`, or the tab lands in whichever window Ghostty happens to return first —
        # the first real tab opened in a window the user was not looking at.
        "    set tb to new tab in front window with configuration cfg\n"
        "  end if\n"
        + named +
        "end tell\n"
    )


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
    ap.add_argument("--coordinator", help="your own session name as a listing shows it, so the session knows who to register with")
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--dry-run", action="store_true", help="print the argv and start nothing")
    args = ap.parse_args(argv_in)

    override = os.environ.get(ENV)
    try:
        body = dispatch_prompt.compose(Path(args.root), args.date, args.lane,
                                      worktree=Path(args.cwd).resolve(), coordinator=args.coordinator)
        # The override is the eval harness's recorder and the only path that still builds an argv.
        command = (argv(launcher(), agent_type=args.agent_type, cwd=args.cwd, title=args.title)
                   if override else [OSASCRIPT, "-"])
        script = None if override else ghostty_script(
            cwd=args.cwd, agent_type=args.agent_type, title=args.title,
            claude=Path(p) if (p := shutil.which("claude")) else None)
    except (RefusedError, dispatch_prompt.RefusedError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        # Quoted per token: the printed line is read by a human and may be pasted into a shell,
        # where a title carrying a space or a semicolon would stop being one argument.
        print("would run: " + " ".join(shlex.quote(t) for t in command))
        if script:
            print("would ask Ghostty for a tab:")
            print(script.rstrip())
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
        if script is None:
            proc = subprocess.Popen(command, cwd=args.cwd, env=launch_env(), start_new_session=True)
            where = f"pid {proc.pid}"
        else:
            # The script goes in on stdin, never as `osascript -e` arguments: a value that reached an
            # argument would be one quoting layer closer to being read as AppleScript.
            out = subprocess.run(command, input=script, text=True, capture_output=True,
                                 timeout=30, env=launch_env())
            if out.returncode != 0:
                print(f"refused: Ghostty would not open a tab: {out.stderr.strip()}", file=sys.stderr)
                return 1
            where = "a new Ghostty tab"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"refused: could not start {command[0]}: {exc}", file=sys.stderr)
        return 1
    print(f"started {args.agent_type or 'the default agent'} in {args.cwd} as {args.title} "
          f"({where}), assignment in {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
