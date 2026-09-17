"""Unit tests for scripts/render_board.py: the board is a render of the tracker, and every bar cites a field."""

import base64
import contextlib
import hashlib
import io
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import render_board as rb  # noqa: E402

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 16, 14, 30, tzinfo=CT)
NOW2 = datetime(2026, 9, 17, 9, 52, tzinfo=CT)  # the day after Launch: the nearest deadline is days away

# Shaped like a live lane whose item cell accumulated its history (anonymised).
LONG_ITEM = (
    "Service architecture refactor (SOLID / clean architecture): rules engine → rule evaluation → service surface; "
    "record fetch tools → service tools; bullets 0–2b landed in the worktree, uncommitted; the worker left the registry at 07:46; "
    "coordinator reassigned the lane twice overnight; plan approved 01:20 (relayed, not verbatim); remaining: bullets 3–6, docs pass, "
    "eval rerun, merge to main after review; blocked on nothing but an owner"
)

CLAUDE_MD = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Daily log template: `templates/daily.md`; tracker template `templates/tracker.md`
- Tracker: `daily/<date>-tracker.md` (sections: Lanes, Decisions, File ownership, Log)
- Timezone: America/Chicago
- Calendar tool: `mcp__calendar__list_events`
- Calendars:
  - `cal-work`: work
- Deadlines:
  - Launch: 2026-09-16 23:59
  - Final: 2026-09-20 12:00
- Board: `mcp__board__publish`; URL board-7
- Human-only actions: spending money
"""

TRACKER = """# Tracker 2026-09-16

Coordinator: coordinator. Board: board-7.

## Lanes

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Write eval README | Robin | open | 09:00 | 17:00 | Checklist: Write eval README |
| Security audit | subagent | running 10:30 |  |  | Checklist: Security audit |
| Review deploy config | coordinator | open | 2026-09-15 | Final | Checklist: Review deploy config |
| Draft release notes | Robin | done 11:15 | 09:00 |  | Checklist: Draft release notes |
| Short row | Robin |
| Long | Robin | open | 09:00 | 10:00 | x | extra | cells |
| Ship docs site | Robin | open | 2026-09-16 | 2026-09-18 15:00 | Checklist: Ship docs site |
| LONG_ITEM | worker-9a | waiting | 2026-09-16 |  | Checklist: refactor |

## Decisions

| time | item | Robin's words |
|---|---|---|
| 11:15 | Draft release notes | "done" |

## File ownership

## Log

- 09:00 opened the day
- 11:15 release notes done
""".replace("LONG_ITEM", LONG_ITEM)

LOG = """# 2026-09-16

## Goal

## Calendar

- 09:00–09:15 CDT Standup (work)
- 13:00-14:00 Class: Data modeling (program)
- Deadline: Launch 23:59 CDT
- not a time line
| 15:00 | 15:30 | Table meeting |

## Checklist

