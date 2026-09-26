"""issue_page.py: one tracker issue's page, `/issues/<n>`, from the tracker and the cached sources (Task.dc.html,
"Task page — laptop"). Stage 1: only what the tracker and board_sources already hold.

The module the implementer creates is `scripts/issue_page.py` with

    render(number: int, trackers: list[tuple[date, str]], sources: board_sources.Sources, now: datetime,
           lanes: dict | None = None) -> str | None

`number` is the issue (`109` for the tracker cell `#109`); `trackers` is (day, tracker text), oldest first, the last
being today's, over the days the Flow chart reaches back; `lanes` is the settings' lanes. It returns the page's HTML,
or None when no task in `trackers` names the issue. Sections are `<h2>` headings; the header is one `<header>`.
pages.py serves it at `GET /issues/<n>`, re-rendered on request like the board and decision pages.
"""

import re
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_sources as bs  # noqa: E402
import flow_chart as fc  # noqa: E402
import one_shot  # noqa: E402
import pages as pg  # noqa: E402
import render_board as rb  # noqa: E402
import tracker_write as tw  # noqa: E402
from test_flow_chart import CT, KANBAN, LANES, NOW, TODAY, TODAY_TRACKER, YESTERDAY  # noqa: E402
from test_pages import get  # noqa: E402

try:
    import issue_page as ip  # noqa: E402
except ModuleNotFoundError:  # red until the implementer creates it; each test then fails on the import
    ip = None

HEAD = "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n" + "|---" * 11 + "|\n"
YESTERDAY_TRACKER = f"""# Tracker

## Tasks

{HEAD}| Upload size limit | Cap uploads | Robin | running 22:00 | {YESTERDAY} |  | M | build | implement | #109 | c |
| Cache warmup | Warm the pool | Robin | running 23:00 | {YESTERDAY} |  | S | build | implement | #111 | c |

## Log

- 22:00 stage: Upload size limit → implement
- 23:00 stage: Cache warmup → implement
"""
TODAY_START = f"""# Tracker

## Tasks

{HEAD}| Upload size limit | Cap uploads | Robin | waiting | {YESTERDAY} |  | M | build | pr | #109 | c |
| Review round 1 | Review r1 | unassigned | open | {TODAY} |  | S |  |  | #109 | c |
| Verify limits | Verify r1 | unassigned | open | {TODAY} |  | S |  |  | #109 | c |
| Fix round 1 | Fix r1 | unassigned | open | {TODAY} |  | S |  |  | #109 | c |
| Cache warmup | Warm the pool | unassigned | open | {YESTERDAY} |  | S | build | implement | #111 | c |
| Tidy config | Tidy | Robin | open | {TODAY} |  | S |  |  |  | c |

## File ownership

| context | paths |
|---|---|
| Review r1 | `src/a/` |
| Verify r1 | `src/d/` |
| Fix r1 | `src/b/` |
| Warm the pool | `src/c/` |

## Log

- 00:01 opened the day
"""
# The Log times of #109's stage and launcher lines, oldest first; then the times of the other tasks' lines.
TIMELINE = ("22:00", "00:05", "00:20", "00:30", "00:50", "01:00", "01:20")
OTHERS = ("23:00", "00:40", "01:50")


