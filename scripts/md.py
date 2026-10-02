"""The Markdown the workspace's documents are written in: `## ` sections, `- key: value` bullets and pipe tables.

Syntax only. What a section or a row means is workspace.py's and tracker.py's to say.
"""

from __future__ import annotations

import re

BULLET = re.compile(r"^(\s*)-\s*([^:]+?):\s*(.*?)\s*$")


def section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == heading:
            body = []
            for nxt in lines[i + 1 :]:
                if nxt.startswith("## "):
                    break
                body.append(nxt)
            return body
    return []


def unquote(value: str) -> str:
    return value.strip().strip("`").strip()


def unmark(text: str) -> str:
    """`**bold**` and `` `code` `` → their words. For a name that cannot wrap: the marks go, and a cut can never split a pair."""
    return re.sub(r"`([^`]+)`", r"\1", re.sub(r"\*\*(.+?)\*\*", r"\1", text))


def split_row(line: str) -> list[str]:
    r"""Split a table row on its delimiters. A cell is a span too: `\|` is a pipe, and so is a pipe inside a
    backtick span — the item column is prose, prose carries code, and code carries pipes. An odd backtick is a
    typo, and a typo degrades to the old split instead of swallowing the rest of the row."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    spans = inner.count("`") % 2 == 0
    cells, cur, coded = [], [], False
    i = 0
    while i < len(inner):
        c = inner[i]
        if c == "\\" and i + 1 < len(inner) and inner[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if c == "`" and spans:
            coded = not coded
        if c == "|" and not coded:
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    cells.append("".join(cur))
    return cells


def cells(line: str) -> list[str]:
    return [c.strip() for c in split_row(line)]


def is_separator(row: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c) for c in row if c) and any(row)
