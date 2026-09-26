#!/usr/bin/env python3
"""Block Claude tool calls that open the configured board in a browser."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit

from render_board import ConfigError, parse_coordinator

REASON = "Serve and render the board only. Never open it automatically; the user decides when to view it."
URL = re.compile(r"(?:https?://|file://)[^\s\"'<>`]+", re.I)
OPENER = re.compile(
    r"(?:^|[\s;/])(open|xdg-open|sensible-browser|start|chrome|google-chrome|chromium|firefox|safari)(?=[\s\"']|$)"
    r"|webbrowser|open\s+location|playwright|puppeteer|selenium|createBrowserTab|open_url"
    r"|\.goto\s*\(|\.navigate\s*\(", re.I)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def origin(url: str):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        host = "loopback"
    return parsed.scheme.lower(), host, parsed.port or (443 if parsed.scheme == "https" else 80)


def board(cwd: Path):
    configured = os.environ.get("CHIEF_OF_STUFF_WORKSPACE")
    roots = ([Path(configured)] if configured else []) + [cwd, *cwd.parents]
    for root in dict.fromkeys(roots):
        try:
            cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())
        except (OSError, ValueError, ConfigError, KeyError):
            continue
        if cfg.board_url:
            pages = root / cfg.pages_dir if cfg.pages_dir else (root / cfg.tracker_path(date.today().isoformat())).parent
            return cfg.board_url, pages.resolve()
    return None


def protected(text: str, url: str, pages: Path, cwd: Path) -> bool:
    for match in URL.finditer(text):
        candidate = match.group().rstrip("),;}")
        try:
            if candidate.lower().startswith("file://"):
                path = Path(unquote(urlsplit(candidate).path))
                if path.suffix == ".html" and path.resolve().is_relative_to(pages):
                    return True
            elif origin(candidate) == origin(url):
                return True
        except (ValueError, OSError):
            continue
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    for token in tokens:
        if token.endswith(".html"):
            path = (cwd / token).resolve()
            if path.is_relative_to(pages):
                return True
    return False


def denied(event: dict) -> bool:
    cwd = Path(event.get("cwd") or os.getcwd()).resolve()
    config = board(cwd)
    if not config:
        return False
    name = event.get("tool_name", "").lower()
    values = list(strings(event.get("tool_input", {})))
    shell = name in {"bash", "powershell"} or any(word in name for word in ("cua", "computer"))
    browser = any(word in name for word in (
        "browser", "chrome", "playwright", "puppeteer", "navigate", "open_url", "open_tab", "preview",
    )) or ("artifact" in name and "open" in name)
    if not browser and not shell:
        return False
    url, pages = config
    return any(protected(value, url, pages, cwd) and (browser or OPENER.search(value)) for value in values)


def main() -> int:
    try:
        event = json.load(sys.stdin)
        blocked = isinstance(event, dict) and denied(event)
    except (ValueError, TypeError, OSError):
        return 0
    if blocked:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": REASON,
        }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
