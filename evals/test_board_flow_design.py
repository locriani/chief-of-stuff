"""#259: the board draws only the Flow artboard's sections, and draws each task once in BUILD.

The reference is the canvas's Flow artboard (Flow.dc.html), top to bottom: the tab bar; the header; six tiles;
BUILD, the stage columns plus a `NO LANE · N` strip; the panels; `FLOW · 24 HOURS` and `FLOW · 7 DAYS` with one legend.
Due next, Blocked, Sessions, the 24-hour day strip and the Lanes table are not in it, so none is drawn.

Artboard markers the stage columns map onto columns.py's classes: a column (`columns-col`, head `columns-name`, gate
columns `columns-gate`, count `columns-count`); a card (`columns-card`, name link `columns-card-name`, refs
`columns-refs`, owner `columns-owner`, state chip `columns-chip`, held card `columns-flagged` + `columns-flag`); the
NO LANE strip (`columns-foot`, `columns-foot-name`). The artboard's BUILD container is `id="flow"`, which the tiles
link to, so that id stays: it is the stage columns, not the Lanes table.

The Flow charts draw a task again by the artboard's own rows (one row per task, a bar per stage). That is the chart, not
a second BUILD, so these tests assert the BUILD drawing is single and leave the chart rows to test_flow_chart.py.
"""

from __future__ import annotations

import re
import sys
import unittest
from functools import cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "scripts"), str(HERE)]
import render_board as rb  # noqa: E402
from workspace import parse_coordinator  # noqa: E402
from test_flow_chart import KANBAN, LANES, NOW, TODAY, TODAY_TRACKER, YESTERDAY, YESTERDAY_TRACKER  # noqa: E402

CLAUDE = ("# W\n\n## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
          "- Timezone: America/Chicago\n- Settings: `cos.toml`\n")
# Four tasks in the build lane (one held at the triage gate, one merged into main) and one in no lane.
LANED = ["Upload size limit", "Cache warmup", "Locale fallback", "Session timeout"]
NO_LANE = ["Rotate keys"]
TRACKER = TODAY_TRACKER.replace(
    "\n\n## Log", "\n| Rotate keys | Rotate the keys | Robin | open | 2026-09-26 |  | S |  |  |  | c |\n\n## Log")

REMOVED = ('id="due-next"', 'id="blocked"', 'id="sessions"', 'id="day-strip"', "<h2>Due next</h2>", "<h2>Blocked</h2>",
           "<h2>Sessions</h2>", "<h2>24 hours</h2>", "<h2>Lanes</h2>", "lane-table", 'class="lt-', "session-row")


@cache
def page() -> str:
    cfg = parse_coordinator(CLAUDE, today=TODAY)
    html = rb.render(TRACKER, cfg, NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN, stage_log=[(YESTERDAY, YESTERDAY_TRACKER)])
    return html.split("</style>", 1)[1]


def at(body: str, mark: str | re.Pattern) -> int:
    """Where `mark` first stands in `body`; a missing section is a failure that names it, not a ValueError."""
    m = re.search(mark, body, re.I) if isinstance(mark, str) else mark.search(body)
    if not m:
        raise AssertionError(f"the board does not draw {mark!r}")
    return m.start()


def build_region() -> str:
    """BUILD's drawing: from its heading to the panels."""
    body = page()
    return body[at(body, r"<h2>\s*Build\s*</h2>"):at(body, '<div class="panels">')]


class Board(unittest.TestCase):
    """A failure here names the section by a short message: the page is far too long to print."""

    @property
    def body(self) -> str:
        return page()

    def absent(self, mark: str, text: str) -> None:
        self.assertFalse(mark in text, f"the board draws {mark!r}, which the Flow artboard does not have")

    def seen(self, pattern: str, text: str, what: str) -> None:
        self.assertTrue(re.search(pattern, text, re.I | re.S), f"expected {what}: {pattern!r}")


class FlowDesign(Board):
    def test_the_sections_are_the_artboards_in_its_order(self) -> None:
        marks = ['<nav class="tabs"', '<header class="panels-head"', '<nav class="panels-tiles"', r"<h2>\s*Build\s*</h2>",
                 '<figure class="columns"', 'class="columns-foot"', '<div class="panels">',
                 '<section class="panels-panel" id="merge"', '<section class="panels-panel" id="decisions"',
                 '<section class="panels-panel" id="workers"', "<h2>Flow · 24 hours</h2>", "<h2>Flow · 7 days</h2>", 'class="gantt-legend"']
        found = [(at(self.body, m), m) for m in marks]
        self.assertEqual([m for _, m in found], [m for _, m in sorted(found)])

    def test_the_panels_are_merge_order_decisions_and_workers(self) -> None:
        titles = re.findall(r'<h3 class="panels-title">(?:<a [^>]*>)?([^<]+)', self.body)
        self.assertEqual(titles, ["MERGE ORDER", "DECISIONS", "WORKERS"])

    def test_no_section_the_artboard_lacks_is_drawn(self) -> None:
        for mark in REMOVED:
            with self.subTest(mark=mark):
                self.absent(mark, self.body)

    def test_only_the_two_flow_charts_draw_a_gantt(self) -> None:
        # The 24-hour day strip was a third `<figure class="gantt">`; the artboard has the 24 HOURS and 7 DAYS charts only.
        self.assertEqual(self.body.count('<figure class="gantt"'), 2)
        self.assertEqual(self.body.count('class="gantt-legend"'), 1)

    def test_id_flow_is_the_build_columns_the_tiles_link_to_not_a_table(self) -> None:
        self.assertEqual(self.body.count('id="flow"'), 1)
        flow = self.body.split('id="flow"', 1)[1].split('<div class="panels">', 1)[0]
        self.seen(r"<h2>\s*Build\s*</h2>", flow, "BUILD inside #flow")
        self.seen('<figure class="columns"', flow, "the stage columns inside #flow")
        self.absent("<table", flow)


