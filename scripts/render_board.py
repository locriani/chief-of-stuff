"""Render today's board from the tracker.

The board is a view of the tracker and nothing else: every bar start and end cites a tracker field
(`since`, `due`, `running HH:MM`, `done HH:MM`) or falls open to the nearest deadline from the
`## Coordinator` block, labelled "no estimate". The page carries the tracker's sha256, the render
instant, and the deadline instants, and draws its own clock and now-lines client-side.

The charts draw the schedule only. Today: running lanes and lanes that end today get a bar; every
other active lane folds into one summary row. Week: running lanes and lanes with a concrete due get
a bar; the rest fold into one row per deadline. Folded lanes keep their citations as `.member`
spans. Done lanes and full item text appear only in the Lanes table.

    python3 render_board.py --date 2026-09-16 [--root <workspace>]

Writes `<log dir>/<date>-board.html` beside the tracker and prints its path and one summary line.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?$")
BULLET = re.compile(r"^(\s*)-\s*([^:]+?):\s*(.*?)\s*$")
CAL_LIST = re.compile(r"^\s*-\s*(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})\s*(?:[A-Z]{2,5}\s+)?(.+?)\s*$")
TIME_RANGE = re.compile(r"^(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})$")
NO_ESTIMATE = "no estimate"
SHORT_NAME = 48
LONG_ITEM = 80
CLAUSE_TIME = re.compile(r"(?:^|\s)(?:at\s+)?(\d{1,2}:\d{2}):?(?=\s|$|[,;)])")
TICK_STEPS_H = (1, 2, 3, 4, 6)
MAX_TICKS = 9


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Deadline:
    name: str
    at: datetime


@dataclass(frozen=True)
class Config:
    user: str
    log_dir: str
    tracker_template: str
    tz: str
    deadlines: tuple[Deadline, ...]
    board_tool: str | None
    board_url: str | None

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def tracker_path(self, day: str) -> str:
        return self.tracker_template.replace("<date>", day)

    def log_path(self, day: str) -> str:
        return f"{self.log_dir.rstrip('/')}/{day}.md"


@dataclass(frozen=True)
class Lane:
    item: str
    owner: str
    state: str
    since: str
    due: str
    checklist: str
    warning: str = ""

    @property
    def kind(self) -> str:
        return (self.state.split() or ["open"])[0].lower()

    @property
    def state_time(self) -> str | None:
        parts = self.state.split()
        return parts[1] if len(parts) > 1 and HHMM.match(parts[1]) else None


@dataclass(frozen=True)
class Tracker:
    board_url: str | None
    lanes: tuple[Lane, ...]


@dataclass(frozen=True)
class Event:
    start: datetime
    end: datetime
    title: str


@dataclass(frozen=True)
class Bar:
    item: str
    owner: str
    kind: str
    start: datetime
    end: datetime
    start_src: str
    end_src: str
    label: str | None


@dataclass(frozen=True)
class Summary:
    key: str
    label: str
    start: datetime
    end: datetime
    members: tuple[Bar, ...]


def short_name(item: str) -> str:
    """The item up to its first ": " (when that leaves a real name), then up to " (" if still long, capped on a word boundary."""
    name = item.strip()
    head = name.split(": ", 1)[0]
    if head != name and len(head) >= 12:
        name = head
    if len(name) > SHORT_NAME and " (" in name:
        name = name.split(" (", 1)[0]
    if len(name) > SHORT_NAME:
        cut = name[:SHORT_NAME]
        space = cut.rfind(" ")
        name = (cut[:space] if space > SHORT_NAME // 2 else cut).rstrip(" ,;:–—-") + "…"
    return name


def _depth0_split(text: str) -> list[str]:
    """Split on "; " and ". " outside parentheses."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        c = text[i]
        depth += c == "("
        depth -= c == ")" and depth > 0
        if depth == 0 and c in ";." and text[i + 1 : i + 2] == " ":
            parts.append(text[start:i])
            start = i + 2
            i += 1
        i += 1
    parts.append(text[start:])
    return [p.strip().rstrip(".").strip() for p in parts if p.strip().rstrip(".").strip()]


