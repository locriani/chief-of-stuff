"""Render a tracker-based daily board without opening a server or browser.

The page is the Flow board (Flow.dc.html): header, tiles, BUILD, the MERGE ORDER, DECISIONS and WORKERS
panels, and the Flow charts. It carries tracker and deadline metadata and ticks its clocks client-side.
The renderer writes the board HTML and prints its path and summary.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clock import hhmm as _hhmm  # noqa: E402
from estimate import (ALL_SIZES, NO_ESTIMATE, SIZES, Estimate, day_bar, estimates, history, horizon,  # noqa: E402
                      nearest_deadline, queue_durations, swimlanes, task_end as _end)
from md import BULLET, section as _section, unquote as _unquote  # noqa: E402
from settings import Kanban, SettingsError, load as load_settings  # noqa: E402
from task_forge import drifts, effective_stage, forge_ends, held, task_changes, worker_names  # noqa: E402
from tracker import (TRAILING_NUMBER, Session, Task, decision_rows, issue_key, parse_tracker,  # noqa: E402
                     resolve_due as _resolve_due)
from workspace import (Config, ConfigError, daily_trackers, read_config,  # noqa: E402
                       with_decision_deadlines, with_workspace_decision_deadlines)
import board_sources  # noqa: E402
import columns  # noqa: E402
import decision_page  # noqa: E402
from fragment import FONTS, TAB_CSS, tab_bar  # noqa: E402
import flow_chart  # noqa: E402
import panels  # noqa: E402
import tracker_log  # noqa: E402

LONG_ITEM = 80
RESUME_CAP = 300    # writer limit from agent rules
REQ_LINE = re.compile(r"^(\s*)- \[([ xX])\]\s+(.*?)\s*$")
EVIDENCE = " — evidence: "


@dataclass(frozen=True)
class Req:
    text: str
    done: bool
    evidence: str
    depth: int


# --- body lane -----------------------------------------------------------------------------------

# The four kinds of the body lane. A body kind is recognised by its own name as a whole word in the
# event's title and by nothing else: the design names these four, and a wider vocabulary (workout,
# breakfast, lunch, dinner on their own) is a decision rather than an implementation of it.
BODY_KINDS = ("sleep", "eat", "gym", "recreation")
BODY_WORD = {k: re.compile(rf"(?i)\b{k}\b") for k in BODY_KINDS}
# The fills are the ones already recorded against the body lane (commit 850bda0) and are not
# re-solved here. They are separate from the main palette, which is parked.
BODY_FILLS: dict[str, str] = {"sleep": "#332288", "eat": "#D55E00", "gym": "#117733", "recreation": "#F0E442"}
# Calendar lines: bullet `- HH:MM–HH:MM [TZ] Title (calendar)` and table `| HH:MM | HH:MM | Title |`.
CAL_BULLET = re.compile(r"^-\s+(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})(?:\s+[A-Z]{2,5})?\s+(.+?)\s*$")
CAL_TABLE = re.compile(r"^\|\s*(\d{1,2}:\d{2})\s*\|\s*(\d{1,2}:\d{2})\s*\|\s*(.+?)\s*\|\s*$")


def body_kind(title: str) -> str:
    """The body kind an event's title names, or \"\" when it names none."""
    for kind in BODY_KINDS:
        if BODY_WORD[kind].search(title):
            return kind
    return ""


@dataclass(frozen=True)
class BodyEvent:
    start: datetime
    end: datetime
    title: str
    kind: str   # "" for non-body events


def parse_calendar(log_text: str, today: date, zone: ZoneInfo) -> list[BodyEvent]:
    """Parse the daily log's ``## Calendar`` section into timed events.

    Reads both bullet lines (`- HH:MM–HH:MM Title`) and table lines (`| HH:MM | HH:MM | Title |`).
    Deadline lines and lines with no time range are skipped. Each event's ``kind`` is its body_kind.
    """
    lines = _section(log_text, "## Calendar")
    events: list[BodyEvent] = []
    for line in lines:
        stripped = line.strip()
        m = CAL_BULLET.match(stripped) or CAL_TABLE.match(stripped)
        if not m:
            continue
        start_s, end_s, raw_title = m.group(1), m.group(2), m.group(3)
        # Strip trailing `(calendar-name)` from the title.
        title = re.sub(r"\s*\([^)]*\)\s*$", "", raw_title).strip()
        if title.lower().startswith("deadline"):
            continue
        start_t = _hhmm(start_s, today, zone)
        end_t = _hhmm(end_s, today, zone)
        if start_t is None or end_t is None:
            continue
        events.append(BodyEvent(start_t, end_t, title, body_kind(title)))
    return events


# --- parsing -------------------------------------------------------------------------------------


