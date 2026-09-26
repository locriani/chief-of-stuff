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
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backlog import Backlog, BacklogError, GitHubBacklog, parse_backlog  # noqa: E402
from settings import Kanban, SettingsError, load as load_settings  # noqa: E402
import board_sources  # noqa: E402
import columns  # noqa: E402
import decision_page  # noqa: E402
import flow_chart  # noqa: E402
import panels  # noqa: E402

HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
# Accept both separators in a completed time range.
RAN = re.compile(r"^(\d{1,2}:\d{2})[–-](\d{1,2}:\d{2})$")
SIZES = ("S", "M", "L", "XL")
ALL_SIZES = "all"
TASK_COLS = ("item", "owner", "state", "since", "due", "checklist")
TASK_COLS_SIZED = ("item", "owner", "state", "since", "due", "size", "checklist")
TASK_COLS_NAMED = ("name", "item", "owner", "state", "since", "due", "size", "checklist")
# Keep issue and stage columns in the recoverable task span.
TASK_COLS_ISSUED = ("name", "item", "owner", "state", "since", "due", "size", "issue", "checklist")
TASK_COLS_LANED = ("name", "item", "owner", "state", "since", "due", "size", "lane", "stage", "issue", "checklist")
# Match the widest supported tracker header first.
TASK_HEADERS = (TASK_COLS_LANED, TASK_COLS_ISSUED, TASK_COLS_NAMED, TASK_COLS_SIZED, TASK_COLS)
DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?$")
DECISION_DEADLINE = re.compile(r"^deadline:\s*(.+?)\s+(?:(\d{4}-\d{2}-\d{2})[ T])?(\d{1,2}):(\d{2})$", re.I)
BULLET = re.compile(r"^(\s*)-\s*([^:]+?):\s*(.*?)\s*$")
NO_ESTIMATE = "no estimate"
FONTS = "https://fonts.googleapis.com/css2?family=Alegreya+Sans:wght@400;500;600&family=Cormorant+SC:wght@600&display=swap"
SHORT_NAME = 48
LONG_ITEM = 80
RESUME_CAP = 300    # writer limit from agent rules
REQ_LINE = re.compile(r"^(\s*)- \[([ xX])\]\s+(.*?)\s*$")
EVIDENCE = " — evidence: "


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
    # An optional workspace identity rule for dispatched workers.
    identity_policy: str | None = None

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
    identity_policy = _unquote(top.get("Identity", "")).lower() or None
    if identity_policy not in (None, "user-name-only"):
        raise ConfigError("`Identity:` must be `user-name-only` when present")
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
        identity_policy=identity_policy,
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


def daily_trackers(root: Path, cfg: Config) -> list[tuple[date, Path]]:
    """Every day's tracker in the workspace, oldest first."""
    template = cfg.tracker_template
    if template.count("<date>") != 1:
        return []
    prefix, suffix = template.split("<date>")
    found = []
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
        found.append((day, path))
    return sorted(found)


def with_workspace_decision_deadlines(root: Path, cfg: Config, tracker_text: str, tracker_day: date) -> Config:
    """Carry future deadlines forward from earlier daily trackers, then apply today's decisions."""
    for day, path in daily_trackers(root, cfg):
        if day < tracker_day:
            cfg = with_decision_deadlines(cfg, path.read_text(), day, min_day=tracker_day)
    return with_decision_deadlines(cfg, tracker_text, tracker_day)


def decision_rows(tracker_text: str) -> list[list[str]]:
    """The Decisions table's rows as (time, item, words), header and separator left out."""
    rows = [_cells(line) for line in _section(tracker_text, "## Decisions") if line.strip().startswith("|")]
    return [(cells + ["", "", ""])[:3] for cells in rows[1:] if not _is_separator(cells)]


def decision_context(root: Path, day: date, now: datetime | None = None) -> decision_page.Context:
    """What the decisions pages read from the workspace: every tracker's Decisions rows and Log lines, `day`'s
    tasks, the cached forge sources and the [kanban] label that holds an issue."""
    cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())
    now = now or datetime.now(cfg.zone).replace(second=0, microsecond=0)

    def at(d: date, hhmm: str) -> datetime | None:
        m = re.match(r"(\d{1,2}):(\d{2})\b", hhmm.strip())
        try:
            return datetime.combine(d, time(int(m[1]), int(m[2])), cfg.zone) if m else None
        except ValueError:
            return None

    rows, log = [], []
    for d, path in daily_trackers(root, cfg):
        text = path.read_text()
        rows += [decision_page.Row(d, at(d, t), item, words) for t, item, words in decision_rows(text)]
        log += [(when, line.strip()[2:]) for line in _section(text, "## Log")
                if line.strip().startswith("- ") and (when := at(d, line.strip()[2:]))]
    tracker = root / cfg.tracker_path(day.isoformat())
    tasks = parse_tracker(tracker.read_text()).tasks if tracker.is_file() else ()
    try:
        kanban = load_settings(root, cfg.settings_path).kanban
    except SettingsError:
        kanban = None
    pages = root / cfg.pages_dir if cfg.pages_dir else tracker.parent
    return decision_page.Context(now, tuple(rows), tuple(sorted(log, key=lambda x: x[0])), tasks, board_sources.load(pages),
                                 kanban.human_review_label if kanban else "", cfg.user, cfg.backlog)


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