def history_lines(item: str) -> list[tuple[str, str]]:
    """What an item cell carries past its short name, one (time, text) per clause; the first HH:MM outside parentheses is pulled out."""
    full, short = item.strip(), short_name(item)
    if short == full:
        return []
    rest = full[len(short) :] if not short.endswith("…") and full.startswith(short) else full
    lines = []
    for clause in _depth0_split(rest.lstrip(": ").strip()):
        when = ""
        for m in CLAUSE_TIME.finditer(clause):
            if clause[: m.start()].count("(") == clause[: m.start()].count(")"):
                when = m.group(1)
                clause = (clause[: m.start()] + " " + clause[m.end() :]).strip()
                clause = re.sub(r"\s{2,}", " ", clause).strip(" ,:")
                break
        lines.append((when, clause))
    return lines


# --- parsing -------------------------------------------------------------------------------------


def _section(text: str, heading: str) -> list[str]:
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


def _unquote(value: str) -> str:
    return value.strip().strip("`").strip()


def parse_coordinator(text: str, today: date) -> Config:
    body = _section(text, "## Coordinator")
    if not body:
        raise ConfigError("no `## Coordinator` block in CLAUDE.md")
    top: dict[str, str] = {}
    nested: dict[str, list[tuple[str, str]]] = {}
    current = ""
    for line in body:
        m = BULLET.match(line)
        if not m:
            continue
        indent, key, value = len(m.group(1)), _unquote(m.group(2)), m.group(3)
        if indent == 0:
            current = key
            top[key] = value
        else:
            nested.setdefault(current, []).append((key, value))
    for needed in ("User", "Daily log dir", "Tracker", "Timezone"):
        if needed not in top:
            raise ConfigError(f"`## Coordinator` block has no `{needed}:` line")
    tz = _unquote(top["Timezone"])
    zone = ZoneInfo(tz)
    deadlines = []
    for name, value in nested.get("Deadlines", []):
        m = DATE.match(_unquote(value))
        if m:
            deadlines.append(Deadline(name, datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 23), int(m[5] or 59), tzinfo=zone)))
    tracker = re.search(r"`([^`]+)`", top["Tracker"])
    board_tool = board_url = None
    if "Board" in top:
        parts = [p.strip() for p in top["Board"].split(";")]
        board_tool = _unquote(parts[0]) or None
        for p in parts[1:]:
            m = re.match(r"(?i)url\s+(\S+)", p)
            if m:
                board_url = m.group(1)
    return Config(
        user=_unquote(top["User"]),
        log_dir=_unquote(top["Daily log dir"]),
        tracker_template=tracker.group(1) if tracker else _unquote(top["Tracker"]),
        tz=tz,
        deadlines=tuple(sorted(deadlines, key=lambda d: d.at)),
        board_tool=board_tool,
        board_url=board_url,
    )


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c) and any(cells)


def parse_tracker(text: str) -> Tracker:
    head = text.split("## Lanes", 1)[0]
    url = None
    m = re.search(r"Board:\s*(\S+)", head)
    if m:
        candidate = m.group(1).rstrip(".,;")
        if candidate and not candidate.startswith("<") and candidate.lower() != "none":
            url = candidate
    lanes: list[Lane] = []
    rows = [line for line in _section(text, "## Lanes") if line.strip().startswith("|")]
    for row in rows[1:]:
        cells = _cells(row)
        if _is_separator(cells):
            continue
        warning = "" if len(cells) == 6 else f"{len(cells)} cells, expected 6"
        cells = (cells + [""] * 6)[:6]
        lanes.append(Lane(*cells, warning=warning))
    return Tracker(board_url=url, lanes=tuple(lanes))


def _hhmm(value: str, day: date, zone: ZoneInfo) -> datetime | None:
    m = HHMM.match(value.strip())
    return datetime.combine(day, time(int(m[1]), int(m[2])), tzinfo=zone) if m else None


def parse_calendar(log_text: str, today: date, zone: ZoneInfo) -> list[Event]:
    events: list[Event] = []
    for line in _section(log_text, "## Calendar"):
        m = CAL_LIST.match(line)
        if m:
            start, end = _hhmm(m[1], today, zone), _hhmm(m[2], today, zone)
            if start and end:
                events.append(Event(start, end, m[3]))
            continue
        if line.strip().startswith("|"):
            cells = _cells(line)
            if _is_separator(cells):
                continue
            times: list[datetime] = []
            title = ""
            for c in cells:
                r = TIME_RANGE.match(c)
                if r:
                    times += [t for t in (_hhmm(r[1], today, zone), _hhmm(r[2], today, zone)) if t]
                elif HHMM.match(c):
                    t = _hhmm(c, today, zone)
                    if t:
                        times.append(t)
                elif c and not title:
                    title = c
            if len(times) >= 2 and title and not (HHMM.match(title)):
                events.append(Event(times[0], times[1], title))
    return events