def parse_requirements(text: str) -> list[tuple[str, list[Req]]]:
    """Checkbox lines grouped under `## ` headings; indentation gives depth; anything else is a note and ignored."""
    groups: list[tuple[str, list[Req]]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            groups.append((line[3:].strip(), []))
            continue
        m = REQ_LINE.match(line)
        if not m:
            continue
        if not groups:
            groups.append(("Requirements", []))
        body, _, evidence = m.group(3).partition(EVIDENCE)
        groups[-1][1].append(Req(body.strip(), m.group(2) != " ", evidence.strip(), len(m.group(1).expandtabs(2)) // 2))
    return [(name, reqs) for name, reqs in groups if reqs]


# `- Verified 16:52: main = ...` splits at the first colon, and that colon is inside the clock read:
# the key came out `verified 16` and the value `52: main = ...`, so nothing ever matched `verified`
# and the line carrying the day's checked facts was silently absent from the board. These put it back.
LABEL_CLOCK = re.compile(r"^(.*\S)\s+(\d{1,2})$")
VALUE_CLOCK = re.compile(r"^(\d{2}):\s*(.*)$")


def parse_resume(text: str) -> dict[str, str]:
    """The `## Resume` block as `key: value` pairs, keys lowercased. No block, no keys, no error."""
    out: dict[str, str] = {}
    for line in _section(text, "## Resume"):
        m = BULLET.match(line)
        if not m:
            continue
        key, value = _unquote(m.group(2)).lower(), m.group(3).strip()
        head, tail = LABEL_CLOCK.match(key), VALUE_CLOCK.match(value)
        if head and tail:
            key, value = head.group(1), f"{head.group(2)}:{tail.group(1)} · {tail.group(2)}"
        out[key] = value
    return out


# Reading order for the fields the ruleset names, and nothing more: a field outside this tuple is
# drawn after them rather than dropped. It used to be an allowlist of four, so the day the ruleset
# gained `Re-arm` the renderer began discarding it without a word.
RESUME_STRIP = ("as of", "in flight", "next", "waiting on", "re-arm", "verified")


def _clip(value: str, cap: int) -> str:
    """Cut on a word boundary and say so. A clipped line that does not admit it is just a wrong line."""
    if len(value) <= cap:
        return value
    head = value[:cap].rsplit(" ", 1)[0].rstrip(" ,;·—–-")
    return f"{head or value[:cap]} …"


def resume_fields(block: dict[str, str]) -> list[str]:
    """The fields that will be drawn, in reading order. The renderer and the summary line share it.

    Finding 65: a count of what was parsed is not a count of what was drawn, and only the second one
    is a check. `resume=N fields` is taken from here so the two can never disagree again.
    """
    return [k for k in RESUME_STRIP if block.get(k)] + [k for k in block if k not in RESUME_STRIP and block[k]]


# --- rendering -----------------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _tasks(n: int) -> str:
    return f"{n} {'task' if n == 1 else 'tasks'}"


def stage_order(lanes: dict) -> list[str]:
    """Every lane's stages in one order that keeps each lane's own: a new stage lands after its predecessor, a lane's
    leading new stages before its first known one, and a lane sharing none after the rest. graphlib's topological
    order would interleave two lanes that share no stage."""
    order: list[str] = []
    for lane in lanes.values():
        at, pending = None, []
        for stage in lane.stages:
            if stage in order:
                i = order.index(stage)
                order[i:i] = pending
                at, pending = i + len(pending) + 1, []
            elif at is None:
                pending.append(stage)
            else:
                order.insert(at, stage)
                at += 1
        order += pending
    return order


PIPELINE_MARK = {"passed": "passed", "failed": "failed", "running": "pending", "pending": "pending"}


def change_marks(changes: tuple[board_sources.Change, ...]) -> tuple[columns.Mark, ...]:
    return tuple(m for c in changes for m in (
        *((columns.Mark(c.pipeline, PIPELINE_MARK[c.pipeline]),) if c.pipeline in PIPELINE_MARK else ()),
        *((columns.Mark("approved", "approved"),) if c.approved else ())))


def merged_today(sources: board_sources.Sources, now: datetime) -> list[board_sources.Change]:
    return sorted((c for c in sources.changes.values() if c.state == "merged" and c.merged_at
                   and c.merged_at.astimezone(now.tzinfo).date() == now.date()), key=lambda c: c.merged_at, reverse=True)


def build_columns(tasks: list[Task], lanes: dict | None, kanban: Kanban | None = None,
                  sources: board_sources.Sources = board_sources.EMPTY, now: datetime | None = None) -> tuple[list[columns.Column], columns.Column]:
    """The Build board: a column per lane stage, a card per task at it, and the tasks with no lane in the footer.
    Sources add each card's changes and marks, and a `main` column of the changes merged today."""
    lanes = lanes or {}
    gates = {g for lane in lanes.values() for g in lane.gates}

    def issue_url(ref: str, cell: str = "") -> str:
        known = sources.issues.get(ref)
        return known.url if known else cell.strip() if cell.strip().startswith(("https://", "http://")) else ""

    def page(cell: str, other: str) -> str:
        """A card name's link: its task page when the issue ends in a number, as Flow rows link, else `other` (#194)."""
        m = TRAILING_NUMBER.search(cell)
        return f"/issues/{int(m[1])}" if m else other

    def change_href(c: board_sources.Change) -> str:
        """A change's own page: the task page of its own first named issue, or its forge link when it names
        none — pages.py serves no route for a change itself (#209)."""
        return page(c.issues[0] if c.issues else "", c.url)

    workers = worker_names(sources)

    def card(task: Task, stage: str | None = None) -> columns.Card:
        changes = task_changes(task, sources)
        url = issue_url(issue_key(task.issue), task.issue)
        issue = issue_key(task.issue) if changes or url else task.issue.strip()
        refs = ((issue, page(issue, url)),) * bool(issue) + tuple((c.ref, change_href(c)) for c in changes)
        marks = change_marks(changes) + ((columns.Mark("drift", "drift"),) if drifts(task, kanban, sources, stage) else ())
        owner = task.shown_owner
        return columns.Card(task.label, task.kind, refs, owner, task.state, marks,
                            flag=columns.Mark("ON HOLD", "hold") if held(task, kanban, stage) else None,
                            href=page(task.issue, next((c.url for c in changes if c.state == "open"), url)),
                            owner_href="/workers" if owner in workers else "")

    laned = [t for t in tasks if t.lane.strip() and t.stage.strip()]
    at = {t.name.strip(): effective_stage(t, sources, lanes) for t in laned}
    order = list(dict.fromkeys(stage_order(lanes) + list(at.values())))
    # No PR column: a card at `pr` draws in the stage after it, normally review.
    into = {"pr": order[order.index("pr") + 1]} if "pr" in order[:-1] else {}
    order = [s for s in order if s not in into]
    cols = [columns.Column(stage, tuple(card(t, at[t.name.strip()]) for t in laned if into.get(at[t.name.strip()], at[t.name.strip()]) == stage),
                           *(("gate", "gate") if stage in gates else ())) for stage in order]
    merged = merged_today(sources, now) if now else []
    if merged:
        cards = tuple(columns.Card(c.title, "merged today", tuple((i, page(i, issue_url(i))) for i in c.issues[:1]) + ((c.ref, change_href(c)),),
                                   f"merged {c.merged_at.astimezone(now.tzinfo):%H:%M}", "done",
                                   href=change_href(c)) for c in merged)
        at = next((i for i, c in enumerate(cols) if c.name == "main"), None)
        if at is None:
            cols.append(columns.Column("main", cards))
        else:
            cols[at] = replace(cols[at], cards=cols[at].cards + cards)
    return cols, columns.Column("no lane", tuple(card(t) for t in tasks if not (t.lane.strip() and t.stage.strip())))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# The panels' and the new marks' colours are the board's own tokens, so the dark scheme reaches them.
PANEL_TOKENS = {"ink": "var(--fg)", "muted": "var(--muted)", "card": "var(--surface)", "rule": "var(--line)",
                "edge": "var(--brass)", "link": "var(--brass)", "hot": "var(--dl)", "hot-ink": "var(--surface)"}
PANEL_COLOURS = {"blocked": "var(--dl)", "decisions": "var(--brass)", "approved": "var(--stage-review)",
                 "running": "var(--stage-implement)", "drift": "var(--dl)", "orphaned": "var(--muted)",
                 "merge": "var(--stage-review)", "workers": "var(--stage-implement)"}
CARD_COLOURS = {**columns.PALETTE, "merged today": columns.PALETTE["done"],
                "approved": ("var(--stage-review)", "var(--surface)", "1px solid var(--stage-review)"),
                "passed": ("var(--surface)", "var(--fg)", "1px solid var(--stage-review)"),
                "failed": ("var(--surface)", "var(--dl)", "1px solid var(--dl)"),
                "pending": ("var(--surface)", "var(--fg)", "1px solid var(--stage-pr)"),
                "drift": ("var(--surface)", "var(--dl)", "1px dashed var(--dl)")}


MERGE_SHOWN = 5


def merge_order(sources: board_sources.Sources) -> panels.Panel:
    """Open changes in the order to merge them: approved first, then in review; within each, a passed pipeline
    first, then those sharing no file with another open change (#75's least-conflict order, by file overlap)."""
    # ponytail: overlap by file names; #75's trial merge (`git merge-tree`) when overlap proves too coarse.
    open_ = [c for c in sources.changes.values() if c.state == "open"]
    shared = {c.ref: sorted({o.ref for o in open_ if o is not c and set(o.files) & set(c.files)}) for c in open_}
    overlap = {c.ref: len({f for o in open_ if o is not c for f in set(o.files) & set(c.files)}) for c in open_}

    def number(c: board_sources.Change) -> int:
        m = TRAILING_NUMBER.search(c.ref)
        return int(m[1]) if m else 0

    ready = sorted((c for c in open_ if not c.draft),
                   key=lambda c: (not c.approved, c.pipeline != "passed", bool(shared[c.ref]), number(c)))
    rows = tuple(panels.Row(c.title, " · ".join([
        "approved" if c.approved else "in review",
        *([f"pipeline {c.pipeline}"] if c.pipeline else []),
        *([f"base {c.base}"] if c.base else []),
        f"shares {_plural(overlap[c.ref], 'file')} with {', '.join(shared[c.ref])}" if shared[c.ref] else "no shared files",
    ]), num=str(i), ref=c.ref, href=c.url) for i, c in enumerate(ready, 1))
    approved = sum(c.approved for c in ready)
    rest = len(rows) - MERGE_SHOWN
    return panels.Panel("merge", "MERGE ORDER", "merge", len(rows), rows[:MERGE_SHOWN],
                        f"{approved} approved · {len(rows) - approved} in review", foot=f"+ {rest} more" if rest > 0 else "")


def decision_panel(pending: list[tuple[str, dict | None, datetime | None, str]], answered: int, now: datetime) -> panels.Panel:
    """The pending decisions (decision_page.entries, as decisions.html lists them) and how many were answered today."""
    when = decision_page.Context(now).when
    rows = tuple(panels.Row(f"decision-{slug}.json", f"unreadable: {why}") if d is None else panels.Row(
        d["headline"], " · ".join([*([f"asked {when(asked)}"] if asked else []),
                                   f"recommended {d['recommended']}", f"default: {d['default']}"]), href=f"/decisions/{slug}")
        for slug, d, asked, why in pending)
    return panels.Panel("decisions", "DECISIONS", "decisions", sum(d is not None for _, d, _, _ in pending), rows,
                        link=("decisions page", "/decisions"), foot=f"{answered} answered today")


def worker_panel(sources: board_sources.Sources, now: datetime) -> panels.Panel:
    def facts(w: board_sources.Worker) -> str:
        # (#199) kind · runtime model · task · tree, skipping empty parts; no last-reply fallback.
        return " · ".join(x for x in (w.kind, " ".join(x for x in (w.runtime, w.model) if x), w.task, w.tree) if x)

    rows = tuple(panels.Row(w.name, facts(w), side=panels.hm(w.started, now) if w.started else "", href="/workers")
                for w in sources.workers)
    kinds = [w.kind for w in sources.workers]
    note = " · ".join(f"{kinds.count(k)} {k}" for k in ("one-shot", "session") if k in kinds)  # (#199) one-shots first
    return panels.Panel("workers", "WORKERS", "workers", len(rows), rows, note, href="/workers")  # (#195)


def flow_tiles(blocked: int, decisions: int, sources: board_sources.Sources, running: int, drift: int, orphaned: int) -> list[panels.Tile]:
    """Six counts, each linking to where its items are listed. RUNNING counts workers, or running tasks when no
    worker source is read."""
    approved = sum(c.state == "open" and c.approved for c in sources.changes.values())
    return [panels.Tile("BLOCKED", blocked, "flow", "blocked"), panels.Tile("DECISIONS", decisions, "decisions", "decisions"),
            panels.Tile("APPROVED", approved, "merge", "approved"),
            panels.Tile("RUNNING", len(sources.workers) or running, "workers", "running"),
            panels.Tile("DRIFT", drift, "flow", "drift"), panels.Tile("ORPHANED", orphaned, "flow", "orphaned")]


def day_strip(tasks: list[Task], cfg: Config, now: datetime, est: dict[str, Estimate] | None = None,
              body_events: list[BodyEvent] | None = None) -> str:
    """The 24 HOURS section: a gantt chart of active tasks on a now → now+24h window, grouped by swimlane.

    Each bar carries `task.label` as its visible name (the gantt-name span); the full detail is
    in the bar's title attribute, shown by CSS :hover / :focus-within. This is what the design
    means by 'hover panel built from the data-* attributes'.

    When `body_events` are given, body events (sleep, eat, gym, recreation) that overlap the 24h
    window are drawn as the strip's first row, above the task swimlanes.
    """
    import gantt
    active = [t for t in tasks if t.kind != "done"]
    if not active and not body_events:
        return ""
    bars = [day_bar(t, cfg, now, est) for t in active]
    win = gantt.window(now, timedelta(0), timedelta(hours=24))
    # Body lane: the first row, above the deadline swimlanes.
    body_rows: list[gantt.Row] = []
    if body_events:
        on_axis = sorted((e for e in body_events if e.end > win.start and e.start < win.end),
                         key=lambda e: e.start)
        if on_axis:
            segs = tuple(
                gantt.Segment(e.start, e.end, e.kind, "done",
                              f"{e.kind} {e.start:%H:%M}–{e.end:%H:%M}")
                for e in on_axis
            )
            body_rows.append(gantt.Row("", "body", "", segs))
    groups = swimlanes(bars, cfg, now)
    rows: list[gantt.Row] = []
    for dl_name, group_bars in groups:
        for bar in group_bars:
            seg = gantt.Segment(bar.start, bar.end, bar.kind, bar.kind,
                                f"{bar.kind} {bar.start:%H:%M}–{bar.end:%H:%M}")
            est_label = f" · {bar.label}" if bar.label and bar.label != NO_ESTIMATE else ""
            note = f"{bar.kind}{est_label}"
            rows.append(gantt.Row(bar.owner, bar.name, note, (seg,)))
    all_rows = body_rows + rows
    if not all_rows:
        return ""
    meta = _tasks(len(active))
    chart = gantt.render(all_rows, win)
    return (f'<section id="day-strip"><h2>24 hours</h2>\n<div class="meta">{meta}</div>\n'
            f'{chart}</section>\n')


def due_next_section(tasks: list[Task], cfg: Config, now: datetime) -> str:
    """DUE NEXT: thin section listing tasks due within the nearest deadline. Stage 3 thickens this."""
    nearest = nearest_deadline(cfg, now)
    due = [t for t in tasks if t.kind != "done"
           and _resolve_due(t.due, cfg, now.date()) is not None
           and _resolve_due(t.due, cfg, now.date()) <= nearest.at]
    count = len(due)
    names = ", ".join(t.label for t in due[:5])
    meta = f'{count} {"task" if count == 1 else "tasks"}{" · " + names if names else ""}'
    return f'<section id="due-next"><h2>Due next</h2>\n<div class="meta">{_esc(meta)}</div>\n</section>\n'


def blocked_section(tasks: list[Task], kanban: Kanban | None = None, sources: board_sources.Sources = board_sources.EMPTY) -> str:
    """BLOCKED: thin section counting blocked/waiting tasks. Stage 3 thickens this with sub-categories."""
    blocked = [t for t in tasks if t.kind in ("waiting", "orphaned")]
    count = len(blocked)
    names = ", ".join(t.label for t in blocked[:5])
    meta = f'{count} {"task" if count == 1 else "tasks"}{" · " + names if names else ""}'
    return f'<section id="blocked"><h2>Blocked</h2>\n<div class="meta">{_esc(meta)}</div>\n</section>\n'


def sessions_section(sessions: tuple[Session, ...]) -> str:
    """SESSIONS: the tracker's session table. Thin for the bullet — names and states."""
    if not sessions:
        return '<section id="sessions"><h2>Sessions</h2>\n<div class="meta">0 sessions</div>\n</section>\n'
    rows = []
    for s in sessions:
        state = s.kind
        doing = _esc(s.doing[:60]) if s.doing.strip() else ""
        rows.append(f'<div class="session-row"><span class="session-name">{_esc(s.label)}</span>'
                    f'<span class="session-state">{_esc(state)}</span>'
                    f'<span class="session-doing">{doing}</span></div>')
    meta = f'{len(sessions)} {"session" if len(sessions) == 1 else "sessions"}'
    return (f'<section id="sessions"><h2>Sessions</h2>\n<div class="meta">{meta}</div>\n'
            + "\n".join(rows) + "\n</section>\n")


LANE_TABLE_CSS = """\
.lane-table{width:100%;border-collapse:collapse;font-size:13px}
.lane-table th{text-align:left;font:600 11px "Cormorant SC",Georgia,serif;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);padding:4px 6px}
.lane-table td{padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:baseline}
.lane-table .lt-name{font-weight:500}
.lane-table .lt-owner{color:var(--muted);font-size:12px}
.lane-table .lt-state{font-size:11px;letter-spacing:.04em;text-transform:uppercase}
.lane-table .lt-lane,.lane-table .lt-stage{font-size:12px;color:var(--muted)}
.lt-hold{font-size:10px;font-weight:700;letter-spacing:.1em;color:var(--dl);margin-left:6px}
"""


def _body_css() -> str:
    """CSS rules for the body lane's four kinds, keyed by ``data-cat`` on the gantt bars."""
    from fragment import colour as _colour, css_str as _css_str
    rules = [f'.gantt-bar[data-cat="{_css_str(k)}"]{{--c:{_colour(v)};--e:{_colour(v)}}}' for k, v in BODY_FILLS.items()]
    return "\n".join(rules) + "\n"


def lane_table(tasks: list[Task], lanes: dict | None = None,
               held_tasks: frozenset[Task] | None = None) -> str:
    """The LANES section body: one HTML table, one row per active (non-done, non-standing) task.

    Unassigned rows carry ``data-unassigned``; owned-but-not-running rows carry ``data-queued``.
    A task in `held_tasks` shows ``ON HOLD`` — the same flag the column board
    carried.  The ``no lane`` footer is gone — an unlaned task is simply a row with empty
    lane/stage cells.
    """
    shown = [t for t in tasks if not t.standing]
    held_set = held_tasks or frozenset()
    if not shown:
        return '<table class="lane-table"><tbody></tbody></table>'
    rows: list[str] = []
    rows.append('<table class="lane-table">')
    rows.append("<thead><tr><th>name</th><th>owner</th><th>state</th><th>lane</th><th>stage</th></tr></thead>")
    rows.append("<tbody>")
    for task in shown:
        attrs: list[str] = []
        owner = task.shown_owner
        if task.kind != "done":
            if not owner:
                attrs.append("data-unassigned")
            elif task.kind != "running":
                attrs.append("data-queued")
        attr_str = (" " + " ".join(attrs)) if attrs else ""
        lane_cell = _esc(task.lane.strip())
        stage_cell = _esc(task.stage.strip())
        state_label = task.state.strip() if task.state.strip() else task.kind
        hold = ' <span class="lt-hold">ON HOLD</span>' if task in held_set else ""
        rows.append(
            f'<tr data-state="{_esc(task.kind)}"{attr_str}>'
            f'<td class="lt-name">{_esc(task.label)}{hold}</td>'
            f'<td class="lt-owner">{_esc(owner)}</td>'
            f'<td class="lt-state">{_esc(state_label)}</td>'
            f'<td class="lt-lane">{lane_cell}</td>'
            f'<td class="lt-stage">{stage_cell}</td>'
            f'</tr>')
    rows.append("</tbody></table>")
    return "\n".join(rows)


def render(tracker_text: str, cfg: Config, now: datetime, lanes: dict | None = None,
           tracker_day: date | None = None, decisions: list[tuple[str, dict | None, datetime | None, str]] | None = None,
           kanban: Kanban | None = None, sources: board_sources.Sources = board_sources.EMPTY, answered: int = 0,
           tracker_at: datetime | None = None, stage_log: list[tuple[date, str]] | None = None, slots: int = 1,
           log_text: str | None = None) -> str:
    """The board page: header, tiles, DUE NEXT, BLOCKED, 24 HOURS, LANES, SESSIONS, the MERGE ORDER,
    DECISIONS and WORKERS panels, and the Flow charts. `stage_log` is (day, tracker text) for the
    earlier days the Flow charts reach back over; `slots` is how many queued tasks run at once,
    `[workers] max_concurrency`. `log_text` is the daily log text whose `## Calendar` section
    provides body events for the 24h strip."""
    cfg = with_decision_deadlines(cfg, tracker_text, tracker_day or now.date())
    sha = hashlib.sha256(tracker_text.encode()).hexdigest()
    tracker = parse_tracker(tracker_text)
    today, zone = now.date(), cfg.zone
    # A standing session with no task is not a task.
    tasks = [task for task in tracker.tasks if not task.standing]
    active = [task for task in tasks if task.kind != "done"]
    nearest = nearest_deadline(cfg, now)
    hist = history(tasks, cfg, now)
    est = estimates(active, cfg, now, hist)

    unowned = [task for task in active if task.kind == "orphaned"]
    running = [task for task in active if task.kind == "running"]

    issues = sum(1 for t in tasks if t.issue.strip())
    changes = sum(1 for c in sources.changes.values() if c.state == "open")
    changes_note = f" · {_plural(changes, 'merge request')}" if changes else ""
    no_lane_count = sum(1 for t in tasks if not t.standing and not (t.lane.strip() and t.stage.strip()))
    workers = worker_names(sources)
    # A task's stage for drift/hold purposes, computed once, keyed by the task: every nameless task shares the blank name.
    stages = {t: effective_stage(t, sources, lanes or {}) for t in tasks}
    # Held: kanban hold or a waiting task owned by a person (not a worker).
    table_held = frozenset(t for t in tasks if held(t, kanban, stages[t]))
    lanes_html = (f'<section id="flow"><h2>Lanes</h2>\n<div class="meta">{_tasks(len(tasks))} · {_plural(issues, "issue")}{changes_note}</div>\n'
                  f'{lane_table(tasks, lanes, held_tasks=table_held)}</section>\n')
    flow_moves = [m for day, text in [*(stage_log or []), (tracker_day or today, tracker_text)]
                  for m in tracker_log.moves(text, day, zone)]
    known = [t for _, text in stage_log or [] for t in parse_tracker(text).tasks] + tasks
    ends = {t.item: end for t in active if (end := _end(t, cfg, now, now, est))[1] in ("due", "derived")}
    # #227: a `waiting` task owned by a person, not a worker, holds too — the same worker/person split
    # the lane table already draws its "ON HOLD" flag and owner link from.
    # flow_chart names its rows' tasks, and a nameless task has no name to hold it by (as forge_ends).
    flow_held = {t.name.strip() for t in tasks if t.name.strip() and
                (held(t, kanban, stages[t]) or
                 (t.kind == "waiting" and t.shown_owner and t.shown_owner not in workers))}
    flow_rows = flow_chart.build(flow_moves, known, lanes or {}, flow_held,
                                 {item: end[0] for item, end in ends.items()}, now,
                                 queue_durations(active, hist), slots,
                                 frozenset(t.name.strip() for t in tasks
                                           if t.name.strip() and any(c.state == "open" and c.approved for c in task_changes(t, sources))),
                                 ended=forge_ends(tasks, sources, zone))
    # An issue's row links to its page, pages.py's /issues/<n>.
    flow_html = flow_chart.section([(s, replace(r, href=f"/issues/{int(m[1])}") if (m := TRAILING_NUMBER.search(r.ref)) else r)
                                    for s, r in flow_rows], now)

    pending = decisions or []
    meta = [f"rendered {now:%H:%M} {now:%Z}", f"tracker {(tracker_at or now).astimezone(zone):%H:%M}",
            *(f"{k} {sources.fetched[k].astimezone(zone):%H:%M}" for k in ("kanban", "merge requests", "workers") if k in sources.fetched),
            _tasks(len(tasks)), f"{no_lane_count} in no lane"]
    head = panels.Header(f"Board · {now.strftime('%a %d %b')}", tuple(meta), now, nearest.name, nearest.at,
                         tuple(f"{k}: {why}" for k, why in sources.errors.items()))
    # BLOCKED counts the ON HOLD cards (Flow.dc.html v22), by the flag that draws them.
    tiles = flow_tiles(sum(held(t, kanban, stages[t]) for t in tasks), sum(d is not None for _, d, _, _ in pending),
                       sources, len(running), sum(drifts(t, kanban, sources, stages[t]) for t in tasks), len(unowned))
    panels_html = panels.panels([merge_order(sources), decision_panel(pending, answered, now), worker_panel(sources, now)])

    # --- new sections (Stage 0: thin, proving the order) ---
    import gantt
    due_next_html = due_next_section(tasks, cfg, now)
    blocked_html = blocked_section(tasks, kanban, sources)
    # Body lane (Stage 2): calendar events classified as body (sleep, eat, gym, recreation)
    # become the first row of the 24h strip. Only body events are drawn; non-body events stay out.
    body_events: list[BodyEvent] = []
    if log_text:
        all_cal = parse_calendar(log_text, today, zone)
        body_events = [e for e in all_cal if e.kind]
    day_strip_html = day_strip(tasks, cfg, now, est, body_events=body_events or None)
    sessions_html = sessions_section(tracker.sessions)

    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Board {today.isoformat()}</title>
<link rel="stylesheet" href="{FONTS}">
<meta name="tracker-sha256" content="{sha}">
<style>
:root{{--bg:#f4efe1;--surface:#fbf8ef;--fg:#2f2630;--muted:#6e6470;--line:#ddd3bd;--brass:#a7843e;--dl:#c9533a;--stage-implement:#0072B2;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#000000}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark;--bg:#1c1813;--surface:#262019;--fg:#efe6d2;--muted:#b3a791;--line:#3d352a;--brass:#d4b06a;--dl:#f08566;--stage-implement:#3D95D6;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#efe6d2}}:root:not([data-theme="light"]) :is(.gantt,.gantt-legend){{--gantt-ink:var(--fg);--gantt-muted:var(--muted);--gantt-bg:var(--bg);--gantt-rule:var(--line);--gantt-edge:var(--line);--gantt-grid:var(--line);--gantt-past:var(--surface);--gantt-now:var(--dl);--gantt-hold:var(--dl)}}}}
:root[data-theme="dark"]{{color-scheme:dark;--bg:#1c1813;--surface:#262019;--fg:#efe6d2;--muted:#b3a791;--line:#3d352a;--brass:#d4b06a;--dl:#f08566;--stage-implement:#3D95D6;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#efe6d2}}
:root[data-theme="dark"] :is(.gantt,.gantt-legend){{--gantt-ink:var(--fg);--gantt-muted:var(--muted);--gantt-bg:var(--bg);--gantt-rule:var(--line);--gantt-edge:var(--line);--gantt-grid:var(--line);--gantt-past:var(--surface);--gantt-now:var(--dl);--gantt-hold:var(--dl)}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 "Alegreya Sans","Gill Sans",system-ui,sans-serif;padding:16px 16px 48px;max-width:1280px;margin:0 auto}}
h1{{font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-size:26px;font-weight:600;letter-spacing:.04em;margin:0 0 2px}}
h2{{display:inline-block;font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-size:17px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;margin:28px 10px 8px 0}}
h2+.meta{{display:inline-block}}
.header{{display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap;margin-bottom:4px}}
.header>.panels-head{{flex:1 1 320px;min-width:0}}
.meta{{color:var(--muted);font-size:12px}}
{TAB_CSS}.board>nav.tabs{{margin-bottom:12px}}
{gantt.css() if day_strip_html else ""}{_body_css() if body_events else ""}{flow_chart.css() if flow_html else ""}{panels.css(PANEL_COLOURS, PANEL_TOKENS)}{LANE_TABLE_CSS}
.session-row{{display:flex;gap:12px;align-items:baseline;font-size:13px;padding:2px 0}}.session-name{{font-weight:500}}.session-state{{color:var(--muted);font-size:12px}}.session-doing{{color:var(--muted);font-size:12px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
@media (max-width:420px){{body{{padding:12px 12px 36px}}}}
</style>
<body>
<div class="board" data-rendered-at="{_iso(now)}" data-tz="{_esc(cfg.tz)}" data-deadline="{_iso(nearest.at)}" data-deadline-name="{_esc(nearest.name)}">
{tab_bar("Board", sum(d is not None for _, d, _, _ in pending))}<div class="header">{panels.header(head)}</div>
{panels.tiles(tiles)}
{due_next_html}{blocked_html}{day_strip_html}{lanes_html}{sessions_html}{panels_html}
{flow_html}</div>
<script>
(function(){{
  var root=document.querySelector('.board'),tz=root.dataset.tz,dl=new Date(root.dataset.deadline);
  function hms(d){{return new Intl.DateTimeFormat('en-GB',{{timeZone:tz,hour:'2-digit',minute:'2-digit',second:'2-digit'}}).format(d);}}
  function pad(n){{return (n<10?'0':'')+n;}}
  function tick(){{
    var now=new Date(),ms=dl-now,sign=ms<0?'-':'',s=Math.floor(Math.abs(ms)/1000);
    root.querySelector('.panels-now').textContent=hms(now);
    root.querySelector('.panels-left').textContent=sign+Math.floor(s/3600)+'h'+pad(Math.floor(s/60)%60)+'m'+pad(s%60)+'s';
  }}
  tick();setInterval(tick,1000);
}})();
</script>
</body>
"""


# --- cli -----------------------------------------------------------------------------------------


def write(root: Path, day: str | None = None) -> tuple[Path, Config, datetime, str, dict]:
    """Write `<day>-board.html` and `decisions.html` into the pages dir. The page server calls this in
    process; a missing CLAUDE.md, tracker or setting raises ConfigError."""
    if not (root / "CLAUDE.md").is_file():
        raise ConfigError(f"no CLAUDE.md at {root}")
    cfg = read_config(root)
    now = datetime.now(cfg.zone).replace(microsecond=0)
    day = day or now.date().isoformat()
    try:
        tracker_day = date.fromisoformat(day)
    except ValueError:
        raise ConfigError(f"invalid date {day!r}; use YYYY-MM-DD") from None
    tracker = root / cfg.tracker_path(day)
    if not tracker.is_file():
        raise ConfigError(f"tracker not found at {tracker}")
    tracker_text = tracker.read_text()
    cfg = with_workspace_decision_deadlines(root, cfg, tracker_text, tracker_day)
    out = (root / cfg.pages_dir if cfg.pages_dir else tracker.parent) / f"{day}-board.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    req_texts = {d.name: ((root / d.requirements).read_text() if (root / d.requirements).is_file() else None) for d in cfg.deadlines if d.requirements}
    try:
        settings = load_settings(root, cfg.settings_path)
    except SettingsError as e:
        raise ConfigError(str(e)) from None
    pending = decision_page.write_all(out.parent, decision_page.decision_context(root, tracker_day, now), day, root)
    reach = (now - flow_chart.WINDOWS[-1][1]).date()
    stage_log = [(d, path.read_text()) for d, path in daily_trackers(root, cfg) if reach <= d < tracker_day]
    # Body lane (Stage 2): read the daily log for its Calendar section.
    log_file = root / cfg.log_path(day)
    log_text = log_file.read_text() if log_file.is_file() else None
    page = render(tracker_text, cfg, now, lanes=settings.lanes,
                  tracker_day=tracker_day, decisions=pending, kanban=settings.kanban,
                  sources=board_sources.load(out.parent), answered=len(decision_rows(tracker_text)),
                  tracker_at=datetime.fromtimestamp(tracker.stat().st_mtime, cfg.zone), stage_log=stage_log,
                  slots=settings.workers.max_concurrency or 1, log_text=log_text)
    out.write_text(page)
    return out, cfg, now, tracker_text, req_texts


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    args = ap.parse_args(argv)
    try:
        out, cfg, now, tracker_text, req_texts = write(Path(args.root), args.date)
    except ConfigError as e:
        sys.exit(f"render_board: {e}")
    parsed = parse_tracker(tracker_text)
    hist = history(list(parsed.tasks), cfg, now)
    est = estimates([task for task in parsed.tasks if task.kind != "done"], cfg, now, hist)
    bars = [day_bar(task, cfg, now, est) for task in parsed.tasks]
    sized = " ".join(f"{k or '?'}:{hist[k][1]}" for k in (*SIZES, "") if k in hist)
    hist_note = f"{sized} · all:{hist[ALL_SIZES][1]}" if hist else "none"
    horizon_name = horizon(cfg, now)[1]
    no_est = sum(1 for b in bars if b.label == NO_ESTIMATE)
    derived = sum(1 for b in bars if b.end_src == "derived")
    long_items = sum(1 for task in parsed.tasks if len(task.item) > LONG_ITEM)
    block = parse_resume(tracker_text)
    resume_long = sum(1 for k in resume_fields(block) if len(block[k]) > RESUME_CAP)
    # A count printed beside a warning count but not counted as one reads as telemetry: `resume_long=4`
    # sat next to `warnings=0` for three hours while the block degraded, and was read past every time.
    warnings = sum(1 for task in parsed.tasks if task.warning) + sum(1 for s in parsed.sessions if s.warning) + resume_long
    print(out)
    req_counts = ",".join(f"{name}:{sum(r.done for _, rs in parse_requirements(t) for r in rs)}/{sum(len(rs) for _, rs in parse_requirements(t))}" for name, t in req_texts.items() if t is not None)
    missing = sum(1 for t in req_texts.values() if t is None)
    drawn = resume_fields(block)
    resume_note = f"{len(drawn)} fields" if drawn else "none"
    print(f"tasks={len(parsed.tasks)} no_estimate={no_est} derived={derived} warnings={warnings} long_items={long_items} resume={resume_note} resume_long={resume_long} requirements={req_counts or 'none'} requirements_missing={missing} history={hist_note} horizon={horizon_name} tracker_sha256={hashlib.sha256(tracker_text.encode()).hexdigest()[:12]}")
    return out


if __name__ == "__main__":
    main()
