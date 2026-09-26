#!/usr/bin/env python3
"""The tracker's File ownership rows, read one way by every script that needs them.

A row names a context and the paths it owns, written as a table row `| context | paths |` or as a
bullet `- context: paths`. Audit read both and dispatch read only tables, so a row the audit
accepted was refused at launch (#74).
"""

from __future__ import annotations

from dataclasses import dataclass

from render_board import _cells, _is_separator

HEADING = "## File ownership"


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
