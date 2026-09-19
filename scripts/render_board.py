"""Render today's board from the tracker.

The board is a view of the tracker and nothing else: every bar start and end cites a tracker field
(`since`, `due`, `running HH:MM`, `done HH:MM`) or falls open to the nearest deadline from the
`## Coordinator` block, labelled "no estimate". The page carries the tracker's sha256, the render
instant, and the deadline instants, and draws its own clock and now-lines client-side.

The charts draw the schedule only. Today: running lanes and lanes that end today get a bar; every
other active lane folds into one summary row. Week: running lanes and lanes with a concrete due get
a bar, grouped into one row per due day when several share it (overdue and past-the-week lanes get one row
each); the rest fold into one row per deadline. The Week axis spans at most 7 days; later deadlines are an
edge marker. Folded lanes keep their citations as `.member`
spans. Done lanes and full item text appear only in the Lanes table.

A deadline line may name a requirements file (`- Final: 2026-09-20 12:00; requirements `path``): checkbox
lines under `## ` headings, evidence after ` — evidence: `. The board shows it, with a done count, for
every deadline that has not passed.

    python3 render_board.py --date 2026-09-16 [--root <workspace>]

Writes `<log dir>/<date>-board.html` beside the tracker and prints its path and one summary line.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
# `done 21:16–22:05`: the running start kept on close. An en dash or a hyphen, because two hands write it.
RAN = re.compile(r"^(\d{1,2}:\d{2})[–-](\d{1,2}:\d{2})$")
SIZES = ("S", "M", "L", "XL")
ALL_SIZES = "all"
LANE_COLS = ("item", "owner", "state", "since", "due", "checklist")
LANE_COLS_SIZED = ("item", "owner", "state", "since", "due", "size", "checklist")
DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?$")
BULLET = re.compile(r"^(\s*)-\s*([^:]+?):\s*(.*?)\s*$")
CAL_LIST = re.compile(r"^\s*-\s*(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})\s*(?:[A-Z]{2,5}\s+)?(.+?)\s*$")
TIME_RANGE = re.compile(r"^(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})$")
NO_ESTIMATE = "no estimate"
LOGO = Path(__file__).resolve().parent.parent / "assets" / "logo.webp"
FONTS = "https://fonts.googleapis.com/css2?family=Alegreya+Sans:wght@400;500;600&family=Cormorant+SC:wght@600&display=swap"
SHORT_NAME = 48
LONG_ITEM = 80
RESUME_LINE = 160
CLAUSE_TIME = re.compile(r"(?:^|\s)(?:at\s+)?(\d{1,2}:\d{2}):?(?=\s|$|[,;)])")
REQ_LINE = re.compile(r"^(\s*)- \[([ xX])\]\s+(.*?)\s*$")
EVIDENCE = " — evidence: "
TICK_STEPS_H = (1, 2, 3, 4, 6)
WEEK_DAYS = 7
MAX_TICKS = 9


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Deadline:
    name: str
    at: datetime
    requirements: str | None = None
    # "all": unnamed lanes fall to it (the default). "named": only lanes whose `due` names it — an exam
    # governs no lane on the table, and a derived end toward it would make it look like a plan (finding 73).
    scope: str = "all"


@dataclass(frozen=True)
class Req:
    text: str
    done: bool
    evidence: str
    depth: int


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
    size: str = ""

    @property
    def kind(self) -> str:
        return (self.state.split() or ["open"])[0].lower()

    @property
    def state_time(self) -> str | None:
        """The one clock the state carries: the running start, or the done end (a range gives its end)."""
        parts = self.state.split()
        if len(parts) < 2:
            return None
        if HHMM.match(parts[1]):
            return parts[1]
        m = RAN.match(parts[1])
        return m.group(2) if m else None

    @property
    def ran(self) -> tuple[str, str] | None:
        """`done 21:16–22:05` → ("21:16", "22:05"): the running start kept on close. None when it was dropped."""
        parts = self.state.split()
        m = RAN.match(parts[1]) if len(parts) > 1 else None
        return (m.group(1), m.group(2)) if m and self.kind == "done" else None


# What a session reports about itself. `starting`, `ready` and `gone` are the coordinator's to work
# out and never a session's to claim: a session cannot report that it is gone, and `ready for
# decommissioning` is a `doing` value that `:189` says never becomes a state of its own.
SESSION_STATES = ("planning", "working", "waiting", "idle")
READY = re.compile(r"(?i)\bready for decommissioning\b")
# `Zach — A2, A3, B`: the target, then why. An em dash, an en dash or a hyphen, because three
# different sessions have written this cell and they did not agree.
WAITS = re.compile(r"\s+[—–-]\s+|,\s+")
# `nothing; the stack-default fix landed` is what a live row says when a session waits on no one.
# Drawn literally it is an edge to a node called "nothing".
NOBODY = re.compile(r"(?i)^(?:(?:nothing|none|nobody|n/?a)\b|[-—–]\s*$)")
# The name cell accretes provenance: `update-claude-md-docs (third name, same ref)`.
PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")
SESSION_REF = re.compile(r"\s*\[[0-9a-f]{4,}\]\s*")


def _bare_name(name: str) -> str:
    """`demo-fixes [b8fca1] (fix 5)` → `demo-fixes`. Both sides of a join have to strip the same things."""
    return PARENTHETICAL.sub("", SESSION_REF.sub(" ", name)).strip().lower()


@dataclass(frozen=True)
class Session:
    ref: str
    name: str
    state: str
    doing: str
    waiting_on: str
    free_at: str
    constraints: str
    children: str
    last_reply: str
    warning: str = ""

    @property
    def kind(self) -> str:
        """What to draw. Derived beats written, because the coordinator knows things the session cannot."""
        if not self.ref.strip():
            return "starting"
        if READY.search(self.doing):
            return "ready"
        state = self.state.strip().lower()
        if state in SESSION_STATES:
            return state
        # Nothing reported and a word nobody knows are two different facts, and every row of the live
        # tracker was the first one — seven-column rows written before the column existed.
        return "unknown" if state else "unreported"

    @property
    def label(self) -> str:
        """The name without the history the cell accretes. The ref is the key; this is for reading."""
        return PARENTHETICAL.sub("", self.name.strip()).strip() or self.name.strip()

    @property
    def waits_on(self) -> str:
        """Who, as an edge. The graph draws the arrow, so the state stays four words wide."""
        cell = self.waiting_on.strip()
        if not cell or NOBODY.match(cell):
            return ""
        return WAITS.split(cell, 1)[0].strip()

    @property
    def waits_for(self) -> str:
        cell = self.waiting_on.strip()
        if not cell or NOBODY.match(cell):
            return ""
        parts = WAITS.split(cell, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @property
    def child_names(self) -> tuple[str, ...]:
        """A subagent is not a peer: it has no ref, answers no poll, and cannot be sent to.

        So children are a cell on their owner's row rather than rows of their own, and what is
        recoverable from `2: rules-audit (working), fixture-sweep (idle)` is names and count.
        """
        body = self.children.strip()
        if not body or body.lower() in ("none", "-", "—"):
            return ()
        body = body.split(":", 1)[1] if re.match(r"^\d+\s*:", body) else body
        names = [re.sub(r"\s*\(.*?\)\s*$", "", part).strip() for part in body.split(",")]
        return tuple(n for n in names if n)


# 7 columns is every tracker written before the state and children columns existed; 9 is after.
# Positional parsing cannot tell `state` from `doing` without knowing which, so the count decides.
SESSION_OLD = ("ref", "name", "doing", "waiting_on", "free_at", "constraints", "last_reply")
SESSION_NEW = ("ref", "name", "state", "doing", "waiting_on", "free_at", "constraints", "children", "last_reply")


def _session(cells: list[str]) -> Session:
    names = SESSION_NEW if len(cells) >= len(SESSION_NEW) else SESSION_OLD
    warning = "" if len(cells) == len(names) else f"{len(cells)} cells, expected {len(names)}"
    fields = dict(zip(names, (cells + [""] * len(names))[: len(names)]))
    session = Session(**{k: fields.get(k, "") for k in SESSION_NEW}, warning=warning)
    state = session.state.strip().lower()
    if state and state not in SESSION_STATES:
        warning = (warning + "; " if warning else "") + f"state {session.state.strip()!r} is not one of {', '.join(SESSION_STATES)}"
        session = Session(**{k: getattr(session, k) for k in SESSION_NEW}, warning=warning)
    return session


@dataclass(frozen=True)
class Tracker:
    board_url: str | None
    lanes: tuple[Lane, ...]
    sessions: tuple[Session, ...] = ()


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
    est: "Estimate | None" = None
    size: str = ""


@dataclass(frozen=True)
class Summary:
    key: str
    label: str
    start: datetime
    end: datetime
    members: tuple[Bar, ...]
    name: str = ""
    attr: str = "summary"


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
        when, *options = [part.strip() for part in value.split(";")]
        m = DATE.match(_unquote(when))
        if m:
            reqs = next((r.group(1) for o in options if (r := re.match(r"(?i)requirements\s+`?([^`]+?)`?\s*$", o))), None)
            scope = "named" if any(re.match(r"(?i)named lanes only\s*$", o) for o in options) else "all"
            deadlines.append(Deadline(name, datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 23), int(m[5] or 59), tzinfo=zone), reqs, scope))
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
RESUME_CLIP = 110


def _clip(value: str, cap: int) -> str:
    """Cut on a word boundary and say so. A clipped line that does not admit it is just a wrong line."""
    if len(value) <= cap:
        return value
    head = value[:cap].rsplit(" ", 1)[0].rstrip(" ,;·—–-")
    return f"{head or value[:cap]} …"


def _resume_value(value: str) -> str:
    """A long field folds, the way `item_cell()` folds a long lane item. Same board, same idiom."""
    if len(value) <= RESUME_LINE:
        return _esc(value)
    return f'<details class="long"><summary>{_esc(_clip(value, RESUME_CLIP))}</summary>{_esc(value)}</details>'


def resume_fields(block: dict[str, str]) -> list[str]:
    """The fields that will be drawn, in reading order. The renderer and the summary line share it.

    Finding 65: a count of what was parsed is not a count of what was drawn, and only the second one
    is a check. `resume=N fields` is taken from here so the two can never disagree again.
    """
    return [k for k in RESUME_STRIP if block.get(k)] + [k for k in block if k not in RESUME_STRIP and block[k]]


def resume_strip(block: dict[str, str]) -> str:
    """Where the last move left off, one row per field. Absent when the tracker has no block.

    It was one inline run joined with ` · `, which is fine at two fields and unreadable at six — and
    six is what a busy day writes. The fields are already a label and a value; they want a list.
    """
    order = resume_fields(block)
    if not order:
        return ""
    rows = []
    for k in order:
        long = ' class="long-field"' if len(block[k]) > RESUME_LINE else ""
        rows.append(f"<dt>{_esc(k)}</dt><dd{long}>{_resume_value(block[k])}</dd>")
    rows = "".join(rows)
    return f'<dl class="resume">{rows}</dl>\n'


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
    head = re.split(r"(?m)^## ", text, maxsplit=1)[0]
    url = None
    m = re.search(r"(?m)^\s*(?:[-*]\s*)?(?:Coordinator:[^\n]*?)?Board:\s*(\S+)", head)
    if m:
        candidate = m.group(1).rstrip(".,;")
        if candidate and not candidate.startswith("<") and candidate.lower() != "none":
            url = candidate
    lanes: list[Lane] = []
    rows = [line for line in _section(text, "## Lanes") if line.strip().startswith("|")]
    # The header decides the width: a six-column table parses as it always has, every lane unsized.
    cols = LANE_COLS_SIZED if rows and [c.lower() for c in _cells(rows[0])] == list(LANE_COLS_SIZED) else LANE_COLS
    for row in rows[1:]:
        cells = _cells(row)
        if _is_separator(cells):
            continue
        warning = "" if len(cells) == len(cols) else f"{len(cells)} cells, expected {len(cols)}"
        fields = dict(zip(cols, (cells + [""] * len(cols))[:len(cols)]))
        lanes.append(Lane(**fields, warning=warning))
    sessions: list[Session] = []
    for row in [line for line in _section(text, "## Sessions") if line.strip().startswith("|")][1:]:
        cells = _cells(row)
        if _is_separator(cells) or not any(c.strip() for c in cells):
            continue
        sessions.append(_session(cells))
    return Tracker(board_url=url, lanes=tuple(lanes), sessions=tuple(sessions))


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
    """The nearest deadline of all, whatever it governs: the Clock line, `data-deadline` and the axes use it."""
    for d in cfg.deadlines:
        if d.at >= now:
            return d
    if cfg.deadlines:
        return cfg.deadlines[-1]
    return _end_of_day(cfg, now)


def _end_of_day(cfg: Config, now: datetime) -> Deadline:
    return Deadline("end of day", datetime.combine(now.date(), time(23, 59), tzinfo=cfg.zone))


def governing_deadline(cfg: Config, now: datetime) -> Deadline:
    """The nearest deadline ahead that unnamed lanes fall to, else end of day.

    A lane that names no deadline is drawn or estimated toward this one. A deadline marked `named lanes
    only` is skipped: after Final the nearest deadline is an exam (finding 73), and nothing on the Lanes
    table is due to it unless its `due` cell says so.
    """
    for d in cfg.deadlines:
        if d.at >= now and d.scope == "all":
            return d
    return _end_of_day(cfg, now)


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


def _dur(td: timedelta) -> str:
    h, m = divmod(int(td.total_seconds()) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


@dataclass(frozen=True)
class Estimate:
    """Where lane i of an owner's N lands.

    Two bases. `history`: the queue drains at the mean elapsed time of closed lanes of each size (Zach,
    2026-09-18: "generate an average amount of time spent per t-shirt sized puzzle ... use that"). `queue`:
    no lane has closed with a range yet, so the queue drains evenly by the horizon — the 0.10.0 model,
    which claims nothing about effort. The label says which, so a reader can tell a measurement from a share.
    """

    end: datetime
    i: int
    n: int
    of: str
    slot: timedelta
    size: str = ""
    basis: str = "queue"
    count: int = 0
    borrowed: bool = False

    @property
    def label(self) -> str:
        if self.basis == "history":
            return f"est. {self.size or '?'} ~{_dur(self.slot)}"
        return f"est. {self.i}/{self.n} → {self.of}"

    def title(self, owner: str) -> str:
        if self.basis == "history":
            if not self.size:
                rests = f"unsized: the mean of all {self.count} lanes closed with a range, {_dur(self.slot)}"
            elif self.borrowed:
                rests = f"size {self.size}: no {self.size} lane has closed with a range, so the mean of all {self.count} closed lanes stands in, {_dur(self.slot)}"
            else:
                rests = f"size {self.size}: mean of {self.count} {self.size} lanes closed with a range, {_dur(self.slot)} each"
            return (f"{self.label}: {rests}; {_ordinal(self.i)} of {self.n} in {owner}'s queue, running first, then oldest first. "
                    "Set a due to override.")
        return (f"{self.label}: where this lane lands if {owner}'s queue of {self.n} drains evenly by {self.of}, "
                "running first, then oldest first. Not a claim about effort; set a due to override.")


UNASSIGNED = re.compile(r"(?i)^unassigned$")


def _owner_key(owner: str) -> str:
    """The queue an owner cell names, or "" when it names nobody: blank, `unassigned`, `—`, `nobody`."""
    key = _bare_name(owner)
    if not key or NOBODY.match(owner.strip()) or UNASSIGNED.match(key):
        return ""
    return key


def horizon(cfg: Config, now: datetime) -> tuple[datetime, str]:
    """The nearest governing deadline still ahead, or midnight once none is (the day strip's own rule)."""
    d = governing_deadline(cfg, now)
    if d.at > now and d.name != "end of day":
        return d.at, d.name
    return datetime.combine(now.date(), time(0, 0), tzinfo=cfg.zone) + timedelta(days=1), "end of day"


def estimates(active: list[Lane], cfg: Config, now: datetime, hist: dict[str, tuple[timedelta, int]] | None = None) -> dict[str, Estimate]:
    """A derived end for every owned lane with no `due`, keyed by item. Nothing here is written to the tracker.

    Per owner, the active lanes that are blank or due by the horizon form a queue of N, running first, then
    oldest first. With `hist` (see `history()`), a cursor starts at now and each lane takes the mean elapsed
    time of closed lanes of its size — a running lane from its own start, never ending before now; a lane
    with a `due` takes its time from the queue and keeps its due. Without history no lane has a duration to
    learn from, and this reads none — not the item text (its length tracks age, not work left), not the
    Log: lane i ends at `H - (N-i)/N x (H - now)`, so lane N is the horizon instant, and the label says so.
    Unowned lanes get nothing: there is no queue to place them in, and the deadline they already draw to
    is the honest end. A gone owner — one an `orphaned` lane names — is unowned for all its lanes.
    """
    h, of = horizon(cfg, now)
    r = h - now
    left = {_bare_name(lane.owner) for lane in active if lane.kind == ORPHANED} - {""}
    queues: dict[str, list[tuple[tuple, Lane]]] = {}
    for idx, lane in enumerate(active):
        if lane.kind in ("done", ORPHANED):
            continue
        key = _owner_key(lane.owner)
        if not key or key in left:
            continue
        due = _resolve_due(lane.due, cfg, now.date())
        if due is not None and due > h:
            continue
        m = DATE.match(lane.since.strip())
        since = date(int(m[1]), int(m[2]), int(m[3])) if m else now.date()
        queues.setdefault(key, []).append(((lane.kind != "running", since, idx), lane))
    out: dict[str, Estimate] = {}
    for entries in queues.values():
        entries.sort(key=lambda e: e[0])
        n = len(entries)
        cursor = now
        for i, (_, lane) in enumerate(entries, 1):
            if hist:
                size = lane.size.strip().upper()
                borrowed = bool(size) and size not in hist
                mean, count = hist[size] if size and size in hist else hist[ALL_SIZES]
                start = _hhmm(lane.state_time, now.date(), cfg.zone) if lane.kind == "running" and lane.state_time else None
                end = max(now, start + mean) if start else cursor + mean
                cursor = max(cursor, end)
                if not lane.due.strip():
                    out[lane.item] = Estimate(end, i, n, of, mean, size, "history", count, borrowed)
            elif not lane.due.strip():
                out[lane.item] = Estimate(h - r * ((n - i) / n), i, n, of, r / n)
    return out


def history(lanes: list[Lane], cfg: Config, now: datetime) -> dict[str, tuple[timedelta, int]]:
    """Mean elapsed time per size over done lanes that kept their running start, plus an `all` row.

    Only `done HH:MM–HH:MM` counts: `since` is a date and `done HH:MM` alone has no start (finding 70), so
    a lane closed without the range records nothing and is left out rather than guessed at. A range that
    runs backwards crossed midnight and is dropped too. Empty when no lane has a range, and the estimator
    then falls back to the queue-drain of 0.10.0, labelled as such.
    """
    today, zone = now.date(), cfg.zone
    spans: dict[str, list[timedelta]] = {}
    for lane in lanes:
        if not (r := lane.ran):
            continue
        a, b = _hhmm(r[0], today, zone), _hhmm(r[1], today, zone)
        if a is None or b is None or b <= a:
            continue
        spans.setdefault(lane.size.strip().upper(), []).append(b - a)
    out = {size: (sum(v, timedelta()) / len(v), len(v)) for size, v in spans.items()}
    if out:
        every = [d for v in spans.values() for d in v]
        out[ALL_SIZES] = (sum(every, timedelta()) / len(every), len(every))
    return out


def _end(lane: Lane, cfg: Config, now: datetime, start: datetime, est: dict[str, Estimate] | None = None) -> tuple[datetime, str, str | None]:
    today = now.date()
    if (due := _resolve_due(lane.due, cfg, today)) is not None:
        return due, "due", None
    if lane.kind == "done":
        if t := lane.state_time:
            return _hhmm(t, today, cfg.zone), "state", None
        return start, "state", "done, no time"
    if est and (e := est.get(lane.item)):
        return max(e.end, start), "derived", e.label
    return governing_deadline(cfg, now).at, "deadline", NO_ESTIMATE


def day_bar(lane: Lane, cfg: Config, now: datetime, est: dict[str, Estimate] | None = None) -> Bar:
    today, zone = now.date(), cfg.zone
    day_start = datetime.combine(today, time(0, 0), tzinfo=zone)
    if lane.kind == "running" and (t := lane.state_time):
        start, start_src = _hhmm(t, today, zone), "state"
    elif t := _hhmm(lane.since, today, zone):
        start, start_src = t, "since"
    else:
        start, start_src = day_start, "carried"
    end, end_src, label = _end(lane, cfg, now, start, est)
    return Bar(lane.item, lane.owner, lane.kind, start, end, start_src, end_src, label, est.get(lane.item) if est and end_src == "derived" else None, lane.size.strip().upper())


def week_bar(lane: Lane, cfg: Config, now: datetime, est: dict[str, Estimate] | None = None) -> Bar:
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
    end, end_src, label = _end(lane, cfg, now, start, est)
    return Bar(lane.item, lane.owner, lane.kind, start, end, start_src, end_src, label, est.get(lane.item) if est and end_src == "derived" else None, lane.size.strip().upper())


# --- rendering -----------------------------------------------------------------------------------


def logo_tag() -> str:
    """The brand mark, inlined: the board is one file and the artifact host serves no other asset."""
    if not LOGO.is_file():
        return ""
    return f'<img class="logo" alt="Chief of Stuff" src="data:image/webp;base64,{base64.b64encode(LOGO.read_bytes()).decode()}">'


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _pct(dt: datetime, a: datetime, b: datetime) -> float:
    span = (b - a).total_seconds() or 1.0
    return max(0.0, min(100.0, (dt - a).total_seconds() / span * 100.0))


def _est_attrs(b: Bar) -> str:
    """A derived end is a citation only if its inputs are on the bar: position in the queue, the basis, the horizon or the size."""
    est = f' data-est="{b.est.i}/{b.est.n}" data-est-basis="{b.est.basis}" data-est-of="{_esc(b.est.of)}"' if b.est else ""
    return est + (f' data-size="{_esc(b.size)}"' if b.size else "")


def _member(b: Bar) -> str:
    label = f' data-label="{_esc(b.label)}"' if b.label else ""
    return f'<span class="member" data-item="{_esc(b.item)}" data-start-src="{b.start_src}" data-end-src="{b.end_src}"{label}{_est_attrs(b)}></span>'


def _lanes(n: int) -> str:
    return f"{n} {'lane' if n == 1 else 'lanes'}"


LEGEND = [
    ("key bar running", "running"),
    ("key bar open", "open"),
    ("key bar orphaned", "orphaned"),
    ("key bar open-end", "no estimate"),
    ("key bar derived", "estimated"),
    ("key bar summary", "folded rows"),
    ("key band", "calendar event"),
    ("key nowline", "now"),
    ("key dline", "deadline"),
]


def legend() -> str:
    return '<div class="legend">' + "".join(f'<span class="item"><span class="{cls}"></span> {label}</span>' for cls, label in LEGEND) + "</div>\n"


# A poll cycle runs about an hour, so under half an hour is current, under two hours is last cycle,
# and past that a node is reporting a memory of the morning. Baked in Python at render: the fade has
# to survive with JavaScript off, and design-inputs §50 settled that this page runs none.
STAMP_FRESH = timedelta(minutes=30)
STAMP_AGING = timedelta(hours=2)
# Every kind a node can draw. The first four are reported; the rest the coordinator works out,
# `gone` from the Lanes join because a session cannot report that it is gone.
SESSION_KINDS = ("planning", "working", "waiting", "idle", "starting", "ready", "gone", "unknown", "unreported")


def _age(session: Session, cfg: Config, now: datetime) -> tuple[datetime | None, int | None]:
    """When the session last spoke, and how long ago in minutes.

    A reply time later than now is last night's, not this evening's: the cell carries HH:MM and no
    date, and a coordinator reading 23:50 at 14:30 is looking at fifteen hours of silence.
    """
    at = _hhmm(session.last_reply, now.date(), cfg.zone)
    if at is None:
        return None, None
    if at > now:
        at -= timedelta(days=1)
    return at, int((now - at).total_seconds() // 60)


def _age_text(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _band(minutes: int) -> str:
    return "fresh" if minutes < STAMP_FRESH.total_seconds() / 60 else ("aging" if minutes < STAMP_AGING.total_seconds() / 60 else "stale")


def _stamp(session: Session, cfg: Config, now: datetime) -> tuple[str, str]:
    """The node's age, as html and as the `data-age` citation it is drawn from."""
    at, minutes = _age(session, cfg, now)
    if at is None:
        if not session.last_reply.strip():
            return '<span class="stamp never">no reply yet</span>', ""
        # A cell that is not HH:MM is shown as written rather than guessed at or dropped.
        return f'<span class="stamp unparsed">as of {_esc(session.last_reply.strip())}</span>', ""
    return (f'<span class="stamp {_band(minutes)}">as of {at.strftime("%H:%M")} · {_age_text(minutes)}</span>',
            f' data-as-of="{at.strftime("%H:%M")}" data-age="{minutes}" data-band="{_band(minutes)}"')


def _facts(session: Session) -> str:
    rows = [("doing", session.doing), ("waiting for", session.waits_for), ("free at", session.free_at),
            ("constraints", session.constraints)]
    body = "".join(f"<dt>{label}</dt><dd>{_esc(value.strip())}</dd>" for label, value in rows if value.strip())
    warn = f'<dt>warning</dt><dd class="warn">{_esc(session.warning)}</dd>' if session.warning else ""
    return f'<dl class="facts">{body}{warn}</dl>' if body or warn else ""


def _session_node(session: Session, cfg: Config, now: datetime, gone: set[str]) -> str:
    kind = "gone" if session.label in gone else session.kind
    stamp, cite = _stamp(session, cfg, now)
    who = session.waits_on
    # An edge, not a state: `waiting on` carries who and then why, and the enum stays four words wide.
    edge = f'<span class="edge">→ {_esc(who)}</span>' if who else ""
    kids = "".join(f"<li>{_esc(name)}</li>" for name in session.child_names)
    kid_list = f'<ul class="kids">{kids}</ul>' if kids else ""
    ref = f' data-ref="{_esc(session.ref.strip())}"' if session.ref.strip() else ""
    waits = f' data-waits-on="{_esc(who)}"' if who else ""
    return (f'<li><details class="node {kind}"{ref} data-state="{kind}"{cite}{waits}>'
            f'<summary><span class="chip {kind}">{kind}</span><span class="who">{_esc(session.label)}</span>'
            f"{edge}{stamp}</summary>{_facts(session)}{kid_list}</details></li>")


ORPHANED = "orphaned"


def gone_sessions(lanes: tuple[Lane, ...], sessions: tuple[Session, ...]) -> set[str]:
    """Sessions the registry still lists whose lane says its owner left. The join, in the renderer.

    A session cannot report that it is gone, so this is the coordinator's to work out, and it is the
    one state the board takes over a session's own word: `orphaned` on the lane and `working` in the
    registry are the same row contradicting itself, and the lane is the column that was updated last.
    """
    left = {_bare_name(lane.owner) for lane in lanes if lane.kind == ORPHANED}
    left -= {""}
    return {s.label for s in sessions if _bare_name(s.name) in left}


def session_graph(sessions: tuple[Session, ...], cfg: Config, now: datetime, gone: set[str] | None = None) -> str:
    """The coordinator, its sessions, and their subagents, as one disclosure tree.

    Nested `<details>` and nothing else: the idiom `_strip()` already uses for folded rows, and the
    one shape a tree can take here. It is not a `Bar` — `_strip` positions everything as a percentage
    of a time axis, and a tree has no time axis.

    A subagent gets a list item and never a node: it has no ref, is in no listing, answers no poll and
    cannot be sent to, and a node beside its peers would say all four were false.
    """
    if not sessions:
        return ""
    gone = gone or set()
    nodes = "\n".join(_session_node(s, cfg, now, gone) for s in sessions)
    stale = sum(1 for s in sessions if (m := _age(s, cfg, now)[1]) is not None and _band(m) == "stale")
    unheard = sum(1 for s in sessions if _age(s, cfg, now)[0] is None)
    kids = sum(len(s.child_names) for s in sessions)
    notes = [f"{len(sessions)} {'session' if len(sessions) == 1 else 'sessions'}"]
    if kids:
        notes.append(f"{kids} {'subagent' if kids == 1 else 'subagents'}")
    if stale:
        notes.append(f"{stale} stale")
    if unheard:
        notes.append(f"{unheard} never replied")
    return f"""<section class="graph" data-sessions="{len(sessions)}">
<h2>Sessions · {len(sessions)}</h2>
<details class="node coordinator" open>
<summary><span class="chip coordinator">coordinator</span><span class="who">chief-of-stuff</span><span class="stamp fresh">{" · ".join(notes)}</span></summary>
<ul class="peers">
{nodes}
</ul>
</details>
</section>
"""


def grid_marks(axis_a: datetime, axis_b: datetime, kind: str) -> list[tuple[datetime, bool]]:
    """Every hour on the day strip (a full line every two), every day on the week strip (a half line at midday)."""
    step = timedelta(hours=1) if kind == "day" else timedelta(days=1)
    marks, at, i = [], axis_a, 0
    last = axis_b if kind == "day" else axis_b - step
    while at <= last:
        marks.append((at, i % 2 == 0 if kind == "day" else True))
        if kind == "week":
            marks.append((at + step / 2, False))
        at += step
        i += 1
    return marks


def _bar_row(b: Bar, axis_a: datetime, axis_b: datetime, row_class: str = "row") -> str:
    """One lane as a positioned row. A top-level bar and a sublane inside a fold are the same markup."""
    left, right = _pct(b.start, axis_a, axis_b), _pct(b.end, axis_a, axis_b)
    edge = (" clamped" if b.end > axis_b else "") + (" clamped-left" if b.end < axis_a else "")
    classes = f"bar {b.kind}" + (" open-end" if b.end_src == "deadline" else "") + (" derived" if b.end_src == "derived" else "") + edge
    label = f' data-label="{_esc(b.label)}"' if b.label else ""
    title = f' title="{_esc(b.est.title(b.owner))}"' if b.est else ""
    text = f"<em>{_esc(b.label)}</em>" if b.label else ""
    return (
        f'<div class="{row_class}"><div class="name">{_esc(short_name(b.item))}</div><div class="track">'
        f'<div class="{classes}" data-item="{_esc(b.item)}" data-start="{_iso(b.start)}" data-end="{_iso(b.end)}" '
        f'data-start-src="{b.start_src}" data-end-src="{b.end_src}"{label}{_est_attrs(b)}{title} style="left:{left:.2f}%;width:{max(right - left, 0.6):.2f}%">{text}</div></div></div>'
    )


def _strip(rows: list[Bar | Summary], axis_a: datetime, axis_b: datetime, events: list[Event], deadlines: list[Deadline], ticks: list[tuple[datetime, str]], kind: str) -> str:
    out = [f'<div class="strip" data-strip="{kind}" data-axis-start="{_iso(axis_a)}" data-axis-end="{_iso(axis_b)}">']
    out.append('<div class="axis">')
    for at, label in ticks:
        out.append(f'<span class="tick" style="left:{_pct(at, axis_a, axis_b):.2f}%">{_esc(label)}</span>')
    out.append("</div>")
    out.append('<div class="rows">')
    for row in rows:
        left, right = _pct(row.start, axis_a, axis_b), _pct(row.end, axis_a, axis_b)
        edge = (" clamped" if row.end > axis_b else "") + (" clamped-left" if row.end < axis_a else "")
        if isinstance(row, Bar):
            out.append(_bar_row(row, axis_a, axis_b))
            continue
        sm = row
        names = " · ".join(short_name(m.item) for m in sm.members)
        hatch = " open-end" if sm.attr == "summary" else ""
        # The folded names were always in the html as empty `.member` spans, readable by a grader and
        # by nobody else. The disclosure opens onto the members drawn as rows on the same axis — sublanes,
        # not a list of names (Zach, 2026-09-18 12:55) — and the citations stay on the summary bar.
        out.append(
            '<details class="folded"><summary>'
            f'<div class="row {sm.attr}-row"><div class="name">{_esc(sm.name or _lanes(len(sm.members)))}</div><div class="track">'
            f'<div class="bar{hatch} {sm.attr}{edge}" data-{sm.attr}="{_esc(sm.key)}" data-count="{len(sm.members)}" data-start="{_iso(sm.start)}" data-end="{_iso(sm.end)}" '
            f'style="left:{left:.2f}%;width:{max(right - left, 0.6):.2f}%" title="{_esc(names)}"><em>{_esc(sm.label)}</em>'
            + "".join(_member(m) for m in sm.members)
            + "</div></div></div></summary>"
            + "".join(_bar_row(m, axis_a, axis_b, "row sub") for m in sm.members)
            + "</details>"
        )
    out.append('<div class="overlay">')
    for at, major in grid_marks(axis_a, axis_b, kind):
        out.append(f'<div class="grid{"" if major else " half"}" style="left:{_pct(at, axis_a, axis_b):.2f}%"></div>')
    for ev in events:
        left, right = _pct(ev.start, axis_a, axis_b), _pct(ev.end, axis_a, axis_b)
        out.append(f'<div class="band" data-band="{_esc(ev.title)}" style="left:{left:.2f}%;width:{max(right - left, 0.3):.2f}%"></div>')
    drawn = [d for d in deadlines if axis_a <= d.at <= axis_b]
    for d in drawn:
        out.append(f'<div class="dline" data-deadline-name="{_esc(d.name)}" style="left:{_pct(d.at, axis_a, axis_b):.2f}%"></div>')
    out.append('<div class="nowline" hidden></div></div>')
    out.append("</div>")
    # Deadline labels sit below the rows, one line each, so they never share a line with each other or the axis.
    callouts = []
    for d in drawn:
        pct = _pct(d.at, axis_a, axis_b)
        span = f'<span class="right" style="right:{100 - pct:.2f}%">' if pct > 70 else f'<span style="left:{pct:.2f}%">'
        callouts.append(f'<div class="callout">{span}{_esc(d.name)}</span></div>')
    beyond = [d for d in deadlines if d.at > axis_b]
    if beyond:
        callouts.append(f'<div class="callout later">{_esc(" · ".join(f"{d.name} {d.at:%a %d}" for d in beyond))} →</div>')
    if callouts:
        out.append('<div class="callouts">' + "".join(callouts) + "</div>")
    out.append("</div>")
    return "\n".join(out)


def _deadline_for(lane: Lane, bar: Bar, cfg: Config, now: datetime) -> Deadline:
    for d in cfg.deadlines:
        if d.name.lower() == lane.due.strip().lower() or (bar.end_src != "due" and d.at == bar.end):
            return d
    return governing_deadline(cfg, now)


def today_rows(active: list[Lane], cfg: Config, now: datetime, axis_b: datetime, est: dict[str, Estimate] | None = None, floor: timedelta | None = None) -> tuple[list[Bar], list[Bar]]:
    """Own bars: running lanes and lanes whose cited end falls by the axis end. Everything else folds.

    A derived end folds too when its slot is narrower than `floor` (the axis tick): a session with five
    lanes and four hours left would otherwise unfold into five 48-minute slivers exactly when the strip
    most needs reading. The estimate survives in the folded row's `.member` span.
    """
    own, folded = [], []
    for lane in active:
        b = day_bar(lane, cfg, now, est)
        narrow = b.est is not None and floor is not None and b.est.slot < floor
        if (lane.kind == "running" and lane.state_time) or (b.end_src != "deadline" and b.end <= axis_b and not narrow):
            own.append(b)
        else:
            folded.append(b)
    return own, folded


def week_axis_end(cfg: Config, week_a: datetime) -> datetime:
    last = max([d.at for d in cfg.deadlines] + [week_a])
    day_after_last = datetime.combine(last.date() + timedelta(days=1), time(0, 0), tzinfo=week_a.tzinfo)
    return max(min(week_a + timedelta(days=WEEK_DAYS), day_after_last), week_a + timedelta(days=1))


def week_rows(active: list[Lane], cfg: Config, now: datetime, week_a: datetime, week_b: datetime, est: dict[str, Estimate] | None = None) -> list[Bar | Summary]:
    """Running lanes get a bar. Lanes with a concrete due group by due day (a lone lane keeps its bar); overdue and
    past-the-axis lanes get one row each. The rest fold into one row per deadline. Returned in drawing order."""
    keyed: list[tuple[tuple, Bar | Summary]] = []
    days: dict[date, list[Bar]] = {}
    overdue: list[Bar] = []
    later: list[Bar] = []
    groups: dict[Deadline, list[Bar]] = {}
    instants = {d.at for d in cfg.deadlines}
    for lane in active:
        b = week_bar(lane, cfg, now, est)
        named = any(d.name.lower() == lane.due.strip().lower() for d in cfg.deadlines)
        if lane.kind == "running" and lane.state_time:
            keyed.append(((1, b.start), b))
        elif b.end_src in ("due", "derived") and not named and b.end not in instants:
            if b.end < week_a:
                overdue.append(b)
            elif b.end >= week_b:
                later.append(b)
            else:
                days.setdefault(b.end.date(), []).append(b)
        else:
            groups.setdefault(_deadline_for(lane, b, cfg, now), []).append(b)
    if overdue:
        keyed.append(((0,), Summary("overdue", f"due {min(b.end for b in overdue):%a %d}", week_a, max(b.end for b in overdue), tuple(overdue), f"Overdue · {_lanes(len(overdue))}", "group")))
    for day, bars in days.items():
        if len(bars) == 1:
            keyed.append(((2, bars[0].end), bars[0]))
            continue
        end = max(b.end for b in bars)
        due_n, est_n = sum(b.end_src == "due" for b in bars), sum(b.end_src == "derived" for b in bars)
        label = " · ".join(part for part, n in ((f"{due_n} due", due_n), (f"{est_n} est.", est_n)) if n)
        keyed.append(((2, end), Summary(day.isoformat(), label, max(week_a, min(b.start for b in bars)), end, tuple(bars), f"{day:%a %d} · {_lanes(len(bars))}", "group")))
    if later:
        keyed.append(((3,), Summary("later", f"due {min(b.end for b in later):%a %d}", week_a, max(b.end for b in later), tuple(later), f"Later · {_lanes(len(later))}", "group")))
    for d, bars in groups.items():
        label = f"→ {d.name}" + (f" {d.at:%a %d}" if d.at > week_b else "")
        keyed.append(((4, d.at), Summary(d.name, label, week_a, d.at, tuple(bars))))
    return [row for _, row in sorted(keyed, key=lambda kr: kr[0])]


def _tick_step(span: timedelta) -> timedelta:
    hours = span.total_seconds() / 3600
    for step in TICK_STEPS_H:
        if int(hours // step) + 1 <= MAX_TICKS:
            return timedelta(hours=step)
    return timedelta(hours=TICK_STEPS_H[-1])


def _inline(text: str) -> str:
    """Escape, then render the three inline Markdown marks requirement lists use: `code`, **strong**, *em*."""
    out = _esc(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    return re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<em>\1</em>", out)


def requirements_section(d: Deadline, text: str | None) -> str:
    if text is None:
        return f'<p class="warn" data-requirements-missing="{_esc(d.name)}">{_esc(d.name)} requirements file not found: {_esc(d.requirements or "")}</p>'
    groups = parse_requirements(text)
    items = [r for _, reqs in groups for r in reqs]
    done, total = sum(r.done for r in items), len(items)
    out = [
        f'<section class="reqs" data-deadline="{_esc(d.name)}" data-done="{done}" data-total="{total}">',
        f"<h2>{_esc(d.name)} requirements · {done} of {total}</h2>",
        f'<div class="meter"><span style="width:{(done / total * 100) if total else 0:.1f}%"></span></div>',
    ]
    for name, reqs in groups:
        g_done = sum(r.done for r in reqs)
        opened = " open" if g_done < len(reqs) else ""
        out.append(f'<details class="req-group"{opened}><summary>{_esc(name)} · {g_done} of {len(reqs)}</summary><ul>')
        for r in reqs:
            evidence = f'<span class="evidence">{_inline(r.evidence)}</span>' if r.evidence else ""
            out.append(f'<li class="req {"done" if r.done else "open"} depth-{r.depth}"><span class="box">{"☑" if r.done else "☐"}</span><span class="text">{_inline(r.text)}</span>{evidence}</li>')
        out.append("</ul></details>")
    out.append("</section>")
    return "\n".join(out)


def render(tracker_text: str, log_text: str, cfg: Config, now: datetime, requirements: dict[str, str | None] | None = None) -> str:
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
    est = estimates(active, cfg, now, history(lanes, cfg, now))
    day_bars, day_folded = today_rows(active, cfg, now, axis_b, est, _tick_step(axis_b - (now - timedelta(hours=1))))
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
    day_summaries = [Summary("today", "no estimate, or due or estimated after today", axis_a, axis_b, tuple(day_folded))] if day_folded else []

    # Week strip: today to the day after the last deadline, at most 7 days, one column per day.
    week_a = datetime.combine(today, time(0, 0), tzinfo=zone)
    week_b = week_axis_end(cfg, week_a)
    week = week_rows(active, cfg, now, week_a, week_b, est)
    week_bars = [r for r in week if isinstance(r, Bar)]
    week_groups = [r for r in week if isinstance(r, Summary) and r.attr == "group"]
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
            size = f' data-size="{_esc(lane.size)}"' if lane.size.strip() else ""
            rows.append(f'<tr data-state="{_esc(lane.kind)}"{size}><td>{item_cell(lane.item)}{warn}</td><td>{_esc(lane.owner)}</td><td>{_esc(lane.state)}</td><td>{_esc(lane.since)}</td><td>{_esc(lane.due)}</td><td>{_esc(lane.size)}</td></tr>')
        return "\n".join(rows) or '<tr><td colspan="6" class="muted">none</td></tr>'

    queue = [lane for lane in active if lane.owner.strip().lower() == cfg.user.lower()]
    unassigned = [lane for lane in active if lane.owner.strip().lower() in ("unassigned", "")]
    unassigned_rows = "\n".join(f"<tr><td>{_esc(short_name(l.item))}</td><td>{_esc(l.due)}</td><td>{_esc(l.state)}</td></tr>" for l in unassigned)
    unassigned_html = f"""<h2>Unassigned · {len(unassigned)}</h2>
<table><tr><th>item</th><th>due</th><th>state</th></tr>
{unassigned_rows}
</table>
""" if unassigned else ""
    queue_rows = "\n".join(f"<tr><td>{_esc(short_name(l.item))}</td><td>{_esc(l.due)}</td><td>{_esc(l.state)}</td></tr>" for l in queue) or '<tr><td colspan="3" class="muted">nothing waiting on you</td></tr>'
    running = [l for l in active if l.kind == "running"]
    orphaned = [l for l in active if l.kind == "orphaned"]
    waiting = [l for l in active if l.kind not in ("running", "orphaned")]
    def group_head(label: str, group: list[Lane]) -> str:
        return f'<tr class="group"><th colspan="6">{_esc(label)} · {len(group)}</th></tr>'

    orphan_group = f'\n{group_head("orphaned", orphaned)[:-10]} · nobody owns these</th></tr>\n{lane_rows(orphaned)}' if orphaned else ""
    requirements = requirements or {}
    req_decks = [d for d in cfg.deadlines if d.requirements and d.at >= now]
    req_html = "\n".join(requirements_section(d, requirements.get(d.name)) for d in req_decks)
    req_meta = "".join(
        f'\n<meta name="requirements-sha256" data-deadline="{_esc(d.name)}" content="{hashlib.sha256(requirements[d.name].encode()).hexdigest()}">'
        for d in req_decks
        if requirements.get(d.name) is not None
    )
    long_items = sum(1 for lane in lanes if len(lane.item) > LONG_ITEM)
    orphan_note = f" · {sum(1 for l in active if l.kind == 'orphaned')} orphaned" if any(l.kind == "orphaned" for l in active) else ""
    long_note = f" · {long_items} {'lane carries' if long_items == 1 else 'lanes carry'} history in the item cell" if long_items else ""
    tzname = now.strftime("%Z")

    return f"""<meta charset="utf-8">
<title>Board {today.isoformat()}</title>
<link rel="stylesheet" href="{FONTS}">
<meta name="tracker-sha256" content="{sha}">{req_meta}
<style>
:root{{--bg:#f4efe1;--surface:#fbf8ef;--fg:#2f2630;--muted:#6e6470;--line:#ddd3bd;--brass:#a7843e;--band:rgba(111,99,180,.14);--open:#9daa72;--noest:#2f8f86;--fold:#8a7f9c;--running:#6f63b4;--done:#bdb3a2;--dl:#c9533a;--now:#4f6b3a;--est:#b5567a;--bar-ink:#fbf8ef}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#1e1a20;--surface:#29232b;--fg:#efe8d6;--muted:#a89fa8;--line:#3d3440;--brass:#c9a45c;--band:rgba(154,143,218,.18);--open:#6f7f48;--noest:#4fb3a7;--fold:#a093bb;--running:#9a8fda;--done:#5a5058;--dl:#f0775a;--now:#9dbb6e;--est:#d98aa8;--bar-ink:#1e1a20}}}}
:root[data-theme="dark"]{{--bg:#1e1a20;--surface:#29232b;--fg:#efe8d6;--muted:#a89fa8;--line:#3d3440;--brass:#c9a45c;--band:rgba(154,143,218,.18);--open:#6f7f48;--noest:#4fb3a7;--fold:#a093bb;--running:#9a8fda;--done:#5a5058;--dl:#f0775a;--now:#9dbb6e;--est:#d98aa8;--bar-ink:#1e1a20}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 "Alegreya Sans","Gill Sans",system-ui,sans-serif;padding:16px 16px 48px;max-width:1100px;margin:0 auto}}
h1{{font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-size:26px;font-weight:600;letter-spacing:.04em;margin:0 0 2px}}
h2{{font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-size:19px;font-weight:600;letter-spacing:.05em;margin:28px 0 8px;border-bottom:1px solid var(--brass);padding-bottom:4px}}
.header{{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:4px}} .header .logo{{width:260px;max-width:100%;border:1px solid var(--brass);border-radius:4px;display:block}}
.head-text{{flex:1 1 260px;min-width:0}}
.meta{{color:var(--muted);font-size:13px}} .clock{{font-family:ui-monospace,monospace;font-size:14px;font-variant-numeric:tabular-nums;margin:6px 0}}
table{{border-collapse:collapse;width:100%;max-width:100%}} td,th{{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}} th{{color:var(--muted);font-weight:600;font-size:12px;letter-spacing:.04em;text-transform:uppercase}}
tr.group th{{background:color-mix(in srgb,var(--brass) 18%,transparent);color:var(--fg);font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-weight:600;font-size:14px;letter-spacing:.12em;text-transform:uppercase;border-top:2px solid var(--brass);padding:6px 8px}}
.muted{{color:var(--muted)}} .warn{{color:var(--dl);font-size:12px}}
.strip{{position:relative;margin:8px 0 4px;--name-w:30%}} .axis{{position:relative;height:18px;margin-left:var(--name-w);font-size:11px;color:var(--muted)}} .tick{{position:absolute;transform:translateX(-50%);white-space:nowrap}}
.rows{{position:relative}} .row{{display:flex;align-items:center;height:26px}} .name{{width:var(--name-w);flex:none;padding-right:8px;box-sizing:border-box;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}} .track{{position:relative;flex:1;height:18px;border-left:1px solid var(--line)}}
.summary-row .name{{color:var(--muted)}} details.folded>summary{{list-style:none;cursor:pointer}} details.folded>summary::-webkit-details-marker{{display:none}} details.folded>summary .name::before{{content:"\u25b8 "}} details.folded[open]>summary .name::before{{content:"\u25be "}} .row.sub{{height:22px}} .row.sub .name{{padding-left:18px;color:var(--muted);font-size:12px}} .row.sub .bar{{height:14px;line-height:14px;font-size:10px;top:2px}}
.bar{{position:absolute;top:0;height:18px;border-radius:3px;background:var(--open);color:var(--bar-ink);font-size:11px;line-height:18px;padding:0 6px;overflow:hidden;white-space:nowrap;box-sizing:border-box}}
.bar.running{{background:var(--running)}} .bar.orphaned{{background:var(--surface);color:var(--fg);outline:2px dashed var(--dl);outline-offset:-2px}} .bar.open-end{{background:var(--noest)}} .bar.derived{{background:var(--est)}} .bar.summary{{background:var(--fold)}} .bar.clamped{{border-right:3px solid var(--dl)}} .bar.clamped-left{{border-left:3px solid var(--dl)}} .group-row .name{{font-weight:600}} .bar em{{font-style:normal;opacity:.85}} .member{{display:none}}
.overlay{{position:absolute;top:0;bottom:0;left:var(--name-w);right:0;pointer-events:none}}
.band{{position:absolute;top:0;bottom:0;background:var(--band)}}
.grid{{position:absolute;top:0;bottom:0;border-left:1px solid var(--line)}} .grid.half{{border-left:1px dotted var(--line);opacity:.6}}
.legend{{display:flex;flex-wrap:wrap;gap:4px 14px;margin:6px 0 2px;font-size:12px;color:var(--muted)}} .legend .item{{position:relative;display:inline-flex;align-items:center;gap:5px}}
/* Keys borrow the chart classes for colour only: those are position:absolute, and an absolute key with no positioned ancestor lands in the page corner. */
.legend .key{{position:static;flex:none;display:inline-block;width:18px;height:10px;border-radius:2px;top:auto;bottom:auto;left:auto;right:auto}}
.legend .key.nowline{{width:0;height:12px;border-radius:0;border-left:2px solid var(--now)}} .legend .key.dline{{width:0;height:12px;border-radius:0;border-left:2px dashed var(--dl);transform:none}}
.legend .key.band{{height:12px;background:var(--band);border:1px solid var(--line)}}
.key{{display:inline-block;width:18px;height:10px;border-radius:2px}} .key.bar{{position:static;background:var(--open);padding:0}}
.key.bar.running{{background:var(--running)}} .key.bar.orphaned{{background:var(--surface);outline:2px dashed var(--dl);outline-offset:-2px}} .key.bar.open-end{{background:var(--noest)}} .key.bar.derived{{background:var(--est)}} .key.bar.summary{{background:var(--fold)}}
.key.band{{background:var(--band);border:1px solid var(--line)}} .key.nowline{{width:0;height:12px;border-left:2px solid var(--now);border-radius:0}} .key.dline{{width:0;height:12px;border-left:2px dashed var(--dl);border-radius:0}}
.dline{{position:absolute;top:0;bottom:0;border-left:2px dashed var(--dl);transform:translateX(-1px)}}
.callouts{{margin-left:var(--name-w);margin-top:2px}} .callout{{position:relative;height:16px;font-size:11px;line-height:16px;color:var(--dl);white-space:nowrap}}
.callout span{{position:absolute;top:0;padding-left:4px}} .callout span.right{{padding:0 4px 0 0}} .callout.later{{text-align:right}}
.nowline{{position:absolute;top:0;bottom:0;border-left:2px solid var(--now)}}
details summary{{cursor:pointer}} details[open] summary{{margin-bottom:4px}}
.reqs h2{{display:flex;gap:8px;align-items:baseline}} .meter{{height:4px;background:var(--line);border-radius:2px;margin:-4px 0 8px;overflow:hidden}} .meter span{{display:block;height:100%;background:var(--now)}}
.req-group{{margin:6px 0}} .req-group summary{{font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-weight:600;font-size:15px;letter-spacing:.04em}} .req-group ul{{list-style:none;margin:4px 0 8px;padding:6px 8px;background:var(--surface);border-radius:4px;display:grid;gap:3px}}
.req{{display:grid;grid-template-columns:1.4em 1fr;column-gap:6px;font-size:13px}} .req.depth-1{{margin-left:1.6em}} .req.depth-2{{margin-left:3.2em}} .req.done .text{{color:var(--muted)}}
.req code{{font-family:ui-monospace,monospace;font-size:12px}} .req .evidence{{grid-column:2;color:var(--muted);font-size:12px}} .req .box{{font-variant-numeric:tabular-nums}}
.hist{{list-style:none;margin:4px 0 2px;padding:0 0 0 10px;border-left:2px solid var(--line);font-size:12px;line-height:1.5;color:var(--muted);display:grid;gap:3px}}
.hist li{{display:grid;grid-template-columns:3em 1fr;gap:8px}} .hist time{{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums;color:var(--fg)}}
.graph{{margin:0}} .graph h2{{margin-top:22px}} .graph>.meta{{margin:-2px 0 6px}}
.graph summary{{list-style:none;cursor:pointer;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;padding:2px 0}}
.graph summary::-webkit-details-marker{{display:none}} .graph summary::before{{content:"\u25b8";color:var(--muted);font-size:10px;flex:none;width:.7em}} .graph details[open]>summary::before{{content:"\u25be"}}
.peers{{list-style:none;margin:2px 0 0;padding:0 0 0 14px;border-left:1px solid var(--line)}} .peers>li{{padding:1px 0}}
.kids{{list-style:none;margin:2px 0 6px;padding:0 0 0 22px;font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:0 14px}} .kids li::before{{content:"\u2514\u2009";color:var(--line)}}
.facts{{margin:2px 0 4px;padding:0 0 0 22px;display:grid;grid-template-columns:auto 1fr;gap:1px 8px;font-size:12px}} .facts dt{{color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-size:10px;padding-top:2px}} .facts dd{{margin:0;min-width:0;overflow-wrap:anywhere}}
.who{{font-size:13px}} .edge{{color:var(--muted);font-size:12px}}
.stamp{{margin-left:auto;font-size:11px;font-variant-numeric:tabular-nums;color:var(--muted);white-space:nowrap}}
/* The claim fades, the name never does: a stale node is the one you most need to read. */
.stamp.stale{{color:var(--dl)}} .stamp.never,.stamp.unparsed{{color:var(--dl)}} .node[data-band="aging"]>summary .chip{{opacity:.66}} .node[data-band="stale"]>summary .chip{{opacity:.4}}
.chip{{flex:none;font-size:10px;letter-spacing:.07em;text-transform:uppercase;padding:1px 7px;border-radius:9px;border:1px solid var(--line);color:var(--fg);background:var(--surface)}}
.chip.planning{{background:var(--noest);color:var(--bar-ink);border-color:var(--noest)}}
.chip.working{{background:var(--running);color:var(--bar-ink);border-color:var(--running)}}
.chip.waiting{{background:var(--brass);color:var(--bar-ink);border-color:var(--brass)}}
.chip.idle{{background:var(--done);color:var(--fg);border-color:var(--done)}}
.chip.starting{{background:var(--open);color:var(--bar-ink);border-color:var(--open)}}
.chip.ready{{background:var(--now);color:var(--bar-ink);border-color:var(--now)}}
.chip.unknown{{background:var(--dl);color:var(--bar-ink);border-color:var(--dl)}}
.chip.unreported{{background:var(--muted);color:var(--bar-ink);border-color:var(--muted)}}
.chip.gone{{background:var(--surface);color:var(--dl);border:1px dashed var(--dl)}}
.chip.coordinator{{background:var(--bg);color:var(--brass);border-color:var(--brass)}}
.resume{{margin:6px 0 0;font-size:13px;color:var(--fg);border-left:3px solid var(--brass);padding:2px 0 2px 8px;display:grid;grid-template-columns:auto 1fr;gap:2px 10px}}
.resume dt{{color:var(--muted);font-weight:600;font-size:10px;letter-spacing:.06em;text-transform:uppercase;padding-top:3px;white-space:nowrap}}
.resume dd{{margin:0;min-width:0;overflow-wrap:anywhere}} .resume dd.long-field{{border-bottom:1px dotted var(--brass)}}
.resume details.long>summary{{color:var(--fg)}} .resume details.long[open]>summary{{color:var(--muted)}}
@media (max-width:520px){{.resume{{grid-template-columns:1fr;gap:0}} .resume dt{{padding-top:4px}}}}
@media (max-width:520px){{.strip{{--name-w:36%}}}}
</style>
<div class="board" data-rendered-at="{_iso(now)}" data-tz="{_esc(cfg.tz)}" data-deadline="{_iso(nearest.at)}" data-deadline-name="{_esc(nearest.name)}">
<div class="header">{logo_tag()}<div class="head-text">
<h1>Board · {now.strftime('%a %d %b')}</h1>
<div class="clock" id="clock">Now — {_esc(tzname)} · {_esc(nearest.name)} ({nearest.at.strftime('%H:%M')} {_esc(tzname)})</div>
<div class="meta">tracker as of {now.strftime('%H:%M')} {_esc(tzname)} <span id="ago"></span> · kept by chief-of-stuff · board {_esc(url or 'not yet published')}{long_note}</div>
{resume_strip(parse_resume(tracker_text))}</div></div>

{session_graph(tracker.sessions, cfg, now, gone_sessions(tracker.lanes, tracker.sessions))}
{unassigned_html}<h2>{_esc(cfg.user)}'s queue</h2>
<table><tr><th>item</th><th>due</th><th>state</th></tr>
{queue_rows}
</table>
{req_html}

<h2>Today</h2>
<div class="meta">{len(day_bars)} scheduled · {len(day_folded)} folded into one row (no estimate, or due or estimated after today) · bands are calendar events · green line is now{orphan_note}</div>
{legend()}{_strip(day_bars + day_summaries, axis_a, axis_b, day_events, [nearest], day_ticks, "day")}

<h2>Week</h2>
<div class="meta">{len(week_bars)} bars · {len(week_groups)} rows grouped by due day · the rest folded into one row per deadline · dashed lines are deadlines</div>
{_strip(week, week_a, week_b, [], list(cfg.deadlines), week_ticks, "week")}

<h2>Lanes</h2>
<table><tr><th>item</th><th>owner</th><th>state</th><th>since</th><th>due</th><th>size</th></tr>
{group_head("running", running)}
{lane_rows(running)}{orphan_group}
{group_head("open · waiting", waiting)}
{lane_rows(waiting)}
{group_head("done", done)}
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
    req_texts = {d.name: ((root / d.requirements).read_text() if (root / d.requirements).is_file() else None) for d in cfg.deadlines if d.requirements}
    page = render(tracker_text, log_text, cfg, now, requirements=req_texts)
    out.write_text(page)
    parsed = parse_tracker(tracker_text)
    hist = history(list(parsed.lanes), cfg, now)
    est = estimates([lane for lane in parsed.lanes if lane.kind != "done"], cfg, now, hist)
    bars = [day_bar(lane, cfg, now, est) for lane in parsed.lanes]
    sized = " ".join(f"{k or '?'}:{hist[k][1]}" for k in (*SIZES, "") if k in hist)
    hist_note = f"{sized} · all:{hist[ALL_SIZES][1]}" if hist else "none"
    horizon_name = horizon(cfg, now)[1]
    no_est = sum(1 for b in bars if b.label == NO_ESTIMATE)
    derived = sum(1 for b in bars if b.end_src == "derived")
    long_items = sum(1 for lane in parsed.lanes if len(lane.item) > LONG_ITEM)
    block = parse_resume(tracker_text)
    resume_long = sum(1 for k in resume_fields(block) if len(block[k]) > RESUME_LINE)
    # A count printed beside a warning count but not counted as one reads as telemetry: `resume_long=4`
    # sat next to `warnings=0` for three hours while the block degraded, and was read past every time.
    warnings = sum(1 for lane in parsed.lanes if lane.warning) + sum(1 for s in parsed.sessions if s.warning) + resume_long
    print(out)
    req_counts = ",".join(f"{name}:{sum(r.done for _, rs in parse_requirements(t) for r in rs)}/{sum(len(rs) for _, rs in parse_requirements(t))}" for name, t in req_texts.items() if t is not None)
    missing = sum(1 for t in req_texts.values() if t is None)
    drawn = resume_fields(block)
    resume_note = f"{len(drawn)} fields" if drawn else "none"
    print(f"lanes={len(parsed.lanes)} no_estimate={no_est} derived={derived} warnings={warnings} long_items={long_items} resume={resume_note} resume_long={resume_long} requirements={req_counts or 'none'} requirements_missing={missing} history={hist_note} horizon={horizon_name} tracker_sha256={hashlib.sha256(tracker_text.encode()).hexdigest()[:12]}")
    return out


if __name__ == "__main__":
    main()