class OneBuildDrawing(Board):
    @property
    def build(self) -> str:
        return build_region()

    @property
    def before(self) -> str:
        return self.body[:at(self.body, 'id="flow"')]

    def drawn(self, name: str, within: str) -> int:
        return len(re.findall(rf">\s*{re.escape(name)}\s*<", within))

    def test_each_task_is_drawn_once_between_the_tiles_and_the_panels(self) -> None:
        for name in LANED + NO_LANE:
            with self.subTest(task=name):
                self.assertEqual(self.drawn(name, self.build), 1)

    def test_no_task_is_drawn_above_build(self) -> None:
        # Due next and Blocked sat above BUILD and named tasks; the header and tiles carry counts only.
        for name in LANED + NO_LANE:
            with self.subTest(task=name):
                self.assertFalse(name in self.before, f"{name!r} is drawn above BUILD")

    def test_a_laned_task_is_a_stage_column_card_and_a_no_lane_task_is_in_the_strip(self) -> None:
        foot = self.build.split('class="columns-foot"', 1)[1]
        columns = self.build.split('class="columns-foot"', 1)[0]
        for name in LANED:
            with self.subTest(task=name):
                self.assertEqual((self.drawn(name, columns), self.drawn(name, foot)), (1, 0))
        for name in NO_LANE:
            with self.subTest(task=name):
                self.assertEqual((self.drawn(name, columns), self.drawn(name, foot)), (0, 1))

    def test_the_cards_are_the_laned_tasks_and_no_more(self) -> None:
        columns = self.build.split('class="columns-foot"', 1)[0]
        self.assertEqual(sorted(re.findall(r'<(?:a|div) class="columns-card-name"[^>]*>([^<]+)<', columns)), sorted(LANED))


class BuildColumns(Board):
    @property
    def build(self) -> str:
        return build_region()

    @property
    def cols(self) -> list[tuple[str, str]]:
        return re.findall(r'<section class="columns-col([^"]*)" aria-label="[^"]*">(.*?)</section>', self.build, re.S)

    def column(self, name: str) -> str:
        for _, inner in self.cols:
            if re.search(rf'<h3 class="columns-name">{name}</h3>', inner, re.I):
                return inner
        raise AssertionError(f"no {name} column")

    def test_build_is_headed_with_its_issue_and_merge_request_counts(self) -> None:
        # Artboard: `{{laneCount}} issues · {{mrCount}} merge requests`, always both.
        self.seen(r'<h2>\s*Build\s*</h2>\s*<div class="meta">4 issues · 0 merge requests</div>', self.build, "BUILD's counts")

    def test_a_column_per_lane_stage_with_the_gates_marked(self) -> None:
        # The pr stage has no column of its own (its cards draw in the stage after, review).
        names = [re.search(r'<h3 class="columns-name">([^<]*)<', inner).group(1) for _, inner in self.cols]
        self.assertEqual(names, ["implement", "review", "triage", "merge", "main"])
        self.assertEqual([("columns-gate" in kind) for kind, _ in self.cols], [False, False, True, True, False])

    def test_each_column_head_counts_its_cards(self) -> None:
        for kind, inner in self.cols:
            with self.subTest(column=re.search(r'columns-name">([^<]*)<', inner).group(1)):
                self.assertEqual(int(re.search(r'<span class="columns-count">(\d+)</span>', inner).group(1)),
                                 len(re.findall(r'<div class="columns-card(?: columns-flagged)?"', inner)))

    def test_a_card_has_name_refs_owner_and_state(self) -> None:
        card = self.column("review").split('<div class="columns-card', 1)[1]
        for pattern in (r'class="columns-card-name"[^>]*>Upload size limit<', r'<div class="columns-refs">.*#109',
                        r'class="columns-owner">impl-1<', r'<span class="columns-tag columns-chip"[^>]*>running 01:00</span>'):
            self.seen(pattern, card, "a card's name, refs, owner and state chip")

    def test_a_held_card_is_flagged_across_its_foot(self) -> None:
        card = self.column("triage")
        self.seen(r'class="columns-card columns-flagged".*>Cache warmup<.*columns-flag"', card, "a flagged Cache warmup card")

    def test_the_no_lane_strip_counts_what_the_header_does(self) -> None:
        self.seen(r'<span class="columns-foot-name">\s*no lane · 1\s*</span>', self.build, "the NO LANE · 1 strip")
        self.seen("1 in no lane", self.body, "the header's no-lane count")


if __name__ == "__main__":
    unittest.main()
