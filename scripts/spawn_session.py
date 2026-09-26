#!/usr/bin/env python3
"""Start a worker in a worktree using fixed argv templates.

Launch templates live here because workers may edit workspace configuration. The eval harness
may override them with `CHIEF_OF_STUFF_LAUNCHER`. Token substitution never re-splits argv.
This module does not stop sessions or delete worktrees or branches.
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
from settings import SettingsError, load as load_settings  # noqa: E402
from shell_setup import ShellError, resolve  # noqa: E402

ENV = "CHIEF_OF_STUFF_LAUNCHER"
# Keep assignments and stop files in the worktree's ignored metadata directory.
PROMPT_DIR = dispatch_prompt.PROMPT_DIR
PROMPT_FILE = f"{PROMPT_DIR}/dispatch.md"
# Use absolute paths: Ghostty can ignore its configured working directory.
BOOTSTRAP = ("Read the file {dispatch} — that is your assignment, in full, and read it before you do "
             "anything else. Your working directory is {cwd}. Start there and stay there: every path "
             "the assignment names is inside it.")
# Pin cwd in argv because the terminal may ignore its configured directory.
ENV_BIN = "/usr/bin/env"
# Enforce planning at launch and pass the assigned session name explicitly.
CLAUDE_ARGV = ["claude", "--agent", "{type}", "--name", "{title}", "--model", "{model}", "--effort", "{effort}",
               "--permission-mode", "plan", BOOTSTRAP]
# Antigravity gets its name from the tab and assignment and reads workspace rules explicitly.
AGY_BOOTSTRAP = BOOTSTRAP + " Then read {root}/CLAUDE.md: its house rules bind you."
AGY_ARGV = ["agy", "--model", "{model}", "--mode", "plan", "-i", AGY_BOOTSTRAP]
CODEX_ARGV = ["codex", "-C", "{cwd}", "--sandbox", "read-only", "--model", "{model}", AGY_BOOTSTRAP]
CURSOR_ARGV = ["agent", "--workspace", "{cwd}", "--mode", "plan", "--model", "{model}", AGY_BOOTSTRAP]
TEMPLATES = {"claude": CLAUDE_ARGV, "agy": AGY_ARGV, "codex": CODEX_ARGV, "cursor": CURSOR_ARGV}
BINARY = {"claude": "claude", "agy": "agy", "codex": "codex", "cursor": "agent"}
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# The eval harness may override this template; normal launches use Ghostty tabs.
DEFAULT_LAUNCHER = ["ghostty", "--working-directory={cwd}", "--title={title}", "-e", *CLAUDE_ARGV]
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
AS_ESCAPE = str.maketrans({'"': '\\"', "\\": "\\\\"})
# Keep shell essentials and SSH auth, but exclude the coordinator's CLAUDE_* identity.
KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TERM_PROGRAM", "TMPDIR",
        "LANG", "LC_ALL", "LC_CTYPE", "COLORTERM", "TZ", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME",
        "CHIEF_OF_STUFF_RELEASE", "CHIEF_OF_STUFF_WORKSPACE")
OSASCRIPT = "/usr/bin/osascript"
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "env", "eval", "exec", "xargs"}
METACHARACTERS = re.compile(r"[;&|`$<>\n]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
FIELDS = ("cwd", "title", "type", "dispatch", "model", "effort", "root")


class RefusedError(ValueError):
    """Invalid launch configuration; no session started."""


def launcher(template: list[str] | None = None) -> list[str]:
    """Return the configured argv template."""
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


def _without(template: list[str], field: str) -> list[str]:
    """Remove an unset field and its flag without changing argv token boundaries."""
    mark = "{" + field + "}"
    out, skip = [], False
    for i, token in enumerate(template):
        if skip:
            skip = False
            continue
        if mark in token:
            continue
        if token.startswith("--") and i + 1 < len(template) and mark in template[i + 1]:
            skip = True
            continue
        out.append(token)
    return out


def _paths(cwd: str) -> tuple[str, str]:
    """Return absolute cwd and dispatch paths, preserving symlink spelling."""
    root = Path(os.path.abspath(cwd))
    return str(root), str(root / PROMPT_FILE)


def _unset(template: list[str], values: dict[str, str]) -> list[str]:
    """Drop each optional flag whose value is empty, as a pair (see `_without`)."""
    for field in ("type", "title", "model", "effort"):
        if not values.get(field):
            template = _without(template, field)
    return template


def argv(template: list[str], *, agent_type: str | None, cwd: str, title: str, model: str = "", effort: str = "",
         workspace: str = ".") -> list[str]:
    """Substitute per whole token. A value never becomes more argv entries than the token it fills."""
    root, dispatch = _paths(cwd)
    values = {"cwd": root, "title": title, "type": agent_type or "", "dispatch": dispatch,
              "model": model, "effort": effort, "root": os.path.abspath(workspace)}
    template = _unset(template, values)
    out = []
    for token in template:
        unknown = [name for name in PLACEHOLDER.findall(token) if name not in values]
        if unknown:
            raise RefusedError(f"launcher token {token!r} uses {unknown[0]!r}; known fields are {', '.join(FIELDS)}")
        out.append(PLACEHOLDER.sub(lambda m: values[m.group(1)], token))
    return out


def _as_string(value: str) -> str:
    """Quote one AppleScript string and reject control characters."""
    if CONTROL.search(value):
        raise RefusedError(f"{value!r} carries a control character; nothing was started")
    return '"' + value.translate(AS_ESCAPE) + '"'


def runtime_tokens(*, cwd: str, agent_type: str | None, binary: Path | None, title: str | None = None,
                   runtime: str = "claude", model: str = "", effort: str = "", workspace: str = ".") -> list[str]:
    """Exact worker argv shared by terminal launchers."""
    if binary is None:
        raise RefusedError(f"cannot find `{runtime}` on PATH")
    root, dispatch = _paths(cwd)
    values = {"cwd": root, "title": title or "", "type": agent_type or "", "dispatch": dispatch,
              "model": model, "effort": effort, "root": os.path.abspath(workspace)}
    template = _unset(TEMPLATES[runtime], values)
    rest = [PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), t) for t in template[1:]]
    tokens = [ENV_BIN, "-C", root, f"PATH={os.environ.get('PATH', '')}",
              f"CHIEF_OF_STUFF_WORKSPACE={os.path.abspath(workspace)}"]
    if runtime == "claude":
        rest = ["--plugin-dir", str(Path(__file__).resolve().parent.parent), *rest]
        tokens += [sys.executable, str(Path(__file__).resolve().parent / "session_exec.py"),
                   "exec-login", "--"]
    else:
        tokens += [sys.executable, str(Path(__file__).resolve().parent / "session_exec.py"),
                   "--registry", str(Path(os.path.abspath(workspace)) / PROMPT_DIR / "sessions" / f"{title}.json"),
                   "--runtime", runtime, "--name", title or "", "--worktree", root,
                   "--login-shell", "--"]
    return tokens + [str(binary)] + rest


def tmux_command(*, tmux: Path | None, **worker) -> list[str]:
    """Open a new tmux window in an existing tmux session, with argv executed directly."""
    if tmux is None:
        raise RefusedError("tmux launcher is configured but tmux is unavailable on PATH")
    tokens = runtime_tokens(**worker)
    return [str(tmux), "new-window", "-d", "-c", os.path.abspath(worker["cwd"]),
            "-n", worker.get("title") or worker["runtime"], *tokens]


def ghostty_script(*, cwd: str, agent_type: str | None, claude: Path | None, title: str | None = None,
                   runtime: str = "claude", model: str = "", effort: str = "", workspace: str = ".") -> str:
    """Build a Ghostty tab script with an absolute binary and the launching shell's PATH."""
    tokens = runtime_tokens(cwd=cwd, agent_type=agent_type, binary=claude, title=title,
                            runtime=runtime, model=model, effort=effort, workspace=workspace)
    command = " ".join(shlex.quote(tok) for tok in tokens)
    # Surface configuration has no title field.
    named = (f'  perform action {_as_string("set_tab_title:" + title)} on focused terminal of tb\n'
             if title else "")
    return (
        'tell application "Ghostty"\n'
        "  activate\n"
        "  set cfg to new surface configuration\n"
        f"  set initial working directory of cfg to {_as_string(cwd)}\n"
        f"  set command of cfg to {_as_string(command)}\n"
        # Keep startup failures visible.
        "  set wait after command of cfg to true\n"
        "  if (count of windows) is 0 then\n"
        "    set tb to selected tab of (new window with configuration cfg)\n"
        "  else\n"
        # Target the front window explicitly.
        "    set tb to new tab in front window with configuration cfg\n"
        "  end if\n"
        + named +
        "end tell\n"
    )