- [ ] Write eval README
"""


class ConfigTest(unittest.TestCase):
    def test_parses_coordinator_block(self) -> None:
        cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.assertEqual(cfg.user, "Robin")
        self.assertEqual(cfg.log_dir, "daily/")
        self.assertEqual(cfg.tracker_path("2026-09-16"), "daily/2026-09-16-tracker.md")
        self.assertEqual(cfg.tz, "America/Chicago")
        self.assertEqual([d.name for d in cfg.deadlines], ["Launch", "Final"])
        self.assertEqual(cfg.deadlines[0].at, datetime(2026, 9, 16, 23, 59, tzinfo=CT))
        self.assertEqual(cfg.deadlines[1].at, datetime(2026, 9, 20, 12, 0, tzinfo=CT))
        self.assertEqual(cfg.board_tool, "mcp__board__publish")
        self.assertEqual(cfg.board_url, "board-7")

    def test_missing_block_raises(self) -> None:
        with self.assertRaises(rb.ConfigError):
            rb.parse_coordinator("# Workspace\n\nnothing here\n", today=NOW.date())

    def test_board_line_without_url(self) -> None:
        cfg = rb.parse_coordinator(CLAUDE_MD.replace("- Board: `mcp__board__publish`; URL board-7", "- Board: Artifact"), today=NOW.date())
        self.assertEqual(cfg.board_tool, "Artifact")
        self.assertIsNone(cfg.board_url)


class TrackerParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = rb.parse_tracker(TRACKER)

    def test_lanes_and_header(self) -> None:
        self.assertEqual(self.t.board_url, "board-7")
        self.assertEqual([lane.item for lane in self.t.lanes][:4], ["Write eval README", "Security audit", "Review deploy config", "Draft release notes"])
        self.assertEqual(self.t.lanes[1].state, "running 10:30")
        self.assertEqual(self.t.lanes[2].since, "2026-09-15")
        self.assertEqual(self.t.lanes[2].due, "Final")

    def test_malformed_rows_kept_with_warning(self) -> None:
        short, long = self.t.lanes[4], self.t.lanes[5]
        self.assertEqual(short.item, "Short row")
        self.assertIn("2 cells", short.warning)
        self.assertEqual(long.item, "Long")
        self.assertIn("8 cells", long.warning)

    def test_calendar_lenient(self) -> None:
        events = rb.parse_calendar(LOG, NOW.date(), CT)
        self.assertEqual([e.title for e in events], ["Standup (work)", "Class: Data modeling (program)", "Table meeting"])
        self.assertEqual(events[0].start, datetime(2026, 9, 16, 9, 0, tzinfo=CT))
        self.assertEqual(events[1].end, datetime(2026, 9, 16, 14, 0, tzinfo=CT))


class BarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.lanes = {lane.item: lane for lane in rb.parse_tracker(TRACKER).lanes}

    def bar(self, item: str) -> rb.Bar:
        return rb.day_bar(self.lanes[item], self.cfg, NOW)

    def test_due_time_closes_the_bar(self) -> None:
        b = self.bar("Write eval README")
        self.assertEqual((b.start_src, b.end_src), ("since", "due"))
        self.assertEqual(b.start, datetime(2026, 9, 16, 9, 0, tzinfo=CT))
        self.assertEqual(b.end, datetime(2026, 9, 16, 17, 0, tzinfo=CT))
        self.assertIsNone(b.label)

    def test_running_state_is_the_start_and_no_due_is_open_to_deadline(self) -> None:
        b = self.bar("Security audit")
        self.assertEqual((b.start_src, b.end_src), ("state", "deadline"))
        self.assertEqual(b.start, datetime(2026, 9, 16, 10, 30, tzinfo=CT))
        self.assertEqual(b.end, self.cfg.deadlines[0].at)
        self.assertEqual(b.label, "no estimate")

    def test_carried_lane_starts_at_axis_and_due_may_name_a_deadline(self) -> None:
        b = self.bar("Review deploy config")
        self.assertEqual(b.start_src, "carried")
        self.assertEqual(b.end_src, "due")
        self.assertEqual(b.end, self.cfg.deadlines[1].at)

    def test_done_time_closes_the_bar(self) -> None:
        b = self.bar("Draft release notes")
        self.assertEqual(b.end_src, "state")
        self.assertEqual(b.end, datetime(2026, 9, 16, 11, 15, tzinfo=CT))

    def test_week_bar_uses_dates(self) -> None:
        w = rb.week_bar(self.lanes["Review deploy config"], self.cfg, NOW)
        self.assertEqual(w.start.date().isoformat(), "2026-09-15")
        self.assertEqual(w.end, self.cfg.deadlines[1].at)
        w2 = rb.week_bar(self.lanes["Security audit"], self.cfg, NOW)
        self.assertEqual((w2.end_src, w2.label), ("deadline", "no estimate"))


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, LOG, self.cfg, NOW)

    def test_embeds_tracker_hash_and_instants(self) -> None:
        sha = hashlib.sha256(TRACKER.encode()).hexdigest()
        self.assertIn(f'<meta name="tracker-sha256" content="{sha}">', self.html)
        self.assertIn('data-rendered-at="2026-09-16T14:30:00-05:00"', self.html)
        self.assertIn('data-deadline="2026-09-16T23:59:00-05:00"', self.html)
        self.assertIn("Launch", self.html)

    def test_bars_carry_sources_and_labels(self) -> None:
        audit = re.search(r'<[^>]*class="[^"]*\bbar\b[^"]*"[^>]*data-item="Security audit"[^>]*>', self.html)
        self.assertIsNotNone(audit)
        self.assertIn('data-end-src="deadline"', audit.group(0))
        self.assertIn('data-start-src="state"', audit.group(0))
        self.assertIn('data-label="no estimate"', audit.group(0))
        readme = re.search(r'<[^>]*\bbar\b[^>]*data-item="Write eval README"[^>]*>', self.html)
        self.assertIn('data-end-src="due"', readme.group(0))

    def test_sections_and_cuts(self) -> None:
        for heading in ("Robin's queue", "Today", "Week", "Lanes"):
            self.assertIn(heading, self.html)
        for cut in ("Decisions", "File ownership", "release notes done"):
            self.assertNotIn(cut, self.html)
        self.assertIn("tracker as of 14:30", self.html)
        self.assertIn("board-7", self.html)

    def test_queue_lists_user_lanes_not_done(self) -> None:
        queue = self.html.split("Robin's queue", 1)[1].split("<h2", 1)[0]
        self.assertIn("Write eval README", queue)
        self.assertNotIn("Draft release notes", queue)
        self.assertNotIn("Security audit", queue)

    def test_calendar_bands_and_week_deadline_lines(self) -> None:
        self.assertIn('data-band="Standup (work)"', self.html)
        self.assertIn('data-deadline-name="Final"', self.html)

    def test_escapes_html(self) -> None:
        html = rb.render(TRACKER.replace("Write eval README", "Write <b>README</b> & more"), LOG, self.cfg, NOW)
        self.assertNotIn("<b>README</b>", html)
        self.assertIn("&lt;b&gt;README&lt;/b&gt; &amp; more", html)

    def test_deterministic(self) -> None:
        self.assertEqual(self.html, rb.render(TRACKER, LOG, self.cfg, NOW))

    def test_warning_rows_are_shown(self) -> None:
        self.assertIn("2 cells", self.html)


def _strip_html(page: str, kind: str) -> str:
    start = page.index(f'data-strip="{kind}"')
    end = page.find('data-strip="week"', start + 1) if kind == "day" else page.index("<h2>Lanes", start)
    return page[start:end]


class ShortNameTest(unittest.TestCase):
    def test_short_name(self) -> None:
        cases = {
            "Service architecture refactor (SOLID / clean architecture): rules engine → rule evaluation": "Service architecture refactor",
            "Fix 5 (FIXES-REQUIRED.md): statement describes cutoffs by recorded sex": "Fix 5 (FIXES-REQUIRED.md)",
            "13 static-analysis errors in fix 4 launch code (Grant.php, launch.php, GrantTest.php), out of TB17": "13 static-analysis errors in fix 4 launch code",
            "Stored, not sent (Robin 20:49 Wed): ARCHITECTURE.md point 2, no finding for some patients": "Stored, not sent (Robin 20:49 Wed)",
            "Fix: login bug": "Fix: login bug",
            "AI cost analysis": "AI cost analysis",
            "a" * 30 + " " + "b" * 30 + " " + "c" * 30: "a" * 30 + "…",
            "": "",
        }
        for item, want in cases.items():
            self.assertEqual(rb.short_name(item), want, item)
            self.assertLessEqual(len(rb.short_name(item)), 49)


class HistoryLinesTest(unittest.TestCase):
    def test_history_lines(self) -> None:
        item = (
            "Service refactor (SOLID / clean): engine → surface; tools → surface. Absorbs fix 2. "
            "Bullet 0 accepted 00:19 (45-case snapshot; uncommitted); 2b accepted 04:37 (rule, 258 passed + 3 xfail); "
            "Stored (Robin 20:49 Wed) for later; 08:20: worker-9a not in the registry; worktree intact"
        )
        self.assertEqual(
            rb.history_lines(item),
            [
                ("", "engine → surface"),
                ("", "tools → surface"),
                ("", "Absorbs fix 2"),
                ("00:19", "Bullet 0 accepted (45-case snapshot; uncommitted)"),
                ("04:37", "2b accepted (rule, 258 passed + 3 xfail)"),
                ("", "Stored (Robin 20:49 Wed) for later"),
                ("08:20", "worker-9a not in the registry"),
                ("", "worktree intact"),
            ],
        )
        self.assertEqual(rb.history_lines("AI cost analysis"), [])


class DensityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, LOG, self.cfg, NOW)
        self.html2 = rb.render(TRACKER, LOG, self.cfg, NOW2)

    def test_day_axis_ends_today_with_few_ticks(self) -> None:
        for page, now in ((self.html, NOW), (self.html2, NOW2)):
            day = _strip_html(page, "day")
            end = datetime.fromisoformat(re.search(r'data-axis-end="([^"]+)"', day).group(1))
            self.assertLessEqual(end, datetime(now.year, now.month, now.day, tzinfo=CT) + timedelta(days=1))
            self.assertLessEqual(day.count('class="tick"'), 9)
            self.assertGreaterEqual(day.count('class="tick"'), 3)

    def test_overlay_is_track_relative(self) -> None:
        for kind in ("day", "week"):
            strip = _strip_html(self.html, kind)
            overlay = re.search(r'<div class="overlay">(.*?)<div class="nowline" hidden></div></div>', strip, re.S)
            self.assertIsNotNone(overlay, kind)
            self.assertIn('class="dline"', overlay.group(1), kind)
            self.assertEqual(strip.count('class="dline"'), overlay.group(1).count('class="dline"'), kind)
        self.assertIn('class="band"', re.search(r'<div class="overlay">(.*?)<div class="nowline"', _strip_html(self.html, "day"), re.S).group(1))
        self.assertNotIn("scaleX", self.html)
        self.assertNotIn("margin-left:34%", self.html)
        self.assertNotIn("*66", self.html)

    def test_today_folds_unscheduled(self) -> None:
        day = _strip_html(self.html, "day")
        self.assertRegex(day, r'class="bar[^"]*"[^>]*data-item="Write eval README"')
        self.assertRegex(day, r'class="bar running[^"]*"[^>]*data-item="Security audit"')
        self.assertRegex(day, r'class="member"[^>]*data-item="Review deploy config"')
        self.assertNotRegex(day, r'class="bar[^"]*"[^>]*data-item="Review deploy config"')
        self.assertRegex(day, r'data-summary="today"[^>]*data-count="4"')
        self.assertNotIn("Draft release notes", day)
        self.assertNotIn("Draft release notes", _strip_html(self.html, "week"))

    def test_week_folds_deadline_due(self) -> None:
        week = _strip_html(self.html2, "week")
        self.assertRegex(week, r'class="bar[^"]*"[^>]*data-item="Ship docs site"')
        final = re.search(r'<div class="bar[^"]*summary[^"]*"[^>]*data-summary="Final"[^>]*>', week)
        self.assertIsNotNone(final)
        self.assertIn('data-end="2026-09-20T12:00:00-05:00"', final.group(0))
        self.assertRegex(week, r'class="member"[^>]*data-item="Review deploy config"')
        self.assertNotRegex(week, r'class="bar[^"]*"[^>]*data-item="Review deploy config"')

    def test_item_text_bounded(self) -> None:
        esc = html_escape(LONG_ITEM)
        # The size cap is about item text, not the inlined logo asset.
        self.html = re.sub(r"data:image/webp;base64,[A-Za-z0-9+/=]+", "", self.html)
        self.assertLessEqual(self.html.count(esc), 2)
        visible = re.sub(r"<[^>]+>", "", self.html.split("</style>", 1)[1].split("<script>", 1)[0])
        self.assertEqual(visible.count(esc), 0)
        self.assertEqual(visible.count("the worker left the registry"), 1)
        self.assertLess(len(self.html), 18_000)  # the page without the logo asset; the brand CSS and font link add ~400 bytes

    def test_history_is_one_line_per_clause(self) -> None:
        body = re.search(r"<details><summary>Service architecture refactor</summary>(.*?)</details>", self.html, re.S).group(1)
        self.assertIn('<ul class="hist">', body)
        self.assertIn("<li><time>01:20</time><span>plan approved (relayed, not verbatim)</span></li>", body)
        self.assertIn("<li><time>07:46</time><span>the worker left the registry</span></li>", body)
        self.assertIn("<li><time></time><span>record fetch tools → service tools</span></li>", body)

    def test_history_expands_fully(self) -> None:
        hist_css = " ".join(re.findall(r"\.hist[^{]*\{[^}]*\}", self.html))
        self.assertIn(".hist{", hist_css)
        self.assertNotIn("max-height", hist_css)
        self.assertNotIn("overflow", hist_css)

    def test_table_rows_carry_state(self) -> None:
        self.assertRegex(self.html, r'<tr data-state="done"><td>Draft release notes')
        self.assertIn("<details><summary>Service architecture refactor</summary>", self.html)

    def test_long_item_count(self) -> None:
        self.assertIn("1 lane carries history in the item cell", self.html)


CLAUDE_MD_REQ = CLAUDE_MD.replace("  - Launch: 2026-09-16 23:59", "  - Launch: 2026-09-16 23:59; requirements `daily/launch-reqs.md`").replace(
    "  - Final: 2026-09-20 12:00", "  - Final: 2026-09-20 12:00; requirements `daily/final-reqs.md`"
)

REQS = """# Final requirements

