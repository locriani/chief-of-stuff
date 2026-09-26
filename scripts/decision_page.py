#!/usr/bin/env python3
"""Render one decision's full context as a page in the pages dir, and print its local URL.

    python3 decision_page.py --root R --name <slug> [--from decision.json] [--date YYYY-MM-DD]

The decision is read from `<pages dir>/decision-<slug>.json` unless `--from` names another file: a
file, because Claude Code refuses JSON piped through a heredoc. The page is `<pages dir>/decision-<slug>.html`, served by pages.py at `http://127.0.0.1:<port>/decision-<slug>.html`.
Never a claude.ai artifact (Zach, 2026-09-25: "don't serve it as an artifact but serve it using the localhost
server thing"). The JSON holds:

    headline, ask, options [{key, title, text}], recommended (a key), why, default   required
    topic, asked (HH:MM), answer (a key, once answered), yes, sources,
    sections [{heading, text: paragraph or [paragraph]}], sides [{label, status: open|done, text}],
    timeline [{when, text, kind}], references [{label, value, kind, status, at}],
    architecture {summary, not_drawn, nodes [{id, name, state, detail, hot}], edges [{from, to, label, state, hot}]}
                                                                                     optional

What the JSON leaves out the page reads from the workspace (render_board.decision_context): `asked` from the first
Log line naming `decision-<slug>`, else the file's mtime; the answer and its chip from the latest Decisions row naming
it, the key being the option its words name; a reference's status from the tracker's stage and board_sources. Rendering
the board re-renders every page, and `decisions.html` lists them (Decisions.dc.html).

A timeline `kind` colours its dot: a lane stage (implement, pr, review, triage, fix, verify, merge), `hot` or
`designed`. A node's `state` is deployed (the default), inflight, designed or external; an edge's is deployed,
inflight or designed; `hot` marks what waits on this decision. A reference's `at` pins its files to a commit.

Text is escaped; `code`, **bold** and http(s) URLs are marked up, and references link to the forge the
`Backlog:` line names: `#12`, `!58` (`!58#note_1`), a commit sha, `pipeline #8812`, `path/file.py:42`
(`:24-58`, `:symbol`), `docs/x.md §4.2` or `docs/x.md#anchor`, `src/dir/`. A section or symbol is placed from a
copy under `--root` when there is one. With no forge, or a reference it cannot place, the text stays text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path

import board_sources
from backlog import Backlog, BacklogError, GitHubBacklog, backlog_from_config
from pages import PagesError, from_config

# The board's fonts (render_board.FONTS; render_board imports this module, so it cannot import that one).
FONTS = "https://fonts.googleapis.com/css2?family=Alegreya+Sans:wght@400;500;600&family=Cormorant+SC:wght@600&display=swap"
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REQUIRED = ("headline", "ask", "options", "recommended", "why", "default")
NODE = {"deployed": "n", "inflight": "n-inflight", "designed": "n-designed", "external": "ext"}
EDGE = {"deployed": "e", "inflight": "e-inflight", "designed": "e-designed"}
CHIPS = (("deployed", "DEPLOYED"), ("inflight", "IN FLIGHT"), ("designed", "DESIGNED"), ("hot", "WAITING ON THIS"),
         ("external", "EXTERNAL"))


class DecisionError(ValueError):
    """The JSON does not describe a decision the user can make from the page."""


def parse(data: dict) -> dict:
    """The decision itself, once it holds everything the user decides from."""
    for key in REQUIRED:
        if not data.get(key):
            raise DecisionError(f"the decision has no {key}")
    keys = [o.get("key") for o in data["options"]]
    if len(keys) < 2 or len(set(keys)) != len(keys) or not all(keys):
        raise DecisionError("a decision needs at least two options, each with its own key")
    if data["recommended"] not in keys:
        raise DecisionError(f"recommended {data['recommended']!r} is not one of the options {', '.join(keys)}")
    if data.get("answer") and data["answer"] not in keys:
        raise DecisionError(f"answer {data['answer']!r} is not one of the options {', '.join(keys)}")
    for side in data.get("sides", []):
        if side.get("status") not in ("open", "done"):
            raise DecisionError(f"side {side.get('label')!r} has status {side.get('status')!r}, not open or done")
    arch = data.get("architecture")
    if arch:
        ids = {n.get("id") for n in arch.get("nodes", [])}
        for n in arch.get("nodes", []):
            if n.get("state", "deployed") not in NODE:
                raise DecisionError(f"node {n.get('id')!r} has state {n.get('state')!r}, not one of {', '.join(NODE)}")
        for e in arch.get("edges", []):
            if e.get("state", "deployed") not in EDGE:
                raise DecisionError(f"edge {e.get('label')!r} has state {e.get('state')!r}, not one of {', '.join(EDGE)}")
            for end in (e.get("from"), e.get("to")):
                if end not in ids:
                    raise DecisionError(f"edge {e.get('label')!r} names node {end!r}, which is not drawn")
    return data


# Where each kind of reference lives on each forge. `{at}` is the tree a file is read at.
GITHUB = {"issue": "issues/{}", "MR": "pull/{}", "commit": "commit/{}", "blob": "blob/{at}/{}", "tree": "tree/{at}/{}",
          "pipeline": "actions/runs/{}"}
GITLAB = {"issue": "-/issues/{}", "MR": "-/merge_requests/{}", "commit": "-/commit/{}", "blob": "-/blob/{at}/{}",
          "tree": "-/tree/{at}/{}", "pipeline": "-/pipelines/{}"}
# One pass over escaped text, so nothing already linked is read again. Order is precedence.
TOKEN = re.compile(r"""
    (?P<tag><[^>]+>)
  | `(?P<code>[^`]+)`
  | (?<![\w/>])(?P<url>https?://[^\s<`]*[^\s<`.,;:)])
  | \b(?:[Pp]ipeline|[Rr]un)\ \#(?P<pid>\d+)\b
  | (?<![\w&/!\#])(?P<sigil>[!\#])(?P<num>\d+)(?:\#(?P<note>note_\d+|issuecomment-\d+|discussion_r\d+))?\b
  | (?<![\w/.-])(?P<path>(?:[\w.-]+/)+(?![\w.-])|(?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z]\w*)
    (?::(?P<line>\d+)(?:[-–](?P<end>\d+))?\b|:(?P<sym>[A-Za-z_]\w*)\b|\ §\ ?(?P<sec>\d+(?:\.\d+)*)|\#(?P<anchor>[\w-]+))?
  | (?<![\w\#&-])(?P<sha>(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40})\b
""", re.X)
DOCS = (".md", ".markdown", ".rst", ".adoc", ".txt")


@dataclass(frozen=True)
class Linker:
    """Turns a reference into its place on the workspace's forge (the `Backlog:` line). `root` holds local copies
    that place a `§` section or a symbol; `at` pins files to a commit. With no forge nothing is linked."""

    forge: Backlog | GitHubBacklog | None = None
    root: Path | None = None
    at: str | None = None

    def _url(self, what: str, target: str) -> str:
        gh = isinstance(self.forge, GitHubBacklog)
        base = f"https://github.com/{self.forge.repo}" if gh else f"{self.forge.host.rstrip('/')}/{self.forge.project}"
        return f"{base}/" + (GITHUB if gh else GITLAB)[what].format(target, at=self.at or "HEAD")

    def _find(self, path: str, sec: str | None, sym: str | None) -> str:
        """The `#` fragment for a section heading or a symbol's definition in a local copy, or "" (never guessed)."""
        if not self.root:
            return ""
        f = (self.root / path).resolve()
        if not f.is_relative_to(self.root.resolve()) or not f.is_file():
            return ""
        lines = f.read_text(errors="replace").splitlines()
        if sym:
            hit = re.compile(rf"^\s*(?:(?:async\s+)?def|class|function|func|fn|const|let|var)\s+{sym}\b|^\s*{sym}\s*=")
            n = next((i for i, text in enumerate(lines, 1) if hit.search(text)), None)
            return f"#L{n}" if n else ""
        head = re.compile(rf"^#+\s+§?\s*{re.escape(sec)}(?![\d.]*\d)")
        found = next((text for text in lines if head.match(text)), None)
        # ponytail: GitHub's heading slug (GitLab's matches for plain headings); duplicate headings get no -1 suffix.
        return "#" + re.sub(r"[^\w\- ]", "", found.lstrip("#").strip().lower()).replace(" ", "-") if found else ""

    def resolve(self, m: re.Match) -> tuple[str, str, str] | None:
        """(kind, label, url) for a TOKEN match that names a reference the forge can place, else None."""
        if not self.forge:
            return None
        gh = isinstance(self.forge, GitHubBacklog)
        if m["pid"]:
            return "pipeline", f"#{m['pid']}", self._url("pipeline", m["pid"])
        if m["num"]:
            mr = m["sigil"] == "!"
            url = self._url("MR" if mr else "issue", m["num"]) + (f"#{m['note']}" if m["note"] else "")
            return ("note" if m["note"] else "PR" if mr and gh else "MR" if mr else "issue"), m[0], url
        if m["sha"]:
            return "commit", m[0], self._url("commit", m["sha"])
        path = m["path"]
        if not path:
            return None
        if path.endswith("/"):
            return "code", m[0], self._url("tree", path.rstrip("/")) + "/"
        if "/" not in path and not (m["line"] or m["sym"] or m["sec"] or m["anchor"]):
            return None  # a bare `name.ext` is too often a word
        frag = ""
        if m["line"]:
            frag = f"#L{m['line']}" + (f"-{'L' if gh else ''}{m['end']}" if m["end"] else "")
        elif m["anchor"]:
            frag = f"#{m['anchor']}"
        elif m["sec"] or m["sym"]:
            frag = self._find(path, m["sec"], m["sym"])
        return ("doc" if path.lower().endswith(DOCS) else "code"), m[0], self._url("blob", path) + frag

    def href(self, text: str) -> str | None:
        """The first placeable reference in `text` (already escaped)."""
        return next((r[2] for m in TOKEN.finditer(text) if (r := self.resolve(m))), None)


def _md(text: str, linker: Linker = Linker()) -> str:
    """Escape, then mark up **bold**, `code`, URLs and references the forge can place."""
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escape(str(text), quote=False))

    def chip(m: re.Match) -> str | None:
        r = linker.resolve(m)
        return f'<a class="ref" data-k="{r[0]}" href="{escape(r[2])}">{r[1]}</a>' if r else None

    def one(m: re.Match) -> str:
        if m["tag"]:
            return m[0]
        if m["code"] is not None:
            whole = TOKEN.fullmatch(m["code"])
            return (whole and chip(whole)) or f"<code>{m['code']}</code>"
        if m["url"]:
            return f'<a href="{m["url"]}">{m["url"]}</a>'
        return chip(m) or m[0]

    return TOKEN.sub(one, out)