def today_tracker() -> str:
    """Today's tracker as the launcher's and the coordinator's own writers leave it."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        path.write_text(tw.append_log(TODAY_START, "- 00:05 stage: Upload size limit → pr"))
        one_shot.record_launch(path, "Review r1", "impl-a1", "worktree `a` (a)", "codex", "gpt-5.5", "00:20")
        one_shot.record_launch(path, "Verify r1", "impl-d4", "worktree `d` (d)", "claude", "sonnet", "00:30")
        one_shot.record_launch(path, "Warm the pool", "impl-c3", "worktree `c` (c)", "codex", "gpt-5.5", "00:40")
        one_shot.update_tracker(path, "Verify r1", "impl-d4", "Robin", "human_review", "unclear", "v.py", "00:50")
        one_shot.update_tracker(path, "Review r1", "impl-a1", "Robin", "done", "reviewed", "r.py", "01:00")
        one_shot.record_launch(path, "Fix r1", "impl-b2", "worktree `b` (b)", "claude", "opus", "01:20")
        path.write_text(tw.append_log(path.read_text(), "- 01:50 stage: Cache warmup → pr"))
        return path.read_text()


ISSUE = bs.Issue("#109", "https://forge.example/grp/app/-/issues/109", "Upload size limit", "open", ())
MR = bs.Change("!48", "https://forge.example/grp/app/-/merge_requests/48", "Cap uploads at 25 MB", "open", False,
               "failed", False, 2, None, "main", (), ("#109",))
OTHER = bs.Change("!50", "https://forge.example/grp/app/-/merge_requests/50", "Warm the pool", "open", False,
                  "passed", True, 1, None, "main", (), ("#111",))


def sources(*changes: bs.Change) -> bs.Sources:
    return bs.Sources({}, {"#109": ISSUE}, {c.ref: c for c in changes}, (), {})


def page(number: int = 109, src: bs.Sources | None = None) -> str | None:
    if ip is None:
        raise AssertionError("scripts/issue_page.py does not exist yet")
    return ip.render(number, [(YESTERDAY, YESTERDAY_TRACKER), (TODAY, today_tracker())],
                     src if src is not None else sources(MR, OTHER), NOW, LANES)


def text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def section(html: str, heading: str) -> str:
    """From the `<h2>` that starts with `heading` to the next `<h2>`."""
    m = re.search(rf"<h2[^>]*>\s*{heading}\b", html, re.I)
    if not m:
        raise AssertionError(f"no {heading!r} section")
    end = html.find("<h2", m.end())
    return html[m.start():end if end >= 0 else len(html)]


class RenderTest(unittest.TestCase):
    def test_an_issue_no_task_names_has_no_page(self):
        # Route: /issues/<n> exists only for an issue a tracker task names.
        self.assertIsNone(page(110))

    def test_the_header_names_the_issue_the_task_and_its_state(self):
        # Header: "Task · #109 · … · board", h1 "Upload size limit", chip "RUNNING".
        header = re.search(r"<header\b.*?</header>", page(), re.S)
        self.assertIsNotNone(header)
        self.assertIn("#109", text(header[0]))
        self.assertIn('href="/"', header[0])
        self.assertIn("Upload size limit", text(re.search(r"<h1\b.*?</h1>", header[0], re.S)[0]))
        self.assertRegex(text(header[0]), r"(?i)\brunning\b")

    def test_flow_is_one_row_of_every_task_on_the_issue(self):
        # FLOW: one row, the issue's tasks merged by flow_chart.build; no other issue's row.
        html = page()
        today = today_tracker()
        built = fc.build(fc.moves(YESTERDAY_TRACKER, YESTERDAY, CT) + fc.moves(today, TODAY, CT),
                         rb.parse_tracker(YESTERDAY_TRACKER).tasks + rb.parse_tracker(today).tasks, LANES, set(), {}, NOW)
        row = next(r for _, r in built if r.ref == "#109")
        figures = re.findall(r'<figure class="gantt".*?</figure>', html, re.S)
        self.assertTrue(figures)
        for fig in figures:
            self.assertEqual(fig.count('<div class="gantt-row">'), 1)
        self.assertEqual(set(re.findall(r'class="gantt-ref">([^<]*)<', html)), {"#109"})
        self.assertEqual(set(re.findall(r'class="gantt-name">([^<]*)<', html)), {row.name})
        for g in row.segments:
            with self.subTest(segment=(g.category, f"{g.start:%H:%M}")):
                self.assertRegex(html, rf'title="{g.category} (?:\w{{3}} )?{g.start:%H:%M}–')

    def test_the_change_card_shows_the_change_that_closes_the_issue(self):
        # MERGE REQUEST card: ref and link, state, pipeline, approvals; another issue's change is not shown.
        html = page()
        refs = section(html, "References")
        outside = html.replace(refs, "")
        self.assertIn("!48", text(outside))
        self.assertIn(f'href="{MR.url}"', outside)
        self.assertRegex(text(outside), r"(?i)pipeline\W+failed")
        self.assertRegex(text(outside), r"(?i)approv\w*\W+2\b|\b2\s+approv")
        self.assertNotIn("closed", text(outside).lower())
        closed = page(src=sources(replace(MR, state="closed"), OTHER))
        self.assertIn("closed", text(closed).lower())
        for other in ("!50", OTHER.url):
            self.assertNotIn(other, html)

    def test_no_change_is_one_line_instead_of_the_card(self):
        # MERGE REQUEST card: with no change naming the issue, the page says so.
        html = page(src=sources(OTHER))
        self.assertNotIn("!50", html)
        self.assertNotRegex(text(html), r"(?i)pipeline\W+(?:passed|failed)")
        self.assertRegex(text(html), r"(?i)\bno\b\W+(?:\w+\W+){0,2}(?:change|merge request|pull request|MR|PR)\b")

    def test_how_it_got_here_is_the_issues_log_lines_in_time_order(self):
        # HOW IT GOT HERE: stage moves and the launcher's started, completed and review lines, oldest first.
        got = text(section(page(), "How it got here"))
        at = [got.find(t) for t in TIMELINE]
        self.assertNotIn(-1, at, got)
        self.assertEqual(at, sorted(at))
        for name in ("impl-a1", "impl-d4", "impl-b2"):
            self.assertIn(name, got)
        for other in (*OTHERS, "impl-c3", "Cache warmup"):
            with self.subTest(other=other):
                self.assertNotIn(other, got)

    def test_workers_are_the_ones_launched_on_the_issue(self):
        # WORKERS: name, runtime and model, and RUNNING while its task row reads running.
        got = text(section(page(), "Workers"))
        self.assertNotIn("impl-c3", got)
        workers = {"impl-a1": ("codex", "gpt-5.5", False), "impl-d4": ("claude", "sonnet", False),
                   "impl-b2": ("claude", "opus", True)}
        starts = sorted((got.find(name), name) for name in workers)
        self.assertNotIn(-1, [s for s, _ in starts], got)
        for (start, name), (end, _) in zip(starts, starts[1:] + [(len(got), "")]):
            runtime, model, running = workers[name]
            one = got[start:end]
            with self.subTest(worker=name):
                self.assertIn(runtime, one)
                self.assertIn(model, one)
                self.assertEqual(bool(re.search(r"(?i)\brunning\b", one)), running)

    def test_references_link_the_issue_and_its_change(self):
        # REFERENCES: the issue, and the change when one is known.
        refs = section(page(), "References")
        self.assertIn(f'href="{ISSUE.url}"', refs)
        self.assertIn(f'href="{MR.url}"', refs)
        refs = section(page(src=sources()), "References")
        self.assertIn(f'href="{ISSUE.url}"', refs)
        self.assertNotIn("merge_requests", refs)


class BoardLinkTest(unittest.TestCase):
    def test_a_flow_row_for_an_issue_links_to_its_page(self):
        # Flow row → Task page: the board's row for #109 opens /issues/109.
        cfg = rb.parse_coordinator("## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n"
                                   "- Tracker: `daily/<date>-tracker.md`\n- Timezone: America/Chicago\n", today=TODAY)
        html = rb.render(TODAY_TRACKER, cfg, NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN)
        flow = html[html.index("<h2>Flow ·"):]
        refs = set(re.findall(r'class="gantt-ref">([^<]+)<', flow))
        self.assertEqual(refs, {"#109", "#111", "#98"})
        self.assertEqual(set(re.findall(r'href="(/issues/[^"]*)"', flow)), {f"/issues/{r[1:]}" for r in refs})


ZONE = "America/Chicago"
ROUTE_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Upload size limit | Cap uploads | Robin | open | {day} |  | S |  |  | #109 | c |

## Log

- 00:01 opened the day
- 00:02 noted #110 for later
- 00:03 stage: Upload size limit → implement
"""


