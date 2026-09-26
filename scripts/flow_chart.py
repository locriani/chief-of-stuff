"""The board's Flow charts: each task's stage history, read from the tracker's `stage:` Log lines, drawn by gantt.py.

`chief-of-stuff log --stage` writes `- HH:MM stage: <name> → <stage>`. A segment runs from each move to the
next and the last to now; a lane's last stage ends the row. A held task is hatched from now through the
window, and an open task with an estimated end draws its remaining stages ahead as forecast. An open task at
its lane's first stage with no move yet is queued: forecast only, after the running forecasts, in worker slots.
"""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import gantt
from fragment import esc

LINE = re.compile(r"^- (\d{1,2}):(\d{2}) stage: (.+) → (\S+)\s*$")
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


def moves(text: str, day: date, zone: ZoneInfo) -> list[Move]:
    """The `stage:` lines of the tracker's `## Log`, stamped on `day`."""
    log = next((part for part in re.split(r"(?m)^## ", text) if part.split("\n", 1)[0].strip() == "Log"), "")
    out = []
    for line in log.splitlines():
        if (m := LINE.match(line.strip())) and int(m[1]) < 24 and int(m[2]) < 60:
            out.append(Move(datetime.combine(day, time(int(m[1]), int(m[2])), tzinfo=zone), m[3].strip(), m[4]))
    return out


def _clock(at: datetime, now: datetime) -> str:
    return f"~{at:%H:%M}" if at.date() == now.date() else f"{at:%a}"


def _ahead(stages: list[str], start: datetime, end: datetime) -> list[gantt.Segment]:
    # ponytail: the estimate split evenly over the stages left; per-stage durations once the Log has enough of them.
    step = (end - start) / len(stages)
    return [gantt.Segment(start + step * i, start + step * (i + 1), s, "forecast") for i, s in enumerate(stages)]


def build(log: list[Move], tasks, lanes: dict, held: set[str], ends: dict[str, datetime], now: datetime,
          durations: dict[str, timedelta] | None = None, slots: int = 1,
          approved: frozenset[str] = frozenset()) -> list[tuple[str, gantt.Row]]:
    """(status, row) per task with a move, first moved first, then the queued tasks in tracker order. `tasks` are
    tracker rows, later ones winning; `held` names the held tasks; `ends` maps a task's item to its estimated end;
    `durations` maps a queued task's item to how long it will take, and `slots` is how many run at once;
    `approved` names the tasks whose open change is approved, labelled `approved · merge ~HH:MM` by their end.
    Status is `merged`, `held` or the task's state word."""
    by_name = {t.name.strip().casefold(): t for t in tasks if t.name.strip()}
    terminal = {lane.stages[-1] for lane in lanes.values()}
    horizon = now + WINDOWS[-1][2]
    busy: list[datetime] = []
    log = sorted(log, key=lambda m: m.at)
    out = []
    for key in dict.fromkeys(m.name.casefold() for m in log):
        mine = [m for m in log if m.name.casefold() == key]
        task, last = by_name.get(key), mine[-1]
        segs = [gantt.Segment(m.at, nxt.at if nxt else now, m.stage, "done")
                for m, nxt in zip(mine, mine[1:] + [None]) if m.stage not in terminal]
        lane = lanes.get(task.lane.strip()) if task else None
        if last.stage in terminal:
            status, note = "merged", f"merged {last.at:%H:%M}"
        elif task and task.name.strip() in held:
            segs.append(gantt.Segment(now, now + WINDOWS[-1][2], last.stage, "hold"))
            status, note = "held", f"hold · {last.stage}"
        elif task and lane and last.stage in lane.stages and (end := ends.get(task.item)) and end > now:
            ahead = [last.stage] + [s for s in lane.stages[lane.stages.index(last.stage) + 1:] if s not in terminal]
            segs += _ahead(ahead, now, end)
            busy.append(end)
            ok = task.name.strip() in approved
            status, note = task.kind, f"approved · merge {_clock(end, now)}" if ok else f"{last.stage} · {_clock(end, now)}"
        else:
            status, note = (task.kind if task else ""), "approved" if task and task.name.strip() in approved else last.stage
        out.append((status, gantt.Row(task.issue.strip() if task else "", task.name.strip() if task else last.name,
                                      note, tuple(segs))))
    # Each slot is free once the forecast holding it ends; more forecasts than slots wait for the (n-k+1)th end.
    slots = max(1, slots)
    busy += [now] * (slots - len(busy))
    heapq.heapify(busy)
    moved = {m.name.casefold() for m in log}
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
                                      tuple(_ahead(stages, start, start + took)))))
    return out


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
