"""A closed issue ends its Flow row, board and task page alike (#220).

Today only a merged change ends a waiting/running task's row (render_board.merged_map,
flow_chart.build(merged=...)). A tracker task whose forge issue was closed without a merge keeps its row
(and the task page's elapsed/waiting totals) running to now, even once "What happens next" already says
the issue is closed. A grouped row (two tasks sharing one issue, merged into a single Flow row) has a
second, related bug: when the row that "moved last" is finished, its group-mates' own segments can still
run past that finish time. These tests pin down:

    1. board_sources.Issue gains `closed_at`; GH_ISSUE/GL_ISSUE ask for `closedAt`; both parsers fill it;
       the cache round-trips it, and an older cache with no `closed_at` key still loads (as None).
    2. render_board.merged_map is replaced by forge_ends(tasks, sources, zone) -> dict[str, tuple[datetime,
       str]]: a task's name to (time, "merged") for its latest merged change, or (time, "closed") for its
       closed issue's close time when there is no merged change. A merge wins.
    3. flow_chart.build's `merged=` keyword becomes `ended: dict[str, tuple[datetime, str]] | None`. A task
       in it has its segments clipped at the time, and its row's status is the word, its note `<word> HH:MM`.
    4. When rows sharing an issue merge into one (flow_chart._merge_issues) and the row that moved last is
       finished (merged, closed or done), every segment in the group ends no later than that row's own last
       segment end — not just the winning row's.
    5. issue_page.render's elapsed end and its worker-span stop both come from those same clipped rows, so
       "Worked X of Y elapsed" reports Y from the last segment end, not from now.
"""

import re
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import json
import os
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_sources as bs  # noqa: E402
import flow_chart as fc  # noqa: E402
import render_board as rb  # noqa: E402
import tracker_write as tw  # noqa: E402
import test_board_sources as tbs  # noqa: E402  (module-qualified: don't re-collect its TestCases)
import test_issue_page as tip  # noqa: E402  (module-qualified: don't re-collect its TestCases)
from test_flow_chart import CT, LANES, NOW, TODAY, TODAY_TRACKER, YESTERDAY, YESTERDAY_TRACKER, at, merged  # noqa: E402

import issue_page as ip  # noqa: E402


# --- point 1: board_sources.Issue.closed_at, the forges' fields, the parsers, the cache -----------------

class QueryFieldsTest(unittest.TestCase):
    def test_both_forges_query_fields_ask_for_the_close_time(self):
        self.assertIn("closedAt", bs.GH_ISSUE)
        self.assertIn("closedAt", bs.GL_ISSUE)


class GitHubClosedIssueFetchTest(unittest.TestCase):
    def test_github_fetch_reads_and_caches_an_issues_close_time(self):
        closed_at = "2026-09-25T20:15:00Z"
        root = tbs.workspace("GitHub issues; repo o/app")  # default issue "#115"
        gh = tbs.FakeGh(n115={**tbs.gh_issue(115, ()), "state": "CLOSED", "closedAt": closed_at})
        got = bs.refresh(root, tbs.NOW, gh=gh)
        issue = got.issues["#115"]
        self.assertEqual((issue.state, issue.closed_at), ("closed", datetime.fromisoformat(closed_at)))
        # The cache round-trips closed_at as a datetime, not a leftover ISO string.
        self.assertEqual(bs.load(root / "pages").issues["#115"].closed_at, issue.closed_at)

    def test_an_open_issue_has_no_close_time(self):
        root = tbs.workspace("GitHub issues; repo o/app")
        got = bs.refresh(root, tbs.NOW, gh=tbs.FakeGh())
        self.assertIsNone(got.issues["#115"].closed_at)


