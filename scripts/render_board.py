"""Render today's board from the tracker.

The board is a view of the tracker and workspace configuration: every bar start and end cites a tracker
field (`since`, `due`, `running HH:MM`, `done HH:MM`) or falls open to the nearest deadline from the
`## Coordinator` block or a precise `deadline:` Decisions row, labelled "no estimate". The page carries
the tracker's sha256, the render instant, and the deadline instants, and draws its own clock and now-lines
client-side.

The charts draw the schedule only. Today: running tasks and tasks that end today get a bar; every
other active task folds into one summary row, and tasks done inside the window fold into one Done row
that opens onto them. The Today axis runs 8 hours behind this hour and 16 ahead. Week: running tasks and tasks with a concrete due get
a bar, grouped into one row per due day when several share it (overdue and past-the-week tasks get one row
each); the rest fold into one row per deadline. The Week axis spans at most 7 days; later deadlines are an
edge marker. Folded tasks keep their citations as `.member`
spans. Full item text appears only in the Tasks table. A standing session's
placeholder row (`impl02: standing implementer …`) is not a task and appears only on the Sessions graph.

A deadline line may name a requirements file (`- Final: 2026-09-20 12:00; requirements `path``): checkbox
lines under `## ` headings, evidence after ` — evidence: `. The board shows it, with a done count, for
every deadline that has not passed.

    python3 render_board.py --date 2026-09-16 [--root <workspace>]

Writes `<date>-board.html` into the Board line's `dir` (served separately by pages.py), or beside the
tracker when it names none, and prints its path and one summary line. It never starts a server or browser.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import re
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backlog import Backlog, BacklogError, GitHubBacklog, issue_ref, parse_backlog  # noqa: E402
from settings import SettingsError, load as load_settings  # noqa: E402

HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
# `done 21:16–22:05`: the running start kept on close. An en dash or a hyphen, because two hands write it.
RAN = re.compile(r"^(\d{1,2}:\d{2})[–-](\d{1,2}:\d{2})$")
SIZES = ("S", "M", "L", "XL")
ALL_SIZES = "all"
TASK_COLS = ("item", "owner", "state", "since", "due", "checklist")
TASK_COLS_SIZED = ("item", "owner", "state", "since", "due", "size", "checklist")
TASK_COLS_NAMED = ("name", "item", "owner", "state", "since", "due", "size", "checklist")
# Zach, 2026-09-22 22:20: each task "is actually backed by an entry in github". The issue sits inside the
# span `_anchor` fixes, between `state` and `checklist`, so a stray pipe still re-reads the same way.
TASK_COLS_ISSUED = ("name", "item", "owner", "state", "since", "due", "size", "issue", "checklist")
# #33 (Zach, 2026-09-23): "A lane: the sequence that a task has to move through". `lane` names one from the
# settings file and `stage` is where the task is in it; both sit inside the `_anchor` span like `issue`.
TASK_COLS_LANED = ("name", "item", "owner", "state", "since", "due", "size", "lane", "stage", "issue", "checklist")
# Widest first: a header is matched whole, and the live tracker carries every width while it is rewritten.
TASK_HEADERS = (TASK_COLS_LANED, TASK_COLS_ISSUED, TASK_COLS_NAMED, TASK_COLS_SIZED, TASK_COLS)
DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?$")
DECISION_DEADLINE = re.compile(r"^deadline:\s*(.+?)\s+(?:(\d{4}-\d{2}-\d{2})[ T])?(\d{1,2}):(\d{2})$", re.I)
BULLET = re.compile(r"^(\s*)-\s*([^:]+?):\s*(.*?)\s*$")
CAL_LIST = re.compile(r"^\s*-\s*(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})\s*(?:[A-Z]{2,5}\s+)?(.+?)\s*$")
TIME_RANGE = re.compile(r"^(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})$")
NO_ESTIMATE = "no estimate"
LOGO = Path(__file__).resolve().parent.parent / "assets" / "logo.webp"
FONTS = "https://fonts.googleapis.com/css2?family=Alegreya+Sans:wght@400;500;600&family=Cormorant+SC:wght@600&display=swap"
SHORT_NAME = 48
LONG_ITEM = 80
# Two questions, two numbers. They were one constant, and the day the written rule moved to 300 the guard
# became unsatisfiable: every field could obey `agents/chief-of-stuff.md` exactly and still be counted.
# A guard that cannot be satisfied is one a writer learns to ignore — `resume_long=5` in every render for
# nine hours is how the live `As of` reached 19 044 characters.
RESUME_FOLD = 160   # display: too long to read inline in a six-row strip, so it folds
RESUME_CAP = 300    # hygiene: the bound `agents/chief-of-stuff.md` gives the writer. Keep the two in step.
# The fold bounds what a long field shows closed; nothing bounded what it shows OPEN, and one 9 400-character
# `As of:` field filled the viewport with the rest of the board below it. `.resume details.long[open]` caps it.
# The comment lives here rather than in the stylesheet: CSS prose ships in every rendered page.
# The lookbehinds are zero-width so the mark that opens a bold clause survives the time being lifted
# out of it: `**22:19: copied**` keeps its `**` and loses only the time.
CLAUSE_TIME = re.compile(r"(?:^|(?<=\s)|(?<=\*\*))(?:at\s+)?(\d{1,2}:\d{2}):?(?=\s|$|[,;)])")
REQ_LINE = re.compile(r"^(\s*)- \[([ xX])\]\s+(.*?)\s*$")
EVIDENCE = " — evidence: "
TICK_STEPS_H = (1, 2, 3, 4, 6)
WEEK_DAYS = 7
# The day strip's window around this hour (Zach, 2026-09-22: "the full rolling 24 hour period - 8 hours
# before, 16 after").
DAY_BEHIND = timedelta(hours=8)
DAY_AHEAD = timedelta(hours=16)
MAX_TICKS = 9


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Deadline:
    name: str
    at: datetime
    requirements: str | None = None
    # "all": unnamed tasks fall to it (the default). "named": only tasks whose `due` names it — an exam
    # governs no task on the table, and a derived end toward it would make it look like a plan (finding 73).
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
    # The `Board:` line's ``dir `X` ``: where the board is written for pages.py to serve. None writes it
    # beside the tracker.
    pages_dir: str | None = None
    # The parsed `Backlog:` line, GitLab or GitHub. None when there is no line: the issue rule binds only
    # where there is a tracker to back a task with.
    backlog: Backlog | GitHubBacklog | None = None
    # `Settings:` — the workspace's chief-of-stuff.toml, relative to the root (settings.py reads it).
    settings_path: str | None = None

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def tracker_path(self, day: str) -> str:
        return self.tracker_template.replace("<date>", day)

    def log_path(self, day: str) -> str:
        return f"{self.log_dir.rstrip('/')}/{day}.md"


@dataclass(frozen=True)
class Task:
    item: str
    owner: str
    state: str
    since: str
    due: str
    checklist: str
    warning: str = ""
    size: str = ""
    name: str = ""
    issue: str = ""
    lane: str = ""
    stage: str = ""

    @property
    def needs_issue(self) -> bool:
        """A task not yet done. A done row with no issue was closed before the rule and is left alone."""
        return self.kind != "done" and not self.standing

    @property
    def label(self) -> str:
        """What the task is called. A written `name` is the coordinator's judgement and stands;
        with no name column the board falls back to guessing the name out of the prose, as it always has."""
        return self.name.strip() or short_name(_unmark(self.item))

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
    def standing(self) -> bool:
        """A standing session's placeholder, not a task: `impl02: standing implementer. … wait idle`, or a name
        `impl02 — standing implementer`, owned by that same session. spawn_session.py cannot start a session
        without a row, so a session spawned with no task was given one (design inputs #83, #93, #94)."""
        return self.standing_for(_bare_name(self.owner))

    def standing_for(self, session: str | None) -> bool:
        """This row is `session`'s standing placeholder, whoever owns it yet: at dispatch it is still `unassigned`."""
        if not session:
            return False
        heads = (STANDING_ITEM.match(_unmark(self.item).strip()), STANDING_NAME.match(self.name.strip()))
        return any(m and m.group(1).lower() == session.lower() for m in heads)

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
# The Tasks rule's own vocabulary, as a whole cell. It is what makes an over-wide row recoverable: every other
# task column is prose, a date or blank, and none of them can be told apart from the item they drifted out of.
# A `done` state may carry the sha that landed it (`done 21:16–22:05 db4aa3b`), which `audit_tasks.py` needs to
# clear a task from `merge-base --is-ancestor` rather than from the owner's tree. Without this the one task
# carrying its own evidence would be the one task `_anchor` could not recover from a stray pipe.
# Hex and 7–40 wide, so it stays recognisable ON SIGHT: `done, merged by impl-2` is prose, and admitting prose
# here would let an item cell claim the anchor.
TASK_STATE = re.compile(
    r"(?i)^(?:open|waiting|orphaned|running\s+\d{1,2}:\d{2}"
    r"|done(?:\s+\d{1,2}:\d{2}(?:[\u2013-]\d{1,2}:\d{2})?)?(?:\s+[0-9a-f]{7,40})?)$")
# A standing session's row is not a task (Zach, 2026-09-22 19:28: "we don't finish LANES, we finish TASKS").
STANDING_ITEM = re.compile(r"(?i)^([\w.-]+):\s+standing\b")
STANDING_NAME = re.compile(r"(?i)^([\w.-]+)\s+[\u2014\u2013-]\s+standing\b")
READY = re.compile(r"(?i)\bready for decommissioning\b")

# A task whose state says it is still going while its own item cell shouts DONE disputes itself.
# Nothing else reads a task's state: TASK_STATE anchors a stray pipe (`_anchor`) and validates
# nothing, so four of eleven live tasks carried a stale state at 03:56 — two overstating progress,
# two understating it — and no instrument saw one of them. A freshness check would need to know
# about the world; this needs only the row, which already carries its own contradiction.
# Case-sensitive, and the same vocabulary `scripts/migrate_backlog.py` uses: a shouted DONE is a
# claim about this row, `resolved by the release` is an ordinary sentence. Matching case-insensitively
# flagged 17 rows there, mostly the latter. A false flag is noisy and self-clearing; a false clear
# is silent and permanent, so this errs loud.
DONE_WORDS = re.compile(r"\b(?:DONE|FINISHED|LANDED|CLEARED|CANCELLED|SUPERSEDED|RESOLVED|DELIVERED)\b")


def _disputes_itself(state: str, item: str) -> str:
    """A task's state cell read against the task's own prose. Empty when the row agrees with itself."""
    if state.strip().lower().startswith("done"):
        return ""
    said = DONE_WORDS.search(item)
    return f"state {state.strip()!r} but the item says {said.group(0)}" if said else ""

# `Zach — A2, A3, B`: the target, then why. An em dash, an en dash or a hyphen, because three
# different sessions have written this cell and they did not agree.
WAITS = re.compile(r"\s+[—–-]\s+|,\s+")
# `nothing; the stack-default fix landed` is what a live row says when a session waits on no one.
# Drawn literally it is an edge to a node called "nothing".
NOBODY = re.compile(r"(?i)^(?:(?:nothing|none|nobody|n/?a)\b|[-—–]\s*$)")
# The name cell accretes provenance: `update-claude-md-docs (third name, same ref)`.
NAME_TAIL_TIME = re.compile(r"[\s,;–—-]*(?:at\s+)?\d{1,2}:\d{2}\**$")
PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")
SESSION_REF = re.compile(r"\s*\[[0-9a-f]{4,}\]\s*")


def _bare_name(name: str) -> str:
    """`demo-fixes [b8fca1] (fix 5)` → `demo-fixes`. Both sides of a join have to strip the same things."""
    return PARENTHETICAL.sub("", SESSION_REF.sub(" ", _unmark(name))).strip().lower()


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
    tasks: tuple[Task, ...]
    sessions: tuple[Session, ...] = ()


# The four kinds of the body track. A body kind is recognised by its own name as a whole word in the
# event's title and by nothing else: the design names these four, and a wider vocabulary (workout,
# breakfast, lunch, dinner on their own) is a decision rather than an implementation of it.
BODY_KINDS = ("sleep", "eat", "gym", "recreation")
BODY_WORD = {k: re.compile(rf"(?i)\b{k}\b") for k in BODY_KINDS}


def body_kind(title: str) -> str:
    """The body kind an event's title names, or "" when it names none."""
    for kind in BODY_KINDS:
        if BODY_WORD[kind].search(title):
            return kind
    return ""


@dataclass(frozen=True)
class Event:
    start: datetime
    end: datetime
    title: str

    @property
    def kind(self) -> str:
        return body_kind(self.title)


@dataclass(frozen=True)
class Body:
    """The body track: one row whose track carries every body event on the axis, in clock order."""
    segments: tuple[Event, ...]


@dataclass(frozen=True)
class Bar:
    item: str
    owner: str
    name: str
    kind: str
    start: datetime
    end: datetime
    start_src: str
    end_src: str
    label: str | None
    est: "Estimate | None" = None
    size: str = ""
    deadline: str = ""


@dataclass(frozen=True)
class Swim:
    """A swimlane header: the deadline a run of rows answers to. The rows under it are its owners."""
    name: str
    count: int


@dataclass(frozen=True)
class Summary:
    key: str
    label: str
    start: datetime
    end: datetime
    members: tuple[Bar, ...]
    name: str = ""
    attr: str = "summary"


def _unmark(text: str) -> str:
    """`**bold**` and `` `code` `` → their words. For a name that cannot wrap: the marks go, and a cut can never split a pair."""
    return re.sub(r"`([^`]+)`", r"\1", re.sub(r"\*\*(.+?)\*\*", r"\1", text))


def _whole(text: str) -> bool:
    """A candidate that ends inside a `**` span hands the rest of the item an orphan mark, and the splitter reads it as prose."""
    return text.count("**") % 2 == 0


def short_name(item: str) -> str:
    """The item up to its first ": " (when that leaves a real name), then up to " (" if still long.

    Both cuts are structural: they find where the name ends and the history begins. Neither loses a
    letter of the name it returns. A name with letters missing is not a shorter name, it is a wrong
    one, and the board has no width that a character cut would be measuring — a `<td>` wraps. The
    cut belongs to a caller that really is one line wide; `clip_name` is that, and `audit_tasks.py`
    printing one finding per terminal line is who calls it.
    """
    name = item.strip()
    head = name.split(": ", 1)[0]
    if head != name and len(head) >= 12 and _whole(head):
        # `**Deploy main.** 21:16: laid out the state` splits after the clock, not before it, so the
        # head comes away holding a time the writer did not put in the name. It costs more than it
        # looks: `history_lines` strips the name off the front of the item to get the history, and a
        # name the item does not literally contain matches nothing — so the headline is printed again
        # as the first history line, under a time column already showing the same clock. Only this
        # branch glues one on; a sentence the writer ended with a time is their sentence.
        name = NAME_TAIL_TIME.sub("", head) or head
    if len(name) > SHORT_NAME and " (" in name and _whole(name.split(" (", 1)[0]):
        name = name.split(" (", 1)[0]
    if len(name) > SHORT_NAME:
        clause = _head_clause(name)
        if clause != name and _whole(clause):
            name = clause
    return name


def _head_clause(text: str) -> str:
    """The first sentence, its punctuation kept. Where an item's name ends when nothing else says.

    `_depth0_split` answers the same question and is the right one to ask — the name has to end where
    the history begins, or the cell loses a clause between the two. It hands its parts back stripped,
    though, and `Session TTL fix, orphaned.` without the full stop is not what the writer wrote.
    """
    parts = _depth0_split(text)
    if len(parts) < 2:
        return text
    end = text.find(parts[0])
    if end < 0:
        return text
    end += len(parts[0])
    return text[: end + 1] if text[end : end + 1] in ".;" else text[:end]


def clip_name(item: str, cap: int = SHORT_NAME) -> str:
    """`short_name` for a medium with a real width, capped on a word boundary.

    Every cut stops at a whole number of mark spans: `**22:19: copied**` holds a ": " and a full stop,
    and cutting through it leaves `**` on one side and the rest unbalanced.
    """
    name = short_name(item)
    if len(name) > cap:
        cut = name[:cap]
        if not _whole(cut):
            # Trimming back to a whole number of mark spans empties the cut when the only opening
            # mark is the first character, and a bare `…` is not a short name, it is the loss of one.
            trimmed = cut[: cut.rfind("**")]
            cut = trimmed if trimmed.strip(" ,;:–—-") else _unmark(name)[:cap]
        space = cut.rfind(" ")
        name = (cut[:space] if space > cap // 2 else cut).rstrip(" ,;:–—-") + "…"
    return name


def _depth0_split(text: str) -> list[str]:
    """Split on "; " and ". " outside parentheses and outside a `**bold**` span.

    A mark pair is a span like a parenthesis. A bold clause routinely holds a full stop, and cutting
    through one leaves half a mark on each side — 21 live history bullets read `**…` for that reason.
    An item with an odd number of marks is already unbalanced, and tracking them there would swallow
    every later split, so the tracking is switched off for it.
    """
    parts, depth, start, i, marked = [], 0, 0, 0, False
    spans = text.count("**") % 2 == 0
    while i < len(text):
        if spans and text[i : i + 2] == "**":
            marked = not marked
            i += 2
            continue
        c = text[i]
        depth += c == "("
        depth -= c == ")" and depth > 0
        if depth == 0 and not marked and c in ";." and text[i + 1 : i + 2] == " ":
            parts.append(text[start:i])
            start = i + 2
            i += 1
        i += 1
    parts.append(text[start:])
    return [p.strip().rstrip(".").strip() for p in parts if p.strip().rstrip(".").strip()]


def history_lines(item: str, label: str | None = None) -> list[tuple[str, str]]:
    """What an item cell carries past its name, one (time, text) per clause; the first HH:MM outside parentheses is pulled out.

    `label` is the task's written name when it has one. The coordinator writes it as a bold headline
    on the front of the cell (`**Deploy main.** 21:16: …`), so the headline is the name said twice:
    strip it, whichever form it is in, rather than printing its asterisks.
    """
    full = item.strip()
    short = label or short_name(item)
    for head in ([f"**{label}.**", f"**{label}**", label] if label else []) + [short]:
        if head and full.startswith(head):
            short = head
            break
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
                # The strip cannot see past an opening mark: `** : copied` is what lifting the time leaves.
                clause = re.sub(r"^(\*\*)[\s,:]+", r"\1", clause)
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


def _first_path(value: str) -> str | None:
    """The path a line names: its first backtick span, else its first word. What follows is prose."""
    m = re.search(r"`([^`]+)`", value)
    words = value.split()
    return m.group(1).strip() if m else (words[0] if words else None)


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
            scope = "named" if any(re.match(r"(?i)named tasks only\s*$", o) for o in options) else "all"
            deadlines.append(Deadline(name, datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 23), int(m[5] or 59), tzinfo=zone), reqs, scope))
    tracker = re.search(r"`([^`]+)`", top["Tracker"])
    board_tool = board_url = pages_dir = None
    if "Board" in top:
        parts = [p.strip() for p in top["Board"].split(";")]
        board_tool = _unquote(parts[0]) or None
        for p in parts[1:]:
            m = re.match(r"(?i)url\s+(\S+)", p)
            if m:
                board_url = m.group(1)
            m = re.match(r"(?i)dir\s+`([^`]+)`", p)
            if m:
                pages_dir = m.group(1)
    backlog = None
    if "Backlog" in top:
        try:
            backlog = parse_backlog(top["Backlog"])
        except BacklogError as e:
            raise ConfigError(f"`Backlog:` line: {e}") from None
    return Config(
        user=_unquote(top["User"]),
        log_dir=_unquote(top["Daily log dir"]),
        tracker_template=tracker.group(1) if tracker else _unquote(top["Tracker"]),
        tz=tz,
        deadlines=tuple(sorted(deadlines, key=lambda d: d.at)),
        board_tool=board_tool,
        board_url=board_url,
        pages_dir=pages_dir,
        backlog=backlog,
        settings_path=_first_path(top.get("Settings", "")),
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
# What is left of a mark once the pairs are gone: an unclosed `**` or a lone backtick.
MARK_LEFTOVER = re.compile(r"\*\*|`")


def _clip(value: str, cap: int) -> str:
    """Cut on a word boundary and say so. A clipped line that does not admit it is just a wrong line."""
    if len(value) <= cap:
        return value
    head = value[:cap].rsplit(" ", 1)[0].rstrip(" ,;·—–-")
    return f"{head or value[:cap]} …"


def _summary_text(value: str) -> str:
    """The plain-text head of a fold. It is escaped and never inlined, so no mark belongs in it.

    Two ways one used to arrive, with the same look on the board. `_unmark` strips pairs, so clipping
    first cuts a long `**` span in half and leaves a mark with no partner for it to match — hence
    unmark, then clip. And a mark the writer never closed has no partner to begin with, which no
    ordering fixes; the summary is plain text either way, so what is left of a mark is dropped.
    """
    return _clip(MARK_LEFTOVER.sub("", _unmark(value)), RESUME_CLIP)


def _resume_value(value: str) -> str:
    """A long field folds, the way `item_cell()` folds a long task item. Same board, same idiom."""
    if len(value) <= RESUME_FOLD:
        return _inline(value)
    return f'<details class="long"><summary>{_esc(_summary_text(value))}</summary>{_inline(value)}</details>'


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
        long = ' class="long-field"' if len(block[k]) > RESUME_FOLD else ""
        rows.append(f"<dt>{_esc(k)}</dt><dd{long}>{_resume_value(block[k])}</dd>")
    rows = "".join(rows)
    return f'<dl class="resume">{rows}</dl>\n'


def _split_row(line: str) -> list[str]:
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


def _cells(line: str) -> list[str]:
    return [c.strip() for c in _split_row(line)]


def with_decision_deadlines(cfg: Config, tracker_text: str, tracker_day: date,
                            *, min_day: date | None = None) -> Config:
    """Project precise Decisions-row deadlines into the board's configured deadline list.

    A time without a date belongs to the tracker day, even when an older board is rendered later.
    If a name is decided more than once, the decision recorded at the latest time wins. A decision
    that gives no exact time cannot supply an instant and leaves the existing configuration alone.
    """
    rows = [line for line in _section(tracker_text, "## Decisions") if line.strip().startswith("|")]
    if not rows:
        return cfg
    header = [cell.lower() for cell in _cells(rows[0])]
    if "item" not in header:
        return cfg
    item_col = header.index("item")
    time_col = header.index("time") if "time" in header else None
    base = {deadline.name.casefold(): deadline for deadline in cfg.deadlines}
    decided: dict[str, tuple[time | None, Deadline]] = {}
    for row in rows[1:]:
        cells = _cells(row)
        if _is_separator(cells) or item_col >= len(cells):
            continue
        item, *options = [part.strip() for part in _unmark(cells[item_col]).split(";")]
        match = DECISION_DEADLINE.fullmatch(item)
        if not match:
            continue
        name, day_text, hour_text, minute_text = match.groups()
        name = name.strip(" ,—–-")
        if not name:
            continue
        try:
            day = date.fromisoformat(day_text) if day_text else tracker_day
            at = datetime.combine(day, time(int(hour_text), int(minute_text)), tzinfo=cfg.zone)
        except ValueError:
            continue
        if min_day is not None and at.date() < min_day:
            continue
        key = name.casefold()
        old = base.get(key)
        requirements = next((r.group(1) for option in options
                             if (r := re.fullmatch(r"(?i)requirements\s+`?([^`]+?)`?", option))),
                            old.requirements if old else None)
        scope = ("named" if any(re.fullmatch(r"(?i)named tasks only", option) for option in options)
                 else old.scope if old else "all")
        recorded = None
        if time_col is not None and time_col < len(cells):
            stamp = HHMM.fullmatch(cells[time_col])
            if stamp:
                try:
                    recorded = time(int(stamp[1]), int(stamp[2]))
                except ValueError:
                    pass
        previous = decided.get(key)
        if previous is None or (recorded is not None and (previous[0] is None or recorded > previous[0])):
            decided[key] = (recorded, Deadline(old.name if old else name, at, requirements, scope))
    if not decided:
        return cfg
    base.update({name: deadline for name, (_, deadline) in decided.items()})
    return replace(cfg, deadlines=tuple(sorted(base.values(), key=lambda deadline: deadline.at)))


def with_workspace_decision_deadlines(root: Path, cfg: Config, tracker_text: str, tracker_day: date) -> Config:
    """Carry future deadlines forward from earlier daily trackers, then apply today's decisions."""
    template = cfg.tracker_template
    if template.count("<date>") != 1:
        return with_decision_deadlines(cfg, tracker_text, tracker_day)
    prefix, suffix = template.split("<date>")
    history = []
    for path in root.glob(f"{prefix}*{suffix}"):
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            continue
        relative = path.relative_to(root).as_posix()
        if not relative.startswith(prefix) or not relative.endswith(suffix):
            continue
        token = relative[len(prefix):len(relative) - len(suffix) if suffix else None]
        try:
            day = date.fromisoformat(token)
        except ValueError:
            continue
        if day < tracker_day:
            history.append((day, path))
    for day, path in sorted(history):
        cfg = with_decision_deadlines(cfg, path.read_text(), day, min_day=tracker_day)
    return with_decision_deadlines(cfg, tracker_text, tracker_day)


def _anchor(raw: list[str], cols: tuple[str, ...]) -> list[str] | None:
    """A row wider than its table is one cell that grew a pipe. Re-read it from its state column — the one cell
    of a task row a machine can recognise on sight: `item` absorbs the excess on its left and `checklist` the
    excess on its right, which is where the prose, and so the stray pipe, lives. Exactly one cell may claim the
    anchor; otherwise the row is left as it was written and only the warning speaks."""
    hits = [i for i, c in enumerate(raw) if TASK_STATE.match(c.strip())]
    if len(hits) != 1:
        return None
    s, state = hits[0], cols.index("state")
    if s < state or len(raw) - s < len(cols) - state:
        return None
    head = ["|".join(raw[: s - state + 1])] + raw[s - state + 1 : s]
    tail = raw[s + 1 : s + len(cols) - state - 1] + ["|".join(raw[s + len(cols) - state - 1 :])]
    return head + [raw[s]] + tail


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
    tasks: list[Task] = []
    # `## Lanes` is the heading a tracker had before #33 made every row a task.
    rows = [line for line in _section(text, "## Tasks") or _section(text, "## Lanes") if line.strip().startswith("|")]
    # The header decides the width: a six-column table parses as it always has, every task unsized and unnamed.
    header = [c.lower() for c in _cells(rows[0])] if rows else []
    cols = next((h for h in TASK_HEADERS if header == list(h)), TASK_COLS)
    for row in rows[1:]:
        raw = _split_row(row)
        cells = [c.strip() for c in raw]
        if _is_separator(cells):
            continue
        warning = "" if len(cells) == len(cols) else f"{len(cells)} cells, expected {len(cols)}"
        if len(cells) > len(cols):
            fixed = _anchor(raw, cols)
            cells = [c.strip() for c in fixed] if fixed else cells
        fields = dict(zip(cols, (cells + [""] * len(cols))[:len(cols)]))
        dispute = _disputes_itself(fields.get("state", ""), fields.get("item", ""))
        if dispute:
            warning = (warning + "; " if warning else "") + dispute
        tasks.append(Task(**fields, warning=warning))
    sessions: list[Session] = []
    for row in [line for line in _section(text, "## Sessions") if line.strip().startswith("|")][1:]:
        cells = _cells(row)
        if _is_separator(cells) or not any(c.strip() for c in cells):
            continue
        sessions.append(_session(cells))
    return Tracker(board_url=url, tasks=tuple(tasks), sessions=tuple(sessions))


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
    """The nearest deadline ahead that unnamed tasks fall to, else end of day.

    A task that names no deadline is drawn or estimated toward this one. A deadline marked `named tasks
    only` is skipped: after Final the nearest deadline is an exam (finding 73), and nothing on the Tasks
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
    """Where task i of an owner's N lands.

    Two bases. `history`: the queue drains at the mean elapsed time of closed tasks of each size (Zach,
    2026-09-18: "generate an average amount of time spent per t-shirt sized puzzle ... use that"). `queue`:
    no task has closed with a range yet, so the queue drains evenly by the horizon — the 0.10.0 model,
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
                rests = f"unsized: the mean of all {self.count} tasks closed with a range, {_dur(self.slot)}"
            elif self.borrowed:
                rests = f"size {self.size}: no {self.size} task has closed with a range, so the mean of all {self.count} closed tasks stands in, {_dur(self.slot)}"
            else:
                rests = f"size {self.size}: mean of {self.count} {self.size} tasks closed with a range, {_dur(self.slot)} each"
            return (f"{self.label}: {rests}; {_ordinal(self.i)} of {self.n} in {owner}'s queue, running first, then oldest first. "
                    "Set a due to override.")
        return (f"{self.label}: where this task lands if {owner}'s queue of {self.n} drains evenly by {self.of}, "
                "running first, then oldest first. Not a claim about effort; set a due to override.")


# The chips over the one task table. `all` first and always checked, then the two owner questions the
# Unassigned and queue tables used to be, then the three tracker states a task can be in.
FILTERS = ("all", "mine", "unassigned", "running", "orphaned", "done")
FILTER_LABELS = {"all": "all", "mine": "yours", "unassigned": "unassigned",
                 "running": "running", "orphaned": "orphaned", "done": "done"}

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


def estimates(active: list[Task], cfg: Config, now: datetime, hist: dict[str, tuple[timedelta, int]] | None = None) -> dict[str, Estimate]:
    """A derived end for every owned task with no `due`, keyed by item. Nothing here is written to the tracker.

    Per owner, the active tasks that are blank or due by the horizon form a queue of N, running first, then
    oldest first. With `hist` (see `history()`), a cursor starts at now and each task takes the mean elapsed
    time of closed tasks of its size — a running task from its own start, never ending before now; a task
    with a `due` takes its time from the queue and keeps its due. Without history no task has a duration to
    learn from, and this reads none — not the item text (its length tracks age, not work left), not the
    Log: task i ends at `H - (N-i)/N x (H - now)`, so task N is the horizon instant, and the label says so.
    Unowned tasks get nothing: there is no queue to place them in, and the deadline they already draw to
    is the honest end. A gone owner — one an `orphaned` task names — is unowned for all its tasks.
    """
    h, of = horizon(cfg, now)
    r = h - now
    left = {_bare_name(task.owner) for task in active if task.kind == ORPHANED} - {""}
    queues: dict[str, list[tuple[tuple, Task]]] = {}
    for idx, task in enumerate(active):
        if task.kind in ("done", ORPHANED):
            continue
        key = _owner_key(task.owner)
        if not key or key in left:
            continue
        due = _resolve_due(task.due, cfg, now.date())
        if due is not None and due > h:
            continue
        m = DATE.match(task.since.strip())
        since = date(int(m[1]), int(m[2]), int(m[3])) if m else now.date()
        queues.setdefault(key, []).append(((task.kind != "running", since, idx), task))
    out: dict[str, Estimate] = {}
    for entries in queues.values():
        entries.sort(key=lambda e: e[0])
        n = len(entries)
        cursor = now
        for i, (_, task) in enumerate(entries, 1):
            if hist:
                size = task.size.strip().upper()
                borrowed = bool(size) and size not in hist
                mean, count = hist[size] if size and size in hist else hist[ALL_SIZES]
                start = _hhmm(task.state_time, now.date(), cfg.zone) if task.kind == "running" and task.state_time else None
                end = max(now, start + mean) if start else cursor + mean
                cursor = max(cursor, end)
                if not task.due.strip():
                    out[task.item] = Estimate(end, i, n, of, mean, size, "history", count, borrowed)
            elif not task.due.strip():
                out[task.item] = Estimate(h - r * ((n - i) / n), i, n, of, r / n)
    return out


def history(tasks: list[Task], cfg: Config, now: datetime) -> dict[str, tuple[timedelta, int]]:
    """Mean elapsed time per size over done tasks that kept their running start, plus an `all` row.

    Only `done HH:MM–HH:MM` counts: `since` is a date and `done HH:MM` alone has no start (finding 70), so
    a task closed without the range records nothing and is left out rather than guessed at. A range that
    runs backwards crossed midnight and is dropped too. Empty when no task has a range, and the estimator
    then falls back to the queue-drain of 0.10.0, labelled as such.
    """
    today, zone = now.date(), cfg.zone
    spans: dict[str, list[timedelta]] = {}
    for task in tasks:
        if not (r := task.ran):
            continue
        a, b = _hhmm(r[0], today, zone), _hhmm(r[1], today, zone)
        if a is None or b is None or b <= a:
            continue
        spans.setdefault(task.size.strip().upper(), []).append(b - a)
    out = {size: (sum(v, timedelta()) / len(v), len(v)) for size, v in spans.items()}
    if out:
        every = [d for v in spans.values() for d in v]
        out[ALL_SIZES] = (sum(every, timedelta()) / len(every), len(every))
    return out


def _end(task: Task, cfg: Config, now: datetime, start: datetime, est: dict[str, Estimate] | None = None) -> tuple[datetime, str, str | None]:
    today = now.date()
    if (due := _resolve_due(task.due, cfg, today)) is not None:
        return due, "due", None
    if task.kind == "done":
        if t := task.state_time:
            return _hhmm(t, today, cfg.zone), "state", None
        return start, "state", "done, no time"
    if est and (e := est.get(task.item)):
        return max(e.end, start), "derived", e.label
    return governing_deadline(cfg, now).at, "deadline", NO_ESTIMATE


def day_bar(task: Task, cfg: Config, now: datetime, est: dict[str, Estimate] | None = None) -> Bar:
    today, zone = now.date(), cfg.zone
    day_start = datetime.combine(today, time(0, 0), tzinfo=zone)
    if task.kind == "running" and (t := task.state_time):
        start, start_src = _hhmm(t, today, zone), "state"
    elif t := _hhmm(task.since, today, zone):
        start, start_src = t, "since"
    else:
        start, start_src = day_start, "carried"
    end, end_src, label = _end(task, cfg, now, start, est)
    bar = Bar(task.item, task.owner, task.label, task.kind, start, end, start_src, end_src, label, est.get(task.item) if est and end_src == "derived" else None, task.size.strip().upper())
    return replace(bar, deadline=_deadline_for(task, bar, cfg, now).name)


def week_bar(task: Task, cfg: Config, now: datetime, est: dict[str, Estimate] | None = None) -> Bar:
    today, zone = now.date(), cfg.zone
    day_start = datetime.combine(today, time(0, 0), tzinfo=zone)
    m = DATE.match(task.since.strip())
    if m:
        start, start_src = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=zone), "since"
    elif task.kind == "running" and (t := task.state_time):
        start, start_src = _hhmm(t, today, zone), "state"
    elif t := _hhmm(task.since, today, zone):
        start, start_src = t, "since"
    else:
        start, start_src = day_start, "carried"
    end, end_src, label = _end(task, cfg, now, start, est)
    bar = Bar(task.item, task.owner, task.label, task.kind, start, end, start_src, end_src, label, est.get(task.item) if est and end_src == "derived" else None, task.size.strip().upper())
    return replace(bar, deadline=_deadline_for(task, bar, cfg, now).name)


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


