"""Issue #199: workers read as kind, runtime, task and tree — never tracker prose.

Two computations are in play and these tests keep them apart:

- `board_sources.workers()` decides a session's `task`: the open task row it owns (its issue ref and row
  name), not the Sessions row's `doing` prose. Tests that check this run the real function over a minimal
  tracker (no registered process, no live one-shot trees), the exact seam the issue's bug lives in.
- `render_board.worker_panel` and `workers_page.render` draw whatever `task` they are handed. Tests of the
  facts format, the note order and the name links build `board_sources.Worker` rows directly, since those
  are markup contracts independent of how `task` was computed.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "scripts"), str(HERE)]

import board_sources as bs  # noqa: E402
import panels  # noqa: E402
import render_board as rb  # noqa: E402
import workers_page  # noqa: E402

ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 26, 14, 30, tzinfo=ZONE)
DAY = NOW.date()

CLAUDE_MD = ("## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n"
             "- Tracker: `daily/<date>-tracker.md`\n- Timezone: America/Chicago\n")
HEAD = "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n" + "|---" * 11 + "|\n"
SESSION_HEAD = "| ref | name | state | doing | waiting on | free at | constraints | children | last reply |\n" + "|---" * 9 + "|\n"
SENTINEL = "zzz sentinel prose that must never reach either view"


def make_workers(tracker_body: str) -> tuple[bs.Worker, ...]:
    """The real `board_sources.workers()` over a minimal tracker: no registered process and no one-shot
    trees, so only the tracker's own Sessions and Tasks rows can produce a worker."""
    root = Path(tempfile.mkdtemp())
    (root / "CLAUDE.md").write_text(CLAUDE_MD)
    cfg = rb.parse_coordinator(CLAUDE_MD, today=DAY)
    tracker = rb.parse_tracker(tracker_body)
    return bs.workers(root, cfg, tracker, tracker_body, DAY)


def tracker(task_row: str, session_row: str) -> str:
    return (f"# Tracker {DAY.isoformat()}\n\n## Tasks\n\n{HEAD}{task_row}\n"
            f"## Sessions\n\n{SESSION_HEAD}{session_row}\n\n## Log\n\n- 00:01 opened the day\n")


class SessionTaskOwnershipTest(unittest.TestCase):
    """(#199) A session's task is the open task row it owns: its issue ref and row name; none when it owns
    no open row (Acceptance line 1)."""

    def test_a_session_shows_the_open_row_it_owns(self):
        body = tracker("| Review round | Review the pass | reviewer-2 | open | 2026-09-25 |  | S |  |  | #119 | c |\n",
                       f"| abc123 | reviewer-2 | working | {SENTINEL} | nothing |  |  | none | 14:00 |\n")
        session = next(w for w in make_workers(body) if w.kind == "session")
        self.assertIn("Review round", session.task, session.task)
        self.assertNotIn(SENTINEL, session.task)

    def test_a_session_that_owns_no_open_row_has_no_task(self):
        body = tracker("| Review round | Review the pass | someone-else | open | 2026-09-25 |  | S |  |  | #119 | c |\n",
                       f"| abc123 | reviewer-2 | working | {SENTINEL} | nothing |  |  | none | 14:00 |\n")
        session = next(w for w in make_workers(body) if w.kind == "session")
        self.assertEqual(session.task, "")

    def test_a_done_row_the_session_owns_does_not_count_as_open(self):
        body = tracker("| Review round | Review the pass | reviewer-2 | done 10:00 | 2026-09-25 |  | S |  |  | #119 | c |\n",
                       f"| abc123 | reviewer-2 | working | {SENTINEL} | nothing |  |  | none | 14:00 |\n")
        session = next(w for w in make_workers(body) if w.kind == "session")
        self.assertEqual(session.task, "")


