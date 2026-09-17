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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

EVALS = Path(__file__).resolve().parent
PLUGIN_ROOT = EVALS.parent
AGENT = "chief-of-stuff"
# Opus only (Zach, 2026-09-16). A run whose init reports another model family fails the arm check.
DEFAULT_MODEL = "opus"

# Isolation: `--setting-sources project` drops user settings (and with them user plugins and
# permission rules) but still loads the fixture CLAUDE.md. `--restricted` was probed and skips
# project CLAUDE.md, which is the config channel under test, so it is not used.
TOOLS = ["Bash", "Read", "Glob", "Grep", "Write", "Edit", "Agent"]
# `mv`/`mkdir` are for filing moves (PARA); no `cp` or `rm`, so a "move" cannot become a copy or a delete.
BOARD_RENDERER = PLUGIN_ROOT / "scripts" / "render_board.py"
# The board renderer is the one script the agent may run: html comes from code, never from the agent.
ALLOWED = ["Bash(date:*)", "Bash(TZ=*)", "Bash(mv:*)", "Bash(mkdir:*)", f"Bash(python3 {BOARD_RENDERER}:*)", "Read", "Glob", "Grep", "Write(./**)", "Edit(./**)"]
DISALLOWED = ["Bash(railway:*)", "Bash(git commit:*)", "Bash(git add:*)", "Bash(git push:*)"]
# These reach real Claude sessions on this machine. No eval run may expose them.
PEER_TOOLS = ("ListAgents", "SendMessage")
MOCK_CALENDAR = EVALS / "mock_calendar.py"
CALENDAR_TOOL = "mcp__calendar__list_events"
MOCK_BOARD = EVALS / "mock_board.py"
BOARD_TOOL = "mcp__board__publish"