Distilled from the brief. This line is a note, not an item.

## Submission

- [x] Deployed URL in the submission — evidence: https://example.test (Log 09:12)
- [ ] Demo video, 3–5 min
  - [ ] Shows the <agent> answering a multi-turn question
- [x] Social post — evidence: commit abc1234

## Engineering

- [ ] Eval suite in CI
- [ ] Cost analysis at 100/1K/10K/100K users
* [ ] star bullets are not items
"""


class RequirementsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD_REQ, today=NOW.date())

    def render(self, now: datetime = NOW, final: str | None = REQS, launch: str | None = None) -> str:
        return rb.render(TRACKER, LOG, self.cfg, now, requirements={"Final": final, "Launch": launch})

    def test_deadline_names_requirements_file(self) -> None:
        launch, final = self.cfg.deadlines
        self.assertEqual(final.at, datetime(2026, 9, 20, 12, 0, tzinfo=CT))
        self.assertEqual(final.requirements, "daily/final-reqs.md")
        self.assertEqual(launch.requirements, "daily/launch-reqs.md")
        plain = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.assertEqual([d.requirements for d in plain.deadlines], [None, None])

    def test_parse_requirements(self) -> None:
        groups = rb.parse_requirements(REQS)
        self.assertEqual([g for g, _ in groups], ["Submission", "Engineering"])
        sub = groups[0][1]
        self.assertEqual([(r.text, r.done, r.depth) for r in sub], [
            ("Deployed URL in the submission", True, 0),
            ("Demo video, 3–5 min", False, 0),
            ("Shows the <agent> answering a multi-turn question", False, 1),
            ("Social post", True, 0),
        ])
        self.assertEqual(sub[0].evidence, "https://example.test (Log 09:12)")
        self.assertEqual(sub[1].evidence, "")
        self.assertEqual(len(groups[1][1]), 2)

    def test_section_counts_and_groups(self) -> None:
        page = self.render()
        section = page.split('<section class="reqs" data-deadline="Final"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Final requirements · 2 of 6", section)
        self.assertIn('data-done="2" data-total="6"', section)
        self.assertIn('<details class="req-group" open><summary>Submission · 2 of 4</summary>', section)
        self.assertIn('<li class="req done depth-0"><span class="box">☑</span><span class="text">Social post</span><span class="evidence">commit abc1234</span></li>', section)
        self.assertIn("Shows the &lt;agent&gt; answering", section)
        self.assertNotIn("star bullets", section)
        self.assertNotIn("max-height", page)
        self.assertLess(page.index('class="reqs"'), page.index("<h2>Today</h2>"))
        self.assertGreater(page.index('class="reqs"'), page.index("Robin's queue"))
        sha = hashlib.sha256(REQS.encode()).hexdigest()
        self.assertIn(f'<meta name="requirements-sha256" data-deadline="Final" content="{sha}">', page)

    def test_inline_markdown(self) -> None:
        page = self.render(final="## G\n\n- [ ] Repo on **`labs.example`** with *setup* & <b>raw</b>\n")
        self.assertIn('<span class="text">Repo on <strong><code>labs.example</code></strong> with <em>setup</em> &amp; &lt;b&gt;raw&lt;/b&gt;</span>', page)

    def test_all_done_group_is_closed(self) -> None:
        page = self.render(final=REQS.replace("- [ ]", "- [x]"))
        self.assertIn('<details class="req-group"><summary>Engineering · 2 of 2</summary>', page)

    def test_passed_deadline_and_no_file_are_omitted(self) -> None:
        page = self.render(now=NOW2, launch=REQS)
        self.assertNotIn('data-deadline="Launch"', page.split("<h2>Today</h2>")[0].split("</style>", 1)[1])
        self.assertIn('<section class="reqs" data-deadline="Final"', page)
        plain = rb.render(TRACKER, LOG, rb.parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW)
        self.assertNotIn('class="reqs"', plain)

    def test_missing_file_is_a_warning(self) -> None:
        page = self.render(final=None)
        self.assertIn('<p class="warn" data-requirements-missing="Final">Final requirements file not found: daily/final-reqs.md</p>', page)

    def test_cli_reads_files_and_summarises(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD_REQ)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(TRACKER)
        (root / "daily" / "final-reqs.md").write_text(REQS)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertIn("requirements=Final:2/6", buf.getvalue())
        self.assertIn("requirements_missing=1", buf.getvalue())
        self.assertIn("Final requirements · 2 of 6", out.read_text())


CLAUDE_MD_FAR = CLAUDE_MD.replace("  - Final: 2026-09-20 12:00", "  - Final: 2026-09-20 12:00\n  - Exam retake: 2026-09-30")

WEEK_TRACKER = """# Tracker 2026-09-17

