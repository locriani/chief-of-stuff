#!/usr/bin/env python3
"""A task's change as source and diff, `/issues/<n>/source` (Source48.dc.html, "Source & diff — task !48"): the change's
files with +additions −deletions, a NEW / CHANGED / REMOVED badge and the other open changes on the same file, then
each file's unified diff against the merge base. The diff is read from the `[graph]` table's clone, fetched as the
module graph fetches it; without the table the page lists the files from the sources cache. pages.py renders it on
request into `issue-<n>-source.html`.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html import escape
from pathlib import Path

import board_sources
import decision_page
from decision_page import HEAD
from issue_page import _paths, _shared, anchor, inputs, put
from render_board import TRAILING_NUMBER, parse_tracker
from settings import Graph

HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
CACHE = ".diffs"
# Additions and deletions tint with the stage tokens, so the adopted dark scheme reaches them.
CSS = """<style id="source">
header .cmp{padding:6px 12px;text-align:right} header .cmp div{font:15px ui-monospace,monospace}
.src{display:grid;grid-template-columns:290px minmax(0,1fr);gap:12px;align-items:start}
ul.files{list-style:none;margin:0;padding:0;position:sticky;top:12px}
ul.files li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 8px;border-top:1px dotted var(--line);padding:7px 0}
ul.files a{font:12px ui-monospace,monospace;color:var(--fg);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
ul.files .d{font:11px ui-monospace,monospace;white-space:nowrap}
ul.files .b{grid-column:1/-1;display:flex;gap:4px}
.plus{color:var(--stage-review)} .minus{color:var(--dl)}
.chip.NEW{color:var(--stage-review);border:1.5px solid var(--stage-review)}
.chip.CHANGED{color:var(--stage-implement);border:1.5px solid var(--stage-implement)}
.chip.REMOVED{color:var(--stage-fix);border:1.5px dashed var(--stage-fix)}
.chip.also{color:var(--dl);border:1.5px dashed var(--dl)}
.diffs{display:flex;flex-direction:column;gap:12px;min-width:0}
.diffs .card{padding:0;overflow-x:auto}
.diffs h3{margin:0;padding:8px 14px;border-bottom:1px solid var(--line);font:600 13px ui-monospace,monospace}
table.diff{border-collapse:collapse;width:100%;font:12.5px/20px ui-monospace,SFMono-Regular,Menlo,monospace}
table.diff .hunk td{background:var(--ref-bg);color:var(--muted);padding:2px 14px;font-size:11.5px}
table.diff td.o,table.diff td.n{width:44px;color:var(--muted);text-align:right;padding-right:8px;font-size:11.5px;user-select:none}
table.diff td.sign{width:18px;text-align:center} table.diff td.tx{white-space:pre}
tr.add{background:color-mix(in srgb,var(--stage-review) 12%,transparent)} tr.add td.sign{color:var(--stage-review)}
tr.del{background:color-mix(in srgb,var(--dl) 12%,transparent)} tr.del td.sign{color:var(--dl)}
@media (max-width:760px){.src{grid-template-columns:minmax(0,1fr)} ul.files{position:static}}
</style>
"""


class File:
    def __init__(self, path: str, badge: str = ""):
        self.path, self.badge, self.add, self.dele, self.rows = path, badge, 0, 0, []


def parse(diff: str) -> list[File]:
    """`git diff` output as files: each with its badge, counts and rows (kind, old no, new no, text); a hunk header is
    ("hunk", "", "", header)."""
    files: list[File] = []
    old = new = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files.append(File(line.split(" b/", 1)[-1], "CHANGED"))
        elif not files:
            continue
        elif not files[-1].rows and not line.startswith("@@"):  # the file's header lines
            f = files[-1]
            if line.startswith("new file mode"):
                f.badge = "NEW"
            elif line.startswith("deleted file mode"):
                f.badge = "REMOVED"
            elif line.startswith("+++ b/"):
                f.path = line[6:]
            elif line.startswith("--- a/") and f.badge == "REMOVED":
                f.path = line[6:]
        elif m := HUNK.match(line):
            old, new = int(m[1]), int(m[2])
            files[-1].rows.append(("hunk", "", "", line))
        elif line.startswith("+"):
            files[-1].rows.append(("add", "", new, line[1:]))
            files[-1].add, new = files[-1].add + 1, new + 1
        elif line.startswith("-"):
            files[-1].rows.append(("del", old, "", line[1:]))
            files[-1].dele, old = files[-1].dele + 1, old + 1
        elif line.startswith(" "):
            files[-1].rows.append(("ctx", old, new, line[1:]))
            old, new = old + 1, new + 1
    return files


def diff(change: board_sources.Change, clone: Path, pages: Path) -> tuple[str, str]:
    """(merge base, `git diff` of the change's head against it), read once per head commit."""
    out = pages / CACHE / f"{change.head}.diff"
    if not out.is_file():
        base = decision_page.fetch(change, clone)
        # ponytail: the whole diff, however big; cap it per file if a change's page gets slow.
        text = decision_page._run(["git", "-C", str(clone), "diff", "--no-color", "--no-renames", "--no-ext-diff",
                                   "--end-of-options", base, change.head])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{base}\n{text}")
    base, _, text = out.read_text().partition("\n")
    return base, text


def _count(a: int, d: int) -> str:
    return " ".join(filter(None, (f'<span class="plus">+{a}</span>' if a else "",
                                  f'<span class="minus">−{d}</span>' if d else ""))) or "+0"


def _rows(f: File) -> str:
    out = []
    for kind, o, n, tx in f.rows:
        if kind == "hunk":
            out.append(f'<tr class="hunk"><td colspan="4">{escape(tx)}</td></tr>')
        else:
            sign = {"add": "+", "del": "−"}.get(kind, "")
            out.append(f'<tr class="{kind}"><td class="o">{o}</td><td class="n">{n}</td><td class="sign">{sign}</td>'
                       f'<td class="tx">{escape(tx)}</td></tr>')
    return "\n".join(out)


def render(number: int, trackers: list[tuple[date, str]], sources: board_sources.Sources, now: datetime,
           graph: Graph | None = None, pages: Path | None = None, root: Path | None = None) -> str | None:
    """The page, or None when no task in `trackers` names issue `number`."""
    ref = f"#{number}"
    mine = [t for _, text in trackers for t in parse_tracker(text).tasks
            if (m := TRAILING_NUMBER.search(t.issue)) and int(m[1]) == number]
    if not mine:
        return None
    issue = sources.issues.get(ref)
    name = issue.title if issue else mine[0].label
    order = {"open": 0, "merged": 1}
    change = min((c for c in sources.changes.values() if ref in c.issues), key=lambda c: order.get(c.state, 2), default=None)
    crumb = f"Source · {escape(change.ref) + ' · ' if change else ''}{ref} {escape(name)}"
    title = f"{escape(change.ref)} · {escape(change.title)}" if change else escape(name)
    compare = note = ""
    files: list[File] = []
    if change:
        if graph and pages and root:
            try:
                base, text = diff(change, root / graph.clone, pages)
                files = parse(text)
                compare = f"{escape(change.base)}@{base[:7]} → {change.head[:7]}"
            except (OSError, ValueError) as e:
                note = f"The diff is unavailable: {escape(str(e))}"
        else:
            note = "The diff needs the clone: the settings have no <code>[graph]</code> table."
        if not files:  # the cache's list, without the diff's badges
            stats = {s.path: s for s in change.stats}
            for p in _paths(change):
                files.append(File(p))
                if s := stats.get(p):
                    files[-1].add, files[-1].dele = s.additions, s.deletions
    shared = _shared(change, list(sources.changes.values())) if change else {}

    items = []
    for f in files:
        chips = (f'<span class="chip {f.badge}">{f.badge}</span>' if f.badge else "") + "".join(
            f'<span class="chip also">ALSO {escape(r)}</span>' for r in shared.get(f.path, ()))
        items.append(f'      <li><a href="#{escape(anchor(f.path))}">{escape(f.path)}</a><span class="d">{_count(f.add, f.dele)}</span>'
                     f'<span class="b">{chips}</span></li>')
    tables = [f'    <div class="card"><h3>{escape(f.path)}</h3>\n    <table class="diff" id="{escape(anchor(f.path))}" '
              f'data-path="{escape(f.path)}">\n{_rows(f)}\n    </table></div>' for f in files if f.rows]
    if not change:
        body = f'  <p class="card">Nothing to diff: no change names {ref} yet.</p>\n'
    else:
        total = f"{len(files)} · +{sum(f.add for f in files)} −{sum(f.dele for f in files)}"
        body = (f'  <div class="src">\n    <div class="card"><div class="cap">files · {total}</div>\n'
                f'    <ul class="files">\n' + "\n".join(items) + "\n    </ul></div>\n"
                f'    <div class="diffs">\n' + (f'    <p class="card">{note}</p>\n' if note else "")
                + "\n".join(tables) + "\n    </div>\n  </div>\n")
    cmp = f'    <div class="card cmp"><span class="cap">compare</span><div>{compare}</div></div>\n' if compare else ""
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>Source {escape(change.ref) if change else ref}</title>\n{HEAD}{CSS}</head>\n"
            f'<body>\n<main class="source">\n  <header class="top">\n    <div>\n      <span class="eyebrow">{crumb} · '
            f'<a href="/issues/{number}">task</a> · <a href="/">board</a> · rendered {now:%H:%M %Z}</span>\n'
            f"      <h1>{title}</h1>\n    </div>\n{cmp}  </header>\n{body}</main>\n</body>\n</html>\n")


def write(root: Path, pages_dir: Path, number: int) -> Path | None:
    """Render `issue-<number>-source.html`; with no page, remove the file and return None."""
    _, now, trackers, settings = inputs(root)
    page = render(number, trackers, board_sources.load(pages_dir), now, settings.graph, pages_dir, root)
    return put(pages_dir / f"issue-{number}-source.html", page)
