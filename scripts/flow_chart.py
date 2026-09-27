"""The board's Flow charts: each task's stage history, read from the tracker's `stage:` Log lines, drawn by gantt.py.

`chief-of-stuff log --stage` writes `- HH:MM stage: <name> → <stage>`. A segment runs from each move to the
next and the last to now; a lane's last stage ends the row. A held task is hatched from now through the
window, and an open task with an estimated end draws its remaining stages ahead as forecast. An open task at
its lane's first stage with no move yet is queued: forecast only, after the running forecasts, in worker slots.
Rows that share a tracker issue (a review loop's build/Review/Fix/Verify passes) merge into one row.
Until a task's first `stage:` move, it moves on the one-shot launcher's lines: started → implement, completed → pr,
HUMAN REVIEW NEEDED → review. A done task's last segment ends at its done time, not now.
"""

from __future__ import annotations

import functools
import heapq
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import gantt
from fragment import esc

LINE = re.compile(r"^- (\d{1,2}):(\d{2}) stage: (.+) → (\S+)\s*$")
STARTED = re.compile(r"^- (\d{1,2}):(\d{2}) one-shot (\S+) started: .*, task (.+?)\s*$")
ENDED = re.compile(r"^- (\d{1,2}):(\d{2}) one-shot (\S+): (completed; awaiting integration|HUMAN REVIEW NEEDED)\b")
REF = re.compile(r"(?<!\w)[#!]\d+")
H = timedelta(hours=1)
# (title, behind now, ahead of now), after the Flow artboard.
WINDOWS = (("24 hours", 6 * H, 18 * H), ("7 days", 48 * H, 120 * H))
COLOURS: dict[str, str | tuple[str, str]] = {stage: f"var(--stage-{stage})" for stage in gantt.PALETTE}
COUNTS = (("merged", "merged"), ("running", "running"), ("held", "held"), ("open", "queued"))


@dataclass(frozen=True)
class Move:
    at: datetime
    name: str
    stage: str
    launch: bool = False
    line: str = field(default="", compare=False)  # the Log line it was read from
    worker: str = field(default="", compare=False)  # a launcher move's worker


def moves(text: str, day: date, zone: ZoneInfo) -> list[Move]:
    """The `stage:` and one-shot launcher lines of the tracker's `## Log`, stamped on `day`. A launcher move names
    the task key of its worker's latest started line."""
    log = next((part for part in re.split(r"(?m)^## ", text) if part.split("\n", 1)[0].strip() == "Log"), "")
    out, task_of = [], {}
    for line in log.splitlines():
        line = line.strip()
        m = LINE.match(line) or STARTED.match(line) or ENDED.match(line)
        if not m or int(m[1]) >= 24 or int(m[2]) >= 60:
            continue
        at = datetime.combine(day, time(int(m[1]), int(m[2])), tzinfo=zone)
        if m.re is LINE:
            out.append(Move(at, m[3].strip(), m[4], line=line))
        elif m.re is STARTED:
            task_of[m[3]] = m[4]
            out.append(Move(at, m[4], "implement", True, line, m[3]))
        elif m[3] in task_of:
            out.append(Move(at, task_of[m[3]], "pr" if m[4].startswith("completed") else "review", True, line, m[3]))
    return out


def launch_row(text: str, tasks):
    """(row, name) for a one-shot's launch-time task text: the row whose item the text begins with, else the row
    whose issue ref it carries (later rows win), and that row's name; with no row, None and the text's first line
    cut to 80 characters. The launch keeps its text; the row's item may change after it (#182)."""
    return launcher(tasks)(text)


def launcher(tasks):
    """launch_row over `tasks`, indexed once: items by length, `#N`/`!N` issues by ref, and each text looked up once
    (#184). An issue cell of another shape (`group/app#118`, a URL) keeps its own search."""
    items, refs, other = {}, {}, []
    for i, t in enumerate(tasks):
        if item := t.item.strip():
            items[item] = (i, t)
        if issue := t.issue.strip():
            if REF.fullmatch(issue):
                refs[issue] = (i, t)
            else:
                other.append(((i, t), re.compile(rf"(?<!\w){re.escape(issue)}(?!\d)")))
    lengths = {len(k) for k in items}

    @functools.cache
    def row(text: str):
        text = text.strip()
        first = text.split("\n", 1)[0][:80]
        for hits in ([items.get(text[:n]) for n in lengths],
                     [refs.get(r) for r in REF.findall(text)] + [hit for hit, p in other if p.search(text)]):
            if hits := [h for h in hits if h]:
                t = max(hits, key=lambda h: h[0])[1]
                return t, t.name.strip() or first
        return None, first
    return row