class RouteTest(unittest.TestCase):
    """pages.py serves `/issues/<n>`: a route, rendered on request, whose file is never served by name."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n")
        (self.root / "daily").mkdir()
        (self.root / "daily" / f"{day}-tracker.md").write_text(ROUTE_TRACKER.format(day=day))
        self.pages = self.root / "pages"
        self.pages.mkdir()
        self.server = pg.make_server(self.pages, 0, root=self.root, refresh=lambda root, now: None, every=3600)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_an_issue_a_task_names_is_a_page(self):
        # Route: GET /issues/109 is the Task page for tracker issue #109.
        resp = get(self.port, "/issues/109")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Upload size limit", resp.body)
        self.assertIn(b"#109", resp.body)

    def test_an_issue_no_task_names_is_not_found(self):
        # Route: #110 is only mentioned in the Log, so it has no page.
        for path in ("/issues/110", "/issues/abc", "/issues/"):
            with self.subTest(path=path):
                self.assertEqual(get(self.port, path).status, 404)

    def test_the_rendered_file_is_not_served_by_name(self):
        # Route: pages are routes only (as /decisions/<slug>); an `.html` name is a 404.
        self.assertEqual(get(self.port, "/issues/109").status, 200)
        self.assertEqual(get(self.port, "/").status, 200)
        self.assertEqual(get(self.port, "/issues/109.html").status, 404)
        for name in sorted(p.name for p in self.pages.glob("*.html")):
            with self.subTest(name=name):
                self.assertEqual(get(self.port, f"/{name}").status, 404)


if __name__ == "__main__":
    unittest.main()
