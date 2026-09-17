"""Unit tests for scripts/render_board.py: the board is a render of the tracker, and every bar cites a field."""

import hashlib
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import render_board as rb  # noqa: E402

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 16, 14, 30, tzinfo=CT)

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

## Decisions

| time | item | Robin's words |
|---|---|---|
| 11:15 | Draft release notes | "done" |

## File ownership

## Log

- 09:00 opened the day
- 11:15 release notes done
"""

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

    def test_missing_tracker_is_an_error(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        with self.assertRaises(SystemExit):
            rb.main(["--date", "2026-09-16", "--root", str(root)])


if __name__ == "__main__":
    unittest.main()
