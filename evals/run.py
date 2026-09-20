"""Eval runner for the chief-of-stuff agent.

`claude plugin eval` cannot run a case under `--agent`, so this drives `claude -p` directly.
Grader vocabulary follows `plugin eval` where it overlaps (`tool_used`, `regex`) so cases can
be ported later.

    python3 evals/run.py --arm baseline --case stale-clock        # model defaults to opus
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import queue
import tempfile
import threading
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_repo  # noqa: E402

EVALS = Path(__file__).resolve().parent
PLUGIN_ROOT = EVALS.parent
AGENT = "chief-of-stuff"
# Zach, 2026-09-19: "also make the case runner use sonnet". This is the HARNESS default only --
# `--model` still overrides it, and the agent frontmatter keeps `model: opus` for the live run, so
# what ships is unchanged. Measured the day it moved: sonnet $0.15/run against opus $0.33, and the
# `--model` flag beats the frontmatter, so `check_arm` sees sonnet and passes rather than failing
# the family check. Supersedes the 2026-09-16 "we don't use Sonnet for this" for the runner.
DEFAULT_MODEL = "sonnet"

# Isolation: `--setting-sources project` drops user settings (and with them user plugins and
# permission rules) but still loads the fixture CLAUDE.md. `--restricted` was probed and skips
# project CLAUDE.md, which is the config channel under test, so it is not used.
TOOLS = ["Bash", "Read", "Glob", "Grep", "Write", "Edit", "Agent"]
# `mv`/`mkdir` are for filing moves (PARA); no `cp` or `rm`, so a "move" cannot become a copy or a delete.
BOARD_RENDERER = PLUGIN_ROOT / "scripts" / "render_board.py"
HEALTH_PROBE = PLUGIN_ROOT / "scripts" / "probe_health.py"
LANE_AUDIT = PLUGIN_ROOT / "scripts" / "audit_lanes.py"
MAKE_WORKTREE = PLUGIN_ROOT / "scripts" / "make_worktree.py"
SPAWN_SESSION = PLUGIN_ROOT / "scripts" / "spawn_session.py"
# The plugin's own scripts are the only ones the agent may run: html, health and merge state come
# from code, never from the agent. git and curl stay off the allowlist — the scripts call them.
SCRIPTS = ("render_board.py", "probe_health.py", "audit_lanes.py", "make_worktree.py", "spawn_session.py")


def allowed_tools(root: Path) -> list[str]:
    """The allowlist, rooted. Finding 114: these were absolute paths into the live checkout, so a run
    read whatever was on disk when each case reached it and a tree edited mid-run produced two
    verdicts wearing one name. Rooting them lets a run point at a snapshot of its own."""
    return (["Bash(date:*)", "Bash(TZ=*)", "Bash(mv:*)", "Bash(mkdir:*)"]
            + [f"Bash(python3 {root / 'scripts' / name}:*)" for name in SCRIPTS]
            + ["Read", "Glob", "Grep", "Write(./**)", "Edit(./**)"])


ALLOWED = allowed_tools(PLUGIN_ROOT)
DISALLOWED = ["Bash(railway:*)", "Bash(git commit:*)", "Bash(git add:*)", "Bash(git push:*)"]
# These reach real Claude sessions on this machine. No eval run may expose them.
PEER_TOOLS = ("ListAgents", "SendMessage")
MOCK_CALENDAR = EVALS / "mock_calendar.py"
CALENDAR_TOOL = "mcp__calendar__list_events"
MOCK_BOARD = EVALS / "mock_board.py"
BOARD_TOOL = "mcp__board__publish"
# The peers mock stands in for ListAgents/SendMessage, which stay out of every run (PEER_TOOLS guard).
MOCK_PEERS = EVALS / "mock_peers.py"
PEER_MOCK_TOOLS = ["mcp__peers__list_sessions", "mcp__peers__send"]

# `{{name}}`, or `{{now+Nh}}` / `{{now-Nh|local}}` / `{{now+Nh|hhmm}}` for times relative to render time.
TOKEN = re.compile(r"\{\{\s*([a-z_]+)(?:([+-]\d+)h)?(?:\|([a-z]+))?\s*\}\}")
NOW_FORMATS = {"local": "%Y-%m-%d %H:%M", "hhmm": "%H:%M"}
DURATION_HM = re.compile(r"(\d+)\s*h(?:ours?|rs?)?\s*(?:and\s*)?(\d{1,2})\s*m(?:in(?:utes?|s)?)?\b")
DURATION_H = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b")
CLOCK = re.compile(
    # The closing parenthesis may carry a day word — "(01:52 CDT, tomorrow)" — because a gate past
    # midnight reads as this morning, already gone, without one.
    r"Now\s+(\d{1,2}):(\d{2})\s+\S+\s+·\s+(.+?)\s+in\s+(\d+)h(\d{2})m\s+\((\d{1,2}):(\d{2})\s+[^\s,)]+(?:,\s*[^)]{1,24})?\)"
)
TOLERANCE = timedelta(minutes=2)


# --- stream -----------------------------------------------------------------------------------


@dataclass
class Stream:
    init: dict[str, Any] = field(default_factory=dict)
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    denied_ids: set[str] = field(default_factory=set)
    last_text: str = ""
    result: dict[str, Any] | None = None


def load_stream(path: Path) -> Stream:
    return parse_stream([json.loads(raw) for raw in path.read_text().splitlines() if raw.strip()])


def parse_stream(events: list[dict[str, Any]]) -> Stream:
    s = Stream()
    for ev in events:
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            s.init = ev
        elif kind == "assistant":
            for block in ev["message"]["content"]:
                if block.get("type") == "tool_use":
                    s.tool_uses.append({"id": block["id"], "name": block["name"], "input": block.get("input", {})})
                elif block.get("type") == "text" and block.get("text", "").strip():
                    s.last_text = block["text"]
        elif kind == "result":
            s.result = ev
            s.denied_ids = {d["tool_use_id"] for d in ev.get("permission_denials", [])}
    return s


def check_arm(init: dict[str, Any], arm: str, agent_flag_used: bool = False, needs_calendar: bool = False, model: str | None = None, needs_board: bool = False, needs_peers: bool = False) -> tuple[bool, str]:
    """Confirm the session loaded what the arm claims, so a silent fallback cannot pass."""
    exposed = [t for t in PEER_TOOLS if t in init.get("tools", [])]
    if exposed:
        return False, f"sandbox exposes peer-session tools {exposed}"
    if model and model.lower() not in str(init.get("model", "")).lower():
        return False, f"init model {init.get('model')!r} is not {model!r}"
    if needs_calendar:
        servers = {m.get("name"): m.get("status") for m in init.get("mcp_servers", [])}
        if servers.get("calendar") != "connected":
            return False, f"mock calendar not connected: {servers}"
    if needs_board:
        servers = {m.get("name"): m.get("status") for m in init.get("mcp_servers", [])}
        if servers.get("board") != "connected":
            return False, f"mock board not connected: {servers}"
    if needs_peers:
        servers = {m.get("name"): m.get("status") for m in init.get("mcp_servers", [])}
        if servers.get("peers") != "connected":
            return False, f"mock peers not connected: {servers}"
    agents = init.get("agents", [])
    present = any(a == AGENT or a.endswith(":" + AGENT) for a in agents)
    if arm == "agent":
        return (True, "agent loaded") if present else (False, f"{AGENT} not in init agents {agents}")
    if arm == "baseline":
        return (False, "baseline ran with --agent") if agent_flag_used else (True, "no --agent")
    raise ValueError(f"unknown arm {arm!r}")


# --- fixtures ---------------------------------------------------------------------------------


def context(tz: str, now: datetime) -> dict[str, str]:
    local = now.astimezone(ZoneInfo(tz))
    day = timedelta(days=1)
    return {
        "today": local.date().isoformat(),
        "yesterday": (local.date() - day).isoformat(),
        "tomorrow": (local.date() + day).isoformat(),
        "tz": tz,
        "now_hhmm": local.strftime("%H:%M"),
        "now_iso": local.replace(second=0, microsecond=0).isoformat(),
        # Fixtures cannot hold a real `.git/`; a file under `{{dotgit}}/` renders as one.
        "dotgit": ".git",
    }


def render(text: str, ctx: dict[str, str]) -> str:
    def token(m: re.Match[str]) -> str:
        name, offset, fmt = m.groups()
        if offset is None and fmt is None:
            return ctx[name]
        if name != "now":
            raise KeyError(f"offset or format only allowed on now: {m.group(0)}")
        when = datetime.fromisoformat(ctx["now_iso"]) + timedelta(hours=int(offset or 0))
        if fmt is None:
            return when.isoformat()
        if fmt not in NOW_FORMATS:
            raise KeyError(f"unknown now format {fmt!r}")
        return when.strftime(NOW_FORMATS[fmt])

    return TOKEN.sub(token, text)


def render_value(value: Any, ctx: dict[str, str]) -> Any:
    if isinstance(value, str):
        return render(value, ctx)
    if isinstance(value, list):
        return [render_value(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render_value(v, ctx) for k, v in value.items()}
    return value


def render_tree(src: Path, dst: Path, ctx: dict[str, str]) -> None:
    for path in sorted(src.rglob("*")):
        rel = Path(render(str(path.relative_to(src)), ctx))
        out = dst / rel
        if path.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render(path.read_text(), ctx))


# --- graders ----------------------------------------------------------------------------------


@dataclass
class RunRecord:
    stream: Stream
    t_start: datetime
    t_end: datetime
    tz: str
    fixture_dir: Path
    mock_calls: list[dict[str, Any]] = field(default_factory=list)
    before_dir: Path | None = None

    @property
    def publishes(self) -> list[dict[str, Any]]:
        return [c for c in self.mock_calls if c.get("tool") == "publish"]

    @property
    def peer_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.mock_calls if c.get("tool") in ("list_sessions", "send")]


def grade(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    kind = g.get("type")
    if kind == "tool_used":
        return _tool_used(g, rec)
    if kind == "regex":
        return _regex(g, rec)
    if kind == "clock_line":
        return _clock_line(g, rec)
    if kind == "mock_calls":
        return _mock_calls(g, rec)
    if kind == "duration_stated":
        return _duration_stated(g, rec)
    if kind in FILE_GRADERS:
        return FILE_GRADERS[kind](g, rec)
    if kind in BOARD_GRADERS:
        return BOARD_GRADERS[kind](g, rec)
    if kind in COORDINATION_GRADERS:
        return COORDINATION_GRADERS[kind](g, rec)
    raise ValueError(f"unknown grader type {kind!r}")


def _tool_used(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    name = re.compile(rf"(?:{g['tool']})")
    match = re.compile(g["input_match"]) if "input_match" in g else None
    hits = [
        t
        for t in rec.stream.tool_uses
        if name.fullmatch(t["name"]) and (match is None or match.search(json.dumps(t["input"])))
    ]
    denied = sum(1 for t in hits if t["id"] in rec.stream.denied_ids)
    lo, hi = g.get("min", 0), g.get("max")
    ok = len(hits) >= lo and (hi is None or len(hits) <= hi)
    bound = f"min {lo}" + (f", max {hi}" if hi is not None else "")
    return ok, f"{len(hits)} call(s) ({denied} denied); want {bound}"


def _regex(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    found = re.search(g["pattern"], rec.stream.last_text, re.MULTILINE) is not None
    mode = g.get("match", "contains")
    ok = found if mode == "contains" else not found
    return ok, f"/{g['pattern']}/ {'found' if found else 'not found'}; want {mode}"


def _clock_line(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    m = CLOCK.search(rec.stream.last_text)
    if not m:
        return False, "no Clock line (want 'Now HH:MM TZ · <gate> in XhYYm (HH:MM TZ)')"
    zone = ZoneInfo(rec.tz)
    gate = datetime.strptime(g["gate"], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
    start, end = rec.t_start.astimezone(zone), rec.t_end.astimezone(zone)
    now_h, now_m, _, rem_h, rem_m, gate_h, gate_m = m.groups()
    problems = []
    stated_now = start.replace(hour=int(now_h), minute=int(now_m), second=0, microsecond=0)
    if not (start.replace(second=0, microsecond=0) - TOLERANCE <= stated_now <= end + TOLERANCE):
        problems.append(f"Now {now_h}:{now_m} outside run window {start:%H:%M}-{end:%H:%M}")
    if (int(gate_h), int(gate_m)) != (gate.hour, gate.minute):
        problems.append(f"gate {gate_h}:{gate_m} != {gate:%H:%M}")
    remaining = timedelta(hours=int(rem_h), minutes=int(rem_m))
    if not (gate - end - TOLERANCE <= remaining <= gate - start + TOLERANCE):
        problems.append(f"remaining {rem_h}h{rem_m}m outside {_hm(gate - end)}-{_hm(gate - start)}")
    return (not problems), ("; ".join(problems) if problems else f"'{m.group(0)}' ok")


def _duration_stated(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Some line matching `line_match` (Clock lines excluded) states a duration within tolerance of `hours`."""
    want, tol = float(g["hours"]), float(g.get("tolerance_hours", 0.25))
    label = re.compile(g.get("line_match", "."))
    seen = []
    for line in rec.stream.last_text.splitlines():
        if CLOCK.search(line) or not label.search(line):
            continue
        durations = [int(h) + int(m) / 60 for h, m in DURATION_HM.findall(line)]
        durations += [float(h) for h in DURATION_H.findall(DURATION_HM.sub(" ", line))]
        seen += durations
        if any(abs(d - want) <= tol for d in durations):
            return True, f"stated {min(durations, key=lambda d: abs(d - want)):.2f}h; want {want}±{tol}"
    return False, f"durations on matching lines {[round(d, 2) for d in seen]}; want {want}±{tol}"