# --- bars ----------------------------------------------------------------------------------------


def nearest_deadline(cfg: Config, now: datetime) -> Deadline:
    for d in cfg.deadlines:
        if d.at >= now:
            return d
    if cfg.deadlines:
        return cfg.deadlines[-1]
    return Deadline("end of day", datetime.combine(now.date(), time(23, 59), tzinfo=cfg.zone))


def _resolve_due(due: str, cfg: Config, today: date) -> datetime | None:
    value = due.strip()
    if not value:
        return None
    if t := _hhmm(value, today, cfg.zone):
        return t
    m = DATE.match(value)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 23), int(m[5] or 59), tzinfo=cfg.zone)
    for d in cfg.deadlines:
        if d.name.lower() == value.lower():
            return d.at
    return None


def _end(lane: Lane, cfg: Config, now: datetime, start: datetime) -> tuple[datetime, str, str | None]:
    today = now.date()
    if (due := _resolve_due(lane.due, cfg, today)) is not None:
        return due, "due", None
    if lane.kind == "done":
        if t := lane.state_time:
            return _hhmm(t, today, cfg.zone), "state", None
        return start, "state", "done, no time"
    return nearest_deadline(cfg, now).at, "deadline", NO_ESTIMATE


def day_bar(lane: Lane, cfg: Config, now: datetime) -> Bar:
    today, zone = now.date(), cfg.zone
    day_start = datetime.combine(today, time(0, 0), tzinfo=zone)
    if lane.kind == "running" and (t := lane.state_time):
        start, start_src = _hhmm(t, today, zone), "state"
    elif t := _hhmm(lane.since, today, zone):
        start, start_src = t, "since"
    else:
        start, start_src = day_start, "carried"
    end, end_src, label = _end(lane, cfg, now, start)
    return Bar(lane.item, lane.owner, lane.kind, start, end, start_src, end_src, label)


def week_bar(lane: Lane, cfg: Config, now: datetime) -> Bar:
    today, zone = now.date(), cfg.zone
    day_start = datetime.combine(today, time(0, 0), tzinfo=zone)
    m = DATE.match(lane.since.strip())
    if m:
        start, start_src = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=zone), "since"
    elif lane.kind == "running" and (t := lane.state_time):
        start, start_src = _hhmm(t, today, zone), "state"
    elif t := _hhmm(lane.since, today, zone):
        start, start_src = t, "since"
    else:
        start, start_src = day_start, "carried"
    end, end_src, label = _end(lane, cfg, now, start)
    return Bar(lane.item, lane.owner, lane.kind, start, end, start_src, end_src, label)


# --- rendering -----------------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _pct(dt: datetime, a: datetime, b: datetime) -> float:
    span = (b - a).total_seconds() or 1.0
    return max(0.0, min(100.0, (dt - a).total_seconds() / span * 100.0))


def _member(b: Bar) -> str:
    label = f' data-label="{_esc(b.label)}"' if b.label else ""
    return f'<span class="member" data-item="{_esc(b.item)}" data-start-src="{b.start_src}" data-end-src="{b.end_src}"{label}></span>'