def _section(heading: str, body: str) -> str:
    return f"  <section>\n    <h2>{escape(heading, quote=False)}</h2>\n{body}\n  </section>\n"


def _layout(nodes: list[dict], edges: list[dict]) -> dict[str, tuple[int, int]]:
    """(column, row) per node: a node sits as far right as its longest path to a sink allows, so every edge of an
    acyclic figure points right; rows follow the order nodes are listed in."""
    # ponytail: longest-path layering, no crossing minimisation; a vendored Sugiyama (grandalf) if figures outgrow ~10 nodes.
    out = {n["id"]: [e["to"] for e in edges if e["from"] == n["id"]] for n in nodes}
    down: dict[str, int] = {}

    def depth(n: str, seen: frozenset) -> int:
        if n in seen:
            return 0
        if n not in down:
            down[n] = max((1 + depth(t, seen | {n}) for t in out[n]), default=0)
        return down[n]

    top = max(depth(n["id"], frozenset()) for n in nodes)
    rows: dict[int, int] = {}
    place = {}
    for n in nodes:
        col = top - down[n["id"]]
        place[n["id"]] = (col, rows.get(col, 0))
        rows[col] = rows.get(col, 0) + 1
    return place


def _clip(cx: float, cy: float, hw: float, hh: float, dx: float, dy: float) -> tuple[float, float]:
    """Where the ray from a box's centre along (dx, dy) leaves the box."""
    t = min(hw / abs(dx) if dx else 1e9, hh / abs(dy) if dy else 1e9)
    return round(cx + dx * t, 1), round(cy + dy * t, 1)