def _mock_calls(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Each calendar id was queried at least once with a window covering `covers`: a whole day
    (`YYYY-MM-DD`) or `{"from": "YYYY-MM-DD HH:MM", "to": ...}` in the case timezone."""
    zone = ZoneInfo(rec.tz)
    if isinstance(g["covers"], dict):
        day_start = datetime.strptime(g["covers"]["from"], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
        day_end = datetime.strptime(g["covers"]["to"], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
    else:
        day_start = datetime.strptime(g["covers"], "%Y-%m-%d").replace(tzinfo=zone)
        day_end = day_start + timedelta(days=1)

    def covers(args: dict[str, Any]) -> bool:
        lo = _instant(args.get("timeMin"), zone)
        hi = _instant(args.get("timeMax"), zone)
        return (lo is None or lo <= day_start) and (hi is None or hi >= day_end)

    missing = []
    for cal in g["calendar_ids"]:
        calls = [c["arguments"] for c in rec.mock_calls if c["arguments"].get("calendarId") == cal]
        if not any(covers(a) for a in calls):
            missing.append(f"{cal} ({len(calls)} call(s), none covering {day_start:%Y-%m-%d %H:%M}-{day_end:%Y-%m-%d %H:%M})")
    return (not missing), ("missing: " + "; ".join(missing) if missing else f"{len(rec.mock_calls)} call(s) cover {day_start:%Y-%m-%d %H:%M}-{day_end:%Y-%m-%d %H:%M}")


def _read(root: Path | None, rel: str) -> str | None:
    path = root / rel if root else None
    return path.read_text() if path and path.is_file() else None


def _file_unchanged(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    before, after = _read(rec.before_dir, g["path"]), _read(rec.fixture_dir, g["path"])
    if before == after:
        return True, f"{g['path']} unchanged"
    state = "created" if before is None else "deleted" if after is None else "modified"
    return False, f"{g['path']} {state}"


def _glob_matches(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """`file_matches` over a path the agent named — a worktree it chose, a dispatch file inside it.

    Every match must satisfy the pattern, and no match at all is a failure rather than a pass: a
    grader that is vacuously true when its subject is missing measures nothing.
    """
    paths = sorted((rec.fixture_dir or Path(".")).glob(g["glob"]))
    if not paths:
        return False, f"no file matching {g['glob']}"
    mode = g.get("match", "contains")
    bad = []
    for path in paths:
        found = re.search(g["pattern"], path.read_text(errors="replace"), re.MULTILINE) is not None
        if found is not (mode == "contains"):
            bad.append(str(path.relative_to(rec.fixture_dir)))
    detail = f"{len(paths)} file(s) matching {g['glob']}: /{g['pattern']}/ want {mode}"
    return not bad, detail + (f"; failed by {', '.join(bad)}" if bad else "")


def _file_matches(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    if "glob" in g:
        return _glob_matches(g, rec)
    text = _read(rec.fixture_dir, g["path"])
    if text is None:
        return False, f"{g['path']} missing"
    if "count" in g:
        n = len(re.findall(g["pattern"], text, re.MULTILINE))
        return n == g["count"], f"{g['path']}: /{g['pattern']}/ matches {n} time(s); want {g['count']}"
    found = re.search(g["pattern"], text, re.MULTILINE) is not None
    mode = g.get("match", "contains")
    ok = found if mode == "contains" else not found
    return ok, f"{g['path']}: /{g['pattern']}/ {'found' if found else 'not found'}; want {mode}"


def _files(root: Path | None) -> set[str]:
    """Every file under `root`, minus anything inside a `.git/`: a repo built for a run is not the agent's doing."""
    if not (root and root.is_dir()):
        return set()
    return {str(f.relative_to(root)) for f in root.rglob("*") if f.is_file() and ".git" not in f.relative_to(root).parts}


def before_snapshot(work: Path, dest: Path) -> None:
    """The tree as the agent first saw it — fixture and, where a case has one, its repo.

    `no_new_files` asks what the agent created, so the baseline has to be taken after every piece of
    setup. Snapshotting the rendered fixture alone reported a case's own `repo` as the agent's work,
    and git object names differ per run, so no `except` list could have covered it.
    """
    shutil.copytree(work, dest, dirs_exist_ok=True, symlinks=True)


def _no_new_files(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """`except` entries are exact paths or globs (`Resources/**`)."""
    allowed = [str(e) for e in g.get("except", [])]
    new = sorted(f for f in _files(rec.fixture_dir) - _files(rec.before_dir) if not any(fnmatch.fnmatch(f, e) for e in allowed))
    return (not new), (f"new files: {new}" if new else "no new files")


def _section(text: str, heading: str) -> list[str] | None:
    """Non-blank lines under `heading`, up to the next heading of the same or higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == heading.strip():
            body = []
            for nxt in lines[i + 1 :]:
                hashes = len(nxt) - len(nxt.lstrip("#"))
                if hashes and hashes <= level and nxt[hashes : hashes + 1] == " ":
                    break
                if nxt.strip():
                    body.append(nxt.rstrip())
            return body
    return None


def _lines_preserved(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Every line of the original section appears, in order, in the result's section (append-only)."""
    before = _section(_read(rec.before_dir, g["path"]) or "", g["section"])
    after = _section(_read(rec.fixture_dir, g["path"]) or "", g["section"])
    if before is None or after is None:
        return False, f"{g['path']}: section {g['section']!r} missing ({'before' if before is None else 'after'})"
    it = iter(after)
    lost = [line for line in before if not any(line == candidate for candidate in it)]
    if lost:
        return False, f"{g['path']} {g['section']}: lost or reordered {lost}"
    added = len(after) - len(before)
    want = g.get("min_added", 0)
    return added >= want, f"{g['path']} {g['section']}: preserved, {added} line(s) added; want >= {want}"


CHECK_LINE = re.compile(r"^(\s*)- \[([ xX])\]\s+(.*?)\s*$")


def _checklist(text: str) -> list[tuple[int, str, bool, str]]:
    items = []
    for line in text.splitlines():
        m = CHECK_LINE.match(line)
        if m:
            body, _, evidence = m.group(3).partition(" — evidence: ")
            items.append((len(m.group(1)), body.strip(), m.group(2) != " ", evidence.strip()))
    return items


def _checklist_ticks_only(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Same items in the same order and words; exactly `ticked` flip to [x], each with evidence (matching `evidence_match`); nothing unticks."""
    before = _read(rec.before_dir, g["path"])
    after = _read(rec.fixture_dir, g["path"])
    if before is None or after is None:
        return False, f"{g['path']} missing ({'before' if before is None else 'after'})"
    b, a = _checklist(before), _checklist(after)
    if [(d, t) for d, t, _, _ in b] != [(d, t) for d, t, _, _ in a]:
        return False, f"{g['path']}: items added, removed, reordered, or reworded ({len(b)} before, {len(a)} after)"
    want = set(g.get("ticked", []))
    problems = []
    for (_, text, was, _), (_, _, now, evidence) in zip(b, a):
        if was and not now:
            problems.append(f"unticked {text!r}")
        elif not was and now and text not in want:
            problems.append(f"ticked {text!r}, not in ticked")
        elif not was and not now and text in want:
            problems.append(f"not ticked {text!r}")
        elif not was and now:
            if not evidence:
                problems.append(f"no evidence on {text!r}")
            elif "evidence_match" in g and not re.search(g["evidence_match"], evidence):
                problems.append(f"evidence {evidence!r} on {text!r} does not match /{g['evidence_match']}/")
    return (not problems), ("; ".join(problems) if problems else f"{g['path']}: ticked {sorted(want) or 'nothing'}, items unchanged")


def _file_moved(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """`from` is gone; a file of the same name and content sits anywhere under `to` (a directory)."""
    src, dst = g["from"], g["to"]
    before = _read(rec.before_dir, src)
    if before is None:
        return False, f"{src} missing from fixture-before"
    if _read(rec.fixture_dir, src) is not None:
        return False, f"{src} still present"
    root = rec.fixture_dir / dst
    hits = [p for p in root.rglob(Path(src).name) if p.is_file()] if root.is_dir() else []
    if not hits:
        return False, f"{Path(src).name} not found under {dst}/"
    same = [p for p in hits if p.read_text() == before]
    if not same:
        return False, f"{src} moved under {dst}/ but content differs"
    return True, f"{src} -> {same[0].relative_to(rec.fixture_dir)}"


def _lane_rows(text: str) -> list[tuple[str, str]] | None:
    """Every Lanes row as (name, item) in order, or None when the table has no `name` column.

    `name` is the newest Lanes column. An older six-column tracker has none — the board falls back
    to guessing a name from the item, and `agents/chief-of-stuff.md` says such a tracker is not to
    be rewritten wholesale to add it. There is nothing to compare in that case, and saying so beats
    passing by default: a grader that cannot see its subject has not checked it.
    """
    rows = _section(text, "## Lanes") or []
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows if r.strip().startswith("|")]
    table = [c for c in cells if not all(set(x) <= set("-: ") for x in c)]
    if not table or table[0][:2] != ["name", "item"]:
        return None
    return [(r[0], r[1]) for r in table[1:] if len(r) > 1]


def _lane_names_unchanged(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """No lane was renamed. A fact carried out of the Resume block goes to the Log or to `item`.

    Not a length rule. A long name can be a house style with readers, and a fixture does not get to
    rule on that; the failure this watches for is a measurement narrative appended to a NAME during
    a move about something else, where `item` is the documented home for history. `except` names the
    lanes a case expects to be renamed, for a move that changes what a lane is about.

    A lane is matched to its former self by its item as a PREFIX. Neither column is a stable key on
    its own: the name is the attribute under test, and the item is documented to accrete history
    (Lanes: "`item` keeps the wording, the history you append"). What holds is that an appended item
    still begins with the item that was there.
    """
    before = _lane_rows(_read(rec.before_dir, g["path"]) or "")
    after = _lane_rows(_read(rec.fixture_dir, g["path"]) or "")
    if before is None or after is None:
        which = "before" if before is None else "after"
        return False, f"{g['path']}: no `name` column in the Lanes table ({which})"
    allowed, renamed, gone = set(g.get("except", [])), [], []
    left = list(after)
    for was, item in before:
        match = next((r for r in left if r[1] == item or r[1].startswith(item)), None)
        if match is None:
            if was not in allowed:
                gone.append(was)
            continue
        left.remove(match)
        if match[0] != was and was not in allowed:
            renamed.append(f"{was!r} -> {match[0]!r}")
    if renamed or gone:
        return False, f"{g['path']}: renamed {renamed}, lost {gone}"
    return True, f"{g['path']}: {len(before)} lane name(s) unchanged"


_RENDERER_CACHE: list[Any] = []


def _renderer():
    """The board renderer, imported once. The rule and its enforcer must not drift apart.

    A grader that re-implements `parse_resume` with a regex is a second definition of the field,
    and two definitions of one number do not argue -- they produce a confident answer each, in
    different directions, with nothing to force the reconciliation until a value lands in the gap.
    """
    import importlib.util
    if _RENDERER_CACHE:
        return _RENDERER_CACHE[0]
    spec = importlib.util.spec_from_file_location("_render_board", BOARD_RENDERER)
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: the renderer defines dataclasses, and @dataclass resolves its own
    # module out of sys.modules. Without this the import dies inside dataclasses, not here.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _RENDERER_CACHE.append(mod)
    return mod


def _resume_fields_bounded(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Every `## Resume` field is within the renderer's own cap, measured by the renderer's parse.

    Not `^- (As of|…)\b[^\n]{300,}$`: that counts from the end of the label WORD, so it charges
    the writer for the `: ` and fails a conforming field at 298, 299 and 300. It is also blind to
    the eight characters `parse_resume` inserts when the label carries a clock (`- Verified 19:39:`
    becomes the value `19:39 · …`), which is a real part of what the board measures.
    """
    text = _read(rec.fixture_dir, g["tracker"])
    if text is None:
        return False, f"{g['tracker']}: not found"
    r = _renderer()
    block = r.parse_resume(text)
    if not block:
        return True, "no Resume block"
    cap = int(g.get("cap", r.RESUME_CAP))
    over = sorted(((k, len(v)) for k, v in block.items() if len(v) > cap), key=lambda kv: -kv[1])
    if over:
        return False, "over %d: %s" % (cap, "; ".join(f"{k} {n}" for k, n in over))
    widest = max(((k, len(v)) for k, v in block.items()), key=lambda kv: kv[1])
    return True, f"{len(block)} field(s) within {cap}, widest {widest[0]} {widest[1]}"


FILE_GRADERS = {
    "file_moved": _file_moved,
    "file_unchanged": _file_unchanged,
    "file_matches": _file_matches,
    "no_new_files": _no_new_files,
    "lines_preserved": _lines_preserved,
    "lane_names_unchanged": _lane_names_unchanged,
    "checklist_ticks_only": _checklist_ticks_only,
    "resume_fields_bounded": _resume_fields_bounded,
}


def _instant(value: str | None, zone: ZoneInfo) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=zone)


def _hm(d: timedelta) -> str:
    mins = int(d.total_seconds() // 60)
    return f"{mins // 60}h{mins % 60:02d}m"


# --- invocation -------------------------------------------------------------------------------


@dataclass
class Case:
    name: str
    root: Path
    spec: dict[str, Any]


def load_cases(patterns: list[str], golden_only: bool = False) -> list[Case]:
    """The cases to run. `golden_only` keeps just the ones their spec marks `"golden": true`.

    Zach, 2026-09-19: sonnet to iterate, opus on the golden cases only for full green. The mark
    lives in each case.json rather than a list here, so adding a case and deciding whether it is
    golden are the same edit and cannot drift apart.
    """
    cases = []
    for spec_path in sorted((EVALS / "cases").glob("*/case.json")):
        name = spec_path.parent.name
        if patterns and not any(fnmatch.fnmatch(name, p) for p in patterns):
            continue
        spec = json.loads(spec_path.read_text())
        if golden_only and spec.get("golden") is not True:
            continue
        cases.append(Case(name, spec_path.parent, spec))
    return cases


def _last_published_html(rec: RunRecord) -> tuple[str | None, str]:
    pubs = rec.publishes
    if not pubs:
        return None, "no publish in this turn"
    stored = Path(pubs[-1].get("stored", ""))
    if not stored.is_file():
        return None, f"stored copy missing: {stored}"
    return stored.read_text(), f"{len(pubs)} publish(es), last {pubs[-1].get('url')}"


def _board_published(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """At least `min` publishes; the last html names every lane in `lanes`, the `deadline` instant, and matches `html_match`."""
    want = g.get("min", 1)
    html_text, note = _last_published_html(rec)
    if len(rec.publishes) < want:
        return False, f"{len(rec.publishes)} publish(es); want >= {want}"
    if html_text is None:
        return False, note
    problems = [f"lane not on board: {lane!r}" for lane in g.get("lanes", []) if _esc_html(lane) not in html_text]
    if "deadline" in g and g["deadline"] not in html_text:
        problems.append(f"deadline {g['deadline']} not on board")
    if "html_match" in g and not re.search(g["html_match"], html_text):
        problems.append(f"/{g['html_match']}/ not in board")
    return (not problems), ("; ".join(problems) if problems else note)


def _esc_html(text: str) -> str:
    import html as _html
    return _html.escape(text, quote=True)


META = re.compile(r'<meta name="tracker-sha256" content="([0-9a-f]{64})">')


def _board_matches_tracker(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """sha256 of the final tracker == the meta in the last published html == the meta in the html on disk."""
    tracker = _read(rec.fixture_dir, g["tracker"])
    if tracker is None:
        return False, f"{g['tracker']} missing"
    want = hashlib.sha256(tracker.encode()).hexdigest()
    published, note = _last_published_html(rec)
    if published is None:
        return False, note
    m = META.search(published)
    if not m:
        return False, "published html has no tracker-sha256 meta (not produced by the renderer?)"
    if m.group(1) != want:
        return False, "published board is stale: tracker edited after the last publish"
    on_disk = _read(rec.fixture_dir, g["html"]) if "html" in g else None
    if "html" in g:
        if on_disk is None:
            return False, f"{g['html']} missing on disk"
        d = META.search(on_disk)
        if not d or d.group(1) != want:
            return False, f"{g['html']} on disk does not match the tracker"
    return True, f"board sha {want[:12]} matches the tracker"


def _board_url_fixed(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Every publish went to one URL, the tracker header carries it, and it equals `expect` if given."""
    urls = sorted({p.get("url") for p in rec.publishes})
    if not urls:
        return False, "no publish"
    if len(urls) > 1:
        return False, f"published to more than one url: {urls}"
    url = urls[0]
    if "expect" in g and url != g["expect"]:
        return False, f"published to {url}, want {g['expect']}"
    tracker = _read(rec.fixture_dir, g["tracker"]) or ""
    head = tracker.split("## Lanes", 1)[0]
    m = re.search(r"Board:\s*(\S+)", head)
    header = m.group(1).rstrip(".,;") if m else None
    if header != url:
        return False, f"tracker header Board: {header!r}, published to {url!r}"
    return True, f"one url {url}, in the header"


CITED = re.compile(r'<[^>]*\bdata-item="[^"]*"[^>]*>')
ATTR = re.compile(r'data-([a-z-]+)="([^"]*)"')


def _board_bars(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Bars cite their sources: each listed lane (its own bar, or a member folded into a summary row) has the given data-*-src (and label); every end source is a known kind."""
    html_text, note = _last_published_html(rec)
    if html_text is None:
        return False, note
    bars = {}
    for tag in CITED.findall(html_text):
        attrs = dict(ATTR.findall(tag))
        if "end-src" in attrs:
            bars.setdefault(attrs["item"], attrs)
    problems = [f"{item!r}: end src {a.get('end-src')!r}" for item, a in bars.items() if a.get("end-src") not in ("due", "state", "deadline", "derived")]
    for want in g.get("bars", []):
        a = bars.get(_esc_html(want["item"]))
        if a is None:
            problems.append(f"no bar for {want['item']!r}")
            continue
        for key in ("start_src", "end_src", "label"):
            if key in want and a.get(key.replace("_", "-")) != want[key]:
                problems.append(f"{want['item']!r}: {key} {a.get(key.replace('_', '-'))!r}, want {want[key]!r}")
    for name in g.get("deadline_lines", []):
        if f'data-deadline-name="{_esc_html(name)}"' not in html_text:
            problems.append(f"no deadline line for {name!r}")
    return (not problems), ("; ".join(problems) if problems else f"{len(bars)} bar(s) cite their sources")


def _board_requirements(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """The last published board has the deadline's requirements section with `done` of `total` ticked."""
    html_text, note = _last_published_html(rec)
    if html_text is None:
        return False, note
    m = re.search(rf'<section class="reqs" data-deadline="{re.escape(_esc_html(g["deadline"]))}" data-done="(\d+)" data-total="(\d+)"', html_text)
    if not m:
        return False, f"no requirements section for {g['deadline']!r}"
    done, total = int(m[1]), int(m[2])
    ok = done == g["done"] and total == g["total"]
    return ok, f"{g['deadline']}: {done} of {total}; want {g['done']} of {g['total']}"


BOARD_GRADERS = {
    "board_published": _board_published,
    "board_matches_tracker": _board_matches_tracker,
    "board_url_fixed": _board_url_fixed,
    "board_bars": _board_bars,
    "board_requirements": _board_requirements,
}


def _peer_calls(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Count this turn's calls to the peers mock that match every filter given."""
    calls = [c for c in rec.peer_calls if c.get("tool") == g["tool"]]
    if "to_ref" in g:
        calls = [c for c in calls if c.get("to_ref") == g["to_ref"]]
    if "ok" in g:
        calls = [c for c in calls if bool(c.get("ok")) == bool(g["ok"])]
    if "text_match" in g:
        calls = [c for c in calls if re.search(g["text_match"], c.get("text", ""))]
    if "text_not_match" in g:
        calls = [c for c in calls if not re.search(g["text_not_match"], c.get("text", ""))]
    lo, hi = g.get("min", 0), g.get("max")
    ok = len(calls) >= lo and (hi is None or len(calls) <= hi)
    bound = f"min {lo}" + (f", max {hi}" if hi is not None else "")
    return ok, f"{len(calls)} call(s) match; want {bound}"


TIME_CELL = re.compile(r"^(\d{1,2}):(\d{2})$")
LEADING_TIME = re.compile(r"^-\s*(\d{1,2}):(\d{2})\b")


def _timestamp_tolerance(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Every time written into `section` during this turn is the move's own clock read: within [t_start-tol, t_end+tol].

    Leading mode (default): each line added since `before_dir` must start `- HH:MM`. Column mode (`column`, 1-based):
    the cell of each added or changed table row; a blank cell passes, a non-time cell (a date, a word) is ignored.
    Times quoted elsewhere on the line are ignored by design.
    """
    before = set(_section(_read(rec.before_dir, g["path"]) or "", g["section"]) or [])
    after = _section(_read(rec.fixture_dir, g["path"]) or "", g["section"])
    if after is None:
        return False, f"{g['path']}: section {g['section']!r} missing"
    tol = timedelta(minutes=g.get("tolerance_minutes", 1))
    lo, hi = rec.t_start - tol, rec.t_end + tol
    zone = ZoneInfo(rec.tz)
    day = rec.t_start.astimezone(zone).date()
    bad, checked = [], 0
    named = re.compile(g["time_match"]) if "time_match" in g else None
    for line in after:
        if line in before and not g.get("rewritten"):
            continue
        if named:
            m = named.search(line)
            if not m:
                continue  # a block line that states no time of its own, or quotes one it did not read
        elif "column" in g:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            idx = g["column"] - 1
            cell = cells[idx] if idx < len(cells) else ""
            m = TIME_CELL.match(cell)
            if not cell or not m:
                continue
        else:
            m = LEADING_TIME.match(line)
            if not m:
                bad.append(f"no leading time: {line[:60]!r}")
                continue
        at = datetime.combine(day, dtime(int(m[1]), int(m[2])), tzinfo=zone)
        checked += 1
        if not (lo <= at <= hi):
            bad.append(f"{m[1]}:{m[2]} outside {lo:%H:%M}-{hi:%H:%M}: {line[:60]!r}")
    return (not bad), ("; ".join(bad) if bad else f"{checked} written time(s) within the move")


def _reply_lines(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    n = len([l for l in rec.stream.last_text.splitlines() if l.strip()])
    return n <= g["max"], f"{n} non-empty line(s); want <= {g['max']}"


def _spawn_calls(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Sessions started this turn, as the recorder saw them. No real terminal opens in the sandbox."""
    calls = [c for c in rec.mock_calls if c.get("tool") == "spawn"]
    if "argv_match" in g:
        pattern = re.compile(g["argv_match"])
        calls = [c for c in calls if pattern.search(" ".join(c.get("argv", [])))]
    lo, hi = g.get("min", 0), g.get("max")
    ok = len(calls) >= lo and (hi is None or len(calls) <= hi)
    return ok, f"{len(calls)} spawn(s); want min {lo}" + (f", max {hi}" if hi is not None else "")


def _health_calls(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Probes recorded by the loopback server in this turn. Without this a case can pass having probed nothing."""
    calls = [c for c in rec.mock_calls if c.get("tool") == "health"]
    if "path" in g:
        calls = [c for c in calls if c.get("path") == g["path"]]
    if "status" in g:
        calls = [c for c in calls if c.get("status") == g["status"]]
    lo, hi = g.get("min", 0), g.get("max")
    ok = len(calls) >= lo and (hi is None or len(calls) <= hi)
    return ok, f"{len(calls)} call(s); want min {lo}" + (f", max {hi}" if hi is not None else "")


COORDINATION_GRADERS = {
    "spawn_calls": _spawn_calls,
    "health_calls": _health_calls,
    "peer_calls": _peer_calls,
    "timestamp_tolerance": _timestamp_tolerance,
    "reply_lines": _reply_lines,
}

# Every `type` a case may name. `grade()` is the dispatcher; this is what `test_cases.py` checks a case
# against, and `test_the_grader_type_tuple_is_the_dispatcher` keeps the two from drifting.
GRADER_TYPES = ("tool_used", "regex", "clock_line", "mock_calls", "duration_stated", *FILE_GRADERS, *BOARD_GRADERS, *COORDINATION_GRADERS)


def peers_mcp_config(sessions: Path, log: Path, tz: str) -> dict[str, Any]:
    args = [str(MOCK_PEERS.resolve()), "--sessions", str(sessions), "--log", str(log), "--tz", tz]
    return {"mcpServers": {"peers": {"type": "stdio", "command": sys.executable, "args": args}}}


def board_mcp_config(log: Path, store: Path, root: Path, tz: str, known_url: str | None) -> dict[str, Any]:
    args = [str(MOCK_BOARD.resolve()), "--log", str(log), "--store", str(store), "--root", str(root), "--tz", tz]
    if known_url:
        args += ["--known-url", known_url]
    return {"mcpServers": {"board": {"type": "stdio", "command": sys.executable, "args": args}}}


def merge_mcp(*configs: dict[str, Any] | None) -> dict[str, Any] | None:
    servers: dict[str, Any] = {}
    for c in configs:
        if c:
            servers.update(c.get("mcpServers", {}))
    return {"mcpServers": servers} if servers else None


def calendar_mcp_config(events: Path, log: Path, tz: str) -> dict[str, Any]:
    args = [str(MOCK_CALENDAR.resolve()), "--events", str(events), "--log", str(log), "--tz", tz]
    return {"mcpServers": {"calendar": {"type": "stdio", "command": sys.executable, "args": args}}}


CLAUDE_ENV = "CHIEF_OF_STUFF_CLAUDE"
# A GUI-launched terminal inherits launchd's PATH, not the shell's, so a bare `claude` is not on it
# and every case a session writes from such a tab goes ungraded. Resolve it once, absolutely, and
# leave an override for a machine that keeps it somewhere else.
CLAUDE_FALLBACKS = ("/opt/homebrew/bin/claude", "/usr/local/bin/claude")


def claude_binary() -> str:
    """The `claude` to run: the override, then PATH, then the two places a Mac install puts it."""
    override = os.environ.get(CLAUDE_ENV, "").strip()
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return str(Path(found).resolve())
    for candidate in CLAUDE_FALLBACKS:
        if Path(candidate).exists():
            return candidate
    return "claude"


SNAPSHOT_SKIP = shutil.ignore_patterns(".git", "results", "__pycache__", "*.pyc", ".DS_Store")


def snapshot_plugin(dest: Path) -> Path:
    """Copy the plugin tree a run will read, so the run is a verdict on one state of the code.

    `evals/results` is skipped because it holds every previous run and would grow each sweep by the
    size of all of them; `.git` for the same reason. What remains is what a case can reach: the
    scripts on the allowlist and the agent definition behind `--plugin-dir`.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGIN_ROOT, dest, ignore=SNAPSHOT_SKIP, dirs_exist_ok=True)
    return dest


def write_verdict(out: Path, case: str, arm: str, model: str, run_no: int,
                  results: list[tuple[str, bool, str]], error: str | None, meta: str) -> Path:
    """What the run decided, beside the inputs it decided from. Finding 114b.

    The result dir held every input to grading and none of its output, so a sweep whose stdout was
    piped through `tail` was unreadable afterwards — 56 of 58 case verdicts gone from a 3.3-hour run.
    Grading is a pure function of this directory and the verdicts could be rebuilt from it, which is
    a recovery route and not a reason to leave them unwritten.

    `passed` is None when a harness error stopped the run, never False: a case that never ran is not
    a case that failed, which is the 0.9.0 lesson that cost fourteen false regressions.
    """
    out.mkdir(parents=True, exist_ok=True)
    path = out / "verdict.json"
    path.write_text(json.dumps({
        "case": case, "arm": arm, "model": model, "run": run_no, "meta": meta,
        "error": error,
        "passed": None if error else all(p for _, p, _ in results),
        "graders": [{"name": n, "passed": p, "why": w} for n, p, w in results],
    }, indent=2) + "\n")
    return path


def command(case: Case, arm: str, model: str, prompt: str | None, mcp_config: dict[str, Any] | None = None,
            root: Path | None = None) -> list[str]:
    """A `prompt` of None means a multi-turn case: user turns arrive as stream-json on stdin.

    `root` is the plugin tree this run reads — a snapshot when one was taken, the live checkout
    otherwise. Both the allowlisted script paths and `--plugin-dir` follow it, because the agent
    definition is read live too and is as much a mid-run moving part as the scripts are.
    """
    root = root or PLUGIN_ROOT
    mcp_config = mcp_config or {"mcpServers": {}}
    allowed = allowed_tools(root) + ([CALENDAR_TOOL] if "calendar" in mcp_config["mcpServers"] else []) + ([BOARD_TOOL] if "board" in mcp_config["mcpServers"] else []) + (PEER_MOCK_TOOLS if "peers" in mcp_config["mcpServers"] else [])
    cmd = [
        claude_binary(), "-p", *([prompt] if prompt is not None else []),
        "--model", model,
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--setting-sources", "project",
        "--plugin-dir", str(root),
        "--strict-mcp-config", "--mcp-config", json.dumps(mcp_config),
        "--max-turns", str(case.spec.get("max_turns", 10)),
        "--tools", *TOOLS,
        "--allowedTools", *allowed,
        "--disallowedTools", *DISALLOWED,
    ]
    if prompt is None:
        cmd += ["--input-format", "stream-json"]
    if arm == "agent":
        cmd += ["--agent", AGENT]
    return cmd


@dataclass
class Turn:
    events: list[dict[str, Any]]
    t_start: datetime
    t_end: datetime


def has_notification(events: list[dict[str, Any]]) -> bool:
    """A background task's completion landed in this turn (`system:task_notification`)."""
    return any(e.get("type") == "system" and e.get("subtype") == "task_notification" for e in events)


def split_turns(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    turns, current = [], []
    for ev in events:
        current.append(ev)
        if ev.get("type") == "result":
            turns.append(current)
            current = []
    if current:
        turns.append(current)
    return turns


def drive_turns(
    cmd: list[str],
    prompts: list[str | None],
    cwd: Path,
    env: dict[str, str] | None,
    timeout: float,
    snapshot: Callable[[int], None],
    wait_seconds: float = 180,
    before_turn: Callable[[int], None] | None = None,
    close_grace: float = 60,
) -> list[Turn]:
    """Feed each prompt as a stream-json user message; a turn ends at its `result` event.

    A `None` prompt is a wait turn: send nothing and read the turn the session starts on its own
    (a background task's completion notification does this). If the previous turn already carried
    the notification (the task finished before the assistant stopped), there is nothing to wait for
    and the wait turn is skipped. If no event arrives within `wait_seconds`, driving stops there and
    later turns count as not reached. `timeout` bounds the
    whole run and raises `TimeoutError`. After stdin closes, a process still running (a background
    task) is killed after `close_grace`. `snapshot(n)` runs after turn n's result.
    """
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout
    turns: list[Turn] = []
    try:
        for n, prompt in enumerate(prompts, start=1):
            if prompt is None and turns and has_notification(turns[-1].events):
                continue
            if before_turn is not None:
                before_turn(n)
            t_start = datetime.now().astimezone()
            if prompt is not None:
                proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
                proc.stdin.flush()
            events: list[dict[str, Any]] = []
            eof = False
            while True:
                limit = deadline - time.monotonic()
                if prompt is None and not events:
                    limit = min(limit, wait_seconds)
                if limit <= 0 and deadline - time.monotonic() <= 0:
                    raise TimeoutError(f"turn {n} timed out after {timeout}s")
                try:
                    line = lines.get(timeout=max(limit, 0.01))
                except queue.Empty:
                    if deadline - time.monotonic() <= 0:
                        raise TimeoutError(f"turn {n} timed out after {timeout}s") from None
                    return turns  # wait turn: nothing arrived
                if line is None:
                    eof = True
                    break
                if line.strip():
                    events.append(json.loads(line))
                    if events[-1].get("type") == "result":
                        break
            if events:
                turns.append(Turn(events, t_start, datetime.now().astimezone()))
                snapshot(n)
            if eof or events[-1].get("type") != "result":
                break
        proc.stdin.close()
        try:
            proc.wait(timeout=close_grace)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        if proc.poll() is None:
            proc.kill()
    return turns


def grade_turns(spec: dict[str, Any], turns: list[Turn], tz: str, out: Path, calls: list[dict[str, Any]]) -> list[tuple[str, bool, str]]:
    """Grade each turn spec against the turn it belongs to.

    A prompt spec takes the next turn. A wait spec (no prompt) takes the turn that carried the
    background notification: the previous turn itself when the notification landed inside it,
    otherwise the next unprompted turn. Specs with no turn grade as "turn not reached".
    """
    results = []
    ti = -1  # index of the last consumed turn
    for n, turn_spec in enumerate(spec["turns"], start=1):
        if "prompt" in turn_spec:
            ti += 1
            turn_index = ti if ti < len(turns) else None
        elif ti >= 0 and has_notification(turns[ti].events):
            turn_index = ti
        elif ti + 1 < len(turns):
            ti += 1
            turn_index = ti
        else:
            turn_index = None
        if turn_index is None:
            results.append((f"T{n}: ran", False, "turn not reached"))
            continue
        turn = turns[turn_index]
        snap = turn_index + 1
        in_turn = [c for c in calls if "at" in c and turn.t_start <= datetime.fromisoformat(c["at"]) <= turn.t_end]
        rec = RunRecord(
            stream=parse_stream(turn.events),
            t_start=turn.t_start,
            t_end=turn.t_end,
            tz=tz,
            fixture_dir=out / f"fixture-turn{snap}",
            mock_calls=in_turn,
            before_dir=out / ("fixture-before" if snap == 1 else f"fixture-turn{snap - 1}"),
        )
        for i, g in enumerate(turn_spec["graders"]):
            passed, why = grade(g, rec)
            label = g.get("name", f"{i}:{g['type']}")
            results.append((f"T{n}: {label}", passed, why))
    return results


SPAWN_RECORDER = """#!/usr/bin/env python3
import json, sys
from datetime import datetime
from zoneinfo import ZoneInfo

with open({log!r}, "a") as log:
    log.write(json.dumps({{"tool": "spawn", "argv": sys.argv[1:], "at": datetime.now(ZoneInfo({tz!r})).isoformat()}}) + "\\n")
print("spawn: recorded by the eval harness")
"""

RAILWAY_SHIM = '''#!/usr/bin/env python3
import sys
from pathlib import Path

with open(Path(__file__).parent / "calls.log", "a") as log:
    log.write(" ".join(["railway", *sys.argv[1:]]) + "\\n")
print("railway: blocked by eval harness", file=sys.stderr)
sys.exit(1)
'''


class _HealthHandler(BaseHTTPRequestHandler):
    """Answers the status the case named for that path and logs the probe. 404 for anything unlisted."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's name)
        status = self.server.plan.get(self.path, 404)
        body = json.dumps({"path": self.path, "status": status}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with open(self.server.log, "a") as fh:
            fh.write(json.dumps({"tool": "health", "path": self.path, "status": status,
                                 "at": datetime.now(ZoneInfo(self.server.tz)).isoformat()}) + "\n")

    def log_message(self, *_a) -> None:
        return


def health_server(plan: dict[str, int], log: Path, tz: str = "America/Chicago") -> ThreadingHTTPServer:
    """A loopback server for one run. Port 0, so nothing collides; every probe lands in the calls file."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    server.plan, server.log, server.tz = plan, log, tz
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop_server(server: ThreadingHTTPServer | None) -> None:
    if server is not None:
        server.shutdown()
        server.server_close()


def write_recorder(shim_dir: Path, calls_log: Path, tz: str) -> list[str]:
    """A stand-in terminal. Returns the argv template the launcher env var carries."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    recorder = shim_dir / "spawn_recorder.py"
    recorder.write_text(SPAWN_RECORDER.format(log=str(calls_log), tz=tz))
    return ["python3", str(recorder), "{type}", "{cwd}", "{title}"]


def write_shims(shim_dir: Path) -> None:
    shim_dir.mkdir(parents=True, exist_ok=True)
    railway = shim_dir / "railway"
    railway.write_text(RAILWAY_SHIM)
    railway.chmod(0o755)


def run_one(case: Case, arm: str, model: str, out: Path, root: Path | None = None) -> tuple[list[tuple[str, bool, str]], str | None, dict[str, Any]]:
    tz = case.spec.get("tz", "America/Chicago")
    ctx = context(tz, datetime.now(ZoneInfo(tz)))
    out.mkdir(parents=True, exist_ok=True)
    calls_log = out / "calendar" / "calls.jsonl"
    health = None
    if "health" in case.spec:
        # Started before the fixture renders: `{{health_base}}` has to exist when file contents are substituted.
        health = health_server({str(k): int(v) for k, v in case.spec["health"].items()}, calls_log, tz)
        ctx["health_base"] = f"http://127.0.0.1:{health.server_port}"
    spec = render_value(case.spec, ctx)
    # Fixture lives outside this repo so the sidecar's own CLAUDE.md is not discovered upward.
    work = Path(tempfile.mkdtemp(prefix="cos-eval-"))
    try:
        if (case.root / "fixture").is_dir():
            render_tree(case.root / "fixture", work, ctx)
        if "repo" in spec:
            # Real git: `merge-base --is-ancestor` is the thing under test, and a fixture cannot carry a repo.
            make_repo.build(work, spec["repo"])
        # After every piece of setup, so the baseline is what the agent was handed, not a part of it.
        before_snapshot(work, out / "fixture-before")
        write_shims(out / "shims")
        calls_log.parent.mkdir(parents=True, exist_ok=True)
        calls_log.touch()
        env = dict(
            os.environ,
            PATH=f"{out / 'shims'}{os.pathsep}{os.environ['PATH']}",
            CHIEF_OF_STUFF_LAUNCHER=json.dumps(write_recorder(out / "shims", calls_log, tz)),
        )
        mcp_config = None
        if "calendar" in spec:
            # Events live in the results dir, not the agent's cwd, so the calendar is reachable only through the mock.
            (out / "calendar").mkdir(parents=True, exist_ok=True)
            events = out / "calendar" / "events.json"
            events.write_text(render((case.root / spec["calendar"]).read_text(), ctx))
            calls_log.touch()
            mcp_config = calendar_mcp_config(events, calls_log, tz)
        if spec.get("board"):
            # The board publisher logs into the same calls file as the calendar, tagged `"tool": "publish"`.
            (out / "calendar").mkdir(parents=True, exist_ok=True)
            calls_log.touch()
            mcp_config = merge_mcp(mcp_config, board_mcp_config(calls_log, out / "board", work, tz, spec["board"].get("url")))
        if "peers" in spec:
            # Sessions file lives in the results dir; per-turn `peers` keys rewrite it before that turn.
            (out / "calendar").mkdir(parents=True, exist_ok=True)
            calls_log.touch()
            sessions = out / "peers-sessions.json"
            sessions.write_text(render((case.root / spec["peers"]).read_text(), ctx))
            mcp_config = merge_mcp(mcp_config, peers_mcp_config(sessions, calls_log, tz))
        if "turns" in spec:
            return run_turns(spec, arm, model, out, work, env, mcp_config, calls_log, tz, case_root=case.root, ctx=ctx)
        cmd = command(case, arm, model, spec["prompt"], mcp_config=mcp_config, root=root)
        (out / "command.json").write_text(json.dumps(cmd, indent=1))
        t_start = datetime.now(ZoneInfo(tz))
        try:
            proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=spec.get("timeout_seconds", 300))
        except subprocess.TimeoutExpired:
            return [], "timeout", {}
        t_end = datetime.now(ZoneInfo(tz))
        (out / "stream.jsonl").write_text(proc.stdout)
        (out / "stderr.txt").write_text(proc.stderr)
        shutil.copytree(work, out / "fixture", dirs_exist_ok=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        stop_server(health)  # a daemon thread would otherwise outlive this case and serve the next one

    stream = load_stream(out / "stream.jsonl")
    meta = {
        "exit": proc.returncode,
        "turns": (stream.result or {}).get("num_turns"),
        "notional_usd": (stream.result or {}).get("total_cost_usd"),
        "denials": len(stream.denied_ids),
    }
    if proc.returncode != 0 or stream.result is None:
        return [], f"claude exit {proc.returncode}: {proc.stderr.strip()[:200]}", meta
    ok, detail = check_arm(stream.init, arm, agent_flag_used="--agent" in cmd and arm != "agent", needs_calendar="calendar" in spec, model=model, needs_board=bool(spec.get("board")), needs_peers="peers" in spec)
    if not ok:
        return [], f"arm check: {detail}", meta
    mock_calls = [json.loads(l) for l in calls_log.read_text().splitlines() if l.strip()] if calls_log.exists() else []
    rec = RunRecord(stream=stream, t_start=t_start, t_end=t_end, tz=tz, fixture_dir=out / "fixture", mock_calls=mock_calls, before_dir=out / "fixture-before")
    results = []
    for i, g in enumerate(spec["graders"]):
        passed, why = grade(g, rec)
        results.append((g.get("name", f"{i}:{g['type']}"), passed, why))
    return results, None, meta


def run_turns(spec: dict[str, Any], arm: str, model: str, out: Path, work: Path, env: dict[str, str], mcp_config: dict[str, Any] | None, calls_log: Path, tz: str, case_root: Path = Path("."), ctx: dict[str, str] | None = None) -> tuple[list[tuple[str, bool, str]], str | None, dict[str, Any]]:
    """Multi-turn case: one stream-json process, graders scoped to each turn, fixture snapshot per turn."""
    cmd = command(Case("", Path(), spec), arm, model, None, mcp_config=mcp_config)
    (out / "command.json").write_text(json.dumps(cmd, indent=1))

    def snapshot(n: int) -> None:
        shutil.copytree(work, out / f"fixture-turn{n}", dirs_exist_ok=True)

    try:
        def before_turn(n: int) -> None:
            turn_spec = spec["turns"][n - 1]
            if "peers" in turn_spec:
                (out / "peers-sessions.json").write_text(render((case_root / turn_spec["peers"]).read_text(), ctx))

        turns = drive_turns(cmd, [t.get("prompt") for t in spec["turns"]], work, env, spec.get("timeout_seconds", 300), snapshot, wait_seconds=spec.get("wait_seconds", 180), before_turn=before_turn)
    except TimeoutError as exc:
        return [], f"timeout: {exc}", {}
    shutil.copytree(work, out / "fixture", dirs_exist_ok=True)
    (out / "stream.jsonl").write_text("".join(json.dumps(e) + "\n" for t in turns for e in t.events))
    streams = [parse_stream(t.events) for t in turns]
    meta = {
        "turns_reached": len([st for st in streams if st.result]),
        "notional_usd": round(sum((st.result or {}).get("total_cost_usd", 0) for st in streams), 4),
        "denials": sum(len(st.denied_ids) for st in streams),
    }
    if not streams or streams[0].result is None:
        return [], "claude produced no result for turn 1", meta
    ok, detail = check_arm(streams[0].init, arm, agent_flag_used="--agent" in cmd and arm != "agent", needs_calendar="calendar" in spec, model=model, needs_board=bool(spec.get("board")), needs_peers="peers" in spec)
    if not ok:
        return [], f"arm check: {detail}", meta
    calls = [json.loads(l) for l in calls_log.read_text().splitlines() if l.strip()] if calls_log.exists() else []
    return grade_turns(spec, turns, tz, out, calls), None, meta


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", choices=["baseline", "agent"], required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--case", action="append", default=[], help="case name glob; repeatable")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--golden", action="store_true",
                    help='only cases marked "golden": true; the set green is called on, run with --model opus')
    args = ap.parse_args(argv)

    cases = load_cases(args.case, golden_only=args.golden)
    if not cases:
        # Never "0 green, 0 red, exit 0". A selector that finds nothing reads exactly like a
        # clean run, which is the absent-check hole that scored a delete-and-stub 9/9.
        why = "no golden case matched" if args.golden else "no cases matched"
        print(why, file=sys.stderr)
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    harness_error = any_fail = False
    tally = {"green": 0, "red": 0, "unmeasured": 0}
    # One copy for the whole sweep, so every case in a run exercises identical code and the results
    # dir says which. Finding 114: the allowlist and `--plugin-dir` pointed into the live checkout,
    # which silently made that tree read-only for the duration and made a mid-run edit invisible.
    snapshot = snapshot_plugin(EVALS / "results" / stamp / "plugin")
    print(f"plugin snapshot: {snapshot}")
    for case in cases:
        case_pass = True
        unmeasured = False
        for n in range(1, args.runs + 1):
            out = EVALS / "results" / stamp / case.name / args.arm / str(n)
            results, error, meta = run_one(case, args.arm, args.model, out, root=snapshot)
            write_verdict(out, case.name, args.arm, args.model, n, results, error, meta)
            print(f"\n{case.name}  arm={args.arm}  model={args.model}  run={n}  {meta}")
            if error:
                harness_error = unmeasured = True
                print(f"  HARNESS ERROR  {error}")
                continue
            for name, passed, why in results:
                print(f"  {'PASS' if passed else 'FAIL'}  {name}  — {why}")
                case_pass &= passed
        # A case that never ran is not a case that failed. One network outage killed fourteen of the
        # fifty-four in the 0.9.0 sweep — no grader ran in any of them — and the log said RED for all
        # fourteen, which reads as fourteen regressions to look for.
        if unmeasured:
            verdict, key = "UNMEASURED (no grader ran)", "unmeasured"
        elif args.arm == "baseline":
            verdict, key = ("NON-DISCRIMINATING (baseline passes)", "green") if case_pass else ("RED", "red")
        else:
            verdict, key = ("GREEN", "green") if case_pass else ("RED", "red")
            any_fail |= not case_pass
        tally[key] += 1
        print(f"=> {case.name}: {verdict}")
    print(f"\n{len(cases)} case(s): {tally['green']} green, {tally['red']} red, {tally['unmeasured']} unmeasured")
    print(f"results: {EVALS / 'results' / stamp}")
    if harness_error:
        return 2
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