class GitLabClosedIssueFetchTest(unittest.TestCase):
    def test_gitlab_fetch_reads_and_caches_an_issues_close_time(self):
        root = tbs.workspace("GitLab issues; host https://labs.example.test; project team/app")
        closed_at = "2026-09-25T20:15:00+00:00"
        issue = {"iid": "115", "webUrl": "https://labs.example.test/team/app/-/issues/115", "title": "Search",
                 "state": "closed", "labels": {"nodes": []}, "closedAt": closed_at}

        def call(method, url, token, timeout, payload=None):
            return {"data": {"p0": {"issues": {"nodes": [issue]}, "merged": {"nodes": []}}}}, {}, ""

        with mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}):
            got = bs.refresh(root, tbs.NOW, call=call)
        self.assertEqual(got.errors, {})
        self.assertEqual((got.issues["#115"].state, got.issues["#115"].closed_at),
                         ("closed", datetime.fromisoformat(closed_at)))
        self.assertEqual(bs.load(root / "pages").issues["#115"].closed_at, got.issues["#115"].closed_at)


class OlderCacheTest(unittest.TestCase):
    def test_a_cache_written_before_closed_at_existed_still_loads(self):
        # A cache from a release before #220: no `closed_at` key on its issues at all.
        pages = Path(tempfile.mkdtemp())
        (pages / bs.CACHE).write_text(json.dumps({
            "fetched": {"kanban": tbs.NOW.isoformat()},
            "issues": {"#109": {"ref": "#109", "url": "u", "title": "t", "state": "closed", "labels": []}},
            "changes": {}, "workers": [], "errors": {},
        }))
        got = bs.load(pages)
        self.assertEqual(got.issues["#109"].state, "closed")
        self.assertIsNone(got.issues["#109"].closed_at)


# --- point 2: render_board.forge_ends -------------------------------------------------------------------

class ForgeEndsTest(unittest.TestCase):
    def tasks(self):
        return rb.parse_tracker(TODAY_TRACKER).tasks  # "Upload size limit" names issue #109

    def test_a_merged_change_gives_its_time_and_the_word_merged(self):
        change = merged("#109", "01:15")
        src = bs.Sources({}, {}, {change.ref: change}, (), {})
        self.assertEqual(rb.forge_ends(self.tasks(), src, CT), {"Upload size limit": (at(TODAY, "01:15"), "merged")})

    def test_a_closed_issue_with_no_merge_gives_its_close_time_and_the_word_closed(self):
        closed_at = at(TODAY, "00:55")
        issue = bs.Issue("#109", "u", "t", "closed", (), closed_at=closed_at.astimezone(timezone.utc))
        src = bs.Sources({}, {"#109": issue}, {}, (), {})
        self.assertEqual(rb.forge_ends(self.tasks(), src, CT), {"Upload size limit": (closed_at, "closed")})

    def test_an_open_issue_or_a_closed_one_with_no_close_time_gives_nothing(self):
        for issue in (bs.Issue("#109", "u", "t", "open", ()), bs.Issue("#109", "u", "t", "closed", ())):
            with self.subTest(state=issue.state, closed_at=issue.closed_at):
                src = bs.Sources({}, {"#109": issue}, {}, (), {})
                self.assertEqual(rb.forge_ends(self.tasks(), src, CT), {})

    def test_a_merge_wins_over_a_close_time_for_the_same_task(self):
        closed_at = at(TODAY, "00:55")
        issue = bs.Issue("#109", "u", "t", "closed", (), closed_at=closed_at.astimezone(timezone.utc))
        change = merged("#109", "01:15")
        src = bs.Sources({}, {"#109": issue}, {change.ref: change}, (), {})
        self.assertEqual(rb.forge_ends(self.tasks(), src, CT), {"Upload size limit": (at(TODAY, "01:15"), "merged")})


# --- point 3: flow_chart.build(ended=...) ---------------------------------------------------------------

def _log():
    return fc.moves(YESTERDAY_TRACKER, YESTERDAY, CT) + fc.moves(TODAY_TRACKER, TODAY, CT)


