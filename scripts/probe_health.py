#!/usr/bin/env python3
"""Probe the `Health:` targets in a workspace `## Coordinator` block and print one line each.

The coordinator runs this on open and on resume instead of remembering what was up an hour ago.
One invocation covers every target, so a restart costs one call rather than one per service.

    python3 probe_health.py --config /path/to/CLAUDE.md

Exit code is the number of targets that did not answer as expected, so a caller can branch on it
without parsing. Every target gets a line even when it fails: a missing line would read as a
healthy service.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HEALTH = re.compile(r"^\s*(?:[-*]\s*)?Health:\s*(.+?)\s*$", re.MULTILINE)
SCHEMES = ("http", "https")
TIMEOUT = 3.0


class TargetError(ValueError):
    """A `Health:` line that cannot be read as a target."""


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    expect: int = 200


@dataclass(frozen=True)
class Result:
    target: Target
    status: int | None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == self.target.expect


def _section(text: str, heading: str) -> list[str]:
    """The lines under `heading`, up to the next `## `. Empty when the heading is absent."""
    out: list[str] = []
    seen = False
    for line in text.splitlines():
        if line.strip() == heading:
            seen = True
            continue
        if seen and line.startswith("## "):
            break
        if seen:
            out.append(line)
    return out


def parse_target(spec: str) -> Target:
    """`<name> <url> [expected status]`. The url must be http or https: nothing else is a probe."""
    parts = spec.split()
    if len(parts) < 2:
        raise TargetError(f"health target needs a name and a url: {spec!r}")
    name, url = parts[0], parts[1]
    if urlsplit(url).scheme not in SCHEMES:
        raise TargetError(f"health target must be http or https: {url!r}")
    expect = 200
    if len(parts) > 2:
        try:
            expect = int(parts[2])
        except ValueError as exc:
            raise TargetError(f"expected status must be a number: {parts[2]!r}") from exc
    return Target(name, url, expect)


def targets_from_config(path: Path) -> list[Target]:
    """Every `Health:` line inside the `## Coordinator` block. No block, no targets, no error."""
    body = "\n".join(_section(path.read_text(), "## Coordinator"))
    return [parse_target(m.group(1)) for m in HEALTH.finditer(body)]


def _safe(url: str) -> str:
    """The url without query or credentials: a probe line is quoted into a tracker people read."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def probe(target: Target, timeout: float = TIMEOUT) -> Result:
    """One GET. Any failure is a Result, never an exception: one dead service is not a dead move."""
    request = urllib.request.Request(target.url, method="GET", headers={"User-Agent": "chief-of-stuff/probe"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (scheme checked in parse_target)
            return Result(target, response.status)
    except urllib.error.HTTPError as exc:
        return Result(target, exc.code)
    except Exception as exc:  # timeout, refused, DNS, TLS
        return Result(target, None, f"{type(exc).__name__}: {exc}")


def line(result: Result) -> str:
    where = _safe(result.target.url)
    if result.status is None:
        return f"{result.target.name} {where} unreachable ({result.detail})"
    if result.ok:
        return f"{result.target.name} {where} {result.status} ok"
    return f"{result.target.name} {where} {result.status}, expected {result.target.expect}"


def report(targets: list[Target], timeout: float = TIMEOUT) -> tuple[list[str], int]:
    results = [probe(t, timeout) for t in targets]
    return [line(r) for r in results], sum(1 for r in results if not r.ok)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the Health: targets in a ## Coordinator block.")
    parser.add_argument("--config", default="CLAUDE.md", help="workspace CLAUDE.md holding the block")
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds per target")
    args = parser.parse_args(argv)

    config = Path(args.config)
    if not config.is_file():
        print(f"no config at {config}", file=sys.stderr)
        return 2
    try:
        targets = targets_from_config(config)
    except TargetError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not targets:
        print("no Health: targets in the ## Coordinator block")
        return 0
    lines, bad = report(targets, args.timeout)
    for text in lines:
        print(text)
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
