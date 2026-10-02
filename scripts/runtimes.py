"""The agent CLIs a coordinator or worker runs in, one record each.

What a launch needs to know about a runtime without knowing which one it is. The argv each launcher builds
and the words each prompt uses stay with that launcher and that prompt. This module imports nothing from the
plugin, so `start_coordinator` can load it as `scripts.runtimes` and the scripts as `runtimes`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Runtime:
    name: str  # the `--runtime` value, a `[models]` entry's prefix, and a registration's `runtime`
    binary: str  # the executable its CLI installs
    display: str  # how a prompt names the host
    takes_effort: bool  # a launch passes `--effort`; elsewhere effort is part of the model id or absent
    needs_model: bool  # a worker launch refuses to start without `--model`
    schedules: bool  # the host can run a recurring in-session check; without one, checks happen each turn


RUNTIMES = (
    Runtime("claude", binary="claude", display="Claude Code", takes_effort=True, needs_model=False, schedules=True),
    Runtime("codex", binary="codex", display="Codex CLI", takes_effort=False, needs_model=False, schedules=False),
    Runtime("cursor", binary="agent", display="Cursor CLI", takes_effort=False, needs_model=False, schedules=True),
    Runtime("agy", binary="agy", display="Antigravity", takes_effort=False, needs_model=True, schedules=True),
)
NAMES = tuple(runtime.name for runtime in RUNTIMES)
_BY_NAME = {runtime.name: runtime for runtime in RUNTIMES}


def get(name: str) -> Runtime:
    """The record for `name`. An unknown name is a KeyError: callers validate before they launch."""
    return _BY_NAME[name]


def binary(name: str) -> str:
    """The executable for `name`. A session registration can name a runtime this release does not know;
    its name stands in for the executable, as the liveness checks always read it."""
    runtime = _BY_NAME.get(name)
    return runtime.binary if runtime else name
