"""The Flow board's top: render_board maps the tracker and the board sources onto panels.py and columns.py."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "scripts"), str(HERE)]
import board_sources as bs  # noqa: E402
import columns  # noqa: E402
import decision_page  # noqa: E402
import panels  # noqa: E402
import render_board as rb  # noqa: E402
import settings as st  # noqa: E402
from test_render_board import CLAUDE_MD, NOW  # noqa: E402


def change(ref: str, *, state="open", draft=False, pipeline="passed", approved=False, merged_at=None,
           files=(), issues=(), title="") -> bs.Change:
    return bs.Change(ref, f"https://forge/{ref[1:]}", title or f"Change {ref}", state, draft, pipeline, approved,
                     int(approved), merged_at, "main", files, issues)


SOURCES = bs.Sources(
    fetched={"kanban": NOW - timedelta(minutes=2), "merge requests": NOW - timedelta(minutes=3), "workers": NOW},
    issues={"#9": bs.Issue("#9", "https://forge/i/9", "Cut", "open", ("stage::review",)),
            "#7": bs.Issue("#7", "https://forge/i/7", "README", "open", ("stage::implement",))},
    changes={c.ref: c for c in (
        change("!41", approved=True, files=("a.py",), issues=("#2",), title="Password reset copy"),
        change("!42", approved=True, files=("b.py", "c.py"), issues=("#3",), title="Health check timeout"),
        change("!48", files=("c.py",), issues=("#9",), title="Upload size limit"),
        change("!50", draft=True, files=("c.py",), issues=("#5",)),
        change("!52", approved=True, pipeline="failed", files=("d.py",), issues=("#6",), title="Login throttle"),
        change("!37", state="merged", merged_at=NOW - timedelta(hours=2), issues=("#8",), title="Locale fallback"),
        change("!36", state="merged", merged_at=NOW - timedelta(days=1), issues=("#1",)),
    )},
    workers=(bs.Worker("impl-07", "one-shot", "claude", "opus", "Cut the release", "trees/cut", NOW - timedelta(minutes=42), None),
             bs.Worker("impl-04", "session", "codex", "", "Audit", "", NOW - timedelta(hours=1, minutes=23), NOW - timedelta(minutes=11))),
    errors={},
)

TRACKER = """# Tracker 2026-09-16

Coordinator: coordinator. Board: board-7.

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Cut the release | Cut the release branch | impl-2 | running 10:30 | 10:30 | 23:00 | M | build | review | #9 | Checklist: cut |
| Write README | Write eval README | Robin | open | 09:00 | 17:00 | S | build | triage | https://forge/i/7 | Checklist: readme |
| Webhook check | Check webhook signature | unassigned | orphaned | 09:00 |  | S | build | implement |  | Checklist: hook |
| Console | Rotate the key in the console | Robin | open | 09:00 | 17:00 | S |  |  |  | Checklist: key |

## Decisions

| time | item | Robin's words |
|---|---|---|
| 11:15 | option A of decision-old | "A" |

## Sessions

## Log