def launch_env(parent: dict[str, str] | None = None) -> dict[str, str]:
    """Keep only approved environment variables; exclude coordinator identity."""
    source = os.environ if parent is None else parent
    return {k: v for k, v in source.items() if k in KEEP and not k.upper().startswith("CLAUDE")}


def write_dispatch(cwd: Path, body: str) -> Path:
    """Create the ignored assignment file without overwriting an earlier dispatch."""
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
    ap.add_argument("--name", "--title", dest="title", required=True,
                    help="the session's name, e.g. impl07; the listing, the prompt box and the tab all show it")
    ap.add_argument("--task", required=True, help="the Tasks row this dispatch owns; the assignment is read back from it")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--coordinator", help="your own session name as a listing shows it, so the session knows who to register with")
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--runtime", choices=sorted(TEMPLATES), default="claude",
                    help="claude (default), agy, codex, or cursor")
    ap.add_argument("--launcher", choices=("ghostty", "tmux"),
                    help="terminal launcher; default: [workers] launcher in workspace settings, then ghostty")
    ap.add_argument("--model", default="", help="model id; required for agy")
    ap.add_argument("--effort", default="", choices=("", "low", "medium", "high", "xhigh", "max"),
                    help="claude only; agy's effort is in its model id")
    ap.add_argument("--dry-run", action="store_true", help="print the argv and start nothing")
    ap.add_argument("--one-shot", action="store_true",
                    help="run this task once; [workers] mode = one-shot already requires this for every task")
    ap.add_argument("--timeout-minutes", type=int, default=60,
                    help="one-shot run limit before human review (default: 60)")
    args = ap.parse_args(argv_in)
    if (args.model or args.runtime == "agy") and not MODEL.fullmatch(args.model):
        print(f"refused: --model needs a model id (agy: one from `agy models`, and it is required), not {args.model!r}", file=sys.stderr)
        return 1
    if args.runtime != "claude" and args.effort:
        print("refused: --effort is claude only", file=sys.stderr)
        return 1
    # Compose and launch must use the same absolute cwd.
    args.cwd = os.path.abspath(args.cwd)

    try:
        config = dispatch_prompt._config(Path(args.root))
        worker_settings = load_settings(Path(args.root), config.settings_path).workers
    except (dispatch_prompt.RefusedError, SettingsError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    one_shot = worker_settings.mode == "one-shot" or args.one_shot

    if one_shot:
        if args.timeout_minutes < 1:
            print("refused: --timeout-minutes must be positive", file=sys.stderr)
            return 1
        if args.launcher:
            print("refused: --launcher applies to interactive sessions, not --one-shot", file=sys.stderr)
            return 1
        try:
            import one_shot
            return one_shot.run(root=Path(args.root).resolve(), day=args.date, task=args.task,
                                cwd=Path(args.cwd), name=args.title, runtime=args.runtime,
                                agent_type=args.agent_type, model=args.model, effort=args.effort,
                                dry_run=args.dry_run, timeout_minutes=args.timeout_minutes)
        except (ValueError, OSError, dispatch_prompt.RefusedError, ShellError, SettingsError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1

    override = os.environ.get(ENV)
    try:
        body = dispatch_prompt.compose(Path(args.root), args.date, args.task,
                                      worktree=Path(args.cwd), coordinator=args.coordinator, name=args.title,
                                      runtime=args.runtime)
        selected = args.launcher or worker_settings.launcher
        # The eval harness uses the argv override.
        worker = dict(cwd=args.cwd, agent_type=args.agent_type, title=args.title, runtime=args.runtime,
                      model=args.model, effort=args.effort, workspace=args.root)
        if override:
            command = argv(launcher(), agent_type=args.agent_type, cwd=args.cwd, title=args.title,
                           model=args.model, effort=args.effort, workspace=args.root)
            script = None
        elif selected == "tmux":
            command = tmux_command(tmux=Path(p) if (p := resolve("tmux")) else None,
                                   binary=Path(p) if (p := resolve(BINARY[args.runtime])) else None,
                                   **worker)
            script = None
        else:
            command = [OSASCRIPT, "-"]
            script = ghostty_script(claude=Path(p) if (p := resolve(BINARY[args.runtime])) else None,
                                   **worker)
    except (RefusedError, dispatch_prompt.RefusedError, SettingsError, ShellError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        # Keep each argv token intact if the preview is copied into a shell.
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
        if override:
            proc = subprocess.Popen(command, cwd=args.cwd, env=launch_env(), start_new_session=True)
            where = f"pid {proc.pid}"
        elif selected == "tmux":
            out = subprocess.run(command, text=True, capture_output=True, timeout=30, env=launch_env())
            if out.returncode != 0:
                print(f"refused: tmux would not open a window: {out.stderr.strip()}", file=sys.stderr)
                return 1
            where = "a new tmux window"
        else:
            # Pass AppleScript on stdin to avoid another quoting layer.
            out = subprocess.run(command, input=script, text=True, capture_output=True,
                                 timeout=30, env=launch_env())
            if out.returncode != 0:
                print(f"refused: Ghostty would not open a tab: {out.stderr.strip()}", file=sys.stderr)
                return 1
            where = "a new Ghostty tab"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"refused: could not start {command[0]}: {exc}", file=sys.stderr)
        return 1
    print(f"started {args.agent_type or 'the default agent'}{f' on agy {args.model}' if args.runtime == 'agy' else ''} in {args.cwd} as {args.title} "
          f"({where}), assignment in {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