# --- rendering -----------------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _tasks(n: int) -> str:
    return f"{n} {'task' if n == 1 else 'tasks'}"


# A poll cycle runs about an hour, so under half an hour is current, under two hours is last cycle,
# and past that a node is reporting a memory of the morning. Baked in Python at render: the fade has
    # to survive with JavaScript off, and docs/archive/design-inputs.md §50 settled that this page runs none.
STAMP_FRESH = timedelta(minutes=30)
STAMP_AGING = timedelta(hours=2)


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


def _band(minutes: int) -> str:
    return "fresh" if minutes < STAMP_FRESH.total_seconds() / 60 else ("aging" if minutes < STAMP_AGING.total_seconds() / 60 else "stale")


ORPHANED = "orphaned"


def _deadline_for(task: Task, bar: Bar, cfg: Config, now: datetime) -> Deadline:
    for d in cfg.deadlines:
        if d.name.lower() == task.due.strip().lower() or (bar.end_src != "due" and d.at == bar.end):
            return d
    return governing_deadline(cfg, now)


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


def held(task: Task, kanban: Kanban | None) -> bool:
    return kanban is not None and kanban.holds(task.stage.strip())


def queue_durations(active: list[Task], hist: dict[str, tuple[timedelta, int]]) -> dict[str, timedelta]:
    """By item, the mean time closed tasks of each task's size took, as `estimates` reads it; empty with no history."""
    if not hist:
        return {}
    return {t.item: (hist[size] if (size := t.size.strip().upper()) and size in hist else hist[ALL_SIZES])[0] for t in active}


TRAILING_NUMBER = re.compile(r"(\d+)\s*$")
PIPELINE_MARK = {"passed": "passed", "failed": "failed", "running": "pending", "pending": "pending"}


def issue_key(cell: str) -> str:
    """The `#N` a board source keys an issue by, from an issue cell: `#118`, `group/app#118` or the issue's URL."""
    m = TRAILING_NUMBER.search(cell)
    return f"#{m[1]}" if m else cell.strip()


def task_changes(task: Task, sources: board_sources.Sources) -> tuple[board_sources.Change, ...]:
    """The changes naming the task's issue: the open ones, or else the merged ones."""
    # ponytail: keyed by `#N` alone, so two projects' #N collide; key by host and project when a board spans them.
    key = issue_key(task.issue) if task.issue.strip() else None
    named = [c for c in sources.changes.values() if key and key in c.issues and c.state != "closed"]
    return tuple(c for c in named if c.state == "open") or tuple(named)


def change_marks(changes: tuple[board_sources.Change, ...]) -> tuple[columns.Mark, ...]:
    return tuple(m for c in changes for m in (
        *((columns.Mark(c.pipeline, PIPELINE_MARK[c.pipeline]),) if c.pipeline in PIPELINE_MARK else ()),
        *((columns.Mark("approved", "approved"),) if c.approved else ())))


def drifts(task: Task, kanban: Kanban | None, sources: board_sources.Sources) -> bool:
    """The forge's labels disagree with the tracker's stage, by the same check the audit runs (kanban.drift)."""
    import kanban as kanban_tool  # kanban imports this module
    issue = sources.issues.get(issue_key(task.issue)) if task.issue.strip() else None
    # ponytail: labels only; a GitHub Project's Status is not in the sources, so a project board never drifts here.
    if kanban is None or kanban.github_project or issue is None or not task.stage.strip() or task.kind == "done":
        return False
    return bool(kanban_tool.drift(kanban, issue.labels, task.stage.strip(), allow_extra_hold=task.kind == "waiting"))


def merged_today(sources: board_sources.Sources, now: datetime) -> list[board_sources.Change]:
    return sorted((c for c in sources.changes.values() if c.state == "merged" and c.merged_at
                   and c.merged_at.astimezone(now.tzinfo).date() == now.date()), key=lambda c: c.merged_at, reverse=True)


