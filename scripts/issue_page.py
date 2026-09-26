#!/usr/bin/env python3
"""One tracker issue's page, `/issues/<n>` (Task.dc.html, "Task page — laptop"), from the trackers and the cached
forge sources: the header; its merge request (merge order, overlap, pipeline jobs, conflicts, threads); the issue's
one Flow row; What happens next; Files changed; Architecture (the decision page's module graph); How it got here
(its Log lines); Review; Acceptance (the bullets under the issue body's "Acceptance" heading); its workers; and
References. pages.py renders it on request into `issue-<n>.html`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import board_sources
import flow_chart
from decision_page import HEAD, _graph, _md, _section, span
from render_board import (Config, ConfigError, TRAILING_NUMBER, _resolve_due, daily_trackers, parse_coordinator,
                          parse_tracker)
from settings import Graph, SettingsError, load as load_settings

# A worker's outcome by its last launcher move's stage (flow_chart.moves: completed → pr, HUMAN REVIEW → review).
ENDED = {"pr": "completed", "review": "human review"}
# The Flow chart's colours on HEAD's tokens in the dark scheme, as the board sets them (render_board.render).
CSS = """<style>
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) :is(.gantt,.gantt-legend){--gantt-ink:var(--fg);--gantt-muted:var(--muted);--gantt-bg:var(--bg);--gantt-rule:var(--line);--gantt-edge:var(--line);--gantt-grid:var(--line);--gantt-past:var(--surface);--gantt-now:var(--dl);--gantt-hold:var(--dl)}}
:root[data-theme="dark"] :is(.gantt,.gantt-legend){--gantt-ink:var(--fg);--gantt-muted:var(--muted);--gantt-bg:var(--bg);--gantt-rule:var(--line);--gantt-edge:var(--line);--gantt-grid:var(--line);--gantt-past:var(--surface);--gantt-now:var(--dl);--gantt-hold:var(--dl)}
.meta{color:var(--muted);font-size:12px}
header .chip.running{color:var(--surface);background:var(--stage-implement);border-color:var(--stage-implement)}
.chk,.wk{display:grid;grid-template-columns:96px 74px minmax(0,1fr);gap:0 10px;align-items:baseline;padding:6px 0;font-size:13px}
.wk{grid-template-columns:minmax(0,1fr) 64px auto;padding:5px 0}
.chk+.chk,.wk+.wk{border-top:1px dotted var(--line)}
.mr .chip{border:1px solid var(--line)}
.mk{font-size:10.5px;padding:0 6px;border-radius:3px;border:1px solid var(--line);justify-self:start;white-space:nowrap}
.mk.passed{border-color:#009E73} .mk.failed{border:1px dashed var(--dl);color:var(--dl)}
.wk .st{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)} .wk .st.running{color:var(--stage-implement)}
.fl{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:0 10px;padding:3px 0;font-size:13px}
.fl .path{font-family:ui-monospace,monospace;overflow-wrap:anywhere} .fl .d{font-variant-numeric:tabular-nums;color:var(--muted)}
.fl.total{border-top:1px solid var(--line);margin-top:4px;padding-top:6px}
.rv{display:grid;grid-template-columns:74px minmax(0,1fr);gap:2px 10px;padding:6px 0;font-size:13px}
.rv+.rv{border-top:1px dotted var(--line)} .rv p{grid-column:2;margin:0}
</style>
"""
ACCEPTANCE = re.compile(r"(?im)^#{1,6}[ \t]*acceptance\b.*$")
HEADING = re.compile(r"^\s*#{1,6}\s")
BULLET = re.compile(r"^\s*[-*+]\s+(?:\[([ xX])\]\s*)?(.*\S)")


def _clock(at: datetime, now: datetime) -> str:
    return f"{at:%H:%M}" if at.date() == now.date() else f"{at:%a %H:%M}"


def _number(c: board_sources.Change) -> int:
    return int(c.ref[1:])


def _queue(c: board_sources.Change, changes) -> tuple[int, int, list[str]] | None:
    """(K, M, the refs merging before it) among the open changes into c's base, by number; None unless c is open.
    ponytail: only the changes the sources cache knows (those the trackers name); list the base's open changes if
    M must count the whole forge."""
    if c.state != "open":
        return None
    line = sorted((o for o in changes if o.state == "open" and o.base == c.base and o.ref != c.ref), key=_number)
    before = [o.ref for o in line if _number(o) < _number(c)]
    return len(before) + 1, len(line) + 1, before


def _after(before: list[str]) -> str:
    return f"after {', '.join(before)}" if before else "next in line"


def _shared(c: board_sources.Change, changes) -> dict[str, list[str]]:
    """Each of c's files that another open change also touches, with those changes' refs."""
    others = [o for o in changes if o.state == "open" and o.ref != c.ref]
    return {p: refs for p in _paths(c) if (refs := [o.ref for o in others if p in o.files])}


def _paths(c: board_sources.Change) -> tuple[str, ...]:
    return c.files or tuple(f.path for f in c.stats)


def _change(c: board_sources.Change, changes) -> str:
    kind, word = ("MR", "Merge request") if c.ref.startswith("!") else ("PR", "Pull request")
    state = "draft" if c.draft and c.state == "open" else c.state
    merged = f" {c.merged_at:%H:%M}" if c.merged_at else ""
    rows = [("pipeline", c.pipeline or "none", c.pipeline or "", "")]
    rows += [("job", j.status, j.status, escape(j.name) + (f" · {span_s(j.seconds)}" if j.seconds is not None else ""))
             for j in c.jobs]
    rows.append(("approvals", str(c.approvals), "", "approved" if c.approved else ""))
    if q := _queue(c, changes):
        rows.append(("merge order", f"{q[0]} of {q[1]}", "", escape(_after(q[2]))))
    shared = _shared(c, changes)
    rows.append(("overlap", f"{len(shared)} file{'s' * (len(shared) != 1)}" if shared else "none", "",
                 "; ".join(f"{escape(p)} with {escape(', '.join(refs))}" for p, refs in shared.items())))
    rows.append(("conflicts", "yes" if c.conflicts else "none", "failed" if c.conflicts else "", ""))
    resolved = sum(t.resolved for t in c.threads)
    rows.append(("threads", f"{len(c.threads) - resolved} open", "", f"{resolved} resolved"))
    body = "\n".join(f'      <div class="chk"><span class="cap">{cap}</span><span class="mk {escape(cls)}">{escape(mark)}</span>'
                     f"<span>{note}</span></div>" for cap, mark, cls, note in rows)
    return (f'  <section>\n    <h2>{word} · <a class="ref" data-k="{kind}" href="{escape(c.url)}">{escape(c.ref)}</a></h2>\n'
            f'    <div class="card mr">\n      <strong>{escape(c.title)}</strong> <span class="chip">{escape(state.upper())}{merged}</span>'
            f' <span class="meta">→ {escape(c.base)}</span>\n{body}\n    </div>\n  </section>\n')


def span_s(seconds: int) -> str:
    return f"{seconds}s" if seconds < 60 else span(timedelta(seconds=seconds))


def _files(c: board_sources.Change, changes) -> str:
    """FILES CHANGED: each file, its +additions −deletions when known, "also !N" for another open change on it."""
    stats, shared = {f.path: f for f in c.stats}, _shared(c, changes)
    rows = []
    for p in _paths(c):
        f = stats.get(p)
        rows.append(f'      <div class="fl"><span class="path">{escape(p)}</span>'
                    f'<span class="d">{f"+{f.additions} −{f.deletions}" if f else ""}</span>'
                    f'<span class="meta">{"also " + escape(", ".join(shared[p])) if p in shared else ""}</span></div>')
    if c.stats:
        rows.append(f'      <div class="fl total"><span class="cap">total</span><span class="d">+{sum(f.additions for f in c.stats)} '
                    f'−{sum(f.deletions for f in c.stats)}</span><span></span></div>')
    return _section(f"Files changed · {len(rows) - bool(c.stats)}", '    <div class="card">\n' + "\n".join(rows) + "\n    </div>")


def _review(c: board_sources.Change, now: datetime) -> str:
    """REVIEW: open threads, then resolved, each newest first."""
    if not c.threads:
        return _section("Review", f'    <div class="card"><p>No review threads on {escape(c.ref)}.</p></div>')
    newest = sorted(c.threads, key=lambda t: t.at.timestamp() if t.at else 0, reverse=True)
    rows = []
    for t in sorted(newest, key=lambda t: t.resolved):
        when = f" · {_clock(t.at.astimezone(now.tzinfo), now)}" if t.at else ""
        link = f' · <a href="{escape(t.url)}">thread</a>' if t.url else ""
        rows.append(f'      <div class="rv"><span class="mk{" passed" if t.resolved else ""}">{"Resolved" if t.resolved else "Open"}</span>'
                    f"<span><b>{escape(t.author)}</b>{when}{link}</span><p>{escape(t.body)}</p></div>")
    resolved = sum(t.resolved for t in c.threads)
    return _section(f"Review · {len(c.threads) - resolved} open · {resolved} resolved",
                    '    <div class="card">\n' + "\n".join(rows) + "\n    </div>")


def _acceptance(issue: board_sources.Issue | None) -> str:
    """ACCEPTANCE: the bullets under the body's "Acceptance" heading, up to the next heading; `[x]` is met."""
    m = ACCEPTANCE.search(issue.body) if issue else None
    met = []
    for line in (issue.body[m.end():].splitlines() if m else []):
        if HEADING.match(line):
            break
        if b := BULLET.match(line):
            met.append((b[1] in ("x", "X"), b[2]))
    if not met:
        return _section("Acceptance", '    <div class="card"><p>No acceptance criteria in the issue.</p></div>')
    rows = "\n".join(f'      <div class="chk"><span class="mk{" passed" if ok else ""}">{"met" if ok else "not met"}</span>'
                     f"<span>{_md(what)}</span></div>" for ok, what in met)
    return _section(f"Acceptance · {sum(ok for ok, _ in met)} of {len(met)} met", f'    <div class="card">\n{rows}\n    </div>')