def _clock(at: datetime, now: datetime) -> str:
    return f"~{at:%H:%M}" if at.date() == now.date() else f"{at:%a}"


def _ahead(stages: list[str], start: datetime, end: datetime, title: str = "") -> list[gantt.Segment]:
    # ponytail: the estimate split evenly over the stages left; per-stage durations once the Log has enough of them.
    step = (end - start) / len(stages)
    return [gantt.Segment(start + step * i, start + step * (i + 1), s, "forecast", title) for i, s in enumerate(stages)]


def build(log: list[Move], tasks, lanes: dict, held: set[str], ends: dict[str, datetime], now: datetime,
          durations: dict[str, timedelta] | None = None, slots: int = 1,
          approved: frozenset[str] = frozenset(), merged: dict[str, datetime] | None = None) -> list[tuple[str, gantt.Row]]:
    """(status, row) per task with a move, first moved first, then the queued tasks in tracker order. `tasks` are
    tracker rows, later ones winning; `held` names the held tasks; `ends` maps a task's item to its estimated end;
    `durations` maps a queued task's item to how long it will take, and `slots` is how many run at once;
    `approved` names the tasks whose open change is approved, labelled `approved · merge ~HH:MM` by their end;
    `merged` maps a task's name to its change's forge merge time, which ends its row. Status is `merged`, `held` or
    the task's state word. Every bar's title names the worker whose launch was running when it began (now, for a
    forecast or hold bar), else the task's owner cell, shown as the board shows it (#192, #193)."""
    by_name = {t.name.strip().casefold(): t for t in tasks if t.name.strip()}
    # A move names a task by its name or its item; the name wins.
    find = {**{t.item.strip().casefold(): t for t in tasks if t.item.strip()}, **by_name}
    row_of = launcher(tasks)

    def key_of(m: Move) -> str:
        t = find.get(m.name.strip().casefold()) or (row_of(m.name)[0] if m.launch else None)
        return t.name.strip().casefold() if t and t.name.strip() else m.name.casefold()

    terminal = {lane.stages[-1] for lane in lanes.values()}
    horizon = now + WINDOWS[-1][2]
    busy: list[datetime] = []
    log = sorted(log, key=lambda m: m.at)
    first, runs = {}, {}  # runs: key → [worker, start, end or None] per launch
    for m in log:
        key = key_of(m)
        if not m.launch:
            first.setdefault(key, m.at)
        elif m.stage == "implement":
            runs.setdefault(key, []).append([m.worker, m.at, None])
        elif run := next((r for r in reversed(runs.get(key, ())) if r[0] == m.worker and r[2] is None), None):
            run[2] = m.at

    def owner(key: str, at: datetime, task) -> str:
        running = [w for w, start, end in runs.get(key, ()) if start <= at and (end is None or at < end)]
        return running[-1] if running else task.shown_owner if task else ""

    log = [m for m in log if not (m.launch and (f := first.get(key_of(m))) and m.at >= f)]
    out, groups = [], {}
    for m in log:
        groups.setdefault(key_of(m), []).append(m)
    for key, mine in groups.items():
        task, last = find.get(key) or find.get(mine[-1].name.strip().casefold()), mine[-1]
        stop = now
        if task and task.kind == "done" and (t := task.state_time):
            stop = min(now, datetime.combine(now.date(), time.fromisoformat(t.zfill(5)), tzinfo=now.tzinfo))
        merge = task and (merged or {}).get(task.name.strip())
        segs = [gantt.Segment(m.at, nxt.at if nxt else stop, m.stage, "done", owner(key, m.at, task))
                for m, nxt in zip(mine, mine[1:] + [None]) if m.stage not in terminal]
        if merge:
            segs = [replace(g, end=min(g.end, merge)) for g in segs if g.start < merge]
        lane = lanes.get(task.lane.strip()) if task else None
        if merge:
            status, note = "merged", f"merged {merge:%H:%M}"
        elif last.stage in terminal:
            status, note = "merged", f"merged {last.at:%H:%M}"
        elif task and task.name.strip() in held:
            segs.append(gantt.Segment(now, now + WINDOWS[-1][2], last.stage, "hold", owner(key, now, task)))
            status, note = "held", f"hold · {last.stage}"
        elif task and lane and last.stage in lane.stages and (end := ends.get(task.item)) and end > now:
            ahead = [last.stage] + [s for s in lane.stages[lane.stages.index(last.stage) + 1:] if s not in terminal]
            segs += _ahead(ahead, now, end, owner(key, now, task))
            busy.append(end)
            ok = task.name.strip() in approved
            status, note = task.kind, f"approved · merge {_clock(end, now)}" if ok else f"{last.stage} · {_clock(end, now)}"
        else:
            status, note = (task.kind if task else ""), "approved" if task and task.name.strip() in approved else last.stage
        out.append((status, gantt.Row(task.issue.strip() if task else "", task.name.strip() if task else launch_row(last.name, ())[1],
                                      note, tuple(segs))))
    # Each slot is free once the forecast holding it ends; more forecasts than slots wait for the (n-k+1)th end.
    slots = max(1, slots)
    busy += [now] * (slots - len(busy))
    heapq.heapify(busy)
    moved = set(groups)
    for t in tasks:
        key, lane = t.name.strip().casefold(), lanes.get(t.lane.strip())
        if (not key or by_name[key] is not t or key in moved or t.kind != "open" or t.name.strip() in held
                or not lane or t.stage.strip() != lane.stages[0] or not (took := (durations or {}).get(t.item))):
            continue
        while len(busy) > slots:
            heapq.heappop(busy)
        start = heapq.heappop(busy)
        if start >= horizon:
            break
        heapq.heappush(busy, start + took)
        stages = [s for s in lane.stages if s not in terminal]
        out.append((t.kind, gantt.Row(t.issue.strip(), t.name.strip(), f"queued · {_clock(start + took, now)}",
                                      tuple(_ahead(stages, start, start + took, owner(key, now, t))))))
    return _merge_issues(out)


