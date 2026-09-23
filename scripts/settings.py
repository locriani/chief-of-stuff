#!/usr/bin/env python3
"""The workspace's chief-of-stuff settings: a TOML file the `## Coordinator` block names with `Settings:`.

Zach, 2026-09-22 22:20, on the day open and close times: "Configurable. We're going to need to pull
settings into a config file. Default to 6 and 22". Only `[notify]` lives here so far; the rest of the
block can move over later.

No `Settings:` line, no file, or no `[notify]` section is notify off, and nothing else changes. A value
that is there and cannot be read is refused: a guessed day-open time is a notification at the wrong hour.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

ADAPTERS = ("md-notify", "off")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
OFFSET = re.compile(r"^(\d+)([hm])$")
DEFAULT_WARNINGS = ("24h", "3h", "1h")


class SettingsError(ValueError):
    """A settings file that is there and cannot be read as written."""


def _offset(label: str) -> timedelta:
    m = OFFSET.match(label) if isinstance(label, str) else None
    if not m:
        raise SettingsError(f"[notify] deadline_warnings: {label!r} is not Nh or Nm")
    n = int(m.group(1))
    return timedelta(hours=n) if m.group(2) == "h" else timedelta(minutes=n)


@dataclass(frozen=True)
class Notify:
    # Interim: md-notify is to be replaced by Zach's Todo tooling (Zach, 2026-09-22 22:20).
    adapter: str = "off"
    queue: str = "Areas/notifications/NOTIFICATIONS.md"
    day_open: str = "06:00"
    day_close: str = "22:00"
    warnings: tuple[tuple[str, timedelta], ...] = tuple((w, _offset(w)) for w in DEFAULT_WARNINGS)

    def queue_path(self, root: Path) -> Path:
        return (Path(root) / self.queue).resolve()


@dataclass(frozen=True)
class Settings:
    notify: Notify = field(default_factory=Notify)


def _notify(table: dict, root: Path) -> Notify:
    got = Notify(adapter="md-notify")
    adapter = table.get("adapter", got.adapter)
    if adapter not in ADAPTERS:
        raise SettingsError(f"[notify] adapter: {adapter!r} is not one of {', '.join(ADAPTERS)}")
    times = {}
    for key in ("day_open", "day_close"):
        value = table.get(key, getattr(got, key))
        if not isinstance(value, str) or not HHMM.match(value):
            raise SettingsError(f"[notify] {key}: {value!r} is not HH:MM")
        times[key] = value
    labels = table.get("deadline_warnings", list(DEFAULT_WARNINGS))
    if not isinstance(labels, list):
        raise SettingsError("[notify] deadline_warnings: a list, like [\"24h\", \"3h\"]")
    queue = table.get("queue", got.queue)
    if not isinstance(queue, str) or not queue.strip():
        raise SettingsError("[notify] queue: a path under the workspace root")
    resolved = (Path(root) / queue).resolve()
    if not resolved.is_relative_to(Path(root).resolve()):
        raise SettingsError(f"[notify] queue: {queue!r} is outside the workspace root")
    return Notify(adapter=adapter, queue=queue, warnings=tuple((w, _offset(w)) for w in labels), **times)


def load(root: Path, settings_path: str | None) -> Settings:
    """`settings_path` is the `Settings:` line's value, relative to `root`, or None when there is none."""
    if not settings_path:
        return Settings()
    path = Path(root) / settings_path
    if not path.is_file():
        return Settings()
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{settings_path}: {e}") from None
    table = data.get("notify")
    if table is None:
        return Settings()
    if not isinstance(table, dict):
        raise SettingsError(f"{settings_path}: [notify] is not a table")
    return Settings(notify=_notify(table, Path(root)))