def _next(rows, change: board_sources.Change | None, changes, why_not: str, now: datetime) -> str:
    """WHAT HAPPENS NEXT: the Flow row's forecast stages and the change's place in its merge queue."""
    if why_not:
        lines = [f"Nothing happens next: {why_not}."]
    else:
        lines = [f"{escape(g.category)} ~{_clock(g.start.astimezone(now.tzinfo), now)}–{_clock(g.end.astimezone(now.tzinfo), now)}"
                 for _, r in rows for g in r.segments if g.kind == "forecast"]
        lines = lines or ["No estimate: the task has no due time to forecast from."]
        if change and (q := _queue(change, changes)):
            lines.append(f"{escape(change.ref)} merge queue {q[0]} of {q[1]} into {escape(change.base)}, {escape(_after(q[2]))}")
    return _section("What happens next", '    <div class="card">\n' + "\n".join(f"      <p>{x}</p>" for x in lines) + "\n    </div>")


def render(number: int, trackers: list[tuple[date, str]], sources: board_sources.Sources, now: datetime,
           lanes: dict | None = None, cfg: Config | None = None, graph: Graph | None = None, pages: Path | None = None,
           root: Path | None = None) -> str | None:
    """The page, or None when no task in `trackers` names issue `number`. `cfg` resolves a task's due (the board's
    deadlines); `graph`, `pages` and `root` draw Architecture as the decision page draws its module graph."""
    ref = f"#{number}"
    names = lambda t: {k.strip().casefold() for k in (t.name, t.item) if k.strip()}  # noqa: E731
    known = [t for _, text in trackers for t in parse_tracker(text).tasks]
    mine = [t for t in known if (m := TRAILING_NUMBER.search(t.issue)) and int(m[1]) == number]
    if not mine:
        return None
    keys = set().union(*map(names, mine))
    log = sorted((m for day, text in trackers for m in flow_chart.moves(text, day, now.tzinfo)), key=lambda m: m.at)
    # A forecast runs to the task's due, as the board's Flow chart feeds render_board._end's due.
    cfg = cfg or Config("", "", "", getattr(now.tzinfo, "key", "UTC"), (), None, None)
    ends = {t.item: end for t in parse_tracker(trackers[-1][1]).tasks
            if t.kind != "done" and (end := _resolve_due(t.due, cfg, now.date()))}
    rows = [(s, r) for s, r in flow_chart.build(log, known, lanes or {}, set(), ends, now)
            if (m := TRAILING_NUMBER.search(r.ref)) and int(m[1]) == number]
    log = [m for m in log if m.name.strip().casefold() in keys]
    issue, latest = sources.issues.get(ref), {k: t for t in known for k in names(t)}  # later trackers win
    status = rows[0][0] if rows else mine[-1].kind
    name = rows[0][1].name if rows else mine[0].label

    order = {"open": 0, "merged": 1}
    changes = sorted((c for c in sources.changes.values() if ref in c.issues), key=lambda c: order.get(c.state, 2))
    every = list(sources.changes.values())
    cards = "".join(_change(c, every) for c in changes) or _section("Change", f'    <div class="card"><p>No change names {ref} yet.</p></div>')
    change = changes[0] if changes else None
    current = {t.name.strip().casefold(): t for t in mine}.values()
    why_not = ("the issue is closed" if issue and issue.state == "closed" else
               "the task is done" if status == "merged" or all(t.kind == "done" for t in current) else "")
    ahead = _next(rows, change, every, why_not, now) + (_files(change, every) if change and _paths(change) else "")
    ahead += _graph(change, graph, pages, root, "Architecture", f"No change names {ref} to graph.")

    steps = "\n".join(f'      <div class="step"><span class="when">{_clock(m.at, now)}</span><span class="dot" data-k="{escape(m.stage)}">'
                      f"</span><span>{_md(m.line.split(' ', 2)[2])}</span></div>" for m in log)
    workers: dict[str, list[flow_chart.Move]] = {}
    for m in log:
        if m.launch and (w := flow_chart.STARTED.match(m.line) or flow_chart.ENDED.match(m.line)):
            workers.setdefault(w[3], []).append(m)
    lines = []
    for who, runs in workers.items():
        started = board_sources.STARTED.match(runs[0].line)
        if not started:
            continue  # its started line is on a day before the window
        task = latest.get(runs[0].name.strip().casefold())
        live = task is not None and task.kind == "running" and task.owner.strip() == who
        state = "running" if live else ENDED.get(runs[-1].stage, "ended")
        took = span((now if live else runs[-1].at) - runs[0].at) if live or len(runs) > 1 else ""
        lines.append(f'      <div class="wk"><span><b>{escape(who)}</b><br><span class="meta">one-shot · {escape(started[3])} · '
                     f'task {escape(started[5])} · {_clock(runs[0].at, now)}</span></span><span class="when">{took}</span>'
                     f'<span class="st{" running" if live else ""}">{escape(state)}</span></div>')

    refs = [("issue", f'<a href="{escape(issue.url)}">{ref} {escape(issue.title)}</a>' if issue else f"{ref} {escape(name)}",
             issue.state if issue else "")]
    refs += [("MR" if c.ref.startswith("!") else "PR", f'<a href="{escape(c.url)}">{escape(c.ref)} {escape(c.title)}</a>',
              " · ".join(filter(None, (c.state, c.pipeline)))) for c in changes]
    refs_html = "\n".join(f'      <span class="cap">{cap}</span><span>{what}</span><span class="st">{escape(st)}</span>' for cap, what, st in refs)

    lane = next((t.lane.strip() for t in reversed(mine) if t.lane.strip()), "")
    eyebrow = " · ".join(["Task", f'<a class="ref" data-k="issue" href="{escape(issue.url)}">{ref}</a>' if issue else ref,
                          *([f"lane {escape(lane)}"] if lane else []), '<a href="/">board</a>', f"rendered {now:%H:%M %Z}"])
    flow = flow_chart.section(rows, now)
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{ref} {escape(name)}</title>\n{HEAD}<style>\n{flow_chart.css() if flow else ''}</style>\n{CSS}</head>\n"
            f'<body>\n<main class="issue">\n  <header class="top">\n    <div>\n      <span class="eyebrow">{eyebrow}</span>\n'
            f"      <h1>{escape(name)}</h1>\n    </div>\n"
            f'    <span class="chip {escape(status)}">{escape(status.upper())}</span>\n  </header>\n\n'
            + cards + (f"  <section>\n{flow}  </section>\n" if flow else "") + ahead
            + '  <div class="cols">\n  <div class="col">\n'
            + _section("How it got here", f'    <div class="card timeline">\n{steps}\n    </div>' if steps else '    <div class="card"><p>No Log lines yet.</p></div>')
            + (_review(change, now) if change else "")
            + '  </div>\n  <div class="col">\n' + _acceptance(issue)
            + _section("Workers", '    <div class="card">\n' + "\n".join(lines) + "\n    </div>" if lines else '    <div class="card"><p>No workers launched.</p></div>')
            + _section("References", f'    <div class="card refs">\n{refs_html}\n    </div>')
            + "  </div>\n  </div>\n</main>\n</body>\n</html>\n")


def write(root: Path, pages_dir: Path, number: int) -> Path | None:
    """Render `issue-<number>.html` from the trackers the Flow chart reaches back over; with no page, remove the file
    and return None. The page server calls this in process."""
    cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())
    now = datetime.now(cfg.zone).replace(microsecond=0)
    reach = (now - flow_chart.WINDOWS[-1][1]).date()
    trackers = [(d, path.read_text()) for d, path in daily_trackers(root, cfg) if reach <= d <= now.date()]
    try:
        settings = load_settings(root, cfg.settings_path)
    except SettingsError as e:
        raise ConfigError(str(e)) from None
    page = render(number, trackers, board_sources.load(pages_dir), now, settings.lanes, cfg, settings.graph, pages_dir, root)
    out = pages_dir / f"issue-{number}.html"
    if page is None:
        out.unlink(missing_ok=True)
        return None
    pages_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return out