def _merge_issues(rows: list[tuple[str, gantt.Row]]) -> list[tuple[str, gantt.Row]]:
    """Rows that share a non-empty ref (the task's issue) merge into one: every segment, sorted by start; the
    name of whichever row started earliest; the status and note of whichever moved last (highest segment start,
    ties keeping the later row); placed where the first of its rows was. A row with no segments (its only move
    was its lane's last stage) still counts toward the group, but only a row with segments can supply the name
    or the status/note, falling back to the first or last row by position when none of the group has any."""
    groups: dict[str, list[int]] = {}
    for i, (_, row) in enumerate(rows):
        if row.ref:
            groups.setdefault(row.ref, []).append(i)
    out, drop = list(rows), set()
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        entries = [rows[i] for i in idxs]
        timed = [e for e in entries if e[1].segments]
        name = min(timed, key=lambda e: e[1].segments[0].start)[1].name if timed else entries[0][1].name
        if timed:
            status, last_row = max(enumerate(timed), key=lambda p: (p[1][1].segments[-1].start, p[0]))[1]
        else:
            status, last_row = entries[-1]
        segs = tuple(sorted((g for _, r in entries for g in r.segments), key=lambda g: g.start))
        out[idxs[0]] = (status, replace(last_row, name=name, segments=segs))
        drop.update(idxs[1:])
    return [item for i, item in enumerate(out) if i not in drop]


def _span(win: gantt.Window) -> str:
    if win.end - win.start <= 24 * H:
        return f"{win.start:%a %H:%M} → {win.end:%a %H:%M}"
    return " → ".join(f"{d:%a} {d.day} {d:%H:%M}" for d in (win.start, win.end))


def section(rows: list[tuple[str, gantt.Row]], now: datetime) -> str:
    """Both charts and one legend, each chart with the rows that reach into its window; empty with no rows."""
    out = []
    for title, before, after in WINDOWS:
        win = gantt.window(now, before, after)
        shown = [(s, r) for s, r in rows if any(g.start < win.end and g.end > win.start for g in r.segments)]
        if not shown:
            continue
        counts = " · ".join(f"{sum(s == k for s, _ in shown)} {word}" for k, word in COUNTS)
        out.append(f'<h2>Flow · {title}</h2>\n<div class="meta">{esc(counts)} · {_span(win)}</div>\n'
                   f"{gantt.render([r for _, r in shown], win)}")
    return "\n".join(out + [gantt.legend(COLOURS)]) + "\n" if out else ""


def css() -> str:
    """gantt.py's stylesheet on the board's stage tokens, with a phone layout of the Phone artboard's widths."""
    return gantt.css(COLOURS) + (".gantt-legend .gantt-forecast:not([data-cat]){--c:var(--stage-implement)}\n"
                                 ".gantt-legend{border-top:1px solid var(--gantt-edge);padding-top:5px}\n"
                                 "@media (max-width:520px){.gantt,.gantt-legend{--gantt-label:96px;--gantt-note:0px}"
                                 ".gantt-note,.gantt-ref{display:none}.gantt-axis{font-size:8.5px}}\n")