def _figure(arch: dict, linker: Linker) -> str:
    nodes, edges = arch.get("nodes", []), arch.get("edges", [])
    if not nodes:
        return ""
    place = _layout(nodes, edges)
    cols = 1 + max(c for c, _ in place.values())
    width = [150.0] * cols
    for n in nodes:
        c = place[n["id"]][0]
        width[c] = max(width[c], 8.4 * len(n["name"]) + 28, 6.6 * len(n.get("detail", "")) + 28)
    gap, h, vgap, pad = 104, 64, 50, 16
    left = [pad + sum(width[:c]) + gap * c for c in range(cols)]
    box = {i: (left[c], pad + r * (h + vgap), width[c]) for i, (c, r) in place.items()}
    W = left[-1] + width[-1] + pad
    H = pad * 2 + (1 + max(r for _, r in place.values())) * (h + vgap) - vgap
    marks, lines = set(), []
    for e in edges:
        (x1, y1, w1), (x2, y2, w2) = box[e["from"]], box[e["to"]]
        c1, c2 = (x1 + w1 / 2, y1 + h / 2), (x2 + w2 / 2, y2 + h / 2)
        dx, dy = c2[0] - c1[0], c2[1] - c1[1]
        a, b = _clip(*c1, w1 / 2, h / 2, dx, dy), _clip(*c2, w2 / 2, h / 2, -dx, -dy)
        cls = EDGE[e.get("state", "deployed")] + (" hot" if e.get("hot") else "")
        mark = "hot" if e.get("hot") else EDGE[e.get("state", "deployed")]
        marks.add(mark)
        lines.append(f'<line class="{cls}" x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" marker-end="url(#ar-{mark})"/>')
        if e.get("label"):
            tone = " hot" if e.get("hot") else "" if cls == "e" else f" {e.get('state')}"
            lines.append(f'<text class="tl halo{tone}" x="{round((a[0] + b[0]) / 2, 1)}" y="{round((a[1] + b[1]) / 2 - 7, 1)}" '
                         f'text-anchor="middle">{escape(e["label"])}</text>')
    shapes = []
    for n in nodes:
        x, y, w = box[n["id"]]
        state = n.get("state", "deployed")
        cls = NODE[state] + (" hot" if n.get("hot") else "")
        rx = ' rx="14"' if state == "external" else ""
        cx = round(x + w / 2, 1)
        g = [f'<g data-node="{escape(n["id"])}"><rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}"{rx}/>',
             f'<text class="t{" t-ext" if state == "external" else ""}" x="{cx}" y="{y + 27}" text-anchor="middle">{escape(n["name"])}</text>']
        if n.get("detail"):
            detail = escape(n["detail"], quote=False)
            href = linker.href(detail)
            text = (f'<text class="{"tm" if href else "tl"}{" warn" if n.get("hot") else ""}" x="{cx}" y="{y + 47}" '
                    f'text-anchor="middle">{detail}</text>')
            g.append(f'<a href="{escape(href)}">{text}</a>' if href else text)
        shapes.append("".join(g) + "</g>")
    defs = "".join(f'<marker id="ar-{m}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                   f'orient="auto-start-reverse"><path class="ar-{m}" d="M0,0 L10,5 L0,10 z"/></marker>' for m in sorted(marks))
    present = {n.get("state", "deployed") for n in nodes} | {e.get("state", "deployed") for e in edges}
    if any(x.get("hot") for x in nodes + edges):
        present.add("hot")
    chips = "".join(f'<span class="chip {k}">{label}</span>' for k, label in CHIPS if k in present)
    not_drawn = f'<span>Not drawn: {_md(arch["not_drawn"], linker)}</span>' if arch.get("not_drawn") else ""
    label = arch.get("summary") or "The components around this decision: " + ", ".join(n["name"] for n in nodes) + "."
    return (f'    <figure class="card fig">\n      <div class="scroll"><svg role="img" aria-label="{escape(label)}" '
            f'viewBox="0 0 {W} {H}" width="{W}" height="{H}" style="min-width:{min(W, 640)}px">\n        <defs>{defs}</defs>\n        '
            + "\n        ".join(lines + shapes)
            + f"\n      </svg></div>\n      <figcaption>{chips}{not_drawn}</figcaption>\n    </figure>")


def render(d: dict, day: str, forge: Backlog | GitHubBacklog | None = None, root: Path | None = None,
           ctx: Context | None = None, slug: str = "", mtime: float | None = None) -> str:
    """The page. With a context, what the JSON leaves out is read from it: when it was asked, the answer and the
    chip, and each reference's status."""
    link = Linker(forge, root)

    def md(text: str) -> str:
        return _md(text, link)

    left, right = [], []
    for s in d.get("sections", []):
        paras = [s["text"]] if isinstance(s.get("text"), str) else s.get("text", [])
        left.append(_section(s["heading"], '    <div class="card">\n' + "\n".join(f"      <p>{md(p)}</p>" for p in paras) + "\n    </div>"))
    if d.get("sides"):
        sides = "\n".join(f'      <div class="card side">\n        <span class="pill {s["status"]}">{escape(s["status"].upper())}</span>\n'
                          f'        <p><b>{escape(s["label"])}</b>: {md(s["text"])}</p>\n      </div>' for s in d["sides"])
        left.append(_section("Where it stands", f'    <div class="compare">\n{sides}\n    </div>'))
    if d.get("timeline"):
        steps = "\n".join(
            f'      <div class="step"><span class="when">{escape(t["when"])}</span><span class="dot"'
            + (f' data-k="{escape(t["kind"])}"' if t.get("kind") else "")
            + f'></span><span>{md(t["text"])}</span></div>' for t in d["timeline"])
        left.append(_section("How it got here", f'    <div class="card timeline">\n{steps}\n    </div>'))
    opts = []
    for o in d["options"]:
        rec = o["key"] == d["recommended"]
        title = escape(o["title"]) + (' <span class="rec">· recommended</span>' if rec else "")
        opts.append(f'      <div class="opt{" rec" if rec else ""}"><span class="k">{escape(o["key"])}</span><b>{title}</b>'
                    f'\n        <p>{md(o.get("text", ""))}</p>\n      </div>')
    right.append(_section("Options", '    <div class="options">\n' + "\n".join(opts) + "\n    </div>"))
    right.append(_section("Recommendation", f'    <div class="card"><p>{md(d["why"])}</p></div>'))
    right.append(_section("If you don't answer", f'    <div class="card default"><p>{md(d["default"])}</p></div>'))
    if d.get("references"):
        rows = []
        for r in d["references"]:
            pin = Linker(forge, root, r.get("at"))
            label = f" {md(r['label'])}" if r.get("kind") and r.get("label") else ""
            one = TOKEN.fullmatch(escape(r["value"], quote=False))
            one = one and pin.resolve(one)
            # A row that is one reference is one plain link: the row's kind already labels it.
            what = f'<a href="{escape(one[2])}">{one[1]}{label}</a>' if one else _md(r["value"], pin) + label
            one_ref = refs_in(str(r.get("value", "")))
            known = r.get("status") or (ctx.where(one_ref[0]) if ctx and len(one_ref) == 1 else "")
            status = " · ".join(x for x in (escape(known), f"@ {escape(r['at'])}" if r.get("at") else "") if x)
            rows.append(f'      <span class="cap">{escape(r.get("kind") or r.get("label", ""))}</span><span>{what}</span>'
                        f'<span class="st">{status}</span>')
        right.append(_section("References", '    <div class="card refs">\n' + "\n".join(rows) + "\n    </div>"))
    keys = [o["key"] for o in d["options"]]
    answer = keys[0] if len(keys) == 1 else ", ".join(keys[:-1]) + (", or " if len(keys) > 2 else " or ") + keys[-1]
    sources = f" from {md(d['sources'])}" if d.get("sources") else ""
    at = ctx.asked(slug, d, mtime) if ctx else None
    asked = f"asked {ctx.when(at)}" if at else f"asked {d['asked']}" if d.get("asked") else None
    row = ctx.answer(slug) if ctx and not d.get("answer") else None
    chosen = d.get("answer") or (ctx.key(row, keys) if row else "")
    answered = bool(chosen or row)
    eyebrow = " · ".join([escape(x) for x in ("Decision", d.get("topic"), _day(day), asked) if x]
                         + ['<a href="decisions.html">decisions</a>', '<a href="/">board</a>'])
    chip = f"ANSWERED · {escape(chosen)}" if chosen else "ANSWERED" if answered else "PENDING"
    yes = f"\n    <p>{md(d['yes'])}</p>" if d.get("yes") else ""
    where = _figure(d["architecture"], link) if d.get("architecture") else ""
    where = _section("Where it sits", where) if where else ""
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{escape(d['headline'])}</title>\n{HEAD}</head>\n<body>\n<main class=\"decision\">\n"
            f'  <header class="top">\n    <div>\n      <span class="eyebrow">{eyebrow}</span>\n'
            f"      <h1>{md(d['headline'])}</h1>\n    </div>\n"
            f'    <span class="chip{" answered" if answered else ""}">{chip}</span>\n  </header>\n\n'
            f'  <div class="card ask">\n    <span class="cap">ask</span>\n'
            f"    <strong>{md(d['ask'])}</strong>{yes}\n  </div>\n\n"
            + where
            + '  <div class="cols">\n  <div class="col">\n' + "".join(left) + '  </div>\n  <div class="col">\n'
            + "".join(right) + "  </div>\n  </div>\n"
            + f'\n  <p class="foot">Prepared by the coordinator{sources}. Answer in chat: {answer}.</p>\n</main>\n</body>\n</html>\n')