def _tasks(n: int) -> str:
    return f"{n} {'task' if n == 1 else 'tasks'}"


LEGEND = [
    ("key bar running", "running"),
    ("key bar open", "open"),
    ("key bar orphaned", "orphaned"),
    ("key bar open-end", "no estimate"),
    ("key bar derived", "estimated"),
    ("key bar summary", "folded rows"),
    ("key bar done", "done"),
    ("key seg sleep", "sleep"),
    ("key seg eat", "eat"),
    ("key seg gym", "gym"),
    ("key seg recreation", "recreation"),
    ("key fill due", "committed"),
    ("key fill derived", "derived, no due written"),
    ("key fill over", "over work time"),
    ("key fill free", "uncommitted"),
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
# `gone` from the Tasks join because a session cannot report that it is gone.
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
    body = "".join(f"<dt>{label}</dt><dd>{_inline(value.strip())}</dd>" for label, value in rows if value.strip())
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


def gone_sessions(tasks: tuple[Task, ...], sessions: tuple[Session, ...]) -> set[str]:
    """Sessions the registry still lists whose task says its owner left. The join, in the renderer.

    A session cannot report that it is gone, so this is the coordinator's to work out, and it is the
    one state the board takes over a session's own word: `orphaned` on the task and `working` in the
    registry are the same row contradicting itself, and the task is the column that was updated last.
    """
    left = {_bare_name(task.owner) for task in tasks if task.kind == ORPHANED}
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
    """A line per tick on the day strip (a half line between), every day on the week strip (a half line at midday).

    The day grid followed the clock, not the axis: an hourly grid over a rolling 24 hours is 25 lines
    across ~1100px, one every 44px, which reads as hatching rather than as a grid. It follows the tick
    step now, so the lines land under the labels whatever span the axis covers.
    """
    step = _tick_step(axis_b - axis_a) if kind == "day" else timedelta(days=1)
    marks, at = [], axis_a
    last = axis_b if kind == "day" else axis_b - step
    while at <= last:
        marks.append((at, True))
        if at + step / 2 <= axis_b:
            marks.append((at + step / 2, False))
        at += step
    return marks


def _bar_row(b: Bar, axis_a: datetime, axis_b: datetime, row_class: str = "row") -> str:
    """One task as a positioned row. A top-level bar and a sublane inside a fold are the same markup."""
    left, right = _pct(b.start, axis_a, axis_b), _pct(b.end, axis_a, axis_b)
    edge = (" clamped" if b.end > axis_b else "") + (" clamped-left" if b.end < axis_a else "")
    classes = f"bar {b.kind}" + (" open-end" if b.end_src == "deadline" else "") + (" derived" if b.end_src == "derived" else "") + edge
    label = f' data-label="{_esc(b.label)}"' if b.label else ""
    title = f' title="{_esc(b.est.title(b.owner))}"' if b.est else ""
    text = f"<em>{_esc(b.label)}</em>" if b.label else ""
    return (
        f'<div class="{row_class}"><div class="name">{_esc(b.name)}</div><div class="track">'
        f'<div class="{classes}" data-name="{_esc(b.name)}" data-owner="{_esc(b.owner)}" data-item="{_esc(b.item)}" data-start="{_iso(b.start)}" data-end="{_iso(b.end)}" '
        f'data-start-src="{b.start_src}" data-end-src="{b.end_src}"{label}{_est_attrs(b)}{title} style="left:{left:.2f}%;width:{max(right - left, 0.6):.2f}%">{text}</div></div></div>'
    )


def week_load(bars: list[Bar], body: list[Event], week_a: datetime, week_b: datetime, now: datetime,
              est: dict[str, Estimate], hist: dict[str, tuple[timedelta, int]]) -> list[dict]:
    """What each day of the week axis has committed to it, against the work time it has left.

    The week strip says when tasks land; it does not say whether they can. A day carrying eighteen
    hours of estimated work and thirteen hours awake looks exactly like a day carrying four.

    Committed is estimated WORK, not the wall-clock span of the bar: a task running since Monday and
    due Friday occupies four days of the strip and is not four days of work. The duration comes from
    the task's own estimate, else the mean of closed tasks of its size, else the mean of all of them;
    a task none of those can size is counted apart rather than guessed at. Each task is charged to
    the day it lands, which is the day its slipping would be felt. Every active task counts, folded
    into a summary row or not — a day's load is not lighter because the strip drew its tasks as one.

    Work time available is the day minus the body track, and today starts at `now` rather than at
    midnight because the morning is already spent. Beyond today the calendar holds no events, so
    nothing is booked and the whole day reads as available — true of the inputs rather than useful.
    """
    def work(b: Bar) -> timedelta | None:
        if (e := est.get(b.item)) is not None:
            return e.slot
        if b.size and b.size in hist:
            return hist[b.size][0]
        return hist[ALL_SIZES][0] if ALL_SIZES in hist else None

    cells = []
    day = week_a
    while day < week_b:
        nxt = min(day + timedelta(days=1), week_b)
        open_a = max(day, now)
        booked = sum(max(0, int((min(e.end, nxt) - max(e.start, open_a)).total_seconds() // 60)) for e in body)
        available = max(0, int((nxt - open_a).total_seconds() // 60) - booked)
        lands = [b for b in bars if day <= b.end < nxt]
        due_m = derived_m = unsized = 0
        for b in lands:
            w = work(b)
            if w is None:
                unsized += 1
            elif b.end_src == "derived":
                derived_m += int(w.total_seconds() // 60)
            else:
                due_m += int(w.total_seconds() // 60)
        cells.append({"day": day.date(), "due": due_m, "derived": derived_m, "unsized": unsized,
                      "committed": due_m + derived_m, "available": available})
        day = nxt
    return cells


def _load_row(cells: list[dict], axis_a: datetime, axis_b: datetime) -> str:
    """One cell per day, the bar scaled against the busiest day so the columns are comparable."""
    peak = max([max(c["committed"], c["available"]) for c in cells] + [1])
    out = ['<div class="load" data-load="week">']
    for c in cells:
        left = _pct(datetime.combine(c["day"], time(0, 0), tzinfo=axis_a.tzinfo), axis_a, axis_b)
        right = _pct(datetime.combine(c["day"] + timedelta(days=1), time(0, 0), tzinfo=axis_a.tzinfo), axis_a, axis_b)
        over = 1 if c["committed"] > c["available"] else 0
        spare = max(c["available"] - c["committed"], 0)
        bits = [("due", c["due"]), ("derived", c["derived"])] if not over else [
            ("due", min(c["due"], c["available"])),
            ("derived", max(min(c["committed"], c["available"]) - c["due"], 0)),
            ("over", c["committed"] - c["available"]),
        ]
        fills = "".join(
            f'<span class="fill {k}" style="height:{m / peak * 100:.2f}%"></span>' for k, m in bits if m > 0
        ) + (f'<span class="fill free" style="height:{spare / peak * 100:.2f}%"></span>' if spare else "")
        out.append(
            f'<div class="load-cell" data-day="{c["day"].isoformat()}" data-committed="{c["committed"]}" '
            f'data-due="{c["due"]}" data-derived="{c["derived"]}" data-unsized="{c["unsized"]}" '
            f'data-available="{c["available"]}" data-over="{over}" '
            f'style="left:{left:.2f}%;width:{max(right - left, 0.6):.2f}%">'
            f'<span class="at">{_dur(timedelta(minutes=c["committed"]))}{" ⚑" if over else ""}</span>'
            f'<span class="col">{fills}</span></div>'
        )
    out.append("</div>")
    return "\n".join(out)


def _body_row(body: Body, axis_a: datetime, axis_b: datetime) -> str:
    """The body track. Hours already spent asleep, eating or off are not available for work, and a
    deadline row above them reads as if they were until they are drawn."""
    segs = []
    for ev in body.segments:
        left, right = _pct(ev.start, axis_a, axis_b), _pct(ev.end, axis_a, axis_b)
        segs.append(
            f'<div class="seg {ev.kind}" data-body="{ev.kind}" data-start="{_iso(ev.start)}" data-end="{_iso(ev.end)}" '
            f'title="{_esc(ev.title)} {ev.start.strftime("%H:%M")}–{ev.end.strftime("%H:%M")}" '
            f'style="left:{left:.2f}%;width:{max(right - left, 0.6):.2f}%"><em>{_esc(ev.kind)}</em></div>'
        )
    return f'<div class="row body"><div class="name">body</div><div class="track">{"".join(segs)}</div></div>'


def _strip(rows: list["Body | Swim | Bar | Summary"], axis_a: datetime, axis_b: datetime, events: list[Event], deadlines: list[Deadline], ticks: list[tuple[datetime, str]], kind: str) -> str:
    out = [f'<div class="strip" data-strip="{kind}" data-axis-start="{_iso(axis_a)}" data-axis-end="{_iso(axis_b)}">']
    out.append('<div class="axis">')
    for at, label in ticks:
        out.append(f'<span class="tick" style="left:{_pct(at, axis_a, axis_b):.2f}%">{_esc(label)}</span>')
    out.append("</div>")
    out.append('<div class="rows">')
    for row in rows:
        if isinstance(row, Body):
            out.append(_body_row(row, axis_a, axis_b))
            continue
        if isinstance(row, Swim):
            out.append(
                f'<div class="row swim-row" data-swimlane="{_esc(row.name)}" data-count="{row.count}">'
                f'<div class="name">{_esc(row.name)} · {row.count}</div><div class="track"></div></div>'
            )
            continue
        left, right = _pct(row.start, axis_a, axis_b), _pct(row.end, axis_a, axis_b)
        edge = (" clamped" if row.end > axis_b else "") + (" clamped-left" if row.end < axis_a else "")
        if isinstance(row, Bar):
            out.append(_bar_row(row, axis_a, axis_b))
            continue
        sm = row
        names = " · ".join(m.name for m in sm.members)
        hatch = " open-end" if sm.attr == "summary" else ""
        # The folded names were always in the html as empty `.member` spans, readable by a grader and
        # by nobody else. The disclosure opens onto the members drawn as rows on the same axis — sublanes,
        # not a list of names (Zach, 2026-09-18 12:55) — and the citations stay on the summary bar.
        out.append(
            '<details class="folded"><summary>'
            f'<div class="row {sm.attr}-row"><div class="name">{_esc(sm.name or _tasks(len(sm.members)))}</div><div class="track">'
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


def _deadline_for(task: Task, bar: Bar, cfg: Config, now: datetime) -> Deadline:
    for d in cfg.deadlines:
        if d.name.lower() == task.due.strip().lower() or (bar.end_src != "due" and d.at == bar.end):
            return d
    return governing_deadline(cfg, now)


def swimlanes(bars: list[Bar]) -> list["Swim | Bar"]:
    """Deadline outer, owner inner — the two groupings Zach asked for at once (2026-09-19).

    Deadlines keep the order they are first drawn in; tasks answering to none come last, under one
    header, because nothing in them can make him late.
    """
    order: list[str] = []
    for b in bars:
        if b.deadline not in order:
            order.append(b.deadline)
    first = {n: i for i, n in enumerate(order)}  # positions before the sort: the list is what is being sorted
    order.sort(key=lambda n: (n == "", first[n]))
    rows: list[Swim | Bar] = []
    for name in order:
        members = [b for b in bars if b.deadline == name]
        rows.append(Swim(name or "no deadline", len(members)))
        rows.extend(sorted(members, key=lambda b: b.owner.lower()))
    return rows


def today_rows(active: list[Task], cfg: Config, now: datetime, axis_b: datetime, est: dict[str, Estimate] | None = None, floor: timedelta | None = None) -> tuple[list[Bar], list[Bar]]:
    """Own bars: running tasks and tasks whose cited end falls by the axis end. Everything else folds.

    A derived end folds too when its slot is narrower than `floor` (the axis tick): a session with five
    tasks and four hours left would otherwise unfold into five 48-minute slivers exactly when the strip
    most needs reading. The estimate survives in the folded row's `.member` span.
    """
    own, folded = [], []
    for task in active:
        b = day_bar(task, cfg, now, est)
        narrow = b.est is not None and floor is not None and b.est.slot < floor
        if (task.kind == "running" and task.state_time) or (b.end_src != "deadline" and b.end <= axis_b and not narrow):
            own.append(b)
        else:
            folded.append(b)
    return own, folded


def done_rows(done: list[Task], cfg: Config, now: datetime, axis_a: datetime) -> list[Bar]:
    """Tasks finished inside the strip's past: `done HH:MM`, or `done HH:MM–HH:MM` for the whole run.

    The tracker writes clock times with no date, so a done time later than now is yesterday's. The start is
    the range's start, else a same-day `since` that is not after the end, else the end itself, drawn as the
    narrowest bar. A bare `done` has no position and is not drawn.
    """
    today, zone = now.date(), cfg.zone
    bars = []
    for task in done:
        if not (t := task.state_time) or (end := _hhmm(t, today, zone)) is None:
            continue
        if end > now:
            end -= timedelta(days=1)
        if not axis_a <= end <= now:
            continue
        start, start_src = end, "state"
        if task.ran and (s := _hhmm(task.ran[0], end.date(), zone)):
            start, start_src = (s if s <= end else s - timedelta(days=1)), "ran"
        elif (s := _hhmm(task.since, end.date(), zone)) and s <= end:
            start, start_src = s, "since"
        bars.append(Bar(task.item, task.owner, task.label, "done", start, end, start_src, "state", None, size=task.size.strip().upper()))
    return sorted(bars, key=lambda b: b.end)


def week_axis_end(cfg: Config, week_a: datetime) -> datetime:
    last = max([d.at for d in cfg.deadlines] + [week_a])
    day_after_last = datetime.combine(last.date() + timedelta(days=1), time(0, 0), tzinfo=week_a.tzinfo)
    return max(min(week_a + timedelta(days=WEEK_DAYS), day_after_last), week_a + timedelta(days=1))


def week_rows(active: list[Task], cfg: Config, now: datetime, week_a: datetime, week_b: datetime, est: dict[str, Estimate] | None = None) -> list[Bar | Summary]:
    """Running tasks get a bar. Tasks with a concrete due group by due day (a lone task keeps its bar); overdue and
    past-the-axis tasks get one row each. The rest fold into one row per deadline. Returned in drawing order."""
    keyed: list[tuple[tuple, Bar | Summary]] = []
    days: dict[date, list[Bar]] = {}
    overdue: list[Bar] = []
    later: list[Bar] = []
    groups: dict[Deadline, list[Bar]] = {}
    instants = {d.at for d in cfg.deadlines}
    for task in active:
        b = week_bar(task, cfg, now, est)
        named = any(d.name.lower() == task.due.strip().lower() for d in cfg.deadlines)
        if task.kind == "running" and task.state_time:
            keyed.append(((1, b.start), b))
        elif b.end_src in ("due", "derived") and not named and b.end not in instants:
            if b.end < week_a:
                overdue.append(b)
            elif b.end >= week_b:
                later.append(b)
            else:
                days.setdefault(b.end.date(), []).append(b)
        else:
            groups.setdefault(_deadline_for(task, b, cfg, now), []).append(b)
    if overdue:
        keyed.append(((0,), Summary("overdue", f"due {min(b.end for b in overdue):%a %d}", week_a, max(b.end for b in overdue), tuple(overdue), f"Overdue · {_tasks(len(overdue))}", "group")))
    for day, bars in days.items():
        if len(bars) == 1:
            keyed.append(((2, bars[0].end), bars[0]))
            continue
        end = max(b.end for b in bars)
        due_n, est_n = sum(b.end_src == "due" for b in bars), sum(b.end_src == "derived" for b in bars)
        label = " · ".join(part for part, n in ((f"{due_n} due", due_n), (f"{est_n} est.", est_n)) if n)
        keyed.append(((2, end), Summary(day.isoformat(), label, max(week_a, min(b.start for b in bars)), end, tuple(bars), f"{day:%a %d} · {_tasks(len(bars))}", "group")))
    if later:
        keyed.append(((3,), Summary("later", f"due {min(b.end for b in later):%a %d}", week_a, max(b.end for b in later), tuple(later), f"Later · {_tasks(len(later))}", "group")))
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


def render(tracker_text: str, log_text: str, cfg: Config, now: datetime,
           requirements: dict[str, str | None] | None = None, lanes: dict | None = None,
           tracker_day: date | None = None) -> str:
    cfg = with_decision_deadlines(cfg, tracker_text, tracker_day or now.date())
    sha = hashlib.sha256(tracker_text.encode()).hexdigest()
    tracker = parse_tracker(tracker_text)
    today, zone = now.date(), cfg.zone
    events = parse_calendar(log_text, today, zone)
    # A body event is drawn once, as a segment of the body track; drawn as a band as well it would
    # read as two things happening at once. Every other event stays the band it was.
    body_events = [e for e in events if e.kind]
    events = [e for e in events if not e.kind]
    # Only tasks from here on. A standing session with no task is on the Sessions graph, which reads
    # `## Sessions`; `gone_sessions` still reads every row, so its orphaned placeholder still marks it gone.
    tasks = [task for task in tracker.tasks if not task.standing]
    standing = len(tracker.tasks) - len(tasks)
    active = [task for task in tasks if task.kind != "done"]
    done = [task for task in tasks if task.kind == "done"]
    nearest = nearest_deadline(cfg, now)
    url = cfg.board_url or tracker.board_url

    # Day strip: a rolling 24 hours, 8 behind this hour and 16 ahead of it (Zach, 2026-09-22). It first
    # ended at the nearest deadline or midnight, so at 23:00 only an hour of it lay ahead of now; then it
    # ran 24 hours forward from this hour (2026-09-19), so nothing already done was on it. The past third
    # carries last night's sleep, today's earlier events and the tasks done in it; a bar that started
    # before the axis draws clamped-left, as a carried bar always has.
    hour = now.replace(minute=0, second=0, microsecond=0)
    axis_a, axis_b = hour - DAY_BEHIND, hour + DAY_AHEAD
    est = estimates(active, cfg, now, history(tasks, cfg, now))
    day_bars, day_folded = today_rows(active, cfg, now, axis_b, est, _tick_step(axis_b - axis_a))
    step = _tick_step(axis_b - axis_a)
    day_ticks = []
    t = axis_a
    while t <= axis_b:
        day_ticks.append((t, t.strftime("%H:%M")))
        t += step
    day_events = [e for e in events if e.end > axis_a and e.start < axis_b]
    on_axis = sorted((e for e in body_events if e.end > axis_a and e.start < axis_b), key=lambda e: e.start)
    day_body: list[Body] = [Body(tuple(on_axis))] if on_axis else []
    # Folded work is still to do, so its row starts at this hour, not at the axis's past edge.
    day_summaries = [Summary("today", "no estimate, or due or estimated after today", hour, axis_b, tuple(day_folded))] if day_folded else []
    day_done = done_rows(done, cfg, now, axis_a)
    # One closed row, opening onto the done tasks as sublanes: 27 of them pushed the work still ahead off the
    # first screen (Zach, 2026-09-22 18:57: "have done collapse down to an expandable row too").
    day_done_rows = [Summary("done", f"{len(day_done)} done", min(b.start for b in day_done), max(b.end for b in day_done), tuple(day_done), f"Done · {_tasks(len(day_done))}", "done")] if day_done else []

    # Week strip: today to the day after the last deadline, at most 7 days, one column per day.
    week_a = datetime.combine(today, time(0, 0), tzinfo=zone)
    week_b = week_axis_end(cfg, week_a)
    week = week_rows(active, cfg, now, week_a, week_b, est)
    week_bars = [r for r in week if isinstance(r, Bar)]
    week_groups = [r for r in week if isinstance(r, Summary) and r.attr == "group"]
    load_cells = week_load([week_bar(l, cfg, now, est) for l in active], body_events, week_a, week_b, now, est, history(tasks, cfg, now))
    week_ticks = []
    t = week_a
    while t < week_b:
        week_ticks.append((t, t.strftime("%a %d")))
        t += timedelta(days=1)

    def item_cell(item: str, label: str) -> str:
        short = label
        if short == _unmark(item).strip():
            return _esc(_unmark(item))
        lines = history_lines(item, label) or [("", item)]
        body = "".join(f"<li><time>{_esc(t)}</time><span>{_inline(text)}</span></li>" if t
                       else f'<li class="notime"><span>{_inline(text)}</span></li>' for t, text in lines)
        return f'<details><summary>{_esc(short)}</summary><ul class="hist">{body}</ul></details>'

    def task_filters(task: Task) -> list[str]:
        """The chips that keep this row. A filter is mechanical and selects over the whole table,
        done rows included: `yours` answers "which tasks name Robin", and the groups keep the state
        reading that the old queue table bought by excluding done."""
        owner = task.owner.strip().lower()
        tokens = ["all"]
        if owner == cfg.user.strip().lower():
            tokens.append("mine")
        if owner in ("unassigned", ""):
            tokens.append("unassigned")
        if task.kind in ("running", "orphaned", "done"):
            tokens.append(task.kind)
        return tokens

    # The issue column exists only where the block names a backlog; the board reads the cell and
    # never the network, so whether the issue is open is audit_tasks.py's to say.
    issued = cfg.backlog is not None
    span = 7 if issued else 6
    issue_head = "<th>issue</th>" if issued else ""

    def issue_cell(task: Task) -> str:
        if not issued:
            return ""
        cell = task.issue.strip()
        ref = issue_ref(cell, cfg.backlog) if cell else None
        if ref:
            body = f'<a href="{_esc(ref.url)}">{_esc(ref.label(cfg.backlog))}</a>'
        elif cell:
            body = f'<span class="warn">not an issue: {_esc(cell)}</span>'
        elif task.needs_issue:
            body = '<span class="warn">no issue</span>'
        else:
            body = ""
        return f"<td>{body}</td>"

    def stage_mark(task: Task) -> str:
        name, stage = task.lane.strip(), task.stage.strip()
        if not name:
            return ""
        lane = (lanes or {}).get(name)
        text = f"{stage} · {name}"
        if lane and stage in lane.gates:
            return f' <span class="stage gate">{_esc(text)} — waiting on you</span>'
        return f' <span class="stage">{_esc(text)}</span>'

    def task_rows(group: list[Task]) -> str:
        rows = []
        for task in group:
            warn = f' <span class="warn">{_esc(task.warning)}</span>' if task.warning else ""
            size = f' data-size="{_esc(task.size)}"' if task.size.strip() else ""
            keeps = " ".join(task_filters(task))
            rows.append(f'<tr data-state="{_esc(task.kind)}" data-in="{keeps}"{size}><td>{item_cell(task.item, task.label)}{warn}</td><td>{_esc(task.owner)}</td><td>{_esc(task.state)}{stage_mark(task)}</td><td>{_esc(task.since)}</td><td>{_esc(task.due)}</td><td>{_esc(task.size)}</td>{issue_cell(task)}</tr>')
        return "\n".join(rows) or f'<tr data-in="all"><td colspan="{span}" class="muted">none</td></tr>'

    running = [l for l in active if l.kind == "running"]
    orphaned = [l for l in active if l.kind == "orphaned"]
    waiting = [l for l in active if l.kind not in ("running", "orphaned")]

    def group_head(label: str, group: list[Task], note: str = "") -> str:
        keeps = " ".join(f for f in FILTERS if f == "all" or any(f in task_filters(l) for l in group))
        tail = f" · {_esc(note)}" if note else ""
        return f'<tr class="group" data-in="{keeps}"><th colspan="{span}">{_esc(label)} · {len(group)}{tail}</th></tr>'

    orphan_group = f'\n{group_head("orphaned", orphaned, "nobody owns these")}\n{task_rows(orphaned)}' if orphaned else ""
    # Unassigned and `<user>'s queue` were the Tasks table printed twice more. They are chips over
    # the one table now; the counts are the only thing those tables said that the rows do not.
    counts = {f: sum(1 for l in tasks if f in task_filters(l)) for f in FILTERS}
    # Each radio sits immediately before its own label, so `:checked`/`:focus-visible` need one rule
    # between them and not one per chip, and the radios still precede the table they filter.
    chips = "".join(
        f'<input type="radio" name="lf" id="lf-{f}"{" checked" if f == "all" else ""}>'
        f'<label for="lf-{f}">{_esc(FILTER_LABELS[f])} <b>{counts[f]}</b></label>'
        for f in FILTERS
    )
    requirements = requirements or {}
    req_decks = [d for d in cfg.deadlines if d.requirements and d.at >= now]
    req_html = "\n".join(requirements_section(d, requirements.get(d.name)) for d in req_decks)
    req_meta = "".join(
        f'\n<meta name="requirements-sha256" data-deadline="{_esc(d.name)}" content="{hashlib.sha256(requirements[d.name].encode()).hexdigest()}">'
        for d in req_decks
        if requirements.get(d.name) is not None
    )
    # 1 · DUE NEXT — a card per governing deadline, in the order the deadlines fall. A flat list
    # sorted by time answers "what is next" but not "what makes me late for the thing with a clock
    # on it": a task answering to tomorrow sitting between two of this morning's reads as one of
    # them. Tasks answering to no deadline come last, because nothing in them can make him late.
    def _clock(at: datetime) -> str:
        return at.strftime("%H:%M") if at.date() == today else at.strftime("%a %d %H:%M")

    due_groups: dict[str, list[tuple[Task, Bar]]] = {}
    for task in active:
        b = day_bar(task, cfg, now, est)
        due_groups.setdefault(_deadline_for(task, b, cfg, now).name, []).append((task, b))
    order = [d.name for d in cfg.deadlines if d.name in due_groups]
    order += [n for n in due_groups if n not in order]

    def due_card(name: str, group: list[tuple[Task, Bar]]) -> str:
        at = next((d.at for d in cfg.deadlines if d.name == name), None)
        # A task with no estimate has no time to sort by; it is counted in the head rather than
        # given a row it cannot fill. `NO_ESTIMATE` is the meaning the rest of the page uses.
        timed = sorted((p for p in group if p[1].label != NO_ESTIMATE), key=lambda p: p[1].end)
        no_est = sum(1 for _, b in group if b.label == NO_ESTIMATE)
        rows = "\n".join(
            f'<div class="due-row" data-kind="{_esc(task.kind)}" data-at-src="{b.end_src}">'
            f'<span class="at">{"~" if b.end_src == "derived" else ""}{_clock(b.end)}'
            f'{" ⚑" if task.kind == "orphaned" else ""}</span>'
            f'<span class="what">{_esc(task.label)}</span>'
            f'<span class="who">{_esc(task.owner)}</span>'
            f'<span class="state">{_esc(task.state)}</span>'
            f'<span class="size">{_esc(task.size)}</span></div>'
            for task, b in timed
        ) or '<div class="due-row muted"><span class="what">nothing with a time on it</span></div>'
        head = f"{_esc(name)} · {_clock(at)}" if at else _esc(name)
        return (f'<div class="card due-card"><div class="card-head"><b>{head}</b>'
                f'<span class="meta">{_tasks(len(group))} · {no_est} with no estimate</span>'
                f"</div>\n{rows}</div>")

    due_html = f"""<h2>Due next</h2>
<div class="cards due">
{chr(10).join(due_card(n, due_groups[n]) for n in order) or '<div class="card muted">nothing with a time on it</div>'}
</div>
"""

    # 2 · BLOCKED — the band that is never folded: on you, unowned, or a session that stopped reporting.
    awaiting = [task for task in active if task.owner.strip().lower() == cfg.user.lower()]
    unowned = [task for task in active if task.kind == "orphaned"]
    quiet = [s for s in tracker.sessions if _band(_age(s, cfg, now)[1] or 10**6) == "stale" or not s.state.strip()]

    # BLOCKED is never folded, so it cannot be allowed to grow without bound either: each card
    # shows its first three and says how many more there are.
    BLOCKED_SHOWN = 3

    def blocked_card(title: str, rows: list[str]) -> str:
        body = "".join(f'<li class="blocked-row">{r}</li>' for r in rows[:BLOCKED_SHOWN]) or '<li class="muted">none</li>'
        if len(rows) > BLOCKED_SHOWN:
            body += f'<li class="more">+ {len(rows) - BLOCKED_SHOWN} more</li>'
        return f'<div class="card"><h3>{_esc(title)} · {len(rows)}</h3><ul>{body}</ul></div>'

    def task_line(l: Task) -> str:
        return f"{_esc(l.label)} <span class='muted'>· {_esc(l.due.strip()) or 'no due'}</span>"

    def quiet_line(s: Session) -> str:
        """A session name on its own does not say what the silence costs: how long, and how many
        tasks are waiting behind it."""
        minutes = _age(s, cfg, now)[1]
        silence = f"silent {_dur(timedelta(minutes=minutes))}" if minutes is not None else "never reported"
        held = sum(1 for l in active if _bare_name(l.owner) == _bare_name(s.name))
        return (f"{_esc(s.name)} <span class='warn'>· {silence}</span> "
                f"<span class='muted'>· {_tasks(held)}</span>")

    blocked_html = f"""<h2>Blocked</h2>
<div class="cards">
{blocked_card("Awaiting you", [task_line(l) for l in awaiting])}
{blocked_card("Orphaned", [task_line(l) for l in unowned])}
{blocked_card("Not reporting", [quiet_line(s) for s in quiet])}
</div>
"""

    filter_css = "\n".join(
        # `~*` and not `~table`: the rows only have to be somewhere after the chips, not directly
        # under them. Stage 6 wrapped the table in `.scroll` for the phone and `~table` stopped
        # matching, which cost all six chips their whole job in silence. Naming the wrapper here
        # would just move the next break to the next wrapper.
        f'#lf-{f}:checked~* tr[data-in]:not([data-in~="{f}"]){{display:none}}' for f in FILTERS
    )
    long_items = sum(1 for task in tasks if len(task.item) > LONG_ITEM)
    orphan_note = f" · {sum(1 for l in active if l.kind == 'orphaned')} orphaned" if any(l.kind == "orphaned" for l in active) else ""
    long_note = f" · {long_items} {'task carries' if long_items == 1 else 'tasks carry'} history in the item cell" if long_items else ""
    standing_note = (f'<div class="meta">{standing} standing {"session" if standing == 1 else "sessions"} with no task '
                     f'{"is not a task" if standing == 1 else "are not tasks"}; see Sessions</div>\n') if standing else ""
    tzname = now.strftime("%Z")

    return f"""<meta charset="utf-8">
<title>Board {today.isoformat()}</title>
<link rel="stylesheet" href="{FONTS}">
<meta name="tracker-sha256" content="{sha}">{req_meta}
<style>
:root{{--bg:#f4efe1;--surface:#fbf8ef;--fg:#2f2630;--muted:#6e6470;--line:#ddd3bd;--brass:#a7843e;--band:rgba(111,99,180,.14);--open:#9daa72;--noest:#2f8f86;--fold:#8a7f9c;--running:#6f63b4;--done:#bdb3a2;--dl:#c9533a;--now:#4f6b3a;--est:#b5567a;--bar-ink:#fbf8ef;--sleep:#332288;--eat:#D55E00;--gym:#117733;--recreation:#F0E442}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#1e1a20;--surface:#29232b;--fg:#efe8d6;--muted:#a89fa8;--line:#3d3440;--brass:#c9a45c;--band:rgba(154,143,218,.18);--open:#6f7f48;--noest:#4fb3a7;--fold:#a093bb;--running:#9a8fda;--done:#5a5058;--dl:#f0775a;--now:#9dbb6e;--est:#d98aa8;--bar-ink:#1e1a20;--sleep:#332288;--eat:#D55E00;--gym:#117733;--recreation:#F0E442}}}}
:root[data-theme="dark"]{{--bg:#1e1a20;--surface:#29232b;--fg:#efe8d6;--muted:#a89fa8;--line:#3d3440;--brass:#c9a45c;--band:rgba(154,143,218,.18);--open:#6f7f48;--noest:#4fb3a7;--fold:#a093bb;--running:#9a8fda;--done:#5a5058;--dl:#f0775a;--now:#9dbb6e;--est:#d98aa8;--bar-ink:#1e1a20;--sleep:#332288;--eat:#D55E00;--gym:#117733;--recreation:#F0E442}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 "Alegreya Sans","Gill Sans",system-ui,sans-serif;padding:16px 16px 48px;max-width:1100px;margin:0 auto}}
h1{{font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-size:26px;font-weight:600;letter-spacing:.04em;margin:0 0 2px}}
h2{{font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-size:19px;font-weight:600;letter-spacing:.05em;margin:28px 0 8px;border-bottom:1px solid var(--brass);padding-bottom:4px}}
.header{{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:4px}} .header .logo{{width:260px;max-width:100%;border:1px solid var(--brass);border-radius:4px;display:block}}
.head-text{{flex:1 1 260px;min-width:0}}
.meta{{color:var(--muted);font-size:13px}} .clock{{font-family:ui-monospace,monospace;font-size:14px;font-variant-numeric:tabular-nums;margin:6px 0}}
table{{border-collapse:collapse;width:100%;max-width:100%}} .tasks table{{table-layout:fixed}} .tasks{{overflow-wrap:anywhere}} .tasks th:first-child{{width:40%}} .tasks th:nth-child(2){{width:18%}} td,th{{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}} th{{color:var(--muted);font-weight:600;font-size:12px;letter-spacing:.04em;text-transform:uppercase}}
tr.group th{{background:color-mix(in srgb,var(--brass) 18%,transparent);color:var(--fg);font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-weight:600;font-size:14px;letter-spacing:.12em;text-transform:uppercase;border-top:2px solid var(--brass);padding:6px 8px}}
.tasks{{position:relative}} .tasks>input{{position:absolute;width:1px;height:1px;opacity:0;margin:0}}
.tasks>label{{display:inline-flex;align-items:center;gap:6px;cursor:pointer;font-size:12.5px;padding:3px 10px;min-height:26px;margin:0 6px 8px 0;border:1px solid var(--line);border-radius:13px;background:var(--surface)}}
.tasks>label b{{font-weight:400;font-variant-numeric:tabular-nums;opacity:.7}}
.tasks>input:checked+label{{background:var(--fg);color:var(--surface);border-color:var(--fg)}}
.tasks>input:focus-visible+label{{outline:2px solid var(--brass);outline-offset:2px}}
{filter_css}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin:8px 0}} .cards.due{{grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:8px 11px}} .due-card{{border-left:3px solid var(--dl)}}
.card h3,.card-head b{{margin:0 0 5px;font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-size:13.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--brass)}}
.card ul{{margin:0;padding:0;list-style:none}} .card li,.due-row{{font-size:12.5px;line-height:1.7}} .card .more{{color:var(--muted)}}
.card-head{{display:flex;align-items:baseline;gap:8px;border-bottom:1px dotted var(--line);padding-bottom:5px;margin-bottom:6px}}
.due-row{{display:flex;align-items:center;gap:10px}} .due-row .at{{width:62px;flex:none;font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums}}
.due-row[data-at-src="derived"] .at{{color:var(--est)}} .due-row[data-kind="orphaned"] .at,.due-row[data-kind="orphaned"] .who{{color:var(--dl)}}
.due-row .what{{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.due-row .who{{width:84px;flex:none;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.due-row .state{{flex:none;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}} .due-row .size{{width:16px;flex:none;text-align:center;color:var(--muted)}}
.load{{position:relative;height:66px;margin:6px 0 2px}}
.load-cell{{position:absolute;top:0;bottom:0;display:flex;flex-direction:column;justify-content:flex-end;padding:0 4px;box-sizing:border-box}}
.load-cell .at{{font-size:10.5px;color:var(--muted);text-align:center;font-variant-numeric:tabular-nums;margin-bottom:2px}} .load-cell[data-over="1"] .at{{color:var(--dl);font-weight:700}}
.load-cell .col{{display:flex;flex-direction:column-reverse;height:48px;background:var(--line);border-radius:3px 3px 0 0;overflow:hidden}}
.fill{{display:block;width:100%}} .fill.due{{background:var(--now)}} .fill.derived{{background:var(--est)}} .fill.over{{background:var(--dl)}}
.key.fill{{display:inline-block;width:18px;height:10px;border-radius:2px}} .key.fill.free{{background:var(--line)}}
.scroll{{overflow-x:auto;max-width:100%}}
.muted{{color:var(--muted)}} .warn{{color:var(--dl);font-size:12px}} .stage{{color:var(--muted);font-size:12px}} .stage.gate{{color:var(--dl)}}
.strip{{position:relative;margin:8px 0 4px;--name-w:30%}} .axis{{position:relative;height:18px;margin-left:var(--name-w);font-size:11px;color:var(--muted)}} .tick{{position:absolute;transform:translateX(-50%);white-space:nowrap}}
.rows{{position:relative}} .row{{display:flex;align-items:center;height:26px}} .name{{width:var(--name-w);flex:none;padding-right:8px;box-sizing:border-box;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}} .track{{position:relative;flex:1;height:18px;border-left:1px solid var(--line)}}
.summary-row .name{{color:var(--muted)}} details.folded>summary{{list-style:none;cursor:pointer}} details.folded>summary::-webkit-details-marker{{display:none}} details.folded>summary .name::before{{content:"\u25b8 "}} details.folded[open]>summary .name::before{{content:"\u25be "}} .row.sub{{height:22px}} .row.sub .name{{padding-left:18px;color:var(--muted);font-size:12px}} .row.sub .bar{{height:14px;line-height:14px;font-size:10px;top:2px}}
.row.body .name{{color:var(--muted)}}
.seg{{position:absolute;top:0;height:18px;border-radius:3px;color:var(--fg);font-size:11px;line-height:18px;padding:0 6px;overflow:hidden;white-space:nowrap;box-sizing:border-box}}
.seg.sleep{{background:var(--sleep)}} .seg.eat{{background:var(--eat)}} .seg.gym{{background:var(--gym)}} .seg.recreation{{background:var(--recreation)}}
.seg.sleep,.seg.gym{{color:var(--bar-ink)}}
.seg em{{font-style:normal;opacity:.85}}
.key.seg{{position:static;padding:0;display:inline-block;width:18px;height:10px;border-radius:2px}}
.bar{{position:absolute;top:0;height:18px;border-radius:3px;background:var(--open);color:var(--bar-ink);font-size:11px;line-height:18px;padding:0 6px;overflow:hidden;white-space:nowrap;box-sizing:border-box}}
.bar.running{{background:var(--running)}} .bar.orphaned{{background:var(--surface);color:var(--fg);outline:2px dashed var(--dl);outline-offset:-2px}} .bar.open-end{{background:var(--noest)}} .bar.derived{{background:var(--est)}} .bar.summary{{background:var(--fold)}} .bar.done{{background:var(--done);color:var(--fg)}} .bar.clamped{{border-right:3px solid var(--dl)}} .bar.clamped-left{{border-left:3px solid var(--dl)}} .group-row .name{{font-weight:600}} .bar em{{font-style:normal;opacity:.85}} .member{{display:none}}
.overlay{{position:absolute;top:0;bottom:0;left:var(--name-w);right:0;pointer-events:none}}
.band{{position:absolute;top:0;bottom:0;background:var(--band)}}
.grid{{position:absolute;top:0;bottom:0;border-left:1px solid var(--line)}} .grid.half{{border-left:1px dotted var(--line);opacity:.6}}
.legend{{display:flex;flex-wrap:wrap;gap:4px 14px;margin:6px 0 2px;font-size:12px;color:var(--muted)}} .legend .item{{position:relative;display:inline-flex;align-items:center;gap:5px}}
/* Keys borrow the chart classes for colour only: those are position:absolute, and an absolute key with no positioned ancestor lands in the page corner. */
.legend .key{{position:static;flex:none;display:inline-block;width:18px;height:10px;border-radius:2px;top:auto;bottom:auto;left:auto;right:auto}}
.legend .key.nowline{{width:0;height:12px;border-radius:0;border-left:2px solid var(--now)}} .legend .key.dline{{width:0;height:12px;border-radius:0;border-left:2px dashed var(--dl);transform:none}}
.legend .key.band{{height:12px;background:var(--band);border:1px solid var(--line)}}
.key{{display:inline-block;width:18px;height:10px;border-radius:2px}} .key.bar{{position:static;background:var(--open);padding:0}}
.key.bar.running{{background:var(--running)}} .key.bar.orphaned{{background:var(--surface);outline:2px dashed var(--dl);outline-offset:-2px}} .key.bar.open-end{{background:var(--noest)}} .key.bar.derived{{background:var(--est)}} .key.bar.summary{{background:var(--fold)}} .key.bar.done{{background:var(--done)}}
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
.hist li{{display:grid;grid-template-columns:3em 1fr;gap:8px}} .hist li.notime{{display:block}} .hist time{{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums;color:var(--fg)}}
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
.resume details.long[open]{{max-height:12em;overflow-y:auto}}
@media (max-width:520px){{.resume{{grid-template-columns:1fr;gap:0}} .resume dt{{padding-top:4px}}}}
@media (max-width:520px){{.strip{{--name-w:36%}}}}
@media (max-width:420px){{
body{{padding:12px 12px 36px}}
.strip{{--name-w:96px}}
.cards,.cards.due{{grid-template-columns:1fr}}
.due-row{{flex-wrap:wrap;gap:0 8px;align-items:baseline;padding:5px 0;border-top:1px dotted var(--line)}}
.due-row .at{{flex:0 0 52px;width:52px;white-space:nowrap;order:0}}
.due-row .what{{flex:1 1 calc(100% - 60px);white-space:normal;order:1;min-width:0}}
.due-row .who{{order:2;flex:0 0 auto;width:auto;margin-left:60px}}
.due-row .state,.due-row .size{{order:3;flex:0 0 auto;width:auto;margin-left:0}}
.due-row .who::after,.due-row .state::after{{content:" ·";color:var(--muted)}}
.load{{height:58px}} .load-cell .col{{height:40px}} .load-cell .at{{font-size:9.5px}}
.tasks>label{{font-size:12px;padding:3px 9px}}
.row.body .name,.strip .name{{font-size:11px}}
.axis .tick:last-child{{transform:translateX(-100%)}}
}}
</style>
<div class="board" data-rendered-at="{_iso(now)}" data-tz="{_esc(cfg.tz)}" data-deadline="{_iso(nearest.at)}" data-deadline-name="{_esc(nearest.name)}">
<div class="header">{logo_tag()}<div class="head-text">
<h1>Board · {now.strftime('%a %d %b')}</h1>
<div class="clock" id="clock">Now — {_esc(tzname)} · {_esc(nearest.name)} ({nearest.at.strftime('%H:%M')} {_esc(tzname)})</div>
<div class="meta">tracker as of {now.strftime('%H:%M')} {_esc(tzname)} <span id="ago"></span> · kept by chief-of-stuff · board {_esc(url or 'not yet published')}{long_note}</div>
{resume_strip(parse_resume(tracker_text))}</div></div>

{due_html}{blocked_html}

<h2>Today</h2>
<div class="meta">{len(day_done)} done · {len(day_bars)} scheduled · {len(day_folded)} folded into one row (no estimate, or due or estimated after today) · bands are calendar events · green line is now{orphan_note}</div>
{legend()}{_strip(day_body + day_done_rows + swimlanes(day_bars) + day_summaries, axis_a, axis_b, day_events, [nearest], day_ticks, "day")}

<h2>Week</h2>
<div class="meta">{len(week_bars)} bars · {len(week_groups)} rows grouped by due day · the rest folded into one row per deadline · dashed lines are deadlines</div>
{_load_row(load_cells, week_a, week_b)}
{_strip(week, week_a, week_b, [], list(cfg.deadlines), week_ticks, "week")}

{req_html}

<h2>Tasks</h2>
{standing_note}<div class="tasks">
{chips}
<div class="scroll"><table><tr><th>item</th><th>owner</th><th>state</th><th>since</th><th>due</th><th>size</th>{issue_head}</tr>
{group_head("running", running)}
{task_rows(running)}{orphan_group}
{group_head("open · waiting", waiting)}
{task_rows(waiting)}
{group_head("done", done)}
{task_rows(done)}
</table></div>
</div>
{session_graph(tracker.sessions, cfg, now, gone_sessions(tracker.tasks, tracker.sessions))}
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
  // Served by pages.py: reload when the file behind this address changes (a re-render, or a new day's board).
  // A hidden tab's timers are throttled, so coming back to the tab checks at once.
  if(location.protocol==='http:'){{var seen=Date.parse(document.lastModified);var poll=function(){{
    fetch(location.pathname,{{method:'HEAD',cache:'no-store'}}).then(function(r){{
      var t=Date.parse(r.headers.get('Last-Modified'));if(t&&t!==seen)location.reload();
    }}).catch(function(){{}});
  }};setInterval(poll,5000);document.onvisibilitychange=poll;}}
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
    try:
        tracker_day = date.fromisoformat(day)
    except ValueError:
        sys.exit(f"render_board: invalid date {day!r}; use YYYY-MM-DD")
    tracker = root / cfg.tracker_path(day)
    if not tracker.is_file():
        sys.exit(f"render_board: tracker not found at {tracker}")
    log = root / cfg.log_path(day)
    log_text = log.read_text() if log.is_file() else ""
    tracker_text = tracker.read_text()
    cfg = with_workspace_decision_deadlines(root, cfg, tracker_text, tracker_day)
    out = (root / cfg.pages_dir if cfg.pages_dir else tracker.parent) / f"{day}-board.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    req_texts = {d.name: ((root / d.requirements).read_text() if (root / d.requirements).is_file() else None) for d in cfg.deadlines if d.requirements}
    try:
        lanes = load_settings(root, cfg.settings_path).lanes
    except SettingsError as e:
        sys.exit(f"render_board: {e}")
    page = render(tracker_text, log_text, cfg, now, requirements=req_texts, lanes=lanes,
                  tracker_day=tracker_day)
    out.write_text(page)
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
