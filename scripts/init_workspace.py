#!/usr/bin/env python3
"""Add a minimal chief-of-stuff Coordinator block to a workspace."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backlog import BacklogError, parse_backlog
from install_model_guidance import destination as guidance_destination, install as install_guidance
from render_board import ConfigError, parse_coordinator
from settings import SettingsError, load as load_settings

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COORDINATOR = re.compile(r"(?m)^## Coordinator\s*$")


class InitError(ValueError):
    pass


def relative_dir(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or not value or any(part in ("", ".", "..") for part in path.parts):
        raise InitError(f"directory {value!r} must be relative and stay inside the workspace")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise InitError(f"directory {value!r} contains unsupported characters")
    return value.rstrip("/") + "/"


def coordinator_block(*, user: str, timezone: str, daily_dir: str, worktrees: str,
                      agents: list[str], board_port: int | None, backlog: str | None) -> str:
    if not user.strip() or any(ord(c) < 32 or c in "`|" for c in user):
        raise InitError("--user must be one printable line without backticks or pipes")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise InitError(f"unknown timezone {timezone!r}") from None
    daily_dir, worktrees = relative_dir(daily_dir), relative_dir(worktrees)
    if board_port is not None and not 1 <= board_port <= 65535:
        raise InitError("--board-port must be between 1 and 65535")
    if backlog:
        try:
            parse_backlog(backlog)
        except BacklogError as exc:
            raise InitError(str(exc)) from None
    lines = ["## Coordinator", "", f"- User: {user.strip()}", f"- Daily log dir: `{daily_dir}`",
             f"- Tracker: `{daily_dir}<date>-tracker.md`", f"- Timezone: {timezone}",
             "- Calendars: none", "- Deadlines: none", f"- Worktrees: `{worktrees}`",
             "- Settings: `chief-of-stuff.toml`"]
    if board_port is not None:
        lines.append(f"- Board: Pages; URL http://127.0.0.1:{board_port}/; dir `pages/`")
    if backlog:
        lines.append(f"- Backlog: {backlog}")
    for agent in agents:
        name, sep, lifetime = agent.partition(":")
        if not sep or not NAME.fullmatch(name) or lifetime not in ("task", "standing"):
            raise InitError(f"--agent {agent!r} must be NAME:task or NAME:standing")
        lines.append(f"- Agent: {name} {lifetime}")
    block = "\n".join(lines) + "\n"
    try:
        parse_coordinator(block, date.today())
    except ConfigError as exc:
        raise InitError(f"generated Coordinator block is invalid: {exc}") from None
    return block


def settings_text(launcher: str, mode: str = "interactive") -> str:
    text = f'[notify]\nadapter = "off"\n\n[workers]\nlauncher = "{launcher}"\nmode = "{mode}"\n'
    # #111: uncomment to pick models from the TOML; without [models], chief-of-stuff-models.md applies.
    text += ('\n# [models.deep]\n# rotation = ["claude:opus@high", "codex:<model-id>@high"]\n'
             '# [models.implement]\n# rotation = ["claude:sonnet@medium", "codex:<model-id>@medium"]\n'
             '# [models.fast]\n# rotation = ["claude:haiku@low"]\n'
             '# [models.review]\n# rotation = ["codex:<model-id>@high", "claude:opus@high"]\n')
    tomllib.loads(text)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="existing workspace directory")
    ap.add_argument("--user", required=True, help="name to show as the workspace user")
    ap.add_argument("--timezone", required=True, help="IANA timezone, e.g. America/Chicago")
    ap.add_argument("--daily-dir", default="daily/")
    ap.add_argument("--worktrees", default="worktrees/")
    ap.add_argument("--agent", action="append", default=[], metavar="NAME:task|standing",
                    help="worker type and lifetime; repeat for multiple types")
    ap.add_argument("--launcher", choices=("ghostty", "tmux"), default="ghostty")
    ap.add_argument("--worker-mode", choices=("interactive", "one-shot"), default="interactive",
                    help="default dispatch mode written to [workers] in the workspace TOML")
    ap.add_argument("--board-port", type=int, help="configure local board serving on this fixed port")
    backlog = ap.add_mutually_exclusive_group()
    backlog.add_argument("--github-repo", help="GitHub owner/repository")
    backlog.add_argument("--gitlab-host", help="GitLab https URL; also pass --gitlab-project")
    ap.add_argument("--gitlab-project", help="GitLab group/repository")
    ap.add_argument("--gitlab-token-env", default="CHIEF_OF_STUFF_GITLAB_TOKEN")
    ap.add_argument("--dry-run", action="store_true", help="print the new block and settings without writing")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    try:
        if not root.is_dir():
            raise InitError(f"workspace {root} is not a directory")
        if args.gitlab_host and not args.gitlab_project:
            raise InitError("--gitlab-host requires --gitlab-project")
        if args.gitlab_project and not args.gitlab_host:
            raise InitError("--gitlab-project requires --gitlab-host")
        if not ENV_NAME.fullmatch(args.gitlab_token_env):
            raise InitError("--gitlab-token-env must be an environment variable name")
        backlog_spec = (f"GitHub issues; repo {args.github_repo}" if args.github_repo else
                        f"GitLab; host {args.gitlab_host}; project {args.gitlab_project}; token env {args.gitlab_token_env}"
                        if args.gitlab_host else None)
        block = coordinator_block(user=args.user, timezone=args.timezone, daily_dir=args.daily_dir,
                                  worktrees=args.worktrees, agents=args.agent, board_port=args.board_port,
                                  backlog=backlog_spec)
        claude = root / "CLAUDE.md"
        existing = claude.read_text() if claude.exists() else ""
        if COORDINATOR.search(existing):
            raise InitError(f"{claude} already has a ## Coordinator block")
        settings = root / "chief-of-stuff.toml"
        guidance = guidance_destination(root)
        if settings.exists():
            if not settings.is_file():
                raise InitError(f"{settings} is not a settings file")
            try:
                load_settings(root, settings.name)
            except SettingsError as exc:
                raise InitError(f"existing settings are invalid: {exc}") from None
        new_settings = settings_text(args.launcher, args.worker_mode)
        if args.dry_run:
            print(f"would {'append to' if claude.exists() else 'create'}: {claude}\n{block}")
            print(f"would {'keep existing' if settings.exists() else 'create'}: {settings}")
            if not settings.exists():
                print(new_settings)
            print(f"would {'keep existing' if guidance.exists() else 'create'}: {guidance}")
            return 0
        if not settings.exists():
            with settings.open("x") as fh:
                fh.write(new_settings)
        if claude.exists():
            claude.write_text(existing.rstrip() + "\n\n" + block)
        else:
            with claude.open("x") as fh:
                fh.write("# Workspace\n\n" + block)
        install_guidance(root)
        print(f"configured {root}; settings in {settings}; model guidance in {guidance}")
        return 0
    except (InitError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
