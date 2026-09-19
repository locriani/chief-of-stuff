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

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| a1b2c3 | worker-9a | working | refactor of the loader | | 15:00 | TDD | 2: rules-audit (working), fixture-sweep (idle) | 11:40 |
| d4e5f6 | 7719-fixer | waiting | nothing; S1 landed | Robin — review of S1 | now | | none | 04:45 |
|  | wt-docs | | spawned, not registered | | | | | 11:58 |
| 9f2a41 | 4821-audit | idle | ready for decommissioning 11:20 | | now | | none | 11:20 |
| b7c8d9 | odd-one | dancing | who knows | | | | | 11:00 |

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


class SessionParseTest(unittest.TestCase):
    """`## Sessions` reaches the board for the first time. Nothing read it before this."""

    def setUp(self) -> None:
        self.s = {x.name: x for x in rb.parse_tracker(TRACKER).sessions}

    def test_it_reads_a_row_into_its_columns(self) -> None:
        w = self.s["worker-9a"]
        self.assertEqual((w.ref, w.state, w.free_at, w.last_reply), ("a1b2c3", "working", "15:00", "11:40"))
        self.assertEqual(w.doing, "refactor of the loader")

    def test_a_session_with_no_ref_is_starting_rather_than_stateless(self) -> None:
        """`:188` — a row that has never carried a ref is not gone, it is not there yet."""
        self.assertEqual(self.s["wt-docs"].kind, "starting")

    def test_ready_is_derived_from_doing_and_never_written(self) -> None:
        """`:189` — `ready for decommissioning` is a `doing` value and never a state of its own."""
        self.assertEqual(self.s["4821-audit"].kind, "ready")

    def test_the_four_written_states_pass_through(self) -> None:
        self.assertEqual(self.s["worker-9a"].kind, "working")
        self.assertEqual(self.s["7719-fixer"].kind, "waiting")

    def test_a_word_outside_the_closed_set_is_kept_and_flagged(self) -> None:
        """Same contract as a malformed Lanes row: keep it, say so, never silently normalise."""
        odd = self.s["odd-one"]
        self.assertEqual(odd.state, "dancing")
        self.assertIn("dancing", odd.warning)

    def test_who_it_waits_on_is_an_edge_not_part_of_the_state(self) -> None:
        """`waiting on` already carries `Zach — A2, A3, B`: the target, then the reason."""
        f = self.s["7719-fixer"]
        self.assertEqual(f.waits_on, "Robin")
        self.assertEqual(f.waits_for, "review of S1")

    def test_a_session_waiting_on_nobody_named_has_no_edge(self) -> None:
        self.assertEqual(self.s["worker-9a"].waits_on, "")

    def test_children_are_counted_and_named(self) -> None:
        w = self.s["worker-9a"]
        self.assertEqual(w.child_names, ("rules-audit", "fixture-sweep"))
        self.assertEqual(self.s["7719-fixer"].child_names, ())

    def test_nothing_reported_is_not_the_same_as_a_word_nobody_knows(self) -> None:
        """Found against the live tracker: four seven-column rows all read `unknown`, which is two facts."""
        self.assertEqual(self.s["wt-docs"].kind, "starting")
        self.assertEqual(self.s["odd-one"].kind, "unknown")
        blank = rb.parse_tracker(
            "# T\n\n## Sessions\n\n| ref | name | state | doing | waiting on | free at | constraints | children | last reply |\n"
            "|---|---|---|---|---|---|---|---|---|\n| a1b2c3 | quiet |  | something |  |  |  |  | 09:00 |\n").sessions[0]
        self.assertEqual(blank.kind, "unreported")

    def test_waiting_on_nobody_is_not_an_edge_to_a_node_called_nothing(self) -> None:
        """The live tracker says `nothing; the stack-default fix landed`. That is not a session."""
        for cell in ("nothing", "none", "-", "—", "nobody", "Nothing; the fix landed"):
            s = rb.Session(ref="a", name="n", state="idle", doing="", waiting_on=cell,
                           free_at="", constraints="", children="", last_reply="")
            self.assertEqual(s.waits_on, "", cell)

    def test_a_comma_separates_who_from_why_when_no_dash_does(self) -> None:
        """Live: `Zach, in that session` — three sessions wrote this cell and did not agree on a separator."""
        s = rb.Session(ref="a", name="n", state="waiting", doing="", waiting_on="Zach, in that session",
                       free_at="", constraints="", children="", last_reply="")
        self.assertEqual(s.waits_on, "Zach")
        self.assertEqual(s.waits_for, "in that session")

    def test_the_node_label_drops_the_history_the_name_cell_accretes(self) -> None:
        """Live: `update-claude-md-docs (third name, same ref)`. The graph wants the name, not its provenance."""
        s = rb.Session(ref="a", name="update-claude-md-docs (third name, same ref)", state="idle", doing="",
                       waiting_on="", free_at="", constraints="", children="", last_reply="")
        self.assertEqual(s.label, "update-claude-md-docs")

    def test_a_tracker_with_no_sessions_section_parses_to_nothing(self) -> None:
        self.assertEqual(rb.parse_tracker("# T\n\n## Lanes\n").sessions, ())

    def test_the_seven_column_shape_still_parses(self) -> None:
        """Every tracker written before this version has no `state` and no `children` column."""
        old = ("# T\n\n## Sessions\n\n| ref | name | doing | waiting on | free at | constraints | last reply |\n"
               "|---|---|---|---|---|---|---|\n| a1b2c3 | old-shape | refactor | Robin — a yes | now | TDD | 09:00 |\n")
        s = rb.parse_tracker(old).sessions[0]
        self.assertEqual((s.ref, s.name, s.doing, s.last_reply), ("a1b2c3", "old-shape", "refactor", "09:00"))
        self.assertEqual(s.state, "")
        self.assertEqual(s.waits_on, "Robin")


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

    def test_the_page_declares_its_charset_first(self) -> None:
        """The Artifact wrapper sets one; a local open of the written file showed `Â·` for every ` · ` (C7's visual check)."""
        self.assertTrue(self.html.startswith('<meta charset="utf-8">\n'), self.html[:80])

    def test_embeds_tracker_hash_and_instants(self) -> None:
        sha = hashlib.sha256(TRACKER.encode()).hexdigest()
        self.assertIn(f'<meta name="tracker-sha256" content="{sha}">', self.html)
        self.assertIn('data-rendered-at="2026-09-16T14:30:00-05:00"', self.html)
        self.assertIn('data-deadline="2026-09-16T23:59:00-05:00"', self.html)
        self.assertIn("Launch", self.html)

    def test_bars_carry_sources_and_labels(self) -> None:
        audit = re.search(r'<[^>]*class="[^"]*\bbar\b[^"]*"[^>]*data-item="Security audit"[^>]*>', self.html)
        self.assertIsNotNone(audit)
        self.assertIn('data-end-src="derived"', audit.group(0))
        self.assertIn('data-start-src="state"', audit.group(0))
        self.assertIn('data-label="est. 1/1 → Launch"', audit.group(0))
        readme = re.search(r'<[^>]*\bbar\b[^>]*data-item="Write eval README"[^>]*>', self.html)
        self.assertIn('data-end-src="due"', readme.group(0))

    def test_sections_and_cuts(self) -> None:
        for heading in ("Today", "Week", "Lanes"):
            self.assertIn(heading, self.html)
        for cut in ("Decisions", "File ownership", "release notes done"):
            self.assertNotIn(cut, self.html)
        self.assertIn("tracker as of 14:30", self.html)
        self.assertIn("board-7", self.html)

    def test_the_yours_chip_selects_robins_lanes(self) -> None:
        """What `Robin's queue` was: her lanes, and nobody else's. It is a filter of the one table
        now, so a done lane of hers is kept by the chip and separated by the `done` group instead."""
        lanes = self.html.split("<h2>Lanes", 1)[1]
        rows = {re.search(r"<td>(?:<details><summary>)?([^<]*)", r).group(1): r
                for r in re.findall(r"<tr data-state=.*?</tr>", lanes, re.S)}
        self.assertIn("mine", rows["Write eval README"])
        self.assertIn("mine", rows["Draft release notes"])
        self.assertNotIn("mine", rows["Security audit"])

    def test_calendar_bands_and_week_deadline_lines(self) -> None:
        # The day strip is the next 24 hours, so it draws the events inside them: Standup at 09:00
        # is behind us at 14:30 and no longer on it. Table meeting at 15:00 is.
        self.assertIn('data-band="Table meeting"', self.html)
        self.assertNotIn('data-band="Standup (work)"', self.html)
        self.assertIn('data-deadline-name="Final"', self.html)

    def test_escapes_html(self) -> None:
        html = rb.render(TRACKER.replace("Write eval README", "Write <b>README</b> & more"), LOG, self.cfg, NOW)
        self.assertNotIn("<b>README</b>", html)
        self.assertIn("&lt;b&gt;README&lt;/b&gt; &amp; more", html)

    def test_deterministic(self) -> None:
        self.assertEqual(self.html, rb.render(TRACKER, LOG, self.cfg, NOW))

    def test_warning_rows_are_shown(self) -> None:
        self.assertIn("2 cells", self.html)


def _top_row(item: str) -> str:
    """A regex for `item` drawn as a top-level row. A sub row inside a fold (`row sub`) is not one."""
    return rf'<div class="row"><div class="name">[^<]*</div><div class="track"><div class="bar[^"]*"[^>]*data-item="{re.escape(item)}"'


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

    def test_day_axis_is_the_next_twenty_four_hours_with_few_ticks(self) -> None:
        for page, now in ((self.html, NOW), (self.html2, NOW2)):
            day = _strip_html(page, "day")
            start = datetime.fromisoformat(re.search(r'data-axis-start="([^"]+)"', day).group(1))
            end = datetime.fromisoformat(re.search(r'data-axis-end="([^"]+)"', day).group(1))
            self.assertEqual(end - start, timedelta(hours=24))
            self.assertLessEqual(start, now)
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
        self.assertNotRegex(day, _top_row("Review deploy config"))
        # Two of the four unscheduled lanes are owned, so 0.10.0 derives their ends inside today and draws them.
        self.assertRegex(day, r'data-summary="today"[^>]*data-count="2"')
        self.assertNotIn("Draft release notes", day)
        self.assertNotIn("Draft release notes", _strip_html(self.html, "week"))

    def test_week_folds_deadline_due(self) -> None:
        week = _strip_html(self.html2, "week")
        self.assertRegex(week, r'class="bar[^"]*"[^>]*data-item="Ship docs site"')
        final = re.search(r'<div class="bar[^"]*summary[^"]*"[^>]*data-summary="Final"[^>]*>', week)
        self.assertIsNotNone(final)
        self.assertIn('data-end="2026-09-20T12:00:00-05:00"', final.group(0))
        self.assertRegex(week, r'class="member"[^>]*data-item="Review deploy config"')
        self.assertNotRegex(week, _top_row("Review deploy config"))

    def test_item_text_bounded(self) -> None:
        esc = html_escape(LONG_ITEM)
        # The size cap is about item text, not the inlined logo asset.
        self.html = re.sub(r"data:image/webp;base64,[A-Za-z0-9+/=]+", "", self.html)
        # The lane table, the fold's `.member` citation, and the sub row the fold opens onto (0.10.0).
        self.assertLessEqual(self.html.count(esc), 3)
        visible = re.sub(r"<[^>]+>", "", self.html.split("</style>", 1)[1].split("<script>", 1)[0])
        self.assertEqual(visible.count(esc), 0)
        self.assertEqual(visible.count("the worker left the registry"), 1)
        # This number keeps going the wrong way and Zach has to set it. The run so far, measured
        # on this fixture: 31 174 before the tracer bullet · 33 287 after it · 34 839 with stage 1's
        # one table · 38 768 with stage 3's deadline cards · 41 407 with stage 4's week load row.
        # Each rise is the design showing more, not the page getting denser — stage 3 gives a row to
        # every lane with a time on it rather than only those with a written `due`, which is the
        # thickening it was asked for.
        #
        # The deeper problem is that this guard cannot tell those two apart. 14 385 of the 41 407
        # here is CSS, a fixed cost that does not grow with lanes; the fixture has nine. On the live
        # 116-lane tracker that CSS amortises to nothing and the per-lane bytes are what matter, so
        # a whole-page byte cap measured on nine lanes is the wrong instrument for "density" and
        # will keep needing a rise at every stage that adds a section.
        #
        # Re-based mechanically rather than chosen: measured + the 2.65% headroom the pre-bullet
        # 32 000 had over 31 174. The item-text assertions above are the part of this test that
        # still bites. See DEFERRED in board-redesign-in-flight.md.
        self.assertLess(len(self.html), 42_500)

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
        self.assertRegex(self.html, r'<tr data-state="done" data-in="[^"]*"><td>Draft release notes')
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
        # Stage 5 moved requirements out of the first screen: it answers "is the deck ready",
        # which is a question about the week, so it follows the week and precedes the lanes.
        self.assertGreater(page.index('class="reqs"'), page.index('data-strip="week"'))
        self.assertLess(page.index('class="reqs"'), page.index("<h2>Lanes"))
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
            self.assertNotRegex(self.week, _top_row(item))
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


class OrphanAndUnassignedTest(unittest.TestCase):
    TRACKER = TRACKER.replace(
        "| Security audit | subagent | running 10:30 |  |  | Checklist: Security audit |",
        "| Security audit | 4821-audit | orphaned | 09:00 | 17:00 | Checklist: Security audit |\n| Pick a deploy window | unassigned | open | 09:00 |  | Checklist: deploy window |",
    )

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(self.TRACKER, LOG, self.cfg, NOW)

    def test_orphaned_is_an_active_kind(self) -> None:
        lane = next(l for l in rb.parse_tracker(self.TRACKER).lanes if l.item == "Security audit")
        self.assertEqual(lane.kind, "orphaned")
        day = _strip_html(self.html, "day")
        self.assertRegex(day, r'class="bar orphaned[^"]*"[^>]*data-item="Security audit"')
        self.assertIn(".bar.orphaned{", self.html)

    def test_orphaned_has_its_own_table_group(self) -> None:
        table = self.html.split("<h2>Lanes</h2>", 1)[1]
        self.assertIn('<tr class="group" data-in="all orphaned"><th colspan="6">orphaned · 1 · nobody owns these</th></tr>', table)
        self.assertLess(table.index("nobody owns these"), table.index("Security audit"))
        self.assertLess(table.index("Security audit"), table.index("open · waiting"))
        self.assertIn("1 orphaned", self.html)

    def test_unassigned_is_a_chip_over_the_one_table(self) -> None:
        lanes = self.html.split("<h2>Lanes", 1)[1]
        self.assertIn('<label for="lf-unassigned">unassigned <b>1</b></label>', lanes)
        row = next(r for r in re.findall(r"<tr data-state=.*?</tr>", lanes, re.S) if "Pick a deploy window" in r)
        self.assertIn('data-in="all unassigned"', row)

    def test_the_chip_counts_zero_when_nothing_is_unassigned(self) -> None:
        self.assertIn('<label for="lf-unassigned">unassigned <b>0</b></label>',
                      rb.render(TRACKER, LOG, self.cfg, NOW))