def build_columns(tasks: list[Task], lanes: dict | None, kanban: Kanban | None = None,
                  sources: board_sources.Sources = board_sources.EMPTY, now: datetime | None = None) -> tuple[list[columns.Column], columns.Column]:
    """The Build board: a column per lane stage, a card per task at it, and the tasks with no lane in the footer.
    Sources add each card's changes and marks, and a `main` column of the changes merged today."""
    lanes = lanes or {}
    gates = {g for lane in lanes.values() for g in lane.gates}

    def card(task: Task) -> columns.Card:
        changes = task_changes(task, sources)
        issue = issue_key(task.issue) if changes or issue_key(task.issue) in sources.issues else task.issue.strip()
        refs = " · ".join([issue] * bool(issue) + [c.ref for c in changes])
        marks = change_marks(changes) + ((columns.Mark("drift", "drift"),) if drifts(task, kanban, sources) else ())
        return columns.Card(task.label, task.kind, refs, task.owner, task.state, marks,
                            flag=columns.Mark("ON HOLD", "hold") if held(task, kanban) else None)

    laned = [t for t in tasks if t.lane.strip() and t.stage.strip()]
    order = list(dict.fromkeys(stage_order(lanes) + [t.stage.strip() for t in laned]))
    cols = [columns.Column(stage, tuple(card(t) for t in laned if t.stage.strip() == stage),
                           *(("gate", "gate") if stage in gates else ())) for stage in order]
    merged = merged_today(sources, now) if now else []
    if merged:
        cards = tuple(columns.Card(c.title, "merged today", " · ".join([*c.issues[:1], c.ref]),
                                   f"merged {c.merged_at.astimezone(now.tzinfo):%H:%M}", "done") for c in merged)
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
                                   f"recommended {d['recommended']}", f"default: {d['default']}"]), href=f"decision-{slug}.html")
        for slug, d, asked, why in pending)
    return panels.Panel("decisions", "DECISIONS", "decisions", sum(d is not None for _, d, _, _ in pending), rows,
                        link=("decisions page", "decisions.html"), foot=f"{answered} answered today")


def worker_panel(sources: board_sources.Sources, now: datetime) -> panels.Panel:
    def facts(w: board_sources.Worker) -> str:
        where = w.tree if w.kind == "one-shot" else (f"last reply {w.last.astimezone(now.tzinfo):%H:%M}" if w.last else "")
        return " · ".join(x for x in (w.kind, " ".join(x for x in (w.runtime, w.model) if x), w.task, where) if x)

    rows = tuple(panels.Row(w.name, facts(w), side=panels.hm(w.started, now) if w.started else "") for w in sources.workers)
    kinds = [w.kind for w in sources.workers]
    note = " · ".join(f"{kinds.count(k)} {k}" for k in dict.fromkeys(kinds))
    return panels.Panel("workers", "WORKERS", "workers", len(rows), rows, note)


def flow_tiles(blocked: int, decisions: int, sources: board_sources.Sources, running: int, drift: int, orphaned: int) -> list[panels.Tile]:
    """Six counts, each linking to where its items are listed. RUNNING counts workers, or running tasks when no
    worker source is read."""
    approved = sum(c.state == "open" and c.approved for c in sources.changes.values())
    return [panels.Tile("BLOCKED", blocked, "flow", "blocked"), panels.Tile("DECISIONS", decisions, "decisions", "decisions"),
            panels.Tile("APPROVED", approved, "merge", "approved"),
            panels.Tile("RUNNING", len(sources.workers) or running, "workers", "running"),
            panels.Tile("DRIFT", drift, "flow", "drift"), panels.Tile("ORPHANED", orphaned, "flow", "orphaned")]