Coordinator: coordinator. Board: board-7.

## Lanes

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Fix A | Robin | open | 2026-09-17 | 2026-09-18 | x |
| Fix B | Robin | waiting | 2026-09-17 | 2026-09-18 | x |
| Fix C | worker | open | 2026-09-17 | 2026-09-18 15:00 | x |
| Docs pass | Robin | open | 2026-09-17 | 2026-09-19 | x |
| Refactor | worker | running 09:30 | 2026-09-17 | 2026-09-18 | x |
| Exam | Robin | open | 2026-09-17 | 2026-09-26 | x |
| Stale fix | Robin | open | 2026-09-14 | 2026-09-15 | x |
| Cost analysis | Robin | open | 2026-09-17 | Final | x |
| Wider cleanup | Robin | open | 2026-09-17 |  | x |
| Shipped | Robin | done 08:00 | 2026-09-17 | 2026-09-18 | x |

## Log
"""


class WeekByDayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD_FAR, today=NOW2.date())
        self.page = rb.render(WEEK_TRACKER, "", self.cfg, NOW2)
        self.week = _strip_html(self.page, "week")

    def test_week_axis_caps_at_seven_days(self) -> None:
        end = datetime.fromisoformat(re.search(r'data-axis-end="([^"]+)"', self.week).group(1))
        self.assertLessEqual(end, datetime(2026, 9, 24, tzinfo=CT))
        self.assertLessEqual(self.week.count('class="tick"'), 7)

    def test_far_deadline_is_edge_marker(self) -> None:
        self.assertNotIn('class="dline" data-deadline-name="Exam retake"', self.week)
        self.assertIn('class="dline" data-deadline-name="Final"', self.week)
        later = re.search(r'<div class="callout later">(.*?)</div>', self.week)
        self.assertIsNotNone(later)
        self.assertIn("Exam retake Wed 30", later.group(1))

    def test_same_day_lanes_group(self) -> None:
        row = re.search(r'<div class="row group-row"><div class="name">([^<]*)</div><div class="track"><div class="bar[^"]*" data-group="2026-09-18" data-count="3"(.*?)</div></div></div>', self.week)
        self.assertIsNotNone(row)
        self.assertEqual(row.group(1), "Fri 18 · 3 lanes")
        self.assertEqual(row.group(2).count('class="member"'), 3)
        for item in ("Fix A", "Fix B", "Fix C"):
            self.assertNotRegex(self.week, rf'class="bar[^"]*"[^>]*data-item="{item}"')
            self.assertRegex(row.group(2), rf'class="member" data-item="{item}"')
        self.assertIn("3 due", row.group(2))

    def test_single_day_lane_and_running_keep_bars(self) -> None:
        self.assertRegex(self.week, r'class="bar open[^"]*"[^>]*data-item="Docs pass"')
        self.assertRegex(self.week, r'class="bar running[^"]*"[^>]*data-item="Refactor"')
        self.assertNotIn("Shipped", self.week)

    def test_overdue_and_later_rows(self) -> None:
        self.assertRegex(self.week, r'data-group="overdue" data-count="1"')
        self.assertRegex(self.week, r'data-group="later" data-count="1"')
        self.assertRegex(self.week, r'<div class="name">Overdue · 1 lane</div>')
        self.assertRegex(self.week, r'<div class="name">Later · 1 lane</div>')
        order = [self.week.index(k) for k in ('data-group="overdue"', 'data-item="Refactor"', 'data-group="2026-09-18"', 'data-item="Docs pass"', 'data-group="later"', 'data-summary="Final"')]
        self.assertEqual(order, sorted(order))

    def test_deadline_summaries_unchanged(self) -> None:
        self.assertRegex(self.week, r'data-summary="Final" data-count="2"')


    def test_callouts_have_their_own_rows(self) -> None:
        self.assertNotRegex(self.page, r'<div class="dline"[^>]*><span>')
        self.assertNotIn("top:-16px", self.page)
        overlay = re.search(r'<div class="overlay">(.*?)<div class="nowline" hidden></div></div>', self.week, re.S).group(1)
        self.assertNotIn("later", overlay)
        callouts = re.search(r'<div class="callouts">(.*?)</div>\n</div>', self.week, re.S)
        self.assertIsNotNone(callouts)
        rows = re.findall(r'<div class="callout( later)?"[^>]*>(.*?)</div>', callouts.group(1))
        self.assertEqual([(bool(later), text) for later, text in rows], [(False, '<span style="left:50.00%">Final</span>'), (True, "Exam retake Wed 30 →")])
        self.assertLess(self.week.index('<div class="rows">'), self.week.index('<div class="callouts">'))

    def test_callout_near_right_edge_is_anchored_right(self) -> None:
        cfg = rb.parse_coordinator(CLAUDE_MD.replace("  - Final: 2026-09-20 12:00", "  - Final: 2026-09-23 12:00"), today=NOW2.date())
        week = _strip_html(rb.render(WEEK_TRACKER, "", cfg, NOW2), "week")
        self.assertRegex(week, r'<div class="callout"><span class="right" style="right:[0-9.]+%">Final</span></div>')


LIGHT = {"--bg": "#F4EFE1", "--surface": "#FBF8EF", "--fg": "#2F2630", "--muted": "#6E6470", "--line": "#DDD3BD", "--brass": "#A7843E", "--open": "#9DAA72", "--running": "#6F63B4", "--done": "#BDB3A2", "--dl": "#C9533A", "--now": "#4F6B3A", "--bar-ink": "#FBF8EF"}
DARK = {"--bg": "#1E1A20", "--surface": "#29232B", "--fg": "#EFE8D6", "--muted": "#A89FA8", "--line": "#3D3440", "--brass": "#C9A45C", "--open": "#6F7F48", "--running": "#9A8FDA", "--done": "#5A5058", "--dl": "#F0775A", "--now": "#9DBB6E", "--bar-ink": "#1E1A20"}


class BrandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, LOG, self.cfg, NOW)
        self.css = self.html.split("<style>", 1)[1].split("</style>", 1)[0]

    def test_logo_inlined(self) -> None:
        m = re.search(r'<img class="logo" alt="Chief of Stuff" src="data:image/webp;base64,([A-Za-z0-9+/=]+)"', self.html)
        self.assertIsNotNone(m)
        self.assertEqual(base64.b64decode(m.group(1)), rb.LOGO.read_bytes())

    def test_logo_missing_renders_without_image(self) -> None:
        keep = rb.LOGO
        try:
            rb.LOGO = Path(tempfile.mkdtemp()) / "gone.webp"
            page = rb.render(TRACKER, LOG, self.cfg, NOW)
        finally:
            rb.LOGO = keep
        self.assertNotIn('<img class="logo"', page)
        self.assertIn("<h1>", page)

    def test_palette_tokens(self) -> None:
        root = re.search(r"^:root\{(.*?)\}", self.css, re.M | re.S).group(1)
        for name, hex_ in LIGHT.items():
            self.assertIn(f"{name}:{hex_.lower()}", root.lower(), name)
        for block in (r'@media \(prefers-color-scheme:dark\)\{:root:not\(\[data-theme="light"\]\)\{(.*?)\}\}', r':root\[data-theme="dark"\]\{(.*?)\}'):
            body = re.search(block, self.css, re.S).group(1).lower()
            for name, hex_ in DARK.items():
                self.assertIn(f"{name}:{hex_.lower()}", body, f"{name} in {block[:30]}")
        used = set(re.findall(r"var\((--[a-z-]+)\)", self.css))
        theme = set(re.findall(r"(--[a-z-]+):", root))
        defined = theme | set(re.findall(r"(--[a-z-]+):", self.css))  # --name-w is a local, set on .strip
        self.assertTrue(used <= defined, used - defined)
        self.assertTrue(set(LIGHT) <= theme, set(LIGHT) - theme)

    def test_fonts_with_fallbacks(self) -> None:
        link = re.search(r'<link rel="stylesheet" href="(https://fonts\.googleapis\.com/css2\?[^"]+)">', self.html)
        self.assertIsNotNone(link)
        self.assertIn("Cormorant+SC", link.group(1))
        self.assertIn("Alegreya+Sans", link.group(1))
        families = re.findall(r"font(?:-family)?:([^;}]+)", self.css)
        self.assertTrue(families)
        for f in families:
            self.assertRegex(f.strip(), r"(serif|sans-serif|monospace)$", f)

    def test_header(self) -> None:
        self.assertIn("<h1>Board · Wed 16 Sep</h1>", self.html)
        self.assertIn("<title>Board 2026-09-16</title>", self.html)
        header = self.html.split('<div class="header">', 1)[1].split("</header>", 1)[0] if '</header>' in self.html else self.html.split('<div class="header">', 1)[1]
        self.assertLess(header.index('class="logo"'), header.index("<h1>"))


class CliTest(unittest.TestCase):
    def test_writes_board_beside_tracker(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(TRACKER)
        (root / "daily" / "2026-09-16.md").write_text(LOG)
        out = rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertEqual(out, root / "daily" / "2026-09-16-board.html")
        self.assertTrue(out.exists())
        self.assertIn('name="tracker-sha256"', out.read_text())

    def test_summary_line_counts_long_items(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(TRACKER)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertIn("long_items=1", buf.getvalue())

    def test_missing_tracker_is_an_error(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        with self.assertRaises(SystemExit):
            rb.main(["--date", "2026-09-16", "--root", str(root)])


if __name__ == "__main__":
    unittest.main()