def _strip(bars: list[Bar], summaries: list[Summary], axis_a: datetime, axis_b: datetime, events: list[Event], deadlines: list[Deadline], ticks: list[tuple[datetime, str]], kind: str) -> str:
    out = [f'<div class="strip" data-strip="{kind}" data-axis-start="{_iso(axis_a)}" data-axis-end="{_iso(axis_b)}">']
    out.append('<div class="axis">')
    for at, label in ticks:
        out.append(f'<span class="tick" style="left:{_pct(at, axis_a, axis_b):.2f}%">{_esc(label)}</span>')
    out.append("</div>")
    out.append('<div class="rows">')
    for b in bars:
        left, right = _pct(b.start, axis_a, axis_b), _pct(b.end, axis_a, axis_b)
        classes = f"bar {b.kind}" + (" open-end" if b.end_src == "deadline" else "") + (" clamped" if b.end > axis_b else "")
        label = f' data-label="{_esc(b.label)}"' if b.label else ""
        text = f"<em>{_esc(b.label)}</em>" if b.label else ""
        out.append(
            f'<div class="row"><div class="name">{_esc(short_name(b.item))}</div><div class="track">'
            f'<div class="{classes}" data-item="{_esc(b.item)}" data-start="{_iso(b.start)}" data-end="{_iso(b.end)}" '
            f'data-start-src="{b.start_src}" data-end-src="{b.end_src}"{label} style="left:{left:.2f}%;width:{max(right - left, 0.6):.2f}%">{text}</div></div></div>'
        )
    for sm in summaries:
        left, right = _pct(sm.start, axis_a, axis_b), _pct(sm.end, axis_a, axis_b)
        clamped = " clamped" if sm.end > axis_b else ""
        names = " · ".join(short_name(m.item) for m in sm.members)
        out.append(
            f'<div class="row summary-row"><div class="name">{len(sm.members)} {"lane" if len(sm.members) == 1 else "lanes"}</div><div class="track">'
            f'<div class="bar open-end summary{clamped}" data-summary="{_esc(sm.key)}" data-count="{len(sm.members)}" data-start="{_iso(sm.start)}" data-end="{_iso(sm.end)}" '
            f'style="left:{left:.2f}%;width:{max(right - left, 0.6):.2f}%" title="{_esc(names)}"><em>{_esc(sm.label)}</em>'
            + "".join(_member(m) for m in sm.members)
            + "</div></div></div>"
        )
    out.append('<div class="overlay">')
    for ev in events:
        left, right = _pct(ev.start, axis_a, axis_b), _pct(ev.end, axis_a, axis_b)
        out.append(f'<div class="band" data-band="{_esc(ev.title)}" style="left:{left:.2f}%;width:{max(right - left, 0.3):.2f}%"></div>')
    for d in deadlines:
        if axis_a <= d.at <= axis_b:
            out.append(f'<div class="dline" data-deadline-name="{_esc(d.name)}" style="left:{_pct(d.at, axis_a, axis_b):.2f}%"><span>{_esc(d.name)}</span></div>')
    out.append('<div class="nowline" hidden></div></div>')
    out.append("</div></div>")
    return "\n".join(out)


def _deadline_for(lane: Lane, bar: Bar, cfg: Config, now: datetime) -> Deadline:
    for d in cfg.deadlines:
        if d.name.lower() == lane.due.strip().lower() or d.at == bar.end:
            return d
    return nearest_deadline(cfg, now)


def today_rows(active: list[Lane], cfg: Config, now: datetime, axis_b: datetime) -> tuple[list[Bar], list[Bar]]:
    """Own bars: running lanes and lanes whose cited end falls by the axis end. Everything else folds."""
    own, folded = [], []
    for lane in active:
        b = day_bar(lane, cfg, now)
        if (lane.kind == "running" and lane.state_time) or (b.end_src != "deadline" and b.end <= axis_b):
            own.append(b)
        else:
            folded.append(b)
    return own, folded


def week_rows(active: list[Lane], cfg: Config, now: datetime, week_a: datetime) -> tuple[list[Bar], list[Summary]]:
    """Own bars: running lanes and lanes with a concrete due. The rest fold into one row per deadline."""
    own: list[Bar] = []
    groups: dict[Deadline, list[Bar]] = {}
    instants = {d.at for d in cfg.deadlines}
    for lane in active:
        b = week_bar(lane, cfg, now)
        named = any(d.name.lower() == lane.due.strip().lower() for d in cfg.deadlines)
        if (lane.kind == "running" and lane.state_time) or (b.end_src == "due" and not named and b.end not in instants):
            own.append(b)
        else:
            groups.setdefault(_deadline_for(lane, b, cfg, now), []).append(b)
    summaries = [Summary(d.name, f"→ {d.name}", week_a, d.at, tuple(groups[d])) for d in sorted(groups, key=lambda d: d.at)]
    return own, summaries