def render(tracker_text: str, cfg: Config, now: datetime, lanes: dict | None = None,
           tracker_day: date | None = None, decisions: list[tuple[str, dict | None, datetime | None, str]] | None = None,
           kanban: Kanban | None = None, sources: board_sources.Sources = board_sources.EMPTY, answered: int = 0,
           tracker_at: datetime | None = None, stage_log: list[tuple[date, str]] | None = None, slots: int = 1) -> str:
    """The Flow board (Flow.dc.html): header, tiles, BUILD, the MERGE ORDER, DECISIONS and WORKERS panels, and the
    Flow charts. `stage_log` is (day, tracker text) for the earlier days the Flow charts reach back over; `slots` is
    how many queued tasks run at once, `[workers] max_concurrency`."""
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

    # BLOCKED counts what is on the user, what nobody owns, and the sessions that stopped reporting.
    awaiting = [task for task in active if task.owner.strip().lower() == cfg.user.lower()]
    unowned = [task for task in active if task.kind == "orphaned"]
    quiet = [s for s in tracker.sessions if _band(_age(s, cfg, now)[1] or 10**6) == "stale" or not s.state.strip()]
    running = [task for task in active if task.kind == "running"]

    build_cols, no_lane = build_columns(tasks, lanes, kanban, sources, now)
    issues = sum(1 for t in tasks if t.issue.strip())
    changes = sum(1 for c in sources.changes.values() if c.state == "open")
    changes_note = f" · {_plural(changes, 'merge request')}" if changes else ""
    build_html = (f'<section id="flow"><h2>Build</h2>\n<div class="meta">{_tasks(len(tasks))} · {_plural(issues, "issue")}{changes_note}</div>\n'
                  f'{columns.render(build_cols, no_lane if no_lane.cards else None)}</section>\n')
    flow_moves = [m for day, text in [*(stage_log or []), (tracker_day or today, tracker_text)]
                  for m in flow_chart.moves(text, day, zone)]
    known = [t for _, text in stage_log or [] for t in parse_tracker(text).tasks] + tasks
    ends = {t.item: end for t in active if (end := _end(t, cfg, now, now, est))[1] in ("due", "derived")}
    flow_html = flow_chart.section(flow_chart.build(flow_moves, known, lanes or {}, {t.name.strip() for t in tasks if held(t, kanban)},
                                                    {item: end[0] for item, end in ends.items()}, now,
                                                    queue_durations(active, hist), slots), now)

    pending = decisions or []
    meta = [f"rendered {now:%H:%M} {now:%Z}", f"tracker {(tracker_at or now).astimezone(zone):%H:%M}",
            *(f"{k} {sources.fetched[k].astimezone(zone):%H:%M}" for k in ("kanban", "merge requests", "workers") if k in sources.fetched),
            _tasks(len(tasks)), f"{len(no_lane.cards)} in no lane"]
    head = panels.Header(f"Board · {now.strftime('%a %d %b')}", tuple(meta), now, nearest.name, nearest.at,
                         tuple(f"{k}: {why}" for k, why in sources.errors.items()))
    tiles = flow_tiles(len(awaiting) + len(unowned) + len(quiet), sum(d is not None for _, d, _, _ in pending), sources, len(running),
                       sum(drifts(t, kanban, sources) for t in tasks), len(unowned))
    panels_html = panels.panels([merge_order(sources), decision_panel(pending, answered, now), worker_panel(sources, now)])

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
{flow_chart.css() if flow_html else ""}{panels.css(PANEL_COLOURS, PANEL_TOKENS)}{columns.css(CARD_COLOURS)}.columns{{--columns-ink:var(--fg);--columns-muted:var(--muted);--columns-card:var(--surface);--columns-rule:var(--line);--columns-gate-ink:var(--brass);--columns-bg:color-mix(in srgb,var(--brass) 10%,var(--bg));--columns-gate:color-mix(in srgb,var(--brass) 14%,var(--surface))}}
@media (max-width:420px){{body{{padding:12px 12px 36px}}}}
</style>
<div class="board" data-rendered-at="{_iso(now)}" data-tz="{_esc(cfg.tz)}" data-deadline="{_iso(nearest.at)}" data-deadline-name="{_esc(nearest.name)}">
<div class="header">{panels.header(head)}</div>
{panels.tiles(tiles)}
{build_html}{panels_html}
{flow_html}</div>
<script>
(function(){{
  var root=document.querySelector('.board'),tz=root.dataset.tz,dl=new Date(root.dataset.deadline);
  function hm(d){{return new Intl.DateTimeFormat('en-GB',{{timeZone:tz,hour:'2-digit',minute:'2-digit'}}).format(d);}}
  function pad(n){{return (n<10?'0':'')+n;}}
  function tick(){{
    var now=new Date(),ms=dl-now,sign=ms<0?'-':'',m=Math.floor(Math.abs(ms)/60000);
    root.querySelector('.panels-now').textContent=hm(now);
    root.querySelector('.panels-left').textContent=sign+Math.floor(m/60)+'h'+pad(m%60)+'m';
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


def write(root: Path, day: str | None = None) -> tuple[Path, Config, datetime, str, dict]:
    """Write `<day>-board.html` and `decisions.html` into the pages dir. The page server calls this in
    process; a missing CLAUDE.md, tracker or setting raises ConfigError."""
    claude_md = root / "CLAUDE.md"
    if not claude_md.is_file():
        raise ConfigError(f"no CLAUDE.md at {root}")
    cfg = parse_coordinator(claude_md.read_text(), today=date.today())
    now = datetime.now(cfg.zone).replace(second=0, microsecond=0)
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
    pending = decision_page.write_all(out.parent, decision_context(root, tracker_day, now), day, root)
    reach = (now - flow_chart.WINDOWS[-1][1]).date()
    stage_log = [(d, path.read_text()) for d, path in daily_trackers(root, cfg) if reach <= d < tracker_day]
    page = render(tracker_text, cfg, now, lanes=settings.lanes,
                  tracker_day=tracker_day, decisions=pending, kanban=settings.kanban,
                  sources=board_sources.load(out.parent), answered=len(decision_rows(tracker_text)),
                  tracker_at=datetime.fromtimestamp(tracker.stat().st_mtime, cfg.zone), stage_log=stage_log,
                  slots=settings.workers.max_concurrency or 1)
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