class FlowChartEndedTest(unittest.TestCase):
    def test_ended_clips_segments_and_labels_the_row_with_its_word(self):
        for word in ("merged", "closed"):
            with self.subTest(word=word):
                at_time = at(TODAY, "01:15")
                built = fc.build(_log(), rb.parse_tracker(TODAY_TRACKER).tasks, LANES, set(), {}, NOW,
                                 ended={"Upload size limit": (at_time, word)})
                status, row = {r.name: (s, r) for s, r in built}["Upload size limit"]
                self.assertEqual((status, row.note), (word, f"{word} 01:15"))
                self.assertTrue(row.segments)
                for g in row.segments:
                    self.assertLessEqual(g.end, at_time, g)


# --- point 4: a finished group clips every one of its rows' segments, not just the winner's ---------------

GROUPED_HEAD = "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n" + "|---" * 11 + "|\n"
GROUPED_TRACKER = f"""# Tracker

## Tasks

{GROUPED_HEAD}| Task A | Item A | Robin | waiting | {TODAY.isoformat()} |  | S | build | review | #150 | c |
| Task B | Item B | Robin | running 18:38 | {TODAY.isoformat()} |  | M | build | main | #150 | c |

## Log

- 18:30 stage: Task A → review
- 18:38 stage: Task B → implement
- 18:49 stage: Task B → pr
- 21:00 stage: Task B → main
"""


class GroupedRowClipTest(unittest.TestCase):
    def test_a_finished_groups_segments_all_end_no_later_than_its_last_segment(self):
        # Task A and Task B share issue #150 and merge into one Flow row. Task B reaches its lane's last
        # stage at 21:00 with no forge data at all, so the group reads "merged 21:00" (the row that moved
        # last wins the note) — but Task A's own `review` segment, still `waiting`, keeps running past it.
        now = at(TODAY, "23:00")  # well after 21:00, so an unclipped segment would visibly overrun
        built = fc.build(fc.moves(GROUPED_TRACKER, TODAY, CT), rb.parse_tracker(GROUPED_TRACKER).tasks,
                         LANES, set(), {}, now)
        rows = [(s, r) for s, r in built if r.ref == "#150"]
        self.assertEqual(len(rows), 1, rows)  # both tasks merged into a single row
        status, row = rows[0]
        end = at(TODAY, "21:00")
        self.assertEqual((status, row.note), ("merged", "merged 21:00"))
        for g in row.segments:
            self.assertLessEqual(g.end, end, g)


# --- point 5: the task page's elapsed and worker spans read the same clipped rows ------------------------

class TaskPageEndedRowsTest(unittest.TestCase):
    def test_elapsed_and_waiting_stop_at_the_grouped_rows_finish_time(self):
        # #220's grouped-row repro (point 4), run through the task page: Review round 1 shares issue #109
        # with Upload size limit and reaches its lane's last stage at 01:15. Both elapsed (the flow rows'
        # last segment end) and rev-w2's still-running span must stop there, not at NOW (02:10).
        (yesterday, y_text), (today, t_text) = tip.work_trackers()
        t_text = tw.append_log(t_text, "- 01:15 stage: Review round 1 → main")
        html = ip.render(109, [(yesterday, y_text), (today, t_text)], tip.sources(), NOW, LANES)
        self.assertRegex(tip.text(html), r"(?i)\bWorked\s+4h00m\s+of\s+5h10m\s+elapsed\s*·\s*1h10m\s+waiting\b")

    def test_elapsed_and_waiting_stop_at_a_closed_issues_close_time(self):
        # Same fixture and arithmetic as #200's merge test, but the row ends via a closed issue, no merge.
        closed_at = at(TODAY, "01:40").astimezone(timezone.utc)
        issue = replace(tip.ISSUE, state="closed", closed_at=closed_at)
        html = ip.render(109, tip.work_trackers(), tip.sources(issue=issue), NOW, LANES)
        self.assertRegex(tip.text(html), r"(?i)\bWorked\s+4h25m\s+of\s+5h35m\s+elapsed\s*·\s*1h10m\s+waiting\b")


if __name__ == "__main__":
    unittest.main()