# `{{name}}`, or `{{now+Nh}}` / `{{now-Nh|local}}` / `{{now+Nh|hhmm}}` for times relative to render time.
TOKEN = re.compile(r"\{\{\s*([a-z_]+)(?:([+-]\d+)h)?(?:\|([a-z]+))?\s*\}\}")
NOW_FORMATS = {"local": "%Y-%m-%d %H:%M", "hhmm": "%H:%M"}
DURATION_HM = re.compile(r"(\d+)\s*h(?:ours?|rs?)?\s*(?:and\s*)?(\d{1,2})\s*m(?:in(?:utes?|s)?)?\b")
DURATION_H = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b")
CLOCK = re.compile(
    r"Now\s+(\d{1,2}):(\d{2})\s+\S+\s+·\s+(.+?)\s+in\s+(\d+)h(\d{2})m\s+\((\d{1,2}):(\d{2})\s+\S+\)"
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


def check_arm(init: dict[str, Any], arm: str, agent_flag_used: bool = False, needs_calendar: bool = False, model: str | None = None, needs_board: bool = False) -> tuple[bool, str]:
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


def _file_matches(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    text = _read(rec.fixture_dir, g["path"])
    if text is None:
        return False, f"{g['path']} missing"
    found = re.search(g["pattern"], text, re.MULTILINE) is not None
    mode = g.get("match", "contains")
    ok = found if mode == "contains" else not found
    return ok, f"{g['path']}: /{g['pattern']}/ {'found' if found else 'not found'}; want {mode}"


def _files(root: Path | None) -> set[str]:
    return {str(f.relative_to(root)) for f in root.rglob("*") if f.is_file()} if root and root.is_dir() else set()


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


FILE_GRADERS = {
    "file_moved": _file_moved,
    "file_unchanged": _file_unchanged,
    "file_matches": _file_matches,
    "no_new_files": _no_new_files,
    "lines_preserved": _lines_preserved,
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


def load_cases(patterns: list[str]) -> list[Case]:
    cases = []
    for spec_path in sorted((EVALS / "cases").glob("*/case.json")):
        name = spec_path.parent.name
        if patterns and not any(fnmatch.fnmatch(name, p) for p in patterns):
            continue
        cases.append(Case(name, spec_path.parent, json.loads(spec_path.read_text())))
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
    problems = [f"{item!r}: end src {a.get('end-src')!r}" for item, a in bars.items() if a.get("end-src") not in ("due", "state", "deadline")]
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


BOARD_GRADERS = {
    "board_published": _board_published,
    "board_matches_tracker": _board_matches_tracker,
    "board_url_fixed": _board_url_fixed,
    "board_bars": _board_bars,
}


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


def command(case: Case, arm: str, model: str, prompt: str | None, mcp_config: dict[str, Any] | None = None) -> list[str]:
    """A `prompt` of None means a multi-turn case: user turns arrive as stream-json on stdin."""
    mcp_config = mcp_config or {"mcpServers": {}}
    allowed = ALLOWED + ([CALENDAR_TOOL] if "calendar" in mcp_config["mcpServers"] else []) + ([BOARD_TOOL] if "board" in mcp_config["mcpServers"] else [])
    cmd = [
        "claude", "-p", *([prompt] if prompt is not None else []),
        "--model", model,
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--setting-sources", "project",
        "--plugin-dir", str(PLUGIN_ROOT),
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


RAILWAY_SHIM = '''#!/usr/bin/env python3
import sys
from pathlib import Path

with open(Path(__file__).parent / "calls.log", "a") as log:
    log.write(" ".join(["railway", *sys.argv[1:]]) + "\\n")
print("railway: blocked by eval harness", file=sys.stderr)
sys.exit(1)
'''


def write_shims(shim_dir: Path) -> None:
    shim_dir.mkdir(parents=True, exist_ok=True)
    railway = shim_dir / "railway"
    railway.write_text(RAILWAY_SHIM)
    railway.chmod(0o755)


def run_one(case: Case, arm: str, model: str, out: Path) -> tuple[list[tuple[str, bool, str]], str | None, dict[str, Any]]:
    tz = case.spec.get("tz", "America/Chicago")
    ctx = context(tz, datetime.now(ZoneInfo(tz)))
    spec = render_value(case.spec, ctx)
    out.mkdir(parents=True, exist_ok=True)
    # Fixture lives outside this repo so the sidecar's own CLAUDE.md is not discovered upward.
    work = Path(tempfile.mkdtemp(prefix="cos-eval-"))
    try:
        if (case.root / "fixture").is_dir():
            render_tree(case.root / "fixture", work, ctx)
            render_tree(case.root / "fixture", out / "fixture-before", ctx)
        write_shims(out / "shims")
        env = dict(os.environ, PATH=f"{out / 'shims'}{os.pathsep}{os.environ['PATH']}")
        mcp_config = None
        calls_log = out / "calendar" / "calls.jsonl"
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
        if "turns" in spec:
            return run_turns(spec, arm, model, out, work, env, mcp_config, calls_log, tz)
        cmd = command(case, arm, model, spec["prompt"], mcp_config=mcp_config)
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

    stream = load_stream(out / "stream.jsonl")
    meta = {
        "exit": proc.returncode,
        "turns": (stream.result or {}).get("num_turns"),
        "notional_usd": (stream.result or {}).get("total_cost_usd"),
        "denials": len(stream.denied_ids),
    }
    if proc.returncode != 0 or stream.result is None:
        return [], f"claude exit {proc.returncode}: {proc.stderr.strip()[:200]}", meta
    ok, detail = check_arm(stream.init, arm, agent_flag_used="--agent" in cmd and arm != "agent", needs_calendar="calendar" in spec, model=model, needs_board=bool(spec.get("board")))
    if not ok:
        return [], f"arm check: {detail}", meta
    mock_calls = [json.loads(l) for l in calls_log.read_text().splitlines() if l.strip()] if calls_log.exists() else []
    rec = RunRecord(stream=stream, t_start=t_start, t_end=t_end, tz=tz, fixture_dir=out / "fixture", mock_calls=mock_calls, before_dir=out / "fixture-before")
    results = []
    for i, g in enumerate(spec["graders"]):
        passed, why = grade(g, rec)
        results.append((g.get("name", f"{i}:{g['type']}"), passed, why))
    return results, None, meta


def run_turns(spec: dict[str, Any], arm: str, model: str, out: Path, work: Path, env: dict[str, str], mcp_config: dict[str, Any] | None, calls_log: Path, tz: str) -> tuple[list[tuple[str, bool, str]], str | None, dict[str, Any]]:
    """Multi-turn case: one stream-json process, graders scoped to each turn, fixture snapshot per turn."""
    cmd = command(Case("", Path(), spec), arm, model, None, mcp_config=mcp_config)
    (out / "command.json").write_text(json.dumps(cmd, indent=1))

    def snapshot(n: int) -> None:
        shutil.copytree(work, out / f"fixture-turn{n}", dirs_exist_ok=True)

    try:
        turns = drive_turns(cmd, [t.get("prompt") for t in spec["turns"]], work, env, spec.get("timeout_seconds", 300), snapshot, wait_seconds=spec.get("wait_seconds", 180))
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
    ok, detail = check_arm(streams[0].init, arm, agent_flag_used="--agent" in cmd and arm != "agent", needs_calendar="calendar" in spec, model=model, needs_board=bool(spec.get("board")))
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
    args = ap.parse_args(argv)

    cases = load_cases(args.case)
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    harness_error = any_fail = False
    for case in cases:
        case_pass = True
        for n in range(1, args.runs + 1):
            out = EVALS / "results" / stamp / case.name / args.arm / str(n)
            results, error, meta = run_one(case, args.arm, args.model, out)
            print(f"\n{case.name}  arm={args.arm}  model={args.model}  run={n}  {meta}")
            if error:
                harness_error = True
                case_pass = False
                print(f"  HARNESS ERROR  {error}")
                continue
            for name, passed, why in results:
                print(f"  {'PASS' if passed else 'FAIL'}  {name}  — {why}")
                case_pass &= passed
        if args.arm == "baseline":
            verdict = "NON-DISCRIMINATING (baseline passes)" if case_pass else "RED"
        else:
            verdict = "GREEN" if case_pass else "RED"
            any_fail |= not case_pass
        print(f"=> {case.name}: {verdict}")
    print(f"\nresults: {EVALS / 'results' / stamp}")
    if harness_error:
        return 2
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
