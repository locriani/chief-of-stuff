#!/usr/bin/env python3
"""The tracker's File ownership rows, read one way by every script that needs them.

A row names a context and the paths it owns, written as a table row `| context | paths |` or as a
bullet `- context: paths`. Audit read both and dispatch read only tables, so a row the audit
accepted was refused at launch (#74).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from md import cells as _cells, is_separator as _is_separator, section as _section

HEADING = "## File ownership"
_NONE = re.compile(r"none(?![\w./-])", re.I)


@dataclass(frozen=True)
class Row:
    context: str
    paths: str


def parse(text: str) -> list[Row]:
    """Every row in the section's body, in order; the table header and separator are not rows."""
    rows: list[Row] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")) and ":" in stripped:
            context, _, paths = stripped[2:].partition(":")
            rows.append(Row(context.strip(), paths.strip()))
            continue
        if not stripped.startswith("|"):
            continue
        cells = _cells(line)
        if _is_separator(cells) or len(cells) < 2 or cells[0].lower() == "context":
            continue
        rows.append(Row(cells[0], cells[1]))
    return rows


def cell(text: str, keys: set[str]) -> str | None:
    """The paths of the first File ownership row in the tracker `text` keyed on one of `keys`, or None."""
    return next((row.paths for row in parse("\n".join(_section(text, HEADING))) if row.context in keys), None)


def read_only(paths: str) -> bool:
    """A cell that starts with the word `none`, not the start of a path, owns nothing, so it changes nothing (#57)."""
    return bool(_NONE.match(paths.strip()))
