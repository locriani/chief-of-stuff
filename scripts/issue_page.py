#!/usr/bin/env python3
"""One tracker issue's page, `/issues/<n>` (Task.dc.html, "Task page — laptop"), from the trackers and the cached
forge sources: the header, the issue's one Flow row, its merge request, How it got here (its Log lines), its
workers, and References. pages.py renders it on request into `issue-<n>.html`.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape
from pathlib import Path

import board_sources
import flow_chart
from decision_page import HEAD, _md, _section, span
from render_board import ConfigError, TRAILING_NUMBER, daily_trackers, parse_coordinator, parse_tracker
from settings import SettingsError, load as load_settings

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
</style>
"""


def _clock(at: datetime, now: datetime) -> str:
    return f"{at:%H:%M}" if at.date() == now.date() else f"{at:%a %H:%M}"


def _change(c: board_sources.Change) -> str:
    kind, word = ("MR", "Merge request") if c.ref.startswith("!") else ("PR", "Pull request")
    state = "draft" if c.draft and c.state == "open" else c.state
    merged = f" {c.merged_at:%H:%M}" if c.merged_at else ""
    rows = [("pipeline", c.pipeline or "none", c.pipeline or "", ""), ("approvals", str(c.approvals), "", "approved" if c.approved else "")]
    body = "\n".join(f'      <div class="chk"><span class="cap">{cap}</span><span class="mk {escape(cls)}">{escape(mark)}</span>'
                     f"<span>{note}</span></div>" for cap, mark, cls, note in rows)
    return (f'  <section>\n    <h2>{word} · <a class="ref" data-k="{kind}" href="{escape(c.url)}">{escape(c.ref)}</a></h2>\n'
            f'    <div class="card mr">\n      <strong>{escape(c.title)}</strong> <span class="chip">{escape(state.upper())}{merged}</span>'
            f' <span class="meta">→ {escape(c.base)}</span>\n{body}\n    </div>\n  </section>\n')


def render(number: int, trackers: list[tuple[date, str]], sources: board_sources.Sources, now: datetime,
           lanes: dict | None = None) -> str | None:
    """The page, or None when no task in `trackers` names issue `number`."""
    ref = f"#{number}"
    names = lambda t: {k.strip().casefold() for k in (t.name, t.item) if k.strip()}  # noqa: E731
    known = [t for _, text in trackers for t in parse_tracker(text).tasks]
    mine = [t for t in known if (m := TRAILING_NUMBER.search(t.issue)) and int(m[1]) == number]
    if not mine:
        return None
    keys = set().union(*map(names, mine))
    log = sorted((m for day, text in trackers for m in flow_chart.moves(text, day, now.tzinfo)), key=lambda m: m.at)
    rows = [(s, r) for s, r in flow_chart.build(log, known, lanes or {}, set(), {}, now)
            if (m := TRAILING_NUMBER.search(r.ref)) and int(m[1]) == number]
    log = [m for m in log if m.name.strip().casefold() in keys]
    issue, latest = sources.issues.get(ref), {k: t for t in known for k in names(t)}  # later trackers win
    status = rows[0][0] if rows else mine[-1].kind
    name = rows[0][1].name if rows else mine[0].label

    order = {"open": 0, "merged": 1}
    changes = sorted((c for c in sources.changes.values() if ref in c.issues), key=lambda c: order.get(c.state, 2))
    cards = "".join(map(_change, changes)) or _section("Change", f'    <div class="card"><p>No change names {ref} yet.</p></div>')

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
            + cards + (f"  <section>\n{flow}  </section>\n" if flow else "")
            + '  <div class="cols">\n  <div class="col">\n'
            + _section("How it got here", f'    <div class="card timeline">\n{steps}\n    </div>' if steps else '    <div class="card"><p>No Log lines yet.</p></div>')
            + '  </div>\n  <div class="col">\n'
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
        lanes = load_settings(root, cfg.settings_path).lanes
    except SettingsError as e:
        raise ConfigError(str(e)) from None
    page = render(number, trackers, board_sources.load(pages_dir), now, lanes)
    out = pages_dir / f"issue-{number}.html"
    if page is None:
        out.unlink(missing_ok=True)
        return None
    pages_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return out