class PanelFactsFormatTest(unittest.TestCase):
    """(#199) The board panel's facts read `<kind> · <runtime model> · <task> · <tree>`, skipping empty
    parts (Acceptance line 2)."""

    def test_a_one_shot_shows_every_part(self):
        w = bs.Worker("impl-07", "one-shot", "claude", "opus", "Cut the release", "trees/cut", NOW, None)
        panel = rb.worker_panel(replace(bs.EMPTY, workers=(w,)), NOW)
        self.assertEqual(panel.rows[0].facts, "one-shot · claude opus · Cut the release · trees/cut")

    def test_a_session_with_no_model_and_no_tree_skips_both(self):
        w = bs.Worker("impl-04", "session", "codex", "", "Audit", "", NOW, NOW)
        panel = rb.worker_panel(replace(bs.EMPTY, workers=(w,)), NOW)
        self.assertEqual(panel.rows[0].facts, "session · codex · Audit")


class WorkersPageSessionTaskTest(unittest.TestCase):
    """(#199) The Workers page's task cell for a session shows the same ref and name, linked to the task
    page (Acceptance line 3)."""

    def test_a_session_task_cell_links_its_owned_row(self):
        body = tracker("| Review round | Review the pass | reviewer-2 | open | 2026-09-25 |  | S |  |  | #119 | c |\n",
                       f"| abc123 | reviewer-2 | working | {SENTINEL} | nothing |  |  | none | 14:00 |\n")
        sources = replace(bs.EMPTY, workers=make_workers(body))
        html = workers_page.render(body, sources, NOW, Path(tempfile.mkdtemp()), None)
        self.assertIn('href="/issues/119"', html)
        self.assertIn("Review round", html)
        self.assertNotIn(SENTINEL, html)


class PanelWorkerNameLinksTest(unittest.TestCase):
    """(#199) The panel's worker names link to /workers (Acceptance line 4)."""

    def test_each_worker_row_links_its_name_to_the_workers_page(self):
        ws = (bs.Worker("impl-07", "one-shot", "claude", "opus", "Cut the release", "trees/cut", NOW, None),
              bs.Worker("planner", "session", "claude", "", "Tidy config", "", NOW, None))
        panel = rb.worker_panel(replace(bs.EMPTY, workers=ws), NOW)
        for row, w in zip(panel.rows, ws):
            with self.subTest(worker=w.name):
                self.assertEqual(row.href, "/workers")
        html = panels.panels([panel])
        for w in ws:
            with self.subTest(worker=w.name, check="markup"):
                self.assertIn(f'<a class="panels-name" href="/workers">{w.name}</a>', html)


class PanelNoteOrderTest(unittest.TestCase):
    """(#199) The panel note counts one-shots first: `N one-shot · M session` (Acceptance line 5)."""

    def test_one_shots_are_counted_before_sessions_regardless_of_list_order(self):
        ws = (bs.Worker("planner", "session", "claude", "", "Tidy", "", NOW, None),
              bs.Worker("impl-1", "one-shot", "claude", "opus", "A", "trees/a", NOW, None),
              bs.Worker("impl-2", "one-shot", "claude", "opus", "B", "trees/b", NOW, None))
        panel = rb.worker_panel(replace(bs.EMPTY, workers=ws), NOW)
        self.assertEqual(panel.note, "2 one-shot · 1 session")


class NoTrackerProseTest(unittest.TestCase):
    """(#199) No worker fact is longer than its parts: no tracker prose reaches either view (Acceptance
    line 6)."""

    def setUp(self):
        self.body = tracker("| Other task | Other item | someone-else | open | 2026-09-25 |  | S |  |  | #201 | c |\n",
                            f"| abc123 | reviewer-2 | working | {SENTINEL} | nothing |  |  | none | 14:00 |\n")
        self.workers = make_workers(self.body)
        self.sources = replace(bs.EMPTY, workers=self.workers)

    def test_the_sentinel_is_absent_from_the_board_panel(self):
        panel = rb.worker_panel(self.sources, NOW)
        self.assertNotIn(SENTINEL, repr(panel.rows))

    def test_the_sentinel_is_absent_from_the_workers_page(self):
        html = workers_page.render(self.body, self.sources, NOW, Path(tempfile.mkdtemp()), None)
        self.assertNotIn(SENTINEL, html)


if __name__ == "__main__":
    unittest.main()