def _tick_step(span: timedelta) -> timedelta:
    hours = span.total_seconds() / 3600
    for step in TICK_STEPS_H:
        if int(hours // step) + 1 <= MAX_TICKS:
            return timedelta(hours=step)
    return timedelta(hours=TICK_STEPS_H[-1])


def render(tracker_text: str, log_text: str, cfg: Config, now: datetime) -> str:
    sha = hashlib.sha256(tracker_text.encode()).hexdigest()
    tracker = parse_tracker(tracker_text)
    today, zone = now.date(), cfg.zone
    events = parse_calendar(log_text, today, zone)
    lanes = list(tracker.lanes)
    active = [lane for lane in lanes if lane.kind != "done"]
    done = [lane for lane in lanes if lane.kind == "done"]
    nearest = nearest_deadline(cfg, now)
    url = tracker.board_url or cfg.board_url

    # Day strip: ends at the nearest deadline or midnight, whichever is first; starts at the earliest of now-1h,
    # today's first calendar event, and the drawn bars' cited starts (carried bars sit at the axis start).
    day_start = datetime.combine(today, time(0, 0), tzinfo=zone)
    midnight = day_start + timedelta(days=1)
    axis_b = min(nearest.at, midnight) if nearest.at >= now else midnight
    day_bars, day_folded = today_rows(active, cfg, now, axis_b)
    cited = [b.start for b in day_bars if b.start_src != "carried" and b.start >= day_start]
    firsts = [e.start for e in events if e.start >= day_start]
    axis_a = max(day_start, min(cited + firsts + [now - timedelta(hours=1)]).replace(minute=0, second=0, microsecond=0))
    axis_b = max(axis_b, axis_a + timedelta(hours=3))
    step = _tick_step(axis_b - axis_a)
    day_ticks = []
    t = axis_a
    while t <= axis_b:
        day_ticks.append((t, t.strftime("%H:%M")))
        t += step
    day_events = [e for e in events if e.end > axis_a and e.start < axis_b]
    day_summaries = [Summary("today", "no estimate or due after today", axis_a, axis_b, tuple(day_folded))] if day_folded else []

    # Week strip: today to the last deadline, one column per day.
    week_a = datetime.combine(today, time(0, 0), tzinfo=zone)
    week_bars, week_summaries = week_rows(active, cfg, now, week_a)
    last = max([d.at for d in cfg.deadlines] + [week_a + timedelta(days=1)])
    week_b = datetime.combine(last.date() + timedelta(days=1), time(0, 0), tzinfo=zone)
    week_ticks = []
    t = week_a
    while t < week_b:
        week_ticks.append((t, t.strftime("%a %d")))
        t += timedelta(days=1)

    def item_cell(item: str) -> str:
        short = short_name(item)
        if short == item.strip():
            return _esc(item)
        lines = history_lines(item) or [("", item)]
        body = "".join(f"<li><time>{_esc(t)}</time><span>{_esc(text)}</span></li>" for t, text in lines)
        return f'<details><summary>{_esc(short)}</summary><ul class="hist">{body}</ul></details>'

    def lane_rows(group: list[Lane]) -> str:
        rows = []
        for lane in group:
            warn = f' <span class="warn">{_esc(lane.warning)}</span>' if lane.warning else ""
            rows.append(f'<tr data-state="{_esc(lane.kind)}"><td>{item_cell(lane.item)}{warn}</td><td>{_esc(lane.owner)}</td><td>{_esc(lane.state)}</td><td>{_esc(lane.since)}</td><td>{_esc(lane.due)}</td></tr>')
        return "\n".join(rows) or '<tr><td colspan="5" class="muted">none</td></tr>'

    queue = [lane for lane in active if lane.owner.strip().lower() == cfg.user.lower()]
    queue_rows = "\n".join(f"<tr><td>{_esc(short_name(l.item))}</td><td>{_esc(l.due)}</td><td>{_esc(l.state)}</td></tr>" for l in queue) or '<tr><td colspan="3" class="muted">nothing waiting on you</td></tr>'
    running = [l for l in active if l.kind == "running"]
    waiting = [l for l in active if l.kind != "running"]
    long_items = sum(1 for lane in lanes if len(lane.item) > LONG_ITEM)
    long_note = f" · {long_items} {'lane carries' if long_items == 1 else 'lanes carry'} history in the item cell" if long_items else ""
    tzname = now.strftime("%Z")

    return f"""<title>Board {today.isoformat()}</title>
<meta name="tracker-sha256" content="{sha}">
<style>
:root{{--bg:#faf9f6;--fg:#1f1f1f;--muted:#6b6b6b;--line:#d9d6cf;--band:rgba(70,110,200,.14);--open:#8fb0e8;--running:#3f7ad6;--done:#b9b5ab;--dl:#c8361f;--now:#1f9d55;--zach:#e0a13a}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#141414;--fg:#ececec;--muted:#9a9a9a;--line:#333;--band:rgba(120,160,240,.18);--open:#3b5a8c;--running:#5d93ea;--done:#4a4a4a}}}}
:root[data-theme="dark"]{{--bg:#141414;--fg:#ececec;--muted:#9a9a9a;--line:#333;--band:rgba(120,160,240,.18);--open:#3b5a8c;--running:#5d93ea;--done:#4a4a4a}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;padding:16px 16px 48px;max-width:1100px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}}
.meta{{color:var(--muted);font-size:13px}} .clock{{font-family:ui-monospace,monospace;font-size:15px;margin:6px 0}}
table{{border-collapse:collapse;width:100%;max-width:100%}} td,th{{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}} th{{color:var(--muted);font-weight:500;font-size:12px}}
.muted{{color:var(--muted)}} .warn{{color:var(--dl);font-size:12px}}
.strip{{position:relative;margin:8px 0 4px;--name-w:30%}} .axis{{position:relative;height:18px;margin-left:var(--name-w);font-size:11px;color:var(--muted)}} .tick{{position:absolute;transform:translateX(-50%);white-space:nowrap}}
.rows{{position:relative}} .row{{display:flex;align-items:center;height:26px}} .name{{width:var(--name-w);flex:none;padding-right:8px;box-sizing:border-box;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}} .track{{position:relative;flex:1;height:18px;border-left:1px solid var(--line)}}
.summary-row .name{{color:var(--muted)}}
.bar{{position:absolute;top:0;height:18px;border-radius:3px;background:var(--open);color:#fff;font-size:11px;line-height:18px;padding:0 6px;overflow:hidden;white-space:nowrap;box-sizing:border-box}}
.bar.running{{background:var(--running)}} .bar.open-end{{background:repeating-linear-gradient(135deg,var(--open),var(--open) 6px,transparent 6px,transparent 10px);color:var(--fg)}} .bar.clamped{{border-right:3px solid var(--dl)}} .bar em{{font-style:normal;opacity:.85}} .member{{display:none}}
.overlay{{position:absolute;top:0;bottom:0;left:var(--name-w);right:0;pointer-events:none}}
.band{{position:absolute;top:0;bottom:0;background:var(--band)}}
.dline{{position:absolute;top:0;bottom:0;border-left:2px dashed var(--dl);transform:translateX(-1px)}} .dline span{{position:absolute;top:-16px;left:4px;font-size:11px;color:var(--dl);white-space:nowrap}}
.nowline{{position:absolute;top:0;bottom:0;border-left:2px solid var(--now)}}
details summary{{cursor:pointer}} details[open] summary{{margin-bottom:4px}}
.hist{{list-style:none;margin:4px 0 2px;padding:0 0 0 10px;border-left:2px solid var(--line);max-height:12.5em;overflow:auto;font-size:12px;line-height:1.5;color:var(--muted);display:grid;gap:3px}}
.hist li{{display:grid;grid-template-columns:3em 1fr;gap:8px}} .hist time{{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums;color:var(--fg)}}
@media (max-width:520px){{.strip{{--name-w:36%}}}}
</style>
<div class="board" data-rendered-at="{_iso(now)}" data-tz="{_esc(cfg.tz)}" data-deadline="{_iso(nearest.at)}" data-deadline-name="{_esc(nearest.name)}">
<h1>Board {today.isoformat()}</h1>
<div class="clock" id="clock">Now — {_esc(tzname)} · {_esc(nearest.name)} ({nearest.at.strftime('%H:%M')} {_esc(tzname)})</div>
<div class="meta">tracker as of {now.strftime('%H:%M')} {_esc(tzname)} <span id="ago"></span> · kept by chief-of-stuff · board {_esc(url or 'not yet published')}{long_note}</div>

<h2>{_esc(cfg.user)}'s queue</h2>
<table><tr><th>item</th><th>due</th><th>state</th></tr>
{queue_rows}
</table>

<h2>Today</h2>
<div class="meta">{len(day_bars)} scheduled · {len(day_folded)} folded into one row (no estimate or due after today) · bands are calendar events · green line is now</div>
{_strip(day_bars, day_summaries, axis_a, axis_b, day_events, [nearest], day_ticks, "day")}

<h2>Week</h2>
<div class="meta">{len(week_bars)} scheduled · the rest folded into one row per deadline · dashed lines are deadlines</div>
{_strip(week_bars, week_summaries, week_a, week_b, [], list(cfg.deadlines), week_ticks, "week")}

<h2>Lanes</h2>
<table><tr><th>item</th><th>owner</th><th>state</th><th>since</th><th>due</th></tr>
<tr><th colspan="5">running</th></tr>
{lane_rows(running)}
<tr><th colspan="5">open · waiting</th></tr>
{lane_rows(waiting)}
<tr><th colspan="5">done</th></tr>
{lane_rows(done)}
</table>
</div>
<script>
(function(){{
  var root=document.querySelector('.board'),tz=root.dataset.tz,dl=new Date(root.dataset.deadline),ra=new Date(root.dataset.renderedAt);
  function hm(d){{return new Intl.DateTimeFormat('en-GB',{{timeZone:tz,hour:'2-digit',minute:'2-digit'}}).format(d);}}
  function tzn(d){{var p=new Intl.DateTimeFormat('en-US',{{timeZone:tz,timeZoneName:'short'}}).formatToParts(d);for(var i=0;i<p.length;i++)if(p[i].type==='timeZoneName')return p[i].value;return tz;}}
  function pad(n){{return (n<10?'0':'')+n;}}
  function tick(){{
    var now=new Date(),ms=dl-now,sign=ms<0?'-':'',m=Math.floor(Math.abs(ms)/60000);
    document.getElementById('clock').textContent='Now '+hm(now)+' '+tzn(now)+' · '+root.dataset.deadlineName+' in '+sign+Math.floor(m/60)+'h'+pad(m%60)+'m ('+hm(dl)+' '+tzn(dl)+')';
    var ago=Math.floor((now-ra)/60000);document.getElementById('ago').textContent='('+(ago<1?'just now':ago+'m ago')+')';
    document.querySelectorAll('.strip').forEach(function(s){{
      var a=new Date(s.dataset.axisStart),b=new Date(s.dataset.axisEnd),line=s.querySelector('.nowline');
      if(now<a||now>b){{line.hidden=true;return;}}
      line.hidden=false;line.style.left=((now-a)/(b-a)*100)+'%';
    }});
  }}
  tick();setInterval(tick,30000);
}})();
</script>
"""


# --- cli -----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    args = ap.parse_args(argv)
    root = Path(args.root)
    claude_md = root / "CLAUDE.md"
    if not claude_md.is_file():
        sys.exit(f"render_board: no CLAUDE.md at {root}")
    try:
        cfg = parse_coordinator(claude_md.read_text(), today=date.today())
    except ConfigError as e:
        sys.exit(f"render_board: {e}")
    now = datetime.now(cfg.zone).replace(second=0, microsecond=0)
    day = args.date or now.date().isoformat()
    tracker = root / cfg.tracker_path(day)
    if not tracker.is_file():
        sys.exit(f"render_board: tracker not found at {tracker}")
    log = root / cfg.log_path(day)
    log_text = log.read_text() if log.is_file() else ""
    tracker_text = tracker.read_text()
    out = tracker.with_name(f"{day}-board.html")
    page = render(tracker_text, log_text, cfg, now)
    out.write_text(page)
    parsed = parse_tracker(tracker_text)
    bars = [day_bar(lane, cfg, now) for lane in parsed.lanes]
    no_est = sum(1 for b in bars if b.label == NO_ESTIMATE)
    warnings = sum(1 for lane in parsed.lanes if lane.warning)
    long_items = sum(1 for lane in parsed.lanes if len(lane.item) > LONG_ITEM)
    print(out)
    print(f"lanes={len(parsed.lanes)} no_estimate={no_est} warnings={warnings} long_items={long_items} tracker_sha256={hashlib.sha256(tracker_text.encode()).hexdigest()[:12]}")
    return out


if __name__ == "__main__":
    main()