- 09:00 opened the day
"""
LANES = {"build": st.Lane(("implement", "pr", "review", "triage", "merge"), ("triage", "merge"))}
KANBAN = st.Kanban(("stage::implement", "stage::review", "stage::triage"), "needs-human",
                   {"implement": 0, "pr": 0, "review": 1, "triage": 2, "merge": 2})


class MergeOrder(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = rb.merge_order(SOURCES)

    def test_approved_first_ready_before_failed_unshared_before_shared_then_in_review(self) -> None:
        self.assertEqual([(r.num, r.ref) for r in self.panel.rows], [("1", "!41"), ("2", "!42"), ("3", "!52"), ("4", "!48")])

    def test_drafts_and_closed_changes_are_not_in_the_order(self) -> None:
        self.assertNotIn("!50", [r.ref for r in self.panel.rows])
        self.assertNotIn("!37", [r.ref for r in self.panel.rows])

    def test_facts_are_approval_pipeline_base_and_shared_files(self) -> None:
        facts = {r.ref: r.facts for r in self.panel.rows}
        self.assertEqual(facts["!41"], "approved · pipeline passed · base main · no shared files")
        self.assertEqual(facts["!42"], "approved · pipeline passed · base main · shares 1 file with !48, !50")
        self.assertEqual(facts["!48"], "in review · pipeline passed · base main · shares 1 file with !42, !50")
        self.assertEqual(facts["!52"], "approved · pipeline failed · base main · no shared files")

    def test_head_counts(self) -> None:
        self.assertEqual((self.panel.id, self.panel.count, self.panel.note), ("merge", 4, "3 approved · 1 in review"))
        self.assertEqual((self.panel.rows[0].name, self.panel.rows[0].href), ("Password reset copy", "https://forge/41"))

    def test_the_order_shows_five_and_counts_the_rest(self) -> None:
        many = bs.Sources({}, {}, {f"!{n}": change(f"!{n}") for n in range(1, 9)}, (), {})
        p = rb.merge_order(many)
        self.assertEqual((p.count, len(p.rows), p.foot), (8, 5, "+ 3 more"))

    def test_empty_sources_leave_an_empty_order(self) -> None:
        p = rb.merge_order(bs.EMPTY)
        self.assertEqual((p.count, p.rows, p.note), (0, (), "0 approved · 0 in review"))


class Workers(unittest.TestCase):
    def test_one_row_per_worker_with_age_and_facts(self) -> None:
        p = rb.worker_panel(SOURCES, NOW)
        self.assertEqual((p.id, p.count, p.note), ("workers", 2, "1 one-shot · 1 session"))
        self.assertEqual([(r.name, r.side, r.facts) for r in p.rows], [
            ("impl-07", "0h42m", "one-shot · claude opus · Cut the release · trees/cut"),
            ("impl-04", "1h23m", "session · codex · Audit · last reply 14:19"),
        ])

    def test_empty(self) -> None:
        self.assertEqual(rb.worker_panel(bs.EMPTY, NOW).rows, ())


class Decisions(unittest.TestCase):
    def test_pending_rows_carry_asked_recommended_and_default(self) -> None:
        d = {"headline": "Cache warmup: keep the pool?", "recommended": "A", "default": "pool stays"}
        p = rb.decision_panel([("cache", d, NOW.replace(hour=1, minute=41), ""), ("bad", None, None, "no ask")], 4, NOW)
        self.assertEqual((p.id, p.count, p.foot, p.link), ("decisions", 1, "4 answered today", ("decisions page", "decisions.html")))
        self.assertEqual(p.rows[0], panels.Row("Cache warmup: keep the pool?", "asked 01:41 · recommended A · default: pool stays",
                                               href="decision-cache.html"))
        self.assertEqual(p.rows[1], panels.Row("decision-bad.json", "unreadable: no ask"))

    def test_the_pending_list_is_the_decisions_page_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pages = Path(tmp)
            ask = {"headline": "H", "ask": "A?", "options": [{"key": "A", "title": "a"}, {"key": "B", "title": "b"}],
                   "recommended": "A", "why": "w", "default": "d"}
            for slug in ("one", "two", "done"):
                (pages / f"decision-{slug}.json").write_text(json.dumps(ask))
            (pages / "decision-broken.json").write_text("{")
            ctx = decision_page.Context(NOW, rows=(decision_page.Row(NOW.date(), None, "option A of decision-done", ""),))
            found = decision_page.entries(pages, ctx)
            self.assertEqual(sorted(slug for slug, *_ in found), ["broken", "one", "two"])
            self.assertIsNone(next(d for slug, d, *_ in found if slug == "broken"))
            self.assertEqual(decision_page.index(pages, ctx, "2026-09-16")[1], found)


class Build(unittest.TestCase):
    def cols(self, sources=SOURCES, kanban=None):
        return rb.build_columns(rb.parse_tracker(TRACKER).tasks, LANES, kanban, sources, NOW)

    def test_a_card_gains_its_change_ref_and_marks(self) -> None:
        card = next(c for col in self.cols()[0] for c in col.cards if c.name == "Cut the release")
        self.assertEqual(card.refs, (("#9", "https://forge/i/9"), ("!48", "https://forge/48")))
        self.assertEqual(card.href, "https://forge/48")
        self.assertEqual(card.marks, (columns.Mark("passed", "passed"),))

    def test_approval_and_a_failed_pipeline_are_marks(self) -> None:
        self.assertEqual(rb.change_marks((SOURCES.changes["!52"],)),
                         (columns.Mark("failed", "failed"), columns.Mark("approved", "approved")))
        self.assertEqual(rb.change_marks((change("!1", pipeline="running"),)), (columns.Mark("running", "pending"),))

    def test_an_issue_url_matches_its_number(self) -> None:
        card = next(c for col in self.cols()[0] for c in col.cards if c.name == "Write README")
        self.assertEqual((card.refs, card.href), ((("#7", "https://forge/i/7"),), "https://forge/i/7"))

    def test_a_tracker_issue_url_links_without_sources(self) -> None:
        card = next(c for col in rb.build_columns(rb.parse_tracker(TRACKER).tasks, LANES)[0] for c in col.cards if c.name == "Write README")
        self.assertEqual((card.refs, card.href), ((("#7", "https://forge/i/7"),), "https://forge/i/7"))
        bare = next(c for col in self.cols()[0] for c in col.cards if c.name == "Webhook check")
        self.assertEqual((bare.refs, bare.href), ((), ""))

    def test_main_holds_the_changes_merged_today(self) -> None:
        main = self.cols()[0][-1]
        self.assertEqual((main.name, [(c.name, c.refs, c.href, c.owner, c.state, c.kind) for c in main.cards]),
                         ("main", [("Locale fallback", (("#8", ""), ("!37", "https://forge/37")), "https://forge/37",
                                    "merged 12:30", "done", "merged today")]))
        html = columns.render([columns.Column("main", main.cards * 5)])
        self.assertIn("+ <span>2 merged today</span>", html)

    def test_drift_marks_the_card(self) -> None:
        card = next(c for col in self.cols(kanban=KANBAN)[0] for c in col.cards if c.name == "Write README")
        self.assertIn(columns.Mark("drift", "drift"), card.marks)
        cut = next(c for col in self.cols(kanban=KANBAN)[0] for c in col.cards if c.name == "Cut the release")
        self.assertNotIn(columns.Mark("drift", "drift"), cut.marks)

    def test_empty_sources_draw_the_cards_as_before(self) -> None:
        tasks = rb.parse_tracker(TRACKER).tasks
        self.assertEqual(rb.build_columns(tasks, LANES, KANBAN, bs.EMPTY, NOW), rb.build_columns(tasks, LANES, KANBAN))
        self.assertEqual([c.name for c in rb.build_columns(tasks, LANES)[0]], ["implement", "review", "triage", "merge"])


class Page(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = rb.parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, self.cfg, NOW, lanes=LANES, kanban=KANBAN, sources=SOURCES,
                              decisions=[("x", {"headline": "Pick", "recommended": "B", "default": "d"}, NOW.replace(hour=10, minute=0), "")],
                              answered=1, tracker_at=NOW - timedelta(minutes=5))
        self.body = self.html.split("</style>", 1)[1]

    def test_the_board_is_the_flow_artboard_in_order(self) -> None:
        marks = ['class="panels-head"', 'class="panels-tiles"', '<section id="flow">', 'class="panels"']
        at = [self.body.index(m) for m in marks]
        self.assertEqual(at, sorted(at), [m for _, m in sorted(zip(at, marks))])

    def test_the_header_names_each_source_time_and_the_counts(self) -> None:
        self.assertIn("rendered 14:30 CDT · tracker 14:25 · kanban 14:28 · merge requests 14:27 · workers 14:30 · "
                      "4 tasks · 1 in no lane", self.body)

    def test_a_source_error_is_in_the_header(self) -> None:
        html = rb.render(TRACKER, self.cfg, NOW, lanes=LANES, sources=bs.Sources({}, {}, {}, (), {"workers": "no registry"}))
        self.assertIn('<div class="panels-warn">workers: no registry</div>', html)

    def test_tile_counts(self) -> None:
        tiles = dict(re.findall(r'<span class="panels-label">([A-Z]+)</span><b class="panels-count">(\d+)</b>', self.body))
        # blocked: Robin's two open tasks and the orphaned one; running: the two workers; drift: README at triage has stage::implement.
        self.assertEqual(tiles, {"BLOCKED": "3", "DECISIONS": "1", "APPROVED": "3", "RUNNING": "2", "DRIFT": "1", "ORPHANED": "1"})

    def test_tiles_link_to_their_sections(self) -> None:
        for anchor in re.findall(r'class="panels-tile" data-kind="[a-z]+" href="#([a-z]+)"', self.body):
            self.assertIn(f'id="{anchor}"', self.body)

    def test_empty_sources_render_and_running_falls_back_to_tasks(self) -> None:
        html = rb.render(TRACKER, self.cfg, NOW, lanes=LANES)
        tiles = dict(re.findall(r'<span class="panels-label">([A-Z]+)</span><b class="panels-count">(\d+)</b>', html))
        self.assertEqual(tiles, {"BLOCKED": "3", "DECISIONS": "0", "APPROVED": "0", "RUNNING": "1", "DRIFT": "0", "ORPHANED": "1"})
        self.assertIn("rendered 14:30 CDT · tracker 14:30 · 4 tasks", html)

    def test_panel_and_tile_colours_are_board_tokens_light_and_dark(self) -> None:
        css = self.html.split("<style>", 1)[1].split("</style>", 1)[0]
        ours = [line for line in css.splitlines() if ".panels" in line and not line.startswith(".panels-head{display")]
        overrides = "\n".join(line for line in ours if "--panels-" in line or "{--k:" in line)
        self.assertTrue(overrides)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", overrides), [])
        self.assertIn(".panels-head,.panels-tiles,.panels{--panels-ink:var(--fg)", css)
        self.assertIn('[data-kind="merge"]{--k:var(--stage-review)}', css)

    def test_the_clocks_tick_every_second_with_seconds(self) -> None:
        script = self.html.split("<script>", 1)[1]
        self.assertIn("setInterval(tick,1000)", script)
        self.assertIn("second:'2-digit'", script)
        self.assertIn("+'s'", script)

    def test_build_cards_link_to_their_items(self) -> None:
        self.assertIn('<a class="columns-card-name" href="https://forge/48">Cut the release</a>', self.body)
        self.assertIn('<a href="https://forge/i/9">#9</a>', self.body)
        self.assertNotIn('href="#"', self.body)

    def test_the_page_fits_a_phone(self) -> None:
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', self.html)


if __name__ == "__main__":
    unittest.main()