class OneLaneTableTest(unittest.TestCase):
    """Stage 1: three lane tables become one, and Unassigned and the queue become filters of it.

    Unassigned and `<user>'s queue` were projections of the Lanes table printed as tables of their
    own, so a lane owned by Robin and due at 17:00 was on the page three times over. They become
    chips above the one table: `all`, `yours`, `unassigned`, and the three tracker states. The
    chips are radio inputs and the rows carry the filters that keep them in `data-in`, so the
    filtering is CSS over precomputed attributes and the page still needs no JS — with JS off, or
    in a reader that ignores `:checked`, every row is visible, which is what the table did before.

    A filter is mechanical: it selects over every row in the table, done rows included. So `yours`
    answers "which lanes name Robin", and a done lane of Robin's still appears under `done` — the
    groups keep the state reading that the old queue table got by excluding done.
    """

    TRACKER = OrphanAndUnassignedTest.TRACKER.replace(
        "| Ship docs site | Robin | open | 2026-09-16 | 2026-09-18 15:00 | Checklist: Ship docs site |",
        "| Ship docs site | Robin | open | 2026-09-16 | 2026-09-18 15:00 | Checklist: Ship docs site |\n"
        "| Cut the release branch | impl-2 | running 13:40 | 13:40 | 17:00 | Checklist: cut the branch |",
    )

    # label -> (the lanes it keeps, the groups left standing)
    FILTERS = {
        "all": 10,
        "yours": 5,
        "unassigned": 1,
        "running": 1,
        "orphaned": 1,
        "done": 1,
    }

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(self.TRACKER, LOG, self.cfg, NOW)
        self.lanes = self.html.split("<h2>Lanes", 1)[1]
        self.css = self.html.split("<style>")[1].split("</style>")[0]

    def test_the_queue_and_unassigned_tables_are_gone(self) -> None:
        self.assertNotIn("Unassigned ·", self.html)
        # `Robin's queue` survives as an estimate basis on a bar's hover text — it is the heading,
        # and the table under it, that go.
        self.assertNotIn("queue</h2>", self.html)
        self.assertEqual(self.html.count("<h2>Lanes"), 1)
        self.assertEqual(self.html.count("<th>item</th>"), 1)

    def test_every_lane_is_in_the_one_table_exactly_once(self) -> None:
        rows = re.findall(r"<tr data-state=[^>]*>", self.lanes)
        self.assertEqual(len(rows), 10, rows)
        for label in ("Write eval README", "Pick a deploy window", "Security audit",
                      "Draft release notes", "Ship docs site", "Cut the release branch"):
            self.assertEqual(self.lanes.count(f"<summary>{label}</summary>") + self.lanes.count(f"<td>{label}"), 1, label)

    def test_six_chips_carry_the_counts(self) -> None:
        chips = re.findall(r'<label for="lf-([a-z]+)"[^>]*>(.*?)</label>', self.lanes, re.S)
        self.assertEqual([c[0] for c in chips], ["all", "mine", "unassigned", "running", "orphaned", "done"], chips)
        seen = {}
        for key, body in chips:
            label = re.sub(r"<[^>]+>", " ", body).split()
            seen[label[0]] = int(label[-1])
        self.assertEqual(seen, self.FILTERS)

    def test_rows_carry_the_filters_that_keep_them(self) -> None:
        def row(label: str) -> str:
            return next(r for r in re.findall(r"<tr data-state=.*?</tr>", self.lanes, re.S) if label in r)

        self.assertIn('data-in="all mine"', row("Write eval README"))
        self.assertIn('data-in="all unassigned"', row("Pick a deploy window"))
        self.assertIn('data-in="all orphaned"', row("Security audit"))
        self.assertIn('data-in="all mine done"', row("Draft release notes"))
        self.assertIn('data-in="all running"', row("Cut the release branch"))

    def test_a_group_header_goes_when_its_group_empties(self) -> None:
        heads = re.findall(r'<tr class="group" data-in="([^"]*)"><th[^>]*>(.*?) · \d', self.lanes)
        self.assertEqual({label: keeps for keeps, label in heads}, {
            "running": "all running",
            "orphaned": "all orphaned",
            "open · waiting": "all mine unassigned",
            "done": "all mine done",
        })

    def test_the_chips_filter_in_css_and_default_to_all(self) -> None:
        self.assertIn('<input type="radio" name="lf" id="lf-all" checked>', self.lanes)
        for key in ("all", "mine", "unassigned", "running", "orphaned", "done"):
            self.assertIn(f'#lf-{key}:checked~table tr[data-in]:not([data-in~="{key}"]){{display:none}}', self.css)
        # The radios stay reachable by keyboard: taken out of flow, never out of the tab order.
        rule = next((l for l in self.css.splitlines() if ".lanes>input{" in l), "")
        self.assertTrue(rule, "no `.lanes>input` rule")
        self.assertNotIn("display:none", rule)
        self.assertIn(".lanes>input:focus-visible+label{", self.css)


BODY_LOG = LOG.replace(
    "- 09:00–09:15 CDT Standup (work)",
    "- 09:00–09:15 CDT Standup (work)\n"
    "- 16:30-17:30 Gym\n"
    "- 18:00–18:40 Eat (dinner)\n"
    "- 20:00–21:30 Recreation\n"
    "| 23:10 | 23:59 | Sleep |",
)


class BodyLaneTest(unittest.TestCase):
    """Stage 2: the BODY lane — sleep, eat, gym, recreation — drawn as its own first row.

    The 24 hours the board draws are a day of a person, not of a queue: the hours already spent
    asleep, eating, at the gym or off are not available for work, and until they are on the strip
    every deadline above them reads as if they were. They come from the calendar the log already
    carries, so this needs no new grammar — an event whose title names one of the four kinds is a
    body segment instead of a background band, and every other event stays the band it was.

    The four fills are the ones already recorded against the body lane, and re-solving them is
    parked: sleep #332288, eat #D55E00, gym #117733, recreation #F0E442, cream ink on sleep and
    gym alone.
    """

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, BODY_LOG, self.cfg, NOW)
        self.day = _strip_html(self.html, "day")
        self.css = self.html.split("<style>")[1].split("</style>")[0]

    def test_the_four_kinds_are_classified_off_the_calendar(self) -> None:
        self.assertEqual(
            [rb.body_kind(t) for t in ("Sleep", "Eat (dinner)", "Gym", "Recreation",
                                       "Standup (work)", "Table meeting", "sleepy hollow review")],
            ["sleep", "eat", "gym", "recreation", "", "", ""],
        )

    def test_the_body_row_is_the_first_row_of_the_day(self) -> None:
        rows = re.findall(r'<div class="(row\b[a-z -]*)"', self.day)
        self.assertEqual(rows[0], "row body", rows[:3])
        self.assertLess(self.day.index('class="row body"'), self.day.index('class="row swim-row"'))
        self.assertIn('<div class="name">body</div>', self.day)
        self.assertEqual(self.day.count('class="row body"'), 1)

    def test_each_kind_is_a_segment_carrying_its_clock(self) -> None:
        segs = re.findall(r'<div class="seg ([a-z]+)"[^>]*data-start="([^"]+)"[^>]*data-end="([^"]+)"', self.day)
        self.assertEqual([s[0] for s in segs], ["gym", "eat", "recreation", "sleep"])
        self.assertTrue(segs[0][1].endswith("16:30:00-05:00"), segs[0])
        self.assertTrue(segs[0][2].endswith("17:30:00-05:00"), segs[0])

    def test_a_body_event_is_not_also_a_band(self) -> None:
        # Drawn twice it would read as two things happening at once.
        self.assertNotIn('data-band="Sleep"', self.day)
        self.assertNotIn('data-band="Gym"', self.day)
        self.assertIn('data-band="Table meeting"', self.day)

    def test_the_fills_are_the_ones_already_recorded(self) -> None:
        for kind, fill in (("sleep", "#332288"), ("eat", "#D55E00"),
                           ("gym", "#117733"), ("recreation", "#F0E442")):
            self.assertIn(f"--{kind}:{fill}", self.css, kind)
            self.assertIn(f".seg.{kind}{{background:var(--{kind})", self.css, kind)

    def test_cream_ink_on_sleep_and_gym_alone(self) -> None:
        rule = re.search(r"\.seg\{([^}]*)\}", self.css).group(1)
        self.assertIn("color:var(--fg)", rule)
        cream = re.search(r"([.a-z,]*)\{color:var\(--bar-ink\)\}", self.css)
        self.assertEqual(sorted(cream.group(1).split(",")), [".seg.gym", ".seg.sleep"])

    def test_the_legend_names_the_four_kinds(self) -> None:
        legend = re.search(r'<div class="legend">(.*?)</div>\n', self.html, re.S).group(1)
        for kind in ("sleep", "eat", "gym", "recreation"):
            self.assertRegex(legend, rf'<span class="key seg {kind}"></span>\s*{kind}')


BLOCKED_TRACKER = TRACKER.replace(
    "| Security audit | subagent | running 10:30 |  |  | Checklist: Security audit |",
    "| Security audit | 4821-audit | orphaned | 09:00 | 17:00 | Checklist: Security audit |\n"
    "| Pick a deploy window | unassigned | orphaned | 09:00 |  | Checklist: deploy window |\n"
    "| Retire the v1 upload path | unassigned | orphaned | 08:00 | Final | Checklist: retire |\n"
    "| Backfill the seed fixtures | unassigned | orphaned | 07:00 |  | Checklist: backfill |\n"
    "| Reply to the scheduling thread | Robin | open | 10:00 | 16:00 | Checklist: reply |",
)


class DueNextCardsTest(unittest.TestCase):
    """Stage 3: DUE NEXT stops being one flat list and becomes a card per governing deadline.

    A flat list sorted by time answers "what is next" but not "what makes me late for the thing
    with a clock on it" — the deadline is the question, and a lane answering to tomorrow sitting
    between two of this morning's reads as though it were one of them. The cards keep the order
    the deadlines fall in, and the lanes that answer to none come last.

    A row appears for any lane that has a time at all, written (`due`) or derived; `no estimate`
    keeps the meaning the rest of the page gives it, `Bar.label == NO_ESTIMATE`, and those lanes
    are counted in their card's head rather than given a row they cannot fill.
    """

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(BLOCKED_TRACKER, LOG, self.cfg, NOW)
        self.due = self.html.split("<h2>Due next</h2>", 1)[1].split("<h2>Blocked", 1)[0]

    def test_a_card_per_deadline_in_deadline_order(self) -> None:
        heads = re.findall(r'<div class="card-head"><b>([^<]*)</b>', self.due)
        self.assertEqual(heads, ["Launch · 23:59", "Final · Sun 20 12:00"], heads)

    def test_the_head_counts_the_group_and_its_unestimated(self) -> None:
        metas = re.findall(r'<div class="card-head">.*?<span class="meta">([^<]*)</span>', self.due, re.S)
        self.assertTrue(metas)
        self.assertTrue(all(re.fullmatch(r"\d+ lanes? · \d+ with no estimate", m) for m in metas), metas)

    def test_a_row_carries_the_clock_the_name_the_owner_the_state_and_the_size(self) -> None:
        row = re.search(r'<div class="due-row"[^>]*>.*?</div>\s*</div>', self.due, re.S).group(0)
        for frag in ('<span class="at">', '<span class="what">', '<span class="who">',
                     '<span class="state">', '<span class="size">'):
            self.assertIn(frag, row, frag)

    def test_a_derived_time_is_marked_and_a_written_one_is_not(self) -> None:
        rows = re.findall(r'<div class="due-row"([^>]*)>.*?<span class="at">([^<]*)</span>', self.due, re.S)
        written = [at for attrs, at in rows if 'data-at-src="due"' in attrs]
        derived = [at for attrs, at in rows if 'data-at-src="derived"' in attrs]
        self.assertTrue(written, rows)
        self.assertTrue(derived, rows)
        self.assertFalse([a for a in written if a.startswith("~")], written)
        self.assertTrue(all(a.startswith("~") for a in derived), derived)

    def test_an_orphaned_row_is_flagged(self) -> None:
        row = next(r for r in re.findall(r'<div class="due-row".*?</div>\s*</div>', self.due, re.S)
                   if "Security audit" in r)
        self.assertIn('data-kind="orphaned"', row)
        self.assertIn("⚑", row)

    def test_every_dated_lane_still_appears(self) -> None:
        for label in ("Write eval README", "Security audit", "Ship docs site",
                      "Reply to the scheduling thread", "Retire the v1 upload path"):
            self.assertIn(label, self.due, label)
        self.assertNotIn("Draft release notes", self.due)


class BlockedCardsTest(unittest.TestCase):
    """Stage 3: the three BLOCKED cards gain their counts, a truncation, and the silence.

    BLOCKED is the band that is never folded, so it cannot be allowed to grow without bound either:
    each card shows its first three and says how many more there are. NOT REPORTING says how long a
    session has been silent and how many lanes are waiting behind that silence — a session name on
    its own does not say what it costs.
    """

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(BLOCKED_TRACKER, LOG, self.cfg, NOW)
        self.blocked = self.html.split("<h2>Blocked</h2>", 1)[1].split("<h2>", 1)[0]

    def cards(self) -> dict[str, str]:
        return {m.group(1): m.group(0) for m in
                re.finditer(r'<div class="card"[^>]*><h3>([^<·]*?) ·.*?</div>', self.blocked, re.S)}

    def test_three_cards_each_with_its_count(self) -> None:
        heads = re.findall(r"<h3>([^<]*)</h3>", self.blocked)
        self.assertEqual([h.split(" · ")[0] for h in heads], ["Awaiting you", "Orphaned", "Not reporting"])
        for h in heads:
            self.assertRegex(h, r" · \d+$")

    def test_a_long_card_shows_three_and_counts_the_rest(self) -> None:
        orphaned = self.cards()["Orphaned"]
        self.assertEqual(orphaned.count('<li class="blocked-row"'), 3, orphaned)
        self.assertRegex(orphaned, r'<li class="more">\+ 1 more</li>')

    def test_a_lane_with_no_due_says_so(self) -> None:
        self.assertIn("no due", self.cards()["Orphaned"])

    def test_not_reporting_says_how_long_and_how_many_lanes(self) -> None:
        card = self.cards()["Not reporting"]
        self.assertRegex(card, r'(silent \d+h\d+m|never reported)')
        self.assertRegex(card, r'\d+ lanes?</span>')