def forge(root: Path) -> Backlog | GitHubBacklog | None:
    """The `Backlog:` line's forge, or None: a line that does not parse links nothing and says so."""
    try:
        return backlog_from_config(root / "CLAUDE.md")
    except (OSError, BacklogError) as e:
        print(f"decision: references stay unlinked — {e}", file=sys.stderr)
        return None


def context(root: Path, day: str) -> Context | None:
    """The workspace's context for `day`, or None when its block does not parse (the page then shows its JSON alone)."""
    import render_board  # render_board imports this module, so not at the top

    try:
        return render_board.decision_context(root, date.fromisoformat(day))
    except (ValueError, OSError, render_board.ConfigError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--from", dest="source", help="the decision as JSON; default <pages dir>/decision-<slug>.json")
    ap.add_argument("--name", required=True, help="the page's slug: lowercase letters, digits and dashes")
    ap.add_argument("--date", default=date.today().isoformat(), help="the day shown on the page")
    args = ap.parse_args(argv)
    if not NAME.fullmatch(args.name):
        print(f"decision: name {args.name!r} is not lowercase letters, digits and dashes", file=sys.stderr)
        return 2
    try:
        pages_dir, port = from_config(Path(args.root))
    except PagesError as e:
        print(f"decision: no page — {e}", file=sys.stderr)
        return 1
    try:
        source = Path(args.source) if args.source else pages_dir / f"decision-{args.name}.json"
        page = render(parse(json.loads(source.read_text())), args.date, forge(Path(args.root)), Path(args.root),
                      context(Path(args.root), args.date), args.name, source.stat().st_mtime)
    except (OSError, json.JSONDecodeError, DecisionError, KeyError, TypeError, AttributeError) as e:
        print(f"decision: {e}", file=sys.stderr)
        return 2
    pages_dir.mkdir(parents=True, exist_ok=True)
    (pages_dir / f"decision-{args.name}.html").write_text(page)
    print(f"http://127.0.0.1:{port}/decision-{args.name}.html")
    return 0


PAGE_REF = re.compile(r"decision-[a-z0-9]+(?:-[a-z0-9]+)*")
# An issue or PR/MR a text names: `#12`, `!58`, `owner/repo#12`, or its forge URL. `pipeline #8812` is not one.
REF = re.compile(r"(?<![\w&])(?<![Pp]ipeline )(?<![Rr]un )([#!])(\d+)\b|/(issues|pull|merge_requests)/(\d+)|[\w.-]+/[\w.-]+#(\d+)\b")
HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def refs_in(text: str) -> list[str]:
    out = []
    for m in REF.finditer(text or ""):
        ref = m[1] + m[2] if m[1] else ("!" if m[3] == "merge_requests" else "#") + (m[4] or m[5])
        if ref not in out:
            out.append(ref)
    return out


def span(td: timedelta) -> str:
    """`29m`, `1h 18m`, `4h`, `1d 1h`."""
    d, m = divmod(max(0, int(td.total_seconds() // 60)), 1440)
    h, m = divmod(m, 60)
    if d:
        return f"{d}d {h}h" if h else f"{d}d"
    if h:
        return f"{h}h {m:02d}m" if m else f"{h}h"
    return f"{m}m"


def _day(day: str) -> str:
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    return f"{d:%a} {d.day} {d:%b}"


@dataclass(frozen=True)
class Row:
    """A Decisions row: its tracker's day, its time (None when the cell is not HH:MM), item and words."""
    day: date
    when: datetime | None
    item: str
    words: str


@dataclass(frozen=True)
class Context:
    """What the pages read besides a decision's JSON: every tracker's Decisions rows and Log lines (oldest first),
    the day's Tasks rows, the forge through board_sources, and the [kanban] label that holds an issue."""
    now: datetime
    rows: tuple[Row, ...] = ()
    log: tuple[tuple[datetime, str], ...] = ()
    tasks: tuple = ()
    sources: board_sources.Sources = board_sources.EMPTY
    hold: str = ""
    user: str = ""
    forge: Backlog | GitHubBacklog | None = None

    def when(self, at: datetime) -> str:
        at = at.astimezone(self.now.tzinfo)
        return f"{at:%H:%M}" if at.date() == self.now.date() else f"{at:%a %H:%M}"

    def asked(self, slug: str, d: dict, mtime: float | None) -> datetime | None:
        """The JSON's `asked`, else the first Log line naming the page, else the file's mtime."""
        base = datetime.fromtimestamp(mtime, self.now.tzinfo) if mtime is not None else None
        m = HHMM.fullmatch(str(d.get("asked") or ""))
        if m:
            return datetime.combine((base or self.now).date(), time(int(m[1]), int(m[2])), self.now.tzinfo)
        page = f"decision-{slug}"
        return next((at for at, text in self.log if page in PAGE_REF.findall(text)), base)

    def answer(self, slug: str) -> Row | None:
        """The latest Decisions row naming the page."""
        page = f"decision-{slug}"
        return next((r for r in reversed(self.rows) if page in PAGE_REF.findall(r.item)), None)

    def key(self, row: Row, keys: list[str]) -> str:
        """The option key the user's words name, else the one the item names."""
        for text in (row.words, row.item):
            hit = next((w for w in re.findall(r"[\w-]+", text) if w in keys), None)
            if hit:
                return hit
        return ""

    def holding(self, slug: str, d: dict) -> list[str]:
        """The issues and PRs/MRs the decision holds: its `holds`, its one-ref references, and the refs of every
        task whose item names the page."""
        page = f"decision-{slug}"
        found = [r for h in d.get("holds") or [] for r in refs_in(str(h))]
        for r in d.get("references") or []:
            one = refs_in(str(r.get("value", "")))
            found += one if len(one) == 1 else []
        for t in self.tasks:
            if page in PAGE_REF.findall(t.item):
                found += refs_in(t.issue) + refs_in(t.item)
        return list(dict.fromkeys(found))

    def change(self, ref: str) -> board_sources.Change | None:
        c = self.sources.changes
        return c.get(ref) or (c.get("#" + ref[1:]) if ref.startswith("!") else None)

    def stage(self, ref: str) -> str:
        return next((t.stage for t in self.tasks if t.stage and ref in refs_in(t.issue) + refs_in(t.item)), "")

    def where(self, ref: str) -> str:
        """Where a ref stands now: `merged 01:15`, `closed`, or its tracker stage with the forge's word on it
        (`merge · passed`, `triage · hold`, `open · failed`)."""
        c = self.change(ref) or next((c for c in self.sources.changes.values() if ref in c.issues), None)
        i = self.sources.issues.get(ref)
        if c and c.state == "merged":
            return "merged" + (f" {c.merged_at.astimezone(self.now.tzinfo):%H:%M}" if c.merged_at else "")
        if (c or i) and (c or i).state == "closed":
            return "closed"
        stage = next(filter(None, (self.stage(r) for r in (ref, *((c.ref, *c.issues) if c else ())))), "")
        held = i and self.hold and self.hold in i.labels
        return " · ".join(x for x in (stage or ("open" if c else ""), c and c.pipeline, "hold" if held else "") if x)

    def ref(self, ref: str) -> str:
        found = self.change(ref) or self.sources.issues.get(ref)
        url = found.url if found else Linker(self.forge).href(escape(ref))
        return f'<a class="ref" href="{escape(url)}">{escape(ref)}</a>' if url else f'<a class="ref">{escape(ref)}</a>'

    def is_change(self, ref: str) -> bool:
        return ref.startswith("!") or bool(self.change(ref))


BAD = (OSError, json.JSONDecodeError, DecisionError, KeyError, TypeError, AttributeError, StopIteration)


def entries(pages_dir: Path, ctx: Context) -> list[tuple[str, dict | None, datetime | None, str]]:
    """Every decision no Decisions row has answered, as (slug, decision, asked, why it is unreadable): the newest
    ask first, the unreadable last."""
    out = []
    for source in pages_dir.glob("decision-*.json"):
        slug = source.stem.removeprefix("decision-")
        if ctx.answer(slug):
            continue
        try:
            d, why = parse(json.loads(source.read_text())), ""
        except BAD as e:
            d, why = None, str(e)
        out.append((slug, d, ctx.asked(slug, d or {}, source.stat().st_mtime), why))
    return sorted(out, key=lambda e: (e[1] is None, -e[2].timestamp() if e[2] else 0, e[0]))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def index(pages_dir: Path, ctx: Context, day: str) -> tuple[str, list[tuple[str, dict | None, datetime | None, str]]]:
    """`decisions.html` (Decisions.dc.html) and its pending entries, which the board's DECISIONS panel lists too."""
    pending = entries(pages_dir, ctx)
    rows, held = [], []
    for slug, d, asked, why in pending:
        when = f'<span class="when">{ctx.when(asked)}</span>' if asked else ""
        if d is None:
            name = escape(f"decision-{slug}.json")
            rows.append(f'      <div class="row pend unread">\n        <span class="stack">{when}</span>\n'
                        f'        <span class="stack"><a class="ref" href="{name}">{name}</a><span class="why">unreadable: {escape(why)}</span></span>\n'
                        + '        <span class="none">—</span>' * 3 + "\n      </div>")
            continue
        holds = ctx.holding(slug, d)
        held += holds
        age = f'<span class="age">{span(ctx.now - asked)}</span>' if asked else ""
        topic = f'<span class="topic">{escape(d["topic"])}</span>' if d.get("topic") else ""
        opts = "".join(f'<span class="o{" rec" if o["key"] == d["recommended"] else ""}"><span class="key">{escape(o["key"])}</span>'
                       f'{escape(o.get("title", ""))}{" · recommended" if o["key"] == d["recommended"] else ""}</span>' for o in d["options"])
        refs = "".join(f"<span>{ctx.ref(r)} {escape(ctx.where(r))}</span>" for r in holds)
        rows.append(f'      <div class="row pend">\n        <span class="stack">{when}{age}</span>\n'
                    f'        <span class="stack"><span class="line"><a class="headline" href="decision-{escape(slug)}.html">{escape(d["headline"])}</a>{topic}</span>'
                    f'<span class="q">{_md(d["ask"])}</span></span>\n        <span class="stack">{opts}</span>\n'
                    f'        <span class="stack holds">{refs}</span>\n        <span class="fallback">{_md(d["default"])}</span>\n      </div>')
    try:
        today = date.fromisoformat(day)
    except ValueError:
        today = ctx.now.date()
    done = []
    for row in reversed([r for r in ctx.rows if r.day == today]):
        page = next(iter(PAGE_REF.findall(row.item)), None)
        source = pages_dir / f"{page}.json" if page else None
        try:
            d = parse(json.loads(source.read_text())) if source and source.is_file() else None
        except BAD:
            d = None
        took, key, answer, refs = "", "", "", refs_in(row.item)
        if d:
            slug = page.removeprefix("decision-")
            key = ctx.key(row, [o["key"] for o in d["options"]]) or d.get("answer", "")
            answer = next((o.get("title", "") for o in d["options"] if o["key"] == key), "")
            asked = ctx.asked(slug, d, source.stat().st_mtime)
            took = f"in {span(row.when - asked)}" if asked and row.when and row.when >= asked else ""
            refs += ctx.holding(slug, d)
            head = f'<a class="headline" href="{escape(page)}.html">{escape(d["headline"])}</a><span class="page">{escape(page)}</span>'
        else:
            first, _, rest = re.sub(r'^\(via [^)]*\)\s*', "", row.words).strip(' "“”').partition(" ")
            if first.rstrip(",.;:").lower() in ("yes", "no"):
                key, answer = first.rstrip(",.;:").lower(), rest.strip(' "“”')
            head = f'<span class="headline">{_md(row.item)}</span><span class="page">{escape(page) if page else "Decisions row"}</span>'
        ref = refs[0] if refs else ""
        since = f"{ctx.ref(ref)} {escape(ctx.where(ref))}" if ref else ""
        keyed = f'<span class="key">{escape(key)}</span>' if key else ""
        done.append(f'      <div class="row done">\n        <span class="stack"><span class="when">{ctx.when(row.when) if row.when else ""}</span>'
                    f'<span class="took">{took}</span></span>\n        <span class="stack">{head}</span>\n'
                    f"        <span>{keyed}{escape(answer)}</span>\n        <span class=\"words\">{escape(row.words)}</span>\n"
                    f'        <span class="since">{since}</span>\n      </div>')
    ready = [a for _, d, a, _ in pending if d is not None]
    held = list(dict.fromkeys(held))
    mrs = sum(1 for r in held if ctx.is_change(r))
    unread = len(pending) - len(ready)
    meta = [f"rendered {ctx.now:%H:%M} {ctx.now:%Z}".rstrip(), f"{len(ready)} pending"]
    if any(ready):
        meta.append(f"oldest {span(ctx.now - min(a for a in ready if a))}")
    if held:
        meta.append(f"holding {_plural(len(held) - mrs, 'issue')}, {_plural(mrs, 'merge request')}")
    if unread:
        meta.append(f"{unread} unreadable")
    meta.append(f"{len(done)} completed today")
    meta += [f"{escape(k)}: {escape(why)}" for k, why in ctx.sources.errors.items()]
    meta.append('<a href="/">board</a>')
    words = f"{escape(ctx.user)}'s words" if ctx.user else "words"
    none = '      <div class="row"><span class="none">none</span></div>'
    body = (f'  <section>\n    <h2 class="pend">PENDING · {len(ready)}</h2>\n    <div class="list">\n'
            '      <div class="row pend head cap"><span>asked</span><span>decision</span><span>options</span><span>holding</span><span>if unanswered</span></div>\n'
            + ("\n".join(rows) or none) + "\n    </div>\n  </section>\n"
            f'  <section>\n    <h2>COMPLETED · {len(done)}</h2>\n    <div class="list fin">\n'
            f'      <div class="row done head cap"><span>answered</span><span>decision</span><span>answer</span><span>{words}</span><span>since</span></div>\n'
            + ("\n".join(done) or none) + "\n    </div>\n  </section>\n")
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>Decisions · {escape(_day(day))}</title>\n{HEAD}</head>\n<body>\n<main class=\"decisions\">\n"
            f'  <header class="top">\n    <div>\n      <h1>Decisions · {escape(_day(day))}</h1>\n'
            f'      <span class="eyebrow">{" · ".join(meta)}</span>\n    </div>\n  </header>\n'
            + body + "</main>\n</body>\n</html>\n"), pending


def write_all(pages_dir: Path, ctx: Context, day: str, root: Path | None = None) -> list[tuple[str, dict | None, datetime | None, str]]:
    """Re-render every readable decision's page from its JSON and the context, and `decisions.html`; the pending entries."""
    for source in pages_dir.glob("decision-*.json"):
        try:
            page = render(parse(json.loads(source.read_text())), day, ctx.forge, root, ctx, source.stem.removeprefix("decision-"),
                          source.stat().st_mtime)
        except BAD:
            continue
        source.with_suffix(".html").write_text(page)
    html, pending = index(pages_dir, ctx, day)
    (pages_dir / "decisions.html").write_text(html)
    return pending


HEAD = f'<link rel="stylesheet" href="{FONTS}">\n' + """<style>
/* The board's parchment scheme (render_board.py) and the architecture review's state encoding (frank-lloyd-aight
   docs/review-page.md). Okabe-Ito colours the timeline stages, as on the board Gantt. */
:root{--bg:#f4efe1;--surface:#fbf8ef;--fg:#2f2630;--muted:#6e6470;--line:#ddd3bd;--brass:#a7843e;--dl:#c9533a;--link:#8a6c30;--ref-bg:#efe7d3;--deployed:#2b3542;--inflight:#a8660f;--inflight-soft:#f6ead6;--designed:#2c58a0;--designed-soft:#dfe8f6;--ext:#8a8f98;--ext-soft:#f1eee6;--ext-ink:#5f6b7b;--stage-implement:#0072B2;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#000000;--now:#4f6b3a}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#1c1813;--surface:#262019;--fg:#efe6d2;--muted:#b3a791;--line:#3d352a;--brass:#d4b06a;--dl:#f08566;--link:#e2c27f;--ref-bg:#342b1f;--deployed:#d9d2c4;--inflight:#e8ad55;--inflight-soft:#35281a;--designed:#8fb3f0;--designed-soft:#1f2735;--ext:#8f8a80;--ext-soft:#2d2822;--ext-ink:#c4bdb0;--stage-implement:#3D95D6;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#efe6d2;--now:#a8c67e}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1c1813;--surface:#262019;--fg:#efe6d2;--muted:#b3a791;--line:#3d352a;--brass:#d4b06a;--dl:#f08566;--link:#e2c27f;--ref-bg:#342b1f;--deployed:#d9d2c4;--inflight:#e8ad55;--inflight-soft:#35281a;--designed:#8fb3f0;--designed-soft:#1f2735;--ext:#8f8a80;--ext-soft:#2d2822;--ext-ink:#c4bdb0;--stage-implement:#3D95D6;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#efe6d2;--now:#a8c67e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 "Alegreya Sans","Gill Sans",system-ui,sans-serif;padding:24px 16px 48px}
main{max-width:1280px;margin:0 auto;display:flex;flex-direction:column;gap:16px}
h1,h2{font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-weight:600;text-wrap:balance;margin:0}
h1{font-size:27px;letter-spacing:.04em;margin-top:2px}
h2{font-size:16px;letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px}
p{margin:0}
a{color:var(--link);overflow-wrap:anywhere}
section{min-width:0}
header.top{display:flex;align-items:flex-end;gap:20px;border-bottom:1px solid var(--brass);padding-bottom:10px}
header.top>div{flex:1 1 auto;min-width:0}
.eyebrow{font-size:12.5px;color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:10px 14px}
.card>p+p{margin-top:6px}
.cap{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.chip,.pill{font-size:10.5px;font-weight:700;letter-spacing:.06em;border-radius:3px;padding:0 6px;white-space:nowrap}
header .chip{color:var(--link);border:1px solid var(--brass);margin-bottom:4px}
header .chip.answered{color:var(--surface);background:#009E73;border-color:#009E73}
.ask{border-top:3px solid var(--brass);display:flex;flex-direction:column;gap:4px}
.ask strong{font-size:16px}
.ask p{color:var(--muted)}
code{font-family:ui-monospace,monospace;font-size:.88em;background:var(--ref-bg);padding:0 3px;border-radius:3px}
.ref{font-family:ui-monospace,monospace;font-size:.88em;color:var(--link);text-decoration:none;border-bottom:1px dotted var(--brass);background:var(--ref-bg);padding:0 3px;border-radius:3px;white-space:nowrap}
.ref::before{content:attr(data-k);font-size:.8em;color:var(--muted);margin-right:3px}
.cols{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:18px}
.col{display:flex;flex-direction:column;gap:14px;min-width:0}
.compare{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.side{display:flex;flex-direction:column;gap:4px;align-items:flex-start}
.pill.done{color:#fff;background:#009E73} .pill.open{color:#2f2630;background:#E69F00}
.timeline{padding:4px 14px}
.step{display:grid;grid-template-columns:70px 14px minmax(0,1fr);gap:0 10px;padding:6px 0}
.step+.step{border-top:1px dotted var(--line)}
.step .when{font-family:ui-monospace,monospace;font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.dot{width:9px;height:9px;border-radius:50%;margin-top:5px;background:var(--brass)}
.dot[data-k="hot"]{background:var(--dl)} .dot[data-k="designed"]{background:var(--designed)}
.dot[data-k="implement"]{background:var(--stage-implement)} .dot[data-k="pr"]{background:var(--stage-pr)}
.dot[data-k="review"]{background:var(--stage-review)} .dot[data-k="triage"]{background:var(--stage-triage)}
.dot[data-k="fix"]{background:var(--stage-fix)} .dot[data-k="verify"]{background:var(--stage-verify)} .dot[data-k="merge"]{background:var(--stage-merge)}
.options{display:flex;flex-direction:column;gap:8px}
.opt{display:grid;grid-template-columns:26px minmax(0,1fr);gap:2px 8px;background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:10px 14px}
.opt.rec{border:2px solid var(--brass)}
.opt .k{font:700 16px/1.3 ui-monospace,monospace;color:var(--muted)} .opt.rec .k{color:var(--link)}
.opt p{grid-column:2}
.default{border-left:3px solid var(--muted)}
.refs{display:grid;grid-template-columns:70px minmax(0,1fr) auto;gap:5px 10px;font-size:13px;align-items:baseline}
.refs .st{color:var(--muted);font-size:12px}
.fig{margin:0;padding:8px 10px 10px}
.scroll{overflow-x:auto}
.fig svg{display:block;max-width:100%;height:auto}
figcaption{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;font-size:12px;color:var(--muted);border-top:1px dotted var(--line);padding-top:7px;margin-top:4px}
figcaption .chip.deployed{border:1.5px solid var(--deployed);color:var(--deployed)}
figcaption .chip.inflight{border:1.5px dashed var(--inflight);color:var(--inflight);background:var(--inflight-soft)}
figcaption .chip.designed{border:1.5px dotted var(--designed);color:var(--designed);background:var(--designed-soft)}
figcaption .chip.hot{border:2px solid var(--dl);color:var(--dl)}
figcaption .chip.external{border:1.5px solid var(--ext);color:var(--ext-ink);border-radius:9px}
svg text{font-family:"Alegreya Sans",system-ui,sans-serif;fill:var(--fg)}
svg .t{font-size:14px;font-weight:700} svg .t-ext{fill:var(--ext-ink)}
svg .tm{font-family:ui-monospace,monospace;font-size:10.5px;fill:var(--link)}
svg .tl{font-size:11.5px;fill:var(--muted)}
svg .tl.inflight{fill:var(--inflight)} svg .tl.designed{fill:var(--designed)} svg .tl.hot,svg .warn{fill:var(--dl);font-weight:700}
svg .halo{paint-order:stroke;stroke:var(--surface);stroke-width:4px;stroke-linejoin:round}
svg .n{fill:var(--surface);stroke:var(--deployed);stroke-width:1.5}
svg .n-inflight{fill:var(--inflight-soft);stroke:var(--inflight);stroke-width:1.5;stroke-dasharray:6 4}
svg .n-designed{fill:var(--designed-soft);stroke:var(--designed);stroke-width:1.5;stroke-dasharray:2 3}
svg rect.ext{fill:var(--ext-soft);stroke:var(--ext);stroke-width:1.5}
svg .e{stroke:var(--deployed);stroke-width:1.5}
svg .e-inflight{stroke:var(--inflight);stroke-width:1.5;stroke-dasharray:6 4}
svg .e-designed{stroke:var(--designed);stroke-width:1.5;stroke-dasharray:2 3}
svg rect.hot,svg line.hot{stroke:var(--dl);stroke-width:2}
svg .ar-e{fill:var(--deployed)} svg .ar-e-inflight{fill:var(--inflight)} svg .ar-e-designed{fill:var(--designed)} svg .ar-hot{fill:var(--dl)}
.foot{font-size:12.5px;color:var(--muted);border-top:1px solid var(--line);padding-top:8px}
.opt .rec{font-weight:500;color:var(--link)}
/* decisions.html (Decisions.dc.html): a list, pending then completed. */
.decisions{font-size:13.5px;line-height:1.45}
.decisions section{display:grid;gap:8px}
h2.pend{color:var(--link)}
.list{background:var(--surface);border:1px solid var(--line);border-top:3px solid var(--brass);border-radius:5px}
.list.fin{border-top-color:var(--now)}
.row{display:grid;align-items:baseline;gap:14px;padding:9px 14px}
.row+.row{border-top:1px dotted var(--line)}
.row.head{padding:6px 14px}
.row.pend{grid-template-columns:78px minmax(0,2.3fr) minmax(0,1.5fr) minmax(0,1fr) minmax(0,1.4fr)}
.row.done{grid-template-columns:78px minmax(0,2fr) minmax(0,1.2fr) minmax(0,1.3fr) minmax(0,1.2fr)}
.stack{display:flex;flex-direction:column;gap:2px;min-width:0;align-items:flex-start}
.holds{gap:3px}
.line{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px}
.row .when,.age,.took,.page{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums}
.row .when{font-size:12.5px} .age{font-size:11.5px;color:var(--dl)} .took{font-size:11.5px;color:var(--muted)}
.headline{font-weight:700;font-size:14.5px;color:var(--fg)} .done .headline{font-size:inherit}
.topic{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border:1px solid var(--line);border-radius:3px;padding:0 5px}
.page{font-size:11px;color:var(--muted)}
.key{display:inline-block;font:700 11px/1.45 ui-monospace,monospace;border-radius:3px;padding:0 5px;margin-right:4px;border:1px solid var(--line);color:var(--muted)}
.o.rec{font-weight:700} .o.rec .key,.done .key{color:var(--surface);background:var(--link);border-color:var(--link)}
.done .key{background:var(--now);border-color:var(--now)}
.words{font-style:italic}
.holds>span,.fallback,.since,.none{font-size:12.5px;color:var(--muted)}
.unread{border-left:3px dashed var(--dl)} .unread .why{color:var(--dl)} .unread .when{color:var(--muted)}
@media (max-width:760px){.row.pend,.row.done{grid-template-columns:minmax(0,1fr);gap:4px}.row.head{display:none}.row .stack:first-child{flex-direction:row;gap:8px}.unread .none{display:none}}
@media (max-width:900px){.cols{grid-template-columns:minmax(0,1fr)}}
@media (max-width:480px){header.top{flex-direction:column;align-items:flex-start;gap:6px}.refs{grid-template-columns:minmax(0,1fr)}}
</style>
"""


if __name__ == "__main__":
    raise SystemExit(main())