class WeekLoadTest(unittest.TestCase):
    """Stage 4: a load row over the week — what is committed against the work time there is for it.

    The week strip says when lanes land. It does not say whether they can: a day carrying eighteen
    hours of estimated work and thirteen hours awake looks exactly like a day carrying four. The
    load row puts the two numbers side by side, one cell per day of the axis, and marks the days
    where the first is larger than the second.

    Work time available is the day minus the body lane — which is why the body lane came first.
    Beyond today the calendar holds no events, so nothing is booked and the whole day reads as
    available; that is true of the inputs rather than useful, and the gap is recorded under
    DEFERRED rather than papered over with an invented working day.
    """

    # SIZED_TRACKER is the fixture that can answer the question at all: it carries sizes and closed
    # lanes with ranges, so there is a mean per size to estimate from. A tracker with neither has no
    # committed time to report, only lanes it cannot size — which is what `data-unsized` counts.
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(SIZED_TRACKER, BODY_LOG, self.cfg, NOW)
        self.load = self.html.split('data-load="week"', 1)[1].split("</div></div>", 1)[0]

    def test_the_load_row_sits_above_the_week_strip(self) -> None:
        body = self.html.split("</style>", 1)[1]
        self.assertLess(body.index("<h2>Week</h2>"), body.index('data-load="week"'))
        self.assertLess(body.index('data-load="week"'), body.index('data-strip="week"'))

    def test_a_cell_per_day_carrying_both_numbers(self) -> None:
        cells = re.findall(
            r'<div class="load-cell"[^>]*data-day="(\d{4}-\d{2}-\d{2})"[^>]*data-committed="(\d+)"[^>]*data-available="(\d+)"',
            self.load)
        self.assertTrue(cells)
        days = [c[0] for c in cells]
        self.assertEqual(days, sorted(days))
        self.assertEqual(days[0], NOW.date().isoformat())
        for _, committed, available in cells:
            self.assertGreaterEqual(int(available), 0)
            self.assertGreaterEqual(int(committed), 0)

    def test_the_body_lane_is_taken_out_of_the_work_time(self) -> None:
        """Today's available time runs from now, not from midnight — the morning is spent. NOW is
        14:30, so 570 minutes of the day are left, and BODY_LOG books 239 of them (gym 60, eat 40,
        recreation 90, sleep 49)."""
        first = re.search(r'<div class="load-cell"[^>]*data-available="(\d+)"', self.load)
        self.assertEqual(int(first.group(1)), 570 - 239)

    def test_a_day_over_its_work_time_is_marked_and_flagged(self) -> None:
        cells = re.findall(
            r'<div class="load-cell"[^>]*data-committed="(\d+)"[^>]*data-available="(\d+)"[^>]*data-over="([01])"'
            r'.*?<span class="at">([^<]*)</span>', self.load, re.S)
        self.assertTrue(cells)
        for committed, available, over, text in cells:
            self.assertEqual(over, "1" if int(committed) > int(available) else "0", (committed, available))
            self.assertEqual("⚑" in text, over == "1", text)

    def test_the_committed_time_is_split_into_written_and_derived(self) -> None:
        cells = re.findall(r'data-committed="(\d+)" data-due="(\d+)" data-derived="(\d+)"', self.load)
        self.assertTrue(cells)
        for committed, due, derived in cells:
            self.assertEqual(int(committed), int(due) + int(derived))
        self.assertIn('<span class="fill due"', self.load)

    def test_committed_is_work_and_not_the_span_of_the_bar(self) -> None:
        """A lane running since Monday and due Friday occupies four days of the strip and is not
        four days of work; charging its span to every day it crosses is what this pins shut.

        SIZED_TRACKER's five active lanes, each charged once to the day it lands, all of which is
        today: A oldest 1h30m + B running 30m + C newer 1h07m derived = 187, and D dated (no
        estimate, no L in the history, so the mean of all four closed lanes, 1h07m) + E unassigned
        (M, 1h30m) = 157 written. 344 against 331 minutes of work time left, so today is over."""
        first = re.search(r'<div class="load-cell"[^>]*data-committed="(\d+)" data-due="(\d+)" '
                          r'data-derived="(\d+)" data-unsized="(\d+)" data-available="(\d+)" '
                          r'data-over="(\d)"', self.load)
        self.assertIsNotNone(first, self.load[:400])
        self.assertEqual(first.groups(), ("344", "157", "187", "0", "331", "1"))

    def test_each_lane_is_charged_to_exactly_one_day(self) -> None:
        total = sum(int(c) for c in re.findall(r'data-committed="(\d+)"', self.load))
        self.assertEqual(total, 344)

    def test_the_legend_names_the_load_marks(self) -> None:
        legend = re.search(r'<div class="legend">(.*?)</div>\n', self.html, re.S).group(1)
        for cls, label in (("key fill due", "committed"), ("key fill derived", "derived, no due written"),
                           ("key fill over", "over work time"), ("key fill free", "uncommitted")):
            self.assertRegex(legend, rf'<span class="{re.escape(cls)}"></span>\s*{re.escape(label)}', cls)


class SectionOrderTest(unittest.TestCase):
    """Stage 5: requirements and sessions take their slots, and the page reads in one order.

    DUE NEXT · BLOCKED · 24 HOURS · WEEK · REQUIREMENTS · LANES · SESSIONS. Requirements sat third,
    between BLOCKED and the day strip, where a long deck pushed the two strips off the first screen
    — the thing with a clock on it should not be below a checklist of what the deck must contain.
    It answers "is the deck ready", which is a question about the week, so it follows the week.

    Sessions stays last: it is the roster the rest of the page cites, and nothing on it is a thing
    to do next.
    """

    REQS = """# Final

## Deck

- [x] Title slide
- [ ] Method
- [ ] Results
"""

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD_REQ, today=NOW.date())
        self.html = rb.render(TRACKER, LOG, self.cfg, NOW, requirements={"Final": self.REQS, "Launch": self.REQS})
        self.body = self.html.split("</style>", 1)[1]

    def test_the_page_reads_in_the_approved_order(self) -> None:
        marks = ["Due next", "Blocked", 'data-strip="day"', 'data-load="week"',
                 'data-strip="week"', 'class="reqs"', "<h2>Lanes", "<h2>Sessions"]
        at = [self.body.index(m) for m in marks]
        self.assertEqual(at, sorted(at), [m for _, m in sorted(zip(at, marks))])

    def test_requirements_sit_between_the_week_and_the_lanes(self) -> None:
        self.assertGreater(self.body.index('class="reqs"'), self.body.index('data-strip="week"'))
        self.assertLess(self.body.index('class="reqs"'), self.body.index("<h2>Lanes"))

    def test_every_deck_still_renders_with_its_meter(self) -> None:
        self.assertEqual(self.body.count('<section class="reqs"'), 2)
        self.assertEqual(self.body.count('class="meter"'), 2)
        self.assertIn("requirements · 1 of 3", self.body)

    def test_sessions_is_last(self) -> None:
        self.assertEqual(self.body.index("<h2>Sessions"), max(
            self.body.index(m) for m in ("Due next", "Blocked", "<h2>Lanes", 'class="reqs"', "<h2>Sessions")))


class PhoneTest(unittest.TestCase):
    """Stage 6: the page at 390px, which is where it is read in the morning.

    Laptop first was the decision, so nothing here changes the wide layout. What it fixes is the
    three places that cannot survive a 390px viewport: the due row's fixed owner and time columns,
    the strip's percentage name column (36% of 390 is 140px of name against 218px of axis), and the
    six-column lane table, which is wider than the screen whatever the CSS says.

    A table is allowed to scroll sideways inside its own box. The page body is not — a board that
    slides under the thumb is the failure this stage exists to prevent.
    """

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, BODY_LOG, self.cfg, NOW)
        self.css = self.html.split("<style>")[1].split("</style>")[0]
        at = self.css.index("@media (max-width:420px)")
        self.phone = self.css[at:]

    def test_there_is_a_phone_block_at_all(self) -> None:
        self.assertRegex(self.css, r"@media \(max-width:420px\)")

    def test_the_due_row_stacks_instead_of_holding_five_columns(self) -> None:
        self.assertRegex(self.phone, r"\.due-row\{[^}]*flex-wrap:wrap")
        self.assertRegex(self.phone, r"\.due-row \.what\{[^}]*flex:1 1 100%")

    def test_the_strip_name_column_is_pixels_not_a_third_of_the_screen(self) -> None:
        self.assertRegex(self.phone, r"\.strip\{--name-w:9[0-9]px\}")

    def test_the_lane_table_scrolls_inside_its_own_box(self) -> None:
        self.assertIn('<div class="scroll">', self.html.split("<h2>Lanes", 1)[1])
        self.assertRegex(self.css, r"\.scroll\{[^}]*overflow-x:auto")

    def test_nothing_forces_the_body_wider_than_a_phone(self) -> None:
        """A min-width wider than the screen is the usual way a page starts sliding sideways."""
        for m in re.finditer(r"min-width:(\d+)px", self.css):
            self.assertLessEqual(int(m.group(1)), 390, m.group(0))

    def test_the_last_axis_tick_is_pulled_inside_the_axis(self) -> None:
        """Only the render caught this. Every tick is centred on its time with translateX(-50%), so
        the one at left:100% hangs half its label past the right edge — 1px of horizontal scroll on
        a 390px screen, and the whole page slides under the thumb. The last tick right-aligns."""
        self.assertRegex(self.phone, r"\.axis \.tick:last-child\{transform:translateX\(-100%\)\}")

    def test_the_wide_layout_is_untouched(self) -> None:
        """Laptop first: every phone rule lives inside a max-width query and none outside one."""
        outside = re.sub(r"@media[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", "", self.css)
        self.assertNotIn("--name-w:96px", outside)
        self.assertNotIn("flex:1 1 100%", outside)


class GridlinesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD_FAR, today=NOW2.date())
        self.page = rb.render(WEEK_TRACKER, "", self.cfg, NOW2)

    def grids(self, kind: str) -> list[tuple[str, float]]:
        strip = _strip_html(self.page, kind)
        return [(("half" if "half" in cls else "full"), float(pct)) for cls, pct in re.findall(r'<div class="grid( half)?" style="left:([0-9.]+)%"></div>', strip)]

    def test_week_grid_at_day_and_half_day(self) -> None:
        grid = self.grids("week")
        # 7 day columns: a full line on each boundary, a half line at each midday.
        self.assertEqual([k for k, _ in grid].count("full"), 7)
        self.assertEqual([k for k, _ in grid].count("half"), 7)
        self.assertEqual([round(p, 2) for k, p in grid if k == "full"][:3], [0.0, 14.29, 28.57])
        self.assertEqual(round(next(p for k, p in grid if k == "half"), 2), 7.14)

    def test_day_grid_lands_under_the_ticks(self) -> None:
        """A full line per tick, a half line between. Hourly lines over a rolling 24h axis were 25
        lines across the strip — hatching, not a grid — and they cost more bytes than they were worth."""
        grid = self.grids("day")
        day = _strip_html(self.page, "day")
        ticks = [float(p) for p in re.findall(r'<span class="tick" style="left:([0-9.]+)%">', day)]
        self.assertEqual([round(p, 2) for k, p in grid if k == "full"], [round(p, 2) for p in ticks])
        self.assertEqual([k for k, _ in grid][:4], ["full", "half", "full", "half"])
        self.assertLessEqual(len(grid), 2 * len(ticks))

    def test_grid_sits_under_the_bars(self) -> None:
        week = _strip_html(self.page, "week")
        overlay = re.search(r'<div class="overlay">(.*?)<div class="nowline" hidden></div></div>', week, re.S).group(1)
        self.assertIn('<div class="grid"', overlay)
        self.assertLess(overlay.index('class="grid"'), overlay.index('class="dline"'))
        self.assertIn(".grid{", self.page)
        self.assertIn(".grid.half{", self.page)


class LegendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(OrphanAndUnassignedTest.TRACKER, LOG, self.cfg, NOW)
        self.legend = re.search(r'<div class="legend">(.*?)</div>\n', self.html, re.S).group(1)

    def test_legend_names_every_mark(self) -> None:
        for cls, label in [
            ("key bar running", "running"),
            ("key bar open", "open"),
            ("key bar orphaned", "orphaned"),
            ("key bar open-end", "no estimate"),
            ("key bar derived", "estimated"),
            ("key bar summary", "folded rows"),
            ("key seg sleep", "sleep"),
            ("key seg eat", "eat"),
            ("key seg gym", "gym"),
            ("key seg recreation", "recreation"),
            ("key band", "calendar event"),
            ("key nowline", "now"),
            ("key dline", "deadline"),
        ]:
            self.assertRegex(self.legend, rf'<span class="{re.escape(cls)}"></span>\s*{re.escape(label)}', cls)

    def test_legend_sits_before_the_first_chart(self) -> None:
        self.assertLess(self.html.index('class="legend"'), self.html.index('data-strip="day"'))
        self.assertGreater(self.html.index('class="legend"'), self.html.index("<h2>Today</h2>"))
        self.assertEqual(self.html.count('class="legend"'), 1)


class LaneGroupHeaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(OrphanAndUnassignedTest.TRACKER, LOG, self.cfg, NOW)
        self.table = self.html.split("<h2>Lanes</h2>", 1)[1]

    def test_group_rows_are_marked_and_counted(self) -> None:
        heads = re.findall(r'<tr class="group" data-in="[^"]*"><th colspan="6">([^<]*)</th></tr>', self.table)
        self.assertEqual(heads, ["running · 0", "orphaned · 1 · nobody owns these", "open · waiting · 7", "done · 1"])

    def test_group_rows_look_like_headers(self) -> None:
        self.assertIn("tr.group th{", self.html)
        css = re.search(r"tr\.group th\{([^}]*)\}", self.html).group(1)
        for prop in ("text-transform:uppercase", "letter-spacing", "background:", "font-weight:"):
            self.assertIn(prop, css, prop)

    def test_empty_group_says_none(self) -> None:
        self.assertRegex(self.table, r'<tr class="group" data-in="all"><th colspan="6">running · 0</th></tr>\s*<tr data-in="all"><td colspan="6" class="muted">none</td></tr>')


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


RESUME = """- As of: 09:52 — gauntlet-f0 [308833]
- In flight: nothing; the poll to 4821-audit is out
- Next: read 4821-audit's reply, then the deploy decision
- Waiting on: Robin — TB17 diff review
- Re-arm: the 30-minute check; the board watch
- Verified: origin/main d6aab3b, 2 unpushed; agent /ready 200, login 200 (09:47)
"""
TRACKER_RESUME = TRACKER.replace("## Lanes", "## Resume\n\n" + RESUME + "\n## Lanes", 1)


class LegendKeyTest(unittest.TestCase):
    """A legend key borrows the chart's classes, and those are absolutely positioned.

    With no positioned ancestor an absolute key anchors to the page itself, so the calendar-event,
    now and deadline marks land in the top-left corner above the header. Seen live on 0.5.0.
    """

    def setUp(self) -> None:
        self.html = rb.render(TRACKER, LOG, rb.parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW)
        self.css = self.html.split("<style>")[1].split("</style>")[0]

    def test_keys_are_taken_out_of_absolute_positioning(self) -> None:
        rule = next((l for l in self.css.splitlines() if ".legend .key{" in l), "")
        self.assertTrue(rule, "no `.legend .key` rule: keys still inherit .bar/.band/.nowline positioning")
        self.assertIn("position:static", rule)

    def test_every_legend_key_has_a_size_of_its_own(self) -> None:
        # A key drawn as a line has no width; one drawn as a swatch has no border. Both need a height.
        for mark in ("nowline", "dline", "band"):
            rule = next((l for l in self.css.splitlines() if f".legend .key.{mark}{{" in l), "")
            self.assertTrue(rule, f"no sizing rule for the {mark} key")
            self.assertIn("height", rule)

    def test_the_legend_sits_inside_a_positioned_row(self) -> None:
        self.assertIn(".legend .item{", self.css)
        rule = next(l for l in self.css.splitlines() if ".legend .item{" in l)
        self.assertIn("position:relative", rule)


class ResumeBlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())

    def test_parses_the_block_into_fields(self) -> None:
        block = rb.parse_resume(TRACKER_RESUME)
        self.assertEqual(block["as of"], "09:52 — gauntlet-f0 [308833]")
        self.assertEqual(block["waiting on"], "Robin — TB17 diff review")
        self.assertIn("origin/main d6aab3b", block["verified"])

    def test_no_block_is_empty_not_an_error(self) -> None:
        self.assertEqual(rb.parse_resume(TRACKER), {})

    def test_board_url_still_comes_from_the_header_not_the_block(self) -> None:
        # A block above Lanes is inside the old header slice; a `Board:` word in it must not win.
        tracker = TRACKER.replace(
            "## Lanes",
            "## Resume\n\n- Next: say the Board: not-a-url line was never republished\n\n## Lanes",
            1,
        )
        self.assertEqual(rb.parse_tracker(tracker).board_url, "board-7")

    def test_a_tracker_with_no_header_url_still_reads_none(self) -> None:
        tracker = TRACKER.replace("Coordinator: coordinator. Board: board-7.", "Coordinator: coordinator.")
        self.assertIsNone(rb.parse_tracker(tracker).board_url)

    def test_a_block_cannot_supply_the_url_when_the_header_has_none(self) -> None:
        # The header is what precedes the first `## `, so no later section can name the board.
        tracker = TRACKER_RESUME.replace("Coordinator: coordinator. Board: board-7.", "Coordinator: coordinator.")
        tracker = tracker.replace("- Re-arm:", "- Note: Board: bogus-url was last published at 09:00\n- Re-arm:")
        self.assertIsNone(rb.parse_tracker(tracker).board_url)

    def test_the_block_does_not_become_a_lane(self) -> None:
        self.assertEqual(len(rb.parse_tracker(TRACKER_RESUME).lanes), len(rb.parse_tracker(TRACKER).lanes))

    def test_page_shows_the_resume_strip(self) -> None:
        html = rb.render(TRACKER_RESUME, LOG, self.cfg, NOW)
        self.assertIn('class="resume"', html)
        self.assertIn("09:52", html)
        self.assertIn("TB17 diff review", html)

    def test_strip_is_absent_without_a_block(self) -> None:
        self.assertNotIn('class="resume"', rb.render(TRACKER, LOG, self.cfg, NOW))

    def test_strip_escapes_its_text(self) -> None:
        tracker = TRACKER_RESUME.replace("TB17 diff review", "TB17 <b>diff</b> review")
        html = rb.render(tracker, LOG, self.cfg, NOW)
        self.assertIn("&lt;b&gt;diff&lt;/b&gt;", html)
        self.assertNotIn("<b>diff</b>", html)

    def test_summary_counts_long_resume_lines(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        long_line = "- Next: " + "a merge order decision that will not fit on one line " * 6
        tracker = TRACKER_RESUME.replace("- Next: read 4821-audit's reply, then the deploy decision", long_line)
        (root / "daily" / "2026-09-16-tracker.md").write_text(tracker)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertIn("resume_long=1", buf.getvalue())

    def test_summary_reports_no_block(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(TRACKER)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertIn("resume=none", buf.getvalue())


if __name__ == "__main__":
    unittest.main()


class StateLegibilityTest(unittest.TestCase):
    """A state you cannot tell from another state is not shown.

    Live on 0.6.1: `open`, `no estimate` and `folded rows` all resolved to `var(--open)` — the only
    difference was a 135 degree stripe and an opacity drop, neither of which survives an 18x10px
    legend swatch. Each state now has a hue of its own. The patterns 0.6.2 layered on top were
    withdrawn in 0.9.1 (see `test_no_state_carries_a_pattern`).
    """

    # Every state the chart can draw, as the class that paints it.
    STATES = ("running", "open", "orphaned", "open-end", "derived", "summary")

    def setUp(self) -> None:
        self.html = rb.render(TRACKER, LOG, rb.parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW)
        self.css = self.html.split("<style>")[1].split("</style>")[0]

    def fill(self, selector: str) -> str:
        """The background declaration a selector sets, or "" when it sets none."""
        for line in self.css.splitlines():
            for rule in line.split("}"):
                if "{" in rule and selector in [c.strip() for c in rule.split("{")[0].split(",")]:
                    body = rule.split("{", 1)[1]
                    for decl in body.split(";"):
                        if decl.strip().startswith("background"):
                            return decl.strip()
        return ""

    def test_no_two_states_share_a_fill(self) -> None:
        fills = {}
        for state in self.STATES:
            selector = ".bar" if state == "open" else f".bar.{state}"
            got = self.fill(selector)
            self.assertTrue(got, f"{selector} sets no background of its own")
            self.assertNotIn(got, fills, f"{state} and {fills.get(got)} are drawn the same: {got}")
            fills[got] = state

    def test_no_state_carries_a_pattern(self) -> None:
        """Finding 51 gave every state a hatch or stripe on top of its hue. One day in the field: "visually
        made things worse" (Zach, 2026-09-18 21:12). Withdrawn in 0.9.1; every fill is flat, and this
        asserts the patterns cannot regrow one state at a time."""
        for state in self.STATES:
            selector = ".bar" if state == "open" else f".bar.{state}"
            rule = self.fill(selector)
            self.assertNotIn("gradient", rule, f"{state} is patterned again: {rule}")

    def test_legend_keys_are_drawn_like_the_bars_they_explain(self) -> None:
        for state in self.STATES:
            key = self.fill(f".key.bar.{state}") or self.fill(".key.bar")
            bar = self.fill(".bar" if state == "open" else f".bar.{state}")
            self.assertEqual(key, bar, f"the {state} legend key does not match its bar")

    def test_the_legend_never_shows_a_state_the_chart_cannot_draw(self) -> None:
        """`done` lanes are filtered out before any bar is built, so a `done` key explains nothing."""
        self.assertNotIn(("key bar done", "done"), rb.LEGEND)


class FoldedRowsExpandTest(unittest.TestCase):
    """45 of 58 lanes behind one strip, with nothing to open it. The names were in the html all along.

    0.6.2 opened the fold onto a list of names. Zach, 2026-09-18 12:55: "lane expansion must show sublanes,
    not a wierd text listing of lanes". The body is now the members drawn as rows on the same axis.
    """

    def setUp(self) -> None:
        self.html = rb.render(TRACKER, LOG, rb.parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW)

    def folded(self) -> str:
        m = re.search(r'<details class="folded">(.*?)</details>', self.html, re.S)
        self.assertIsNotNone(m, "no folded disclosure in the rendered board")
        return m.group(1)

    def test_a_folded_row_is_wrapped_in_a_disclosure(self) -> None:
        self.assertIn('<details class="folded">', self.html)

    def test_the_fold_opens_onto_one_positioned_bar_per_member(self) -> None:
        folded = self.folded()
        count = int(re.search(r'data-count="(\d+)"', folded).group(1))
        body = folded.split("</summary>", 1)[1]
        rows = re.findall(r'<div class="row sub"><div class="name">([^<]+)</div><div class="track"><div class="bar [^"]*"([^>]*)style="left:[0-9.]+%;width:[0-9.]+%">', body)
        self.assertEqual(len(rows), count, body)
        for _, attrs in rows:  # every citation is present, in whatever order the bar writes them
            for cite in ("data-item=", "data-start=", "data-end=", "data-start-src=", "data-end-src="):
                self.assertIn(cite, attrs, body)
        self.assertNotIn("folded-list", self.html)
        self.assertNotIn("<li", body)

    def test_the_member_citations_stay_on_the_summary_bar(self) -> None:
        summary = self.folded().split("</summary>", 1)[0]
        self.assertEqual(summary.count('class="member"'), int(re.search(r'data-count="(\d+)"', summary).group(1)))
        css = self.html.split("<style>")[1].split("</style>")[0]
        self.assertIn(".member{display:none}", css)

    def test_a_sub_row_carries_the_same_state_and_citations_as_a_top_row(self) -> None:
        body = self.folded().split("</summary>", 1)[1]
        sub = re.search(r'<div class="row sub">.*?<div class="bar ([^"]*)"[^>]*data-item="Review deploy config"[^>]*>', body)
        self.assertIsNotNone(sub, body)
        self.assertIn("open", sub.group(1).split())
        self.assertIn('data-end-src="due"', sub.group(0))

    def test_one_disclosure_per_folded_row_and_none_otherwise(self) -> None:
        folded = self.html.count('<details class="folded">')
        rows = self.html.count('class="row summary-row"') + self.html.count('class="row group-row"')
        self.assertEqual(folded, rows, "every folded row opens, and a board with none has no stray disclosure")


class SessionGraphTest(unittest.TestCase):
    """The coordinator's own knowledge, drawn. Until now the board said nothing at all about who is working.

    The argument for building it is the live tracker: four sessions whose `last reply` was eight hours
    old, and nothing anywhere saying so. A node that does not carry its own age is a claim about the
    present made from a memory of the morning.
    """

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, LOG, self.cfg, NOW)
        self.graph = re.search(r'<section class="graph".*?</section>', self.html, re.S)
        self.assertIsNotNone(self.graph, "no session graph in the rendered board")
        self.graph = self.graph.group(0)

    def node(self, ref: str) -> str:
        m = re.search(rf'<details class="node[^"]*" data-ref="{ref}".*?(?=<details class="node|</ul>\n</details>)', self.graph, re.S)
        self.assertIsNotNone(m, f"no node for {ref}")
        return m.group(0)

    def test_the_graph_closes_the_page_after_the_work(self) -> None:
        """Who is working used to open the page. The first screen answers what is due and what is
        blocked (Zach, 2026-09-19); a session that has stopped reporting is called out in BLOCKED,
        so the full roster is reference and reads last."""
        self.assertGreater(self.html.index('<section class="graph"'), self.html.index("<h2>Today</h2>"))
        self.assertGreater(self.html.index('<section class="graph"'), self.html.index("<h2>Lanes</h2>"))

    def test_every_session_is_a_node_citing_its_row(self) -> None:
        """`data-ref`/`data-state`/`data-as-of`, so a grader cites structure rather than prose."""
        self.assertEqual(self.graph.count('<details class="node'), len(rb.parse_tracker(TRACKER).sessions) + 1)
        for ref in ("a1b2c3", "d4e5f6", "9f2a41", "b7c8d9"):
            self.assertIn(f'data-ref="{ref}"', self.graph)
        self.assertIn('data-state="working"', self.graph)
        self.assertIn('data-as-of="11:40"', self.graph)

    def test_the_node_draws_the_derived_kind_and_not_the_written_cell(self) -> None:
        """A session cannot report that it is starting or ready; the coordinator works both out."""
        self.assertIn('data-state="starting"', self.graph)
        self.assertIn('data-state="ready"', self.graph)
        self.assertIn('data-state="unknown"', self.graph)

    def test_the_coordinator_is_the_root_and_every_session_hangs_from_it(self) -> None:
        root = re.search(r'<details class="node coordinator"[^>]*>(.*)</details>', self.graph, re.S)
        self.assertIsNotNone(root, "the graph has no coordinator root")
        for ref in ("a1b2c3", "d4e5f6", "9f2a41"):
            self.assertIn(f'data-ref="{ref}"', root.group(1))

    def test_children_hang_under_their_owner_and_are_never_nodes_of_their_own(self) -> None:
        """A subagent has no ref, answers no poll and cannot be sent to. A node implies it can be."""
        owner = self.node("a1b2c3")
        self.assertIn("rules-audit", owner)
        self.assertIn("fixture-sweep", owner)
        kids = re.search(r'<ul class="kids">(.*?)</ul>', owner, re.S)
        self.assertIsNotNone(kids, "worker-9a reported two subagents and the graph drew neither")
        self.assertNotIn("data-ref", kids.group(1))

    def test_the_waiting_edge_names_who_and_reads_as_an_arrow(self) -> None:
        f = self.node("d4e5f6")
        self.assertIn('data-waits-on="Robin"', f)
        self.assertIn("Robin", re.search(r"<summary>(.*?)</summary>", f, re.S).group(1))

    def test_a_session_waiting_on_nobody_draws_no_edge(self) -> None:
        self.assertNotIn("data-waits-on", self.node("a1b2c3"))

    def test_the_age_is_baked_into_the_html_not_left_to_javascript(self) -> None:
        """Design-inputs §50: the artifact CSP allows nothing external and this needs nothing."""
        self.assertIn("2h50m", self.graph)
        self.assertIn('data-age="170"', self.graph)

    def test_a_stamp_ages_through_three_named_bands(self) -> None:
        """State stays readable and visibly unreliable, rather than quietly becoming a lie."""
        for minutes, band in ((5, "fresh"), (45, "aging"), (170, "stale"), (476, "stale")):
            at = (NOW - timedelta(minutes=minutes)).strftime("%H:%M")
            s = rb.Session(ref="a1b2c3", name="n", state="working", doing="d", waiting_on="",
                           free_at="", constraints="", children="", last_reply=at)
            out = rb.session_graph((s,), self.cfg, NOW)
            self.assertIn(f'class="stamp {band}"', out, f"{minutes}m should read {band}")

    def test_a_reply_time_after_now_was_yesterday_not_the_future(self) -> None:
        """23:50 read at 14:30 is fifteen hours old, not nine hours from now."""
        s = rb.Session(ref="a1b2c3", name="n", state="working", doing="d", waiting_on="",
                       free_at="", constraints="", children="", last_reply="23:50")
        self.assertIn('data-age="880"', rb.session_graph((s,), self.cfg, NOW))

    def test_a_session_that_has_never_replied_says_so_rather_than_showing_an_age(self) -> None:
        s = rb.Session(ref="a1b2c3", name="n", state="planning", doing="d", waiting_on="",
                       free_at="", constraints="", children="", last_reply="")
        out = rb.session_graph((s,), self.cfg, NOW)
        self.assertIn("no reply yet", out)
        self.assertNotIn("data-age=", out)

    def test_the_tree_opens_with_css_and_attaches_no_handlers(self) -> None:
        self.assertNotIn("onclick", self.graph)
        script = self.html.split("<script>")[1]
        self.assertNotIn(".node", script)
        self.assertNotIn("addEventListener", script)

    def test_a_tracker_with_no_sessions_draws_no_graph(self) -> None:
        bare = TRACKER.split("## Sessions")[0] + "## File ownership\n\n## Log\n"
        self.assertNotIn('<section class="graph"', rb.render(bare, LOG, self.cfg, NOW))

    def test_a_node_escapes_the_text_it_carries(self) -> None:
        s = rb.Session(ref="a1b2c3", name="<script>x</script>", state="working", doing="&", waiting_on="",
                       free_at="", constraints="", children="", last_reply="14:00")
        out = rb.session_graph((s,), self.cfg, NOW)
        self.assertNotIn("<script>x", out)
        self.assertIn("&lt;script&gt;x", out)


class SessionStateLegibilityTest(unittest.TestCase):
    """The same rule the bars live under, applied to the chips: a state you cannot tell apart is not shown."""

    KINDS = ("planning", "working", "waiting", "idle", "starting", "ready", "gone", "unknown", "unreported")

    def setUp(self) -> None:
        self.html = rb.render(TRACKER, LOG, rb.parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW)
        self.css = self.html.split("<style>")[1].split("</style>")[0]

    def fill(self, selector: str) -> str:
        for line in self.css.splitlines():
            for rule in line.split("}"):
                if "{" in rule and selector in [c.strip() for c in rule.split("{")[0].split(",")]:
                    for decl in rule.split("{", 1)[1].split(";"):
                        if decl.strip().startswith("background"):
                            return decl.strip()
        return ""

    def test_no_two_session_states_share_a_fill(self) -> None:
        fills: dict[str, str] = {}
        for kind in self.KINDS:
            got = self.fill(f".chip.{kind}")
            self.assertTrue(got, f".chip.{kind} sets no background of its own")
            self.assertNotIn(got, fills, f"{kind} and {fills.get(got)} are drawn the same: {got}")
            fills[got] = kind

    def test_no_session_state_carries_a_pattern(self) -> None:
        """Same withdrawal as the bars (0.9.1): a chip is a flat hue and a word, never a hatch."""
        for kind in self.KINDS:
            rule = self.fill(f".chip.{kind}")
            self.assertNotIn("gradient", rule, f"{kind} is patterned again: {rule}")

    def test_the_chip_names_the_state_in_words_as_well(self) -> None:
        """A hue separates two states; only the word says which is which."""
        graph = re.search(r'<section class="graph".*?</section>', self.html, re.S).group(0)
        for kind in ("working", "waiting", "starting", "ready"):
            self.assertRegex(graph, rf'<span class="chip {kind}">{kind}</span>')


class ResumeLegibilityTest(unittest.TestCase):
    """Zach, reading the live board: "That's unintelligible. no formatting."

    Three faults under one complaint, and the one that was reported is the least of them.
    """

    LIVE = """- As of: 16:52 — gauntlet-bc [56e3fd]
- In flight: nothing, and nothing has changed since 13:40
- Next: goal #1 — owners for the three orphaned trees, then deploy main
- Waiting on: Zach — tree owners, redeploy, S2, CLAUDE.md:172, restart
- Re-arm: no Standing list; nothing handed without a fresh yes. Board watch dropped 16:51.
- Verified 16:52: main = origin/main 6621341, unmoved. Both services 200. Audit clean.
"""

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.tracker = TRACKER.replace("## Lanes", "## Resume\n\n" + self.LIVE + "\n## Lanes", 1)
        self.block = rb.parse_resume(self.tracker)
        self.html = rb.render(self.tracker, LOG, self.cfg, NOW)
        self.strip = re.search(r'<(dl|div) class="resume".*?</\1>', self.html, re.S)

    def test_a_clock_read_in_the_label_stays_out_of_the_key(self) -> None:
        """`- Verified 16:52:` split at the first colon, so the key was `verified 16` and nothing ever matched it."""
        self.assertIn("verified", self.block)
        self.assertNotIn("verified 16", self.block)
        self.assertIn("16:52", self.block["verified"])
        self.assertIn("origin/main 6621341", self.block["verified"])

    def test_every_parsed_field_reaches_the_board(self) -> None:
        """An allowlist of four field names silently dropped `Re-arm` the day the ruleset added it."""
        self.assertIsNotNone(self.strip, "no resume block in the rendered board")
        for key, value in self.block.items():
            self.assertIn(_first_words(value), self.strip.group(0), f"{key} is parsed, counted, and never drawn")

    def test_the_block_is_one_row_per_field_and_not_an_inline_run(self) -> None:
        """Six short lines joined with ` · ` are one 900-character paragraph, worst exactly when the day is busiest."""
        self.assertIsNotNone(self.strip)
        self.assertEqual(self.strip.group(1), "dl")
        self.assertEqual(self.strip.group(0).count("<dt"), len(self.block))
        self.assertEqual(self.strip.group(0).count("<dd"), len(self.block))

    def test_a_field_past_the_threshold_folds_rather_than_running_off(self) -> None:
        """The board already owns this idiom: `item_cell()` folds a long lane item the same way."""
        long_value = "a merge order decision that will not fit on one line " * 5
        tracker = self.tracker.replace("- In flight: nothing, and nothing has changed since 13:40",
                                       "- In flight: " + long_value)
        strip = re.search(r'<dl class="resume".*?</dl>', rb.render(tracker, LOG, self.cfg, NOW), re.S).group(0)
        fold = re.search(r"<details class=\"long\"><summary>(.*?)</summary>(.*?)</details>", strip, re.S)
        self.assertIsNotNone(fold, "a field over RESUME_LINE did not fold")
        self.assertLess(len(fold.group(1)), len(fold.group(2)))
        self.assertTrue(fold.group(1).rstrip().endswith("…"), "a clipped summary says it was clipped")

    def test_an_over_long_field_is_counted_as_a_warning_not_as_telemetry(self) -> None:
        """`resume_long=4` printed beside `warnings=0` for three hours and read as a lane count."""
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        long_line = "- Next: " + "a merge order decision that will not fit on one line " * 6
        tracker = self.tracker.replace("- Next: goal #1 — owners for the three orphaned trees, then deploy main", long_line)
        (root / "daily" / "2026-09-16-tracker.md").write_text(tracker)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        out = buf.getvalue()
        self.assertIn("resume_long=1", out)
        self.assertNotIn("warnings=0", out)

    def test_the_fields_keep_their_reading_order(self) -> None:
        """As of, then in flight, then next: where we are, then what is moving, then what is next."""
        strip = self.strip.group(0)
        order = [strip.index(f"<dt>{k}") for k in ("as of", "in flight", "next", "waiting on")]
        self.assertEqual(order, sorted(order))

    def test_the_strip_still_escapes_its_text(self) -> None:
        tracker = self.tracker.replace("tree owners", "<b>tree owners</b>")
        html = rb.render(tracker, LOG, self.cfg, NOW)
        self.assertIn("&lt;b&gt;tree owners&lt;/b&gt;", html)
        self.assertNotIn("<b>tree owners</b>", html)


def _first_words(value: str, n: int = 4) -> str:
    return html_escape(" ".join(value.split()[:n]), quote=True)


class ResumeCountTest(unittest.TestCase):
    """Finding 65, applied to itself: the number on stdout is the number of rows on the page.

    A field written with no value is parsed, counted, and not drawn — which is the same gap that hid
    `re-arm` for a day, one size smaller. The count that belongs on stdout is the one after the filter.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "CLAUDE.md").write_text(CLAUDE_MD)
        (self.root / "daily").mkdir()
        resume = "- As of: 09:00 — gauntlet-f0\n- In flight:\n- Next: the deploy decision\n"
        self.tracker = TRACKER.replace("## Lanes", "## Resume\n\n" + resume + "\n## Lanes", 1)
        (self.root / "daily" / "2026-09-16-tracker.md").write_text(self.tracker)

    def test_the_reported_count_is_what_was_drawn_and_not_what_was_parsed(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(self.root)])
        self.assertEqual(len(rb.parse_resume(self.tracker)), 3, "three fields are parsed")
        self.assertIn("resume=2 fields", buf.getvalue())

    def test_the_count_matches_the_rows_on_the_page(self) -> None:
        html = rb.render(self.tracker, LOG, rb.parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW)
        drawn = re.search(r'<dl class="resume">.*?</dl>', html, re.S).group(0).count("<dt>")
        self.assertEqual(drawn, len(rb.resume_fields(rb.parse_resume(self.tracker))))


class GoneSessionTest(unittest.TestCase):
    """The join, in the renderer. A session cannot report that it is gone, so the coordinator works it out.

    `orphaned` is a lane state meaning the owner left. A session still listed in `## Sessions` whose
    lane is orphaned is a row the registry has not caught up with, and drawing it as `working` on the
    strength of its own last reply is the board repeating a claim its other column already contradicts.
    """

    LANES = ("| Ghost work | wanderer | orphaned | 09:00 |  | Checklist: Ghost work |\n"
             "| Live work | worker-9a | running 10:30 |  |  | Checklist: Live work |")

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.tracker = f"""# Tracker 2026-09-16

Coordinator: coordinator. Board: board-7.

## Lanes

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
{self.LANES}

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| a1b2c3 | worker-9a | working | a lane | | 15:00 | | none | 11:40 |
| e5f6a7 | wanderer | working | a lane | | 15:00 | | none | 11:40 |

## File ownership

## Log

- 09:00 opened the day
"""
        parsed = rb.parse_tracker(self.tracker)
        self.gone = rb.gone_sessions(parsed.lanes, parsed.sessions)

    def test_a_session_whose_lane_is_orphaned_is_gone(self) -> None:
        self.assertIn("wanderer", self.gone)

    def test_a_session_whose_lane_is_running_is_not(self) -> None:
        self.assertNotIn("worker-9a", self.gone)

    def test_the_ref_a_listing_shows_does_not_defeat_the_join(self) -> None:
        lanes = (rb.Lane("Ghost work", "wanderer [e5f6a7]", "orphaned", "", "", ""),)
        s = (rb.Session(ref="e5f6a7", name="wanderer", state="working", doing="", waiting_on="",
                        free_at="", constraints="", children="", last_reply="11:40"),)
        self.assertIn("wanderer", rb.gone_sessions(lanes, s))

    def test_the_graph_draws_gone_over_what_the_session_reported(self) -> None:
        out = rb.session_graph(rb.parse_tracker(self.tracker).sessions, self.cfg, NOW, gone=self.gone)
        self.assertIn('data-ref="e5f6a7" data-state="gone"', out)
        self.assertIn('data-ref="a1b2c3" data-state="working"', out)

    def test_gone_has_a_fill_of_its_own(self) -> None:
        html = rb.render(self.tracker, LOG, self.cfg, NOW)
        css = html.split("<style>")[1].split("</style>")[0]
        self.assertIn(".chip.gone{", css)


QUEUE_TRACKER = """# Tracker 2026-09-16

## Lanes

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| A oldest | worker | open | 2026-09-15 |  | c |
| B running | worker [ab12cd] | running 10:00 | 2026-09-16 |  | c |
| C newer | worker (was ab12cd) | open | 2026-09-16 |  | c |
| D dated | worker | open | 2026-09-16 | 17:00 | c |
| E unassigned | unassigned | open | 2026-09-16 |  | c |
| F blank |  | open | 2026-09-16 |  | c |
| G orphaned | left-session | orphaned 09:05 | 2026-09-15 |  | c |
| H left behind | left-session | open | 2026-09-16 |  | c |
| I dash | — | open | 2026-09-16 |  | c |
| J beyond | worker | open | 2026-09-16 | Final | c |
| K done | worker | done 11:00 | 2026-09-16 |  | c |

## Log

- 09:00 opened the day
"""


class EstimateTest(unittest.TestCase):
    """An owned lane with no `due` ends where its owner's queue puts it, and cites `derived`.

    The tracker's clock is day-grained (`since` and `due` are dates; only `done` carries a time), so no
    lane has a duration to learn from. The estimator therefore claims nothing about effort: lane i of
    an owner's N ends at `H - (N-i)/N x (H - now)`, where H is the nearest deadline still ahead. The
    one fabricated input is the order, and it is stated: running first, then oldest first.
    """

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.lanes = [lane for lane in rb.parse_tracker(QUEUE_TRACKER).lanes if lane.kind != "done"]
        self.by = {lane.item: lane for lane in self.lanes}
        self.est = rb.estimates(self.lanes, self.cfg, NOW)
        self.h = self.cfg.deadlines[0].at  # Launch 23:59, the nearest deadline ahead of 14:30
        self.r = self.h - NOW

    def test_the_queue_is_running_first_then_oldest_and_the_dated_lane_counts(self) -> None:
        # worker: B (running), A (since 09-15), C (09-16), D (09-16, due 17:00 <= H) -> N=4; J's due is past H.
        self.assertEqual({k for k in self.est if self.by[k].owner.startswith("worker")}, {"A oldest", "B running", "C newer"})
        self.assertEqual((self.est["B running"].i, self.est["B running"].n), (1, 4))
        self.assertEqual((self.est["A oldest"].i, self.est["A oldest"].n), (2, 4))
        self.assertEqual((self.est["C newer"].i, self.est["C newer"].n), (3, 4))
        self.assertEqual(self.est["B running"].end, self.h - self.r * (3 / 4))
        self.assertEqual(self.est["A oldest"].end, self.h - self.r * (2 / 4))
        self.assertEqual(self.est["C newer"].end, self.h - self.r * (1 / 4))
        self.assertEqual(self.est["A oldest"].label, "est. 2/4 → Launch")

    def test_a_ref_and_a_parenthetical_are_one_owner(self) -> None:
        self.assertEqual({self.est[k].n for k in ("A oldest", "B running", "C newer")}, {4})

    def test_the_last_lane_ends_at_the_deadline_instant(self) -> None:
        lanes = [self.by["A oldest"], self.by["C newer"]]
        est = rb.estimates(lanes, self.cfg, NOW)
        self.assertEqual((est["A oldest"].i, est["C newer"].i), (1, 2))
        self.assertEqual(est["C newer"].end, self.h)
        self.assertEqual(est["A oldest"].end, NOW + self.r / 2)

    def test_unowned_gone_and_dated_lanes_get_no_estimate(self) -> None:
        for item in ("D dated", "E unassigned", "F blank", "G orphaned", "H left behind", "I dash", "J beyond"):
            self.assertNotIn(item, self.est, item)

    def test_a_derived_end_reaches_the_bar_only_when_asked_for(self) -> None:
        lane = self.by["A oldest"]
        plain = rb.day_bar(lane, self.cfg, NOW)
        self.assertEqual((plain.end_src, plain.label), ("deadline", "no estimate"))
        b = rb.day_bar(lane, self.cfg, NOW, self.est)
        self.assertEqual((b.end_src, b.label, b.end), ("derived", "est. 2/4 → Launch", self.est["A oldest"].end))
        w = rb.week_bar(lane, self.cfg, NOW, self.est)
        self.assertEqual((w.end_src, w.end), ("derived", self.est["A oldest"].end))
        d = rb.day_bar(self.by["D dated"], self.cfg, NOW, self.est)
        self.assertEqual(d.end_src, "due")
        u = rb.day_bar(self.by["E unassigned"], self.cfg, NOW, self.est)
        self.assertEqual((u.end_src, u.label), ("deadline", "no estimate"))

    def test_a_passed_deadline_makes_the_horizon_midnight(self) -> None:
        late = datetime(2026, 9, 21, 9, 0, tzinfo=CT)
        est = rb.estimates([self.by["A oldest"]], self.cfg, late)
        self.assertEqual(est["A oldest"].end, datetime(2026, 9, 22, 0, 0, tzinfo=CT))
        self.assertEqual(est["A oldest"].label, "est. 1/1 → end of day")

    def test_a_since_after_now_never_ends_before_it_starts(self) -> None:
        text = QUEUE_TRACKER.replace("| A oldest | worker | open | 2026-09-15 |", "| A oldest | worker | open | 2026-09-17 |")
        lanes = [lane for lane in rb.parse_tracker(text).lanes if lane.item == "A oldest"]
        w = rb.week_bar(lanes[0], self.cfg, NOW, rb.estimates(lanes, self.cfg, NOW))
        self.assertEqual(w.end_src, "derived")
        self.assertGreaterEqual(w.end, w.start)

    def test_item_text_is_never_read(self) -> None:
        text = QUEUE_TRACKER.replace("| A oldest |", "| A oldest, a very long item whose length says nothing about the work left (history: 09:00 opened; 10:00 blocked; 11:00 resumed; 12:00 more of the same) |")
        lanes = [lane for lane in rb.parse_tracker(text).lanes if lane.kind != "done"]
        other = rb.estimates(lanes, self.cfg, NOW)
        self.assertEqual({e.end for e in other.values()}, {e.end for e in self.est.values()})


class DrawnEstimateTest(unittest.TestCase):
    """A derived end is drawn as its own state, cites its inputs, and folds when its slot is too narrow to read."""

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(QUEUE_TRACKER, LOG, self.cfg, NOW)
        self.day = _strip_html(self.html, "day")

    def bar(self, item: str, html: str | None = None) -> str:
        m = re.search(rf'<div class="bar[^"]*"[^>]*data-item="{re.escape(item)}"[^>]*>', html or self.day)
        self.assertIsNotNone(m, item)
        return m.group(0)

    def test_the_bar_is_its_own_state_and_cites_its_inputs(self) -> None:
        a = self.bar("A oldest")
        self.assertIn('class="bar open derived"', a)
        self.assertIn('data-end-src="derived"', a)
        self.assertIn('data-est="2/4"', a)
        self.assertIn('data-est-of="Launch"', a)
        self.assertIn('data-label="est. 2/4 → Launch"', a)
        self.assertRegex(a, r'title="[^"]*queue[^"]*"')
        self.assertIn('class="bar running derived"', self.bar("B running"))

    def test_an_unowned_lane_still_says_no_estimate(self) -> None:
        for item in ("E unassigned", "F blank", "H left behind"):
            self.assertRegex(self.day, rf'class="member" data-item="{re.escape(item)}"[^>]*data-end-src="deadline"[^>]*data-label="no estimate"')

    def test_a_slot_narrower_than_a_tick_folds(self) -> None:
        rows = "".join(f"| L{i} | many | open | 2026-09-16 |  | c |\n" for i in range(12))
        text = QUEUE_TRACKER.replace("| K done |", rows + "| K done |")
        day = _strip_html(rb.render(text, LOG, self.cfg, NOW), "day")
        self.assertNotRegex(day, _top_row("L3"))
        member = re.search(r'<span class="member" data-item="L3"[^>]*>', day)
        self.assertIsNotNone(member)
        self.assertIn('data-end-src="derived"', member.group(0))
        self.assertIn('data-est="4/12"', member.group(0))
        self.assertIn('class="bar open derived"', self.bar("A oldest"))

    def test_the_legend_key_matches_the_bar(self) -> None:
        css = self.html.split("<style>")[1].split("</style>")[0]
        self.assertIn(".bar.derived{background:var(--est)}", css)
        self.assertIn(".key.bar.derived{background:var(--est)}", css)
        self.assertRegex(css, r"--est:#[0-9a-f]{6}")

    def test_the_week_routes_a_derived_end_by_day_and_counts_it(self) -> None:
        text = QUEUE_TRACKER.replace("| K done |", "| P | w | open | 2026-09-16 |  | c |\n| Q | w | open | 2026-09-16 |  | c |\n| R | w | open | 2026-09-16 | 17:00 | c |\n| K done |")
        week = _strip_html(rb.render(text, LOG, self.cfg, NOW), "week")
        row = re.search(r'data-group="2026-09-16" data-count="(\d+)"(.*?)</div></div></div>', week)
        self.assertIsNotNone(row)
        members = re.findall(r'class="member" data-item="([^"]+)"', row.group(2))
        self.assertTrue({"P", "Q", "R", "D dated"} <= set(members), members)
        self.assertIn("2 due · 4 est.", row.group(2))
        self.assertNotRegex(week, r'class="member" data-item="P"[^>]*data-end-src="deadline"')

    def test_the_cli_counts_derived_beside_no_estimate(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(QUEUE_TRACKER)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertIn("no_estimate=5 derived=3", buf.getvalue())


SIZED_TRACKER = """# Tracker 2026-09-16

## Lanes

| item | owner | state | since | due | size | checklist |
|---|---|---|---|---|---|---|
| A oldest | worker | open | 2026-09-15 |  | M | c |
| B running | worker [ab12cd] | running 13:00 | 2026-09-16 |  | S | c |
| C newer | worker | open | 2026-09-16 |  |  | c |
| D dated | worker | open | 2026-09-16 | 17:00 | L | c |
| E unassigned | unassigned | open | 2026-09-16 |  | M | c |
| K1 done | worker | done 09:00–10:00 | 2026-09-16 |  | M | c |
| K2 done | worker | done 10:00-12:00 | 2026-09-16 |  | M | c |
| K3 done | other | done 11:00–11:30 | 2026-09-16 |  | S | c |
| K4 done | other | done 08:00–09:00 | 2026-09-16 |  |  | c |
| K5 no range | worker | done 11:00 | 2026-09-16 |  | M | c |
| K6 backwards | worker | done 23:00–01:00 | 2026-09-16 |  | XL | c |

## Log

- 09:00 opened the day
"""


class SizeGrammarTest(unittest.TestCase):
    """The Lanes table may carry a `size` column, and `done` may keep the running start as a range.

    Finding 77: the estimator's second model needs two things the tracker never recorded — how big a
    lane is and how long lanes of that size took. The size is the coordinator's judgement in a column;
    the duration is the `running HH:MM` start kept on close, `done 21:16–22:05`, which `done 22:05`
    alone threw away (finding 78).
    """

    def setUp(self) -> None:
        self.t = rb.parse_tracker(SIZED_TRACKER)
        self.by = {lane.item: lane for lane in self.t.lanes}

    def test_seven_columns_parse_and_the_size_is_read_by_header(self) -> None:
        self.assertEqual(self.by["A oldest"].size, "M")
        self.assertEqual(self.by["C newer"].size, "")
        self.assertEqual(self.by["A oldest"].checklist, "c")
        self.assertEqual([lane.warning for lane in self.t.lanes], [""] * len(self.t.lanes))

    def test_six_columns_still_parse_unsized_without_a_warning(self) -> None:
        t = rb.parse_tracker(QUEUE_TRACKER)
        self.assertEqual({lane.size for lane in t.lanes}, {""})
        self.assertEqual([lane.warning for lane in t.lanes], [""] * len(t.lanes))
        self.assertEqual(t.lanes[0].checklist, "c")

    def test_a_row_matching_neither_width_still_warns(self) -> None:
        text = SIZED_TRACKER.replace("| C newer | worker | open | 2026-09-16 |  |  | c |", "| C newer | worker | open |")
        lane = {l.item: l for l in rb.parse_tracker(text).lanes}["C newer"]
        self.assertIn("3 cells", lane.warning)
        self.assertIn("expected 7", lane.warning)

    def test_a_done_range_keeps_the_end_as_state_time_and_exposes_the_start(self) -> None:
        self.assertEqual(self.by["K1 done"].state_time, "10:00")
        self.assertEqual(self.by["K1 done"].ran, ("09:00", "10:00"))
        self.assertEqual(self.by["K2 done"].ran, ("10:00", "12:00"))
        self.assertIsNone(self.by["K5 no range"].ran)
        self.assertEqual(self.by["K5 no range"].state_time, "11:00")
        self.assertEqual(self.by["B running"].state_time, "13:00")
        self.assertIsNone(self.by["B running"].ran)

    def test_a_done_range_draws_the_bar_as_before(self) -> None:
        cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        b = rb.day_bar(self.by["K1 done"], cfg, NOW)
        self.assertEqual((b.end, b.end_src), (datetime(2026, 9, 16, 10, 0, tzinfo=CT), "state"))

    def test_history_means_per_size_with_an_all_sizes_row(self) -> None:
        cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        h = rb.history(self.t.lanes, cfg, NOW)
        self.assertEqual(h["M"], (timedelta(hours=1, minutes=30), 2))
        self.assertEqual(h["S"], (timedelta(minutes=30), 1))
        self.assertEqual(h[""], (timedelta(hours=1), 1))
        self.assertEqual(h[rb.ALL_SIZES], (timedelta(minutes=67, seconds=30), 4))  # 60+120+30+60 over four
        self.assertNotIn("XL", h)  # K6 ran backwards across midnight and is dropped
        self.assertNotIn("L", h)

    def test_no_range_anywhere_means_no_history(self) -> None:
        cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.assertEqual(rb.history(rb.parse_tracker(QUEUE_TRACKER).lanes, cfg, NOW), {})


class SizedEstimateTest(unittest.TestCase):
    """With history, a lane ends where its owner's queue puts it times what lanes of its size have taken.

    Zach, 2026-09-18 23:07: "infer a t-shirt size ... generate an average amount of time spent per t-shirt
    sized puzzle ... use that." A cursor starts at now; a running lane ends at its own start plus the mean
    for its size, never before now; every other lane ends at the cursor plus its mean; a lane with a due
    advances the cursor and keeps its due. Only blank-due owned lanes get a derived end.
    """

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.lanes = list(rb.parse_tracker(SIZED_TRACKER).lanes)
        self.active = [lane for lane in self.lanes if lane.kind != "done"]
        self.by = {lane.item: lane for lane in self.lanes}
        self.hist = rb.history(self.lanes, self.cfg, NOW)
        self.est = rb.estimates(self.active, self.cfg, NOW, self.hist)
        self.m, self.s, self.unsized = timedelta(minutes=90), timedelta(minutes=30), timedelta(minutes=67, seconds=30)

    def test_ends_chain_from_now_by_size_means(self) -> None:
        # worker: B running (S, started 13:00), A oldest (M), C newer (unsized -> all-sizes mean), D dated (L, due 17:00, no L history -> all-sizes mean).
        b = self.est["B running"]
        self.assertEqual(b.end, NOW)  # 13:00 + 30m = 13:30 is already past 14:30; never before now
        self.assertEqual(self.est["A oldest"].end, NOW + self.m)
        self.assertEqual(self.est["C newer"].end, NOW + self.m + self.unsized)
        self.assertNotIn("D dated", self.est)

    def test_labels_name_the_size_and_the_mean(self) -> None:
        self.assertEqual(self.est["A oldest"].label, "est. M ~1h30m")
        self.assertEqual(self.est["B running"].label, "est. S ~30m")
        self.assertEqual(self.est["C newer"].label, "est. ? ~1h07m")
        self.assertEqual({e.basis for e in self.est.values()}, {"history"})
        self.assertEqual((self.est["A oldest"].size, self.est["A oldest"].i, self.est["A oldest"].n), ("M", 2, 4))

    def test_the_title_says_what_the_mean_rests_on(self) -> None:
        t = self.est["A oldest"].title("worker")
        self.assertIn("size M", t)
        self.assertIn("2 M lanes", t)
        self.assertIn("2nd of 4", t)
        self.assertIn("worker", t)
        t = self.est["C newer"].title("worker")
        self.assertIn("unsized", t)
        self.assertIn("all 4", t)

    def test_a_size_with_no_history_borrows_the_all_sizes_mean(self) -> None:
        text = SIZED_TRACKER.replace("| C newer | worker | open | 2026-09-16 |  |  | c |", "| C newer | worker | open | 2026-09-16 |  | XL | c |")
        lanes = list(rb.parse_tracker(text).lanes)
        est = rb.estimates([l for l in lanes if l.kind != "done"], self.cfg, NOW, rb.history(lanes, self.cfg, NOW))
        self.assertEqual(est["C newer"].end, NOW + self.m + self.unsized)
        self.assertEqual(est["C newer"].label, "est. XL ~1h07m")
        self.assertIn("no XL lane has closed with a range", est["C newer"].title("worker"))

    def test_unowned_lanes_get_nothing_even_when_sized(self) -> None:
        self.assertNotIn("E unassigned", self.est)

    def test_no_history_falls_back_to_the_queue_drain(self) -> None:
        est = rb.estimates(self.active, self.cfg, NOW, {})
        self.assertEqual(est["A oldest"].label, "est. 2/4 → Launch")
        self.assertEqual(est["A oldest"].basis, "queue")
        self.assertEqual(rb.estimates(self.active, self.cfg, NOW), est)


class DrawnSizedEstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(SIZED_TRACKER, LOG, self.cfg, NOW)
        self.day = _strip_html(self.html, "day")

    def bar(self, item: str) -> str:
        m = re.search(rf'<div class="bar[^"]*"[^>]*data-item="{re.escape(item)}"[^>]*>', self.day)
        self.assertIsNotNone(m, item)
        return m.group(0)

    def test_the_bar_cites_size_basis_and_the_measured_label(self) -> None:
        a = self.bar("A oldest")
        self.assertIn('class="bar open derived"', a)
        self.assertIn('data-size="M"', a)
        self.assertIn('data-est-basis="history"', a)
        self.assertIn('data-est="2/4"', a)
        self.assertIn('data-label="est. M ~1h30m"', a)
        self.assertRegex(a, r'title="[^"]*2 M lanes[^"]*"')

    def test_the_queue_fallback_says_so_on_the_bar(self) -> None:
        day = _strip_html(rb.render(QUEUE_TRACKER, LOG, self.cfg, NOW), "day")
        m = re.search(r'<div class="bar[^"]*"[^>]*data-item="A oldest"[^>]*>', day)
        self.assertIn('data-est-basis="queue"', m.group(0))
        self.assertIn('data-est-of="Launch"', m.group(0))
        self.assertNotIn("data-size", m.group(0))

    def test_an_unowned_sized_lane_says_no_estimate_and_its_size(self) -> None:
        self.assertRegex(self.day, r'data-item="E unassigned"[^>]*data-size="M"[^>]*data-label="no estimate"|data-item="E unassigned"[^>]*data-label="no estimate"[^>]*data-size="M"')

    def test_the_lanes_table_has_a_size_column(self) -> None:
        lanes = self.html.split("<h2>Lanes</h2>")[1]
        self.assertIn("<th>item</th><th>owner</th><th>state</th><th>since</th><th>due</th><th>size</th>", lanes)
        self.assertRegex(lanes, r'<tr data-state="open"[^>]*data-size="M">.*?<td>M</td>')

    def test_the_cli_reports_history_per_size(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(SIZED_TRACKER)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--root", str(root), "--date", "2026-09-16"])
        self.assertRegex(buf.getvalue(), r"history=S:1 M:2 \?:1 · all:4")
        (root / "daily" / "2026-09-16-tracker.md").write_text(QUEUE_TRACKER)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--root", str(root), "--date", "2026-09-16"])
        self.assertIn("history=none", buf.getvalue())


CLAUDE_MD_EXAM = CLAUDE_MD.replace("  - Final: 2026-09-20 12:00", "  - Final: 2026-09-20 12:00\n  - PCCAT 1st attempt: 2026-09-26; named lanes only")
CLAUDE_MD_EXAM_GOVERNS = CLAUDE_MD.replace("  - Final: 2026-09-20 12:00", "  - Final: 2026-09-20 12:00\n  - PCCAT 1st attempt: 2026-09-26")
SUNDAY = datetime(2026, 9, 20, 12, 1, tzinfo=CT)
SUNDAY_TRACKER = """# Tracker 2026-09-20

## Lanes

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Leftover one | worker | open | 2026-09-19 |  | c |
| Leftover two | worker | open | 2026-09-20 |  | c |
| Nobody's | unassigned | open | 2026-09-20 |  | c |
| Study | Zach | open | 2026-09-20 | PCCAT 1st attempt | c |

## Log

- 09:00 opened the day
"""


class DeadlineScopeTest(unittest.TestCase):
    """Finding 73: after Final, the nearest deadline is an exam, and nothing on the Lanes table is due to it.

    A deadline line may say `named lanes only`: lanes whose `due` names it still draw to it, and no lane
    falls to it by default. The estimator's horizon, the unowned lane's open end and the week strip's
    fold use the nearest deadline that governs unnamed lanes; the Clock line and the axes keep the
    nearest deadline of all, because the exam is real.
    """

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD_EXAM, today=SUNDAY.date())
        self.governs = rb.parse_coordinator(CLAUDE_MD_EXAM_GOVERNS, today=SUNDAY.date())
        self.lanes = list(rb.parse_tracker(SUNDAY_TRACKER).lanes)
        self.by = {lane.item: lane for lane in self.lanes}

    def test_the_option_parses_and_defaults_to_all(self) -> None:
        exam = next(d for d in self.cfg.deadlines if d.name.startswith("PCCAT"))
        self.assertEqual(exam.scope, "named")
        self.assertEqual({d.scope for d in self.cfg.deadlines if d.name != exam.name}, {"all"})
        self.assertEqual({d.scope for d in self.governs.deadlines}, {"all"})
        both = rb.parse_coordinator(CLAUDE_MD.replace("  - Final: 2026-09-20 12:00", "  - Final: 2026-09-20 12:00; named lanes only; requirements `reqs.md`"), today=SUNDAY.date())
        final = next(d for d in both.deadlines if d.name == "Final")
        self.assertEqual((final.scope, final.requirements), ("named", "reqs.md"))

    def test_governing_deadline_skips_a_named_only_one(self) -> None:
        self.assertEqual(rb.governing_deadline(self.cfg, SUNDAY).name, "end of day")
        self.assertEqual(rb.governing_deadline(self.cfg, SUNDAY).at, datetime(2026, 9, 20, 23, 59, tzinfo=CT))
        self.assertEqual(rb.governing_deadline(self.governs, SUNDAY).name, "PCCAT 1st attempt")
        before = datetime(2026, 9, 20, 11, 0, tzinfo=CT)
        self.assertEqual(rb.governing_deadline(self.cfg, before).name, "Final")
        self.assertEqual(rb.nearest_deadline(self.cfg, SUNDAY).name, "PCCAT 1st attempt")

    def test_the_horizon_and_the_estimates_stop_at_end_of_day(self) -> None:
        self.assertEqual(rb.horizon(self.cfg, SUNDAY), (datetime(2026, 9, 21, 0, 0, tzinfo=CT), "end of day"))
        est = rb.estimates(self.lanes, self.cfg, SUNDAY)
        self.assertEqual({e.of for e in est.values()}, {"end of day"})
        self.assertEqual(est["Leftover one"].label, "est. 1/2 → end of day")
        self.assertEqual({e.of for e in rb.estimates(self.lanes, self.governs, SUNDAY).values()}, {"PCCAT 1st attempt"})

    def test_an_unowned_lane_ends_today_not_at_the_exam(self) -> None:
        b = rb.day_bar(self.by["Nobody's"], self.cfg, SUNDAY)
        self.assertEqual((b.end, b.end_src, b.label), (datetime(2026, 9, 20, 23, 59, tzinfo=CT), "deadline", "no estimate"))
        self.assertEqual(rb.day_bar(self.by["Nobody's"], self.governs, SUNDAY).end, datetime(2026, 9, 26, 23, 59, tzinfo=CT))

    def test_a_lane_that_names_the_exam_still_draws_to_it(self) -> None:
        b = rb.day_bar(self.by["Study"], self.cfg, SUNDAY)
        self.assertEqual((b.end, b.end_src), (datetime(2026, 9, 26, 23, 59, tzinfo=CT), "due"))
        self.assertEqual(rb._deadline_for(self.by["Study"], b, self.cfg, SUNDAY).name, "PCCAT 1st attempt")
        nobody = rb.day_bar(self.by["Nobody's"], self.cfg, SUNDAY)
        self.assertEqual(rb._deadline_for(self.by["Nobody's"], nobody, self.cfg, SUNDAY).name, "end of day")

    def test_the_page_carries_no_exam_estimate_and_the_week_folds_under_end_of_day(self) -> None:
        html = rb.render(SUNDAY_TRACKER, LOG, self.cfg, SUNDAY)
        self.assertNotIn('data-est-of="PCCAT 1st attempt"', html)
        self.assertIn('data-deadline-name="PCCAT 1st attempt"', html)  # the Clock line and the axes keep the real deadline
        week = _strip_html(html, "week")
        self.assertRegex(week, r'data-item="Nobody&#x27;s"[^>]*data-end-src="deadline"')
        self.assertNotRegex(week, r'data-group="PCCAT 1st attempt"[^>]*>(?:(?!</details>).)*data-item="Nobody&#x27;s"')
        governs = rb.render(SUNDAY_TRACKER, LOG, self.governs, SUNDAY)
        self.assertIn('data-est-of="PCCAT 1st attempt"', governs)

    def test_the_cli_names_the_horizon(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD_EXAM)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(QUEUE_TRACKER)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--root", str(root), "--date", "2026-09-16"])
        # main() renders at the real clock, so the name is whichever governing deadline is next — never the exam.
        m = re.search(r"horizon=(Launch|Final|end of day) ", buf.getvalue())
        self.assertIsNotNone(m, buf.getvalue())


# --- the tracer bullet ----------------------------------------------------------------------------
# One lane threaded through every layer: written with a `name`, parsed, labelled by that name,
# placed on a rolling 24h axis, inside a deadline swimlane under its owner, on a reordered page.

TRACKER_NAMED = """# Tracker 2026-09-16

Coordinator: coordinator. Board: board-7.

## Lanes

| name | item | owner | state | since | due | size | checklist |
|---|---|---|---|---|---|---|---|
| Cut the release branch | **Cut the release branch.** 23:40: handed to impl-2 with the checklist; 00:12: branch cut, suite green on the branch | impl-2 | running 10:30 | 10:30 | 23:00 | M | Checklist: cut |
|  | Write eval README | Robin | open | 09:00 | 17:00 |  | Checklist: readme |

## Sessions

## Log

- 09:00 opened the day
"""


MARKS_TRACKER = """# Tracker 2026-09-16

## Resume

- As of: 09:52 — gauntlet-f0 [308833]
- Verified 09:47: origin/main `d6aab3b`, 2 unpushed; agent /ready 200

## Lanes

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| **Deploy main.** Deployed tips are agent `39e516f`, 30 commits behind. **22:20: handed to research-1 on Robin's yes** — sizing and wiring | Robin | open | 2026-09-16 |  | c |
| **Session TTL fix, orphaned.** `session.py` reads 3600 where §10.2 says 1800 | unassigned | open | 2026-09-16 |  | c |

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| a1b2c3 | worker-9a | working | **left the registry by 20:09** — both lanes `done` | | 15:00 | TDD | none | 11:40 |

## Log

- 09:00 opened the day
"""


class TracerTest(unittest.TestCase):
    """The one lane, end to end. Every assertion here is a layer of the design."""

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.tracker = rb.parse_tracker(TRACKER_NAMED)
        self.html = rb.render(TRACKER_NAMED, LOG, self.cfg, NOW)

    def test_the_name_column_parses_and_an_unnamed_row_falls_back(self) -> None:
        named, unnamed = self.tracker.lanes
        self.assertEqual(named.name, "Cut the release branch")
        self.assertEqual(named.label, "Cut the release branch")
        self.assertEqual(named.owner, "impl-2")
        self.assertEqual(named.size, "M")
        self.assertEqual(named.warning, "")
        # No name written: the label is what short_name() has always produced.
        self.assertEqual(unnamed.name, "")
        self.assertEqual(unnamed.label, rb.short_name(unnamed.item))

    def test_the_six_and_seven_column_trackers_still_parse(self) -> None:
        """100 live rows are not rewritten in one go: all three header widths keep working."""
        old = rb.parse_tracker(TRACKER)
        self.assertEqual(old.lanes[0].label, "Write eval README")
        self.assertEqual(old.lanes[0].name, "")
        sized = rb.parse_tracker(TRACKER.replace(
            "| item | owner | state | since | due | checklist |",
            "| item | owner | state | since | due | size | checklist |"))
        self.assertEqual(sized.lanes[0].name, "")

    def test_the_bar_is_labelled_by_the_name_not_by_the_prose(self) -> None:
        # `data-item` keeps its meaning: the whole item cell, the bar's provenance. The lane's
        # identity is the written name, and it gets its own attribute rather than overloading that one.
        bar = re.search(r'<[^>]*\bbar\b[^>]*data-name="Cut the release branch"[^>]*>', self.html)
        self.assertIsNotNone(bar, "the bar cites the lane by its written name")
        self.assertIn("handed to impl-2", bar.group(0), "and still carries the item cell as provenance")
        # The point of the name column: 50 live lanes open with a bold headline and render its marks.
        # `data-item` keeps the cell verbatim; no asterisk survives into anything a person reads.
        visible = re.sub(r"<[^>]+>", "", self.html.split("</style>", 1)[1])
        self.assertNotIn("**", visible)
        self.assertIn("Cut the release branch", visible)
        strip = re.sub(r"<[^>]+>", "", _strip_html(self.html, "day"))
        self.assertNotIn("handed to impl-2", strip, "the strip draws the name, never the history")
        self.assertIn("Cut the release branch", _strip_html(self.html, "day"))

    def test_the_day_axis_is_a_rolling_twenty_four_hours(self) -> None:
        day = _strip_html(self.html, "day")
        a = datetime.fromisoformat(re.search(r'data-axis-start="([^"]+)"', day).group(1))
        b = datetime.fromisoformat(re.search(r'data-axis-end="([^"]+)"', day).group(1))
        self.assertEqual(b - a, timedelta(hours=24))
        self.assertLessEqual(a, NOW)
        self.assertGreaterEqual(b, NOW + timedelta(hours=23))

    def test_late_at_night_the_axis_is_still_twenty_four_hours(self) -> None:
        """The bug this replaces: the axis end clamped to midnight, so at 23:00 only one hour of
        the strip lay ahead of now however much work was scheduled past it."""
        late = datetime(2026, 9, 16, 23, 0, tzinfo=CT)
        day = _strip_html(rb.render(TRACKER_NAMED, LOG, self.cfg, late), "day")
        a = datetime.fromisoformat(re.search(r'data-axis-start="([^"]+)"', day).group(1))
        b = datetime.fromisoformat(re.search(r'data-axis-end="([^"]+)"', day).group(1))
        self.assertEqual(b - a, timedelta(hours=24))

    def test_the_bar_sits_under_a_deadline_swimlane_and_names_its_owner(self) -> None:
        day = _strip_html(self.html, "day")
        self.assertRegex(day, r'data-swimlane="Launch"', "a group row per governing deadline")
        bar = re.search(r'<[^>]*\bbar\b[^>]*data-name="Cut the release branch"[^>]*>', day).group(0)
        self.assertIn('data-owner="impl-2"', bar)
        self.assertLess(day.index('data-swimlane="Launch"'), day.index('data-name="Cut the release branch"'))

    def test_the_blocked_band_renders_its_stamps_rather_than_printing_their_markup(self) -> None:
        """`_stamp` hands back html. Escaping it put `&lt;span class=&quot;stamp stale&quot;&gt;`
        in front of a reader on the first end-to-end render. TRACKER has sessions that have gone
        quiet, so the card has rows to get wrong."""
        page = rb.render(TRACKER, LOG, self.cfg, NOW)
        blocked = page.split("<h2>Blocked</h2>", 1)[1].split("<h2", 1)[0]
        self.assertIn("Not reporting · ", blocked)
        self.assertRegex(blocked, r'Not reporting · [1-9]')
        # Stage 3 replaced the stamp with the silence and the lanes behind it, but the property the
        # first end-to-end render caught is the same one: the row is html, and escaping it puts the
        # markup in front of a reader.
        self.assertRegex(blocked, r"<span class='warn'>· (silent \d+h\d+m|never reported)</span>")
        self.assertNotIn("&lt;span", blocked)

    def test_the_page_reads_due_next_then_blocked_then_the_day(self) -> None:
        body = self.html.split("</style>", 1)[1]
        order = [body.index(h) for h in ("Due next", "Blocked", 'data-strip="day"', 'data-strip="week"', "<h2>Lanes")]
        self.assertEqual(order, sorted(order), "sections in the order they are read")


class InlineMarksTest(unittest.TestCase):
    """The coordinator writes `**bold**` headlines and `code` into items, facts and Resume values; the board showed the punctuation.

    Finding 98: 50 of 100 live lane names began with literal asterisks. A nowrap name drops the marks and
    keeps the words; prose that wraps — history bullets, session facts, Resume values — renders them. The
    citation attributes (`data-item`) stay raw, because that is what the graders match.
    """

    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(MARKS_TRACKER, LOG, self.cfg, NOW)
        self.day = _strip_html(self.html, "day")

    def test_unmark_drops_pairs_and_keeps_a_lone_marker(self) -> None:
        self.assertEqual(rb._unmark("**Deploy main.** x"), "Deploy main. x")
        self.assertEqual(rb._unmark("a ** b"), "a ** b")
        self.assertEqual(rb._unmark("**a** and **b**"), "a and b")

    def test_names_show_the_words_and_not_the_marks(self) -> None:
        names = re.findall(r'<div class="name">([^<]*)</div>', self.day)
        self.assertTrue(names)
        for name in names:
            self.assertNotIn("*", name, name)
        self.assertTrue(any(n.startswith("Deploy main.") for n in names), names)
        lanes_table = self.html.split("<h2>Lanes", 1)[1].split("</table>")[0]
        self.assertIn("<summary>Session TTL fix, orphaned.", lanes_table)
        self.assertNotIn("**", lanes_table)
        lanes = self.html.split("<h2>Lanes</h2>")[1]
        self.assertRegex(lanes, r"<summary>Deploy main\.[^<*]*</summary>")

    def test_the_citation_stays_raw(self) -> None:
        self.assertIn('data-item="**Deploy main.** Deployed tips', self.day)

    def test_history_renders_code_and_bold(self) -> None:
        lanes = self.html.split("<h2>Lanes</h2>")[1]
        hist = re.findall(r'<ul class="hist">(.*?)</ul>', lanes, re.S)
        self.assertTrue(hist)
        joined = "".join(hist)
        self.assertIn("<code>39e516f</code>", joined)
        self.assertIn("<time>22:20</time><span><strong>handed to research-1 on Robin&#x27;s yes</strong>", joined)
        self.assertNotIn("**", joined)

    def test_session_facts_render_bold_and_code(self) -> None:
        facts = re.search(r'<dl class="facts">(.*?)</dl>', self.html, re.S).group(1)
        self.assertIn("<strong>left the registry by 20:09</strong>", facts)
        self.assertIn("<code>done</code>", facts)
        self.assertNotIn("**", facts)

    def test_resume_values_render_code(self) -> None:
        resume = re.search(r'<dl class="resume">(.*?)</dl>', self.html, re.S).group(1)
        self.assertIn("<code>d6aab3b</code>", resume)


class MarkSpansTest(unittest.TestCase):
    """A mark pair is a span like a parenthesis: a clause split through the middle of one leaves half a mark on each side.

    Found on the live board after the first fix: 21 history bullets held an orphan `**`, all of them a
    bold clause containing a full stop, which `_depth0_split` cut in two. A long Resume field kept its
    marks in the summary it folds behind, and a backtick in a name is the same noise as an asterisk.
    """

    def test_a_split_never_falls_inside_a_bold_span(self) -> None:
        text = "Head. **22:19: copied into bruno/ (source untouched). Zach wants this:** launch.php reads it. Tail"
        self.assertEqual(
            rb._depth0_split(text),
            ["Head", "**22:19: copied into bruno/ (source untouched). Zach wants this:** launch.php reads it", "Tail"],
        )

    def test_an_unbalanced_item_splits_as_it_always_did(self) -> None:
        self.assertEqual(rb._depth0_split("**half open. and then"), ["**half open", "and then"])

    def test_a_marked_clause_renders_whole(self) -> None:
        tracker = MARKS_TRACKER.replace(
            "**Session TTL fix, orphaned.** `session.py` reads 3600 where §10.2 says 1800",
            "Head. **22:19: copied into bruno/ (source untouched). Zach wants this:** `launch.php` reads it",
        )
        html = rb.render(tracker, LOG, rb.parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW)
        hist = "".join(re.findall(r'<ul class="hist">(.*?)</ul>', html, re.S))
        self.assertNotIn("**", hist)
        self.assertIn("<time>22:19</time><span><strong>copied into bruno/ (source untouched). Zach wants this:</strong>", hist)

    def test_a_long_resume_field_folds_without_its_marks(self) -> None:
        long = "Zach — **the seed over `railway ssh`** " + "and the rest of a line that runs past the clip " * 3
        tracker = MARKS_TRACKER.replace("- As of: 09:52 — gauntlet-f0 [308833]", f"- As of: 09:52 — gauntlet-f0 [308833]\n- Waiting on: {long}")
        html = rb.render(tracker, LOG, rb.parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW)
        resume = re.search(r'<dl class="resume">(.*?)</dl>', html, re.S).group(1)
        summary = re.search(r"<summary>(.*?)</summary>", resume, re.S).group(1)
        self.assertNotIn("*", summary)
        self.assertNotIn("`", summary)
        self.assertIn("<strong>the seed over <code>railway ssh</code></strong>", resume)

    def test_a_name_drops_backticks_and_keeps_a_lone_star(self) -> None:
        self.assertEqual(rb._unmark("D8 `AGENT_FRAME_ANCESTORS` defaults to `*`"), "D8 AGENT_FRAME_ANCESTORS defaults to *")
        self.assertEqual(rb._unmark("agent `39e516f`, 30 behind"), "agent 39e516f, 30 behind")


PIPED_TRACKER = SIZED_TRACKER.replace(
    "| A oldest | worker | open | 2026-09-15 |  | M | c |",
    "| A oldest | worker | open | 2026-09-15 |  | M | c |\n"
    "| P coded | worker | done 00:28–01:36 | 2026-09-19 | Final | L | c |\n"
    "| P bare | worker | running 09:00 | 2026-09-16 |  | S | c |",
).replace("| P coded |", '| P coded, `"ok"|"degraded"` in prose |').replace("| P bare |", "| P bare A|B plain |")


class CellSpanTest(unittest.TestCase):
    """A cell is a span too: the delimiter is a pipe nobody spoke for."""

    def test_a_pipe_inside_a_backtick_span_is_not_a_delimiter(self) -> None:
        self.assertEqual(rb._cells('| a | `x|y` | b |'), ["a", "`x|y`", "b"])

    def test_an_escaped_pipe_is_a_pipe_and_loses_its_backslash(self) -> None:
        self.assertEqual(rb._cells(r"| a | x \| y | b |"), ["a", "x | y", "b"])

    def test_a_cell_holding_both_comes_back_once(self) -> None:
        self.assertEqual(rb._cells(r"| a | `a|b` \| c | d |"), ["a", "`a|b` | c", "d"])

    def test_an_odd_backtick_splits_as_it_always_did(self) -> None:
        """A typo degrades to the old behaviour instead of swallowing the rest of the row."""
        self.assertEqual(rb._cells("| a | `x|y | b |"), ["a", "`x", "y", "b"])

    def test_the_ordinary_rows_parse_exactly_as_before(self) -> None:
        self.assertEqual(rb._cells("| Write eval README | Robin | open | 09:00 | 17:00 | c |"),
                         ["Write eval README", "Robin", "open", "09:00", "17:00", "c"])
        self.assertEqual(rb._cells("|---|---|---|"), ["---", "---", "---"])

    def test_a_session_row_keeps_its_columns_when_a_cell_carries_a_pipe(self) -> None:
        cells = rb._cells('| ab12cd | impl-2 | working | reads `"ok"|"degraded"` | | | | | 09:00 |')
        self.assertEqual(len(cells), 9)
        self.assertEqual(cells[3], 'reads `"ok"|"degraded"`')


class LaneAnchorTest(unittest.TestCase):
    """An over-wide row is re-read from the one cell a machine can recognise: its state."""

    def setUp(self) -> None:
        self.by = {lane.item: lane for lane in rb.parse_tracker(PIPED_TRACKER).lanes}

    def test_a_backticked_pipe_needs_no_recovery_at_all(self) -> None:
        lane = self.by['P coded, `"ok"|"degraded"` in prose']
        self.assertEqual(lane.warning, "")
        self.assertEqual((lane.owner, lane.state, lane.due, lane.size, lane.checklist),
                         ("worker", "done 00:28–01:36", "Final", "L", "c"))

    def test_a_bare_pipe_in_the_item_is_anchored_back_onto_its_columns(self) -> None:
        lane = self.by["P bare A|B plain"]
        self.assertEqual((lane.owner, lane.state, lane.since, lane.size, lane.checklist),
                         ("worker", "running 09:00", "2026-09-16", "S", "c"))

    def test_a_recovered_row_still_warns(self) -> None:
        self.assertIn("8 cells", self.by["P bare A|B plain"].warning)

    def test_surplus_on_the_right_lands_in_the_checklist(self) -> None:
        t = rb.parse_tracker(TRACKER)
        long = [lane for lane in t.lanes if lane.item == "Long"][0]
        self.assertEqual((long.owner, long.state, long.since, long.due), ("Robin", "open", "09:00", "10:00"))
        self.assertEqual(long.checklist, "x | extra | cells")
        self.assertIn("8 cells", long.warning)

    def test_two_state_cells_fall_back_to_the_left_anchored_parse(self) -> None:
        """Two candidates is not an anchor. The row keeps the parse it was written with, and the warning speaks."""
        text = SIZED_TRACKER.replace("| A oldest | worker | open | 2026-09-15 |  | M | c |",
                                     "| A|open|B | worker | open | 2026-09-15 |  | M | c |")
        lane = [l for l in rb.parse_tracker(text).lanes if l.item == "A"][0]
        self.assertEqual((lane.owner, lane.state), ("open", "B"))
        self.assertIn("9 cells", lane.warning)

    def test_no_state_cell_falls_back_to_the_left_anchored_parse(self) -> None:
        text = SIZED_TRACKER.replace("| A oldest | worker | open | 2026-09-15 |  | M | c |",
                                     "| A|B | worker | dancing | 2026-09-15 |  | M | c |")
        lane = [l for l in rb.parse_tracker(text).lanes if l.item == "A"][0]
        self.assertEqual(lane.owner, "B")
        self.assertIn("8 cells", lane.warning)

    def test_a_short_row_is_never_anchored(self) -> None:
        text = SIZED_TRACKER.replace("| C newer | worker | open | 2026-09-16 |  |  | c |", "| C newer | worker | open |")
        lane = {l.item: l for l in rb.parse_tracker(text).lanes}["C newer"]
        self.assertEqual(lane.since, "")
        self.assertIn("3 cells", lane.warning)
