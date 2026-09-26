"""The column board lays out cards under stage heads without knowing what a stage or a card means;
render_board adapts lanes and tasks to it."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import columns  # noqa: E402
import render_board as rb  # noqa: E402
import settings as st  # noqa: E402

C = columns.Card


def col(name: str, n: int, **kw) -> columns.Column:
    return columns.Column(name, tuple(C(f"{name} {i}", "open") for i in range(n)), **kw)


class Render(unittest.TestCase):
    def test_one_section_per_column_in_order_with_its_count(self) -> None:
        html = columns.render([col("implement", 2), col("review", 0)])
        self.assertEqual(re.findall(r'<h3 class="columns-name">([^<]*)</h3>', html), ["implement", "review"])
        self.assertEqual(re.findall(r'<span class="columns-count">(\d+)</span>', html), ["2", "0"])

    def test_a_card_carries_name_refs_owner_and_a_chip(self) -> None:
        card = C("Rate limit headers", "running", refs=(("#118", "https://forge/i/118"), ("!54", "")), owner="worker-07",
                 state="running 01:28", href="https://forge/i/118")
        html = columns.render([columns.Column("implement", (card,))])
        for part in ('<a class="columns-card-name" href="https://forge/i/118">Rate limit headers</a>',
                     '<div class="columns-refs"><a href="https://forge/i/118">#118</a><span>!54</span></div>',
                     '<span class="columns-owner">worker-07</span>',
                     '<span class="columns-tag columns-chip" data-cat="running">running 01:28</span>'):
            self.assertIn(part, html)

    def test_an_unlinked_card_never_links_to_a_placeholder(self) -> None:
        html = columns.render([columns.Column("implement", (C("Plain", "open", refs=(("#1", ""),)),))])
        self.assertIn('<div class="columns-card-name">Plain</div>', html)
        self.assertNotIn("href", html)

    def test_marks_draw_only_when_there_are_some(self) -> None:
        held = C("a", "waiting", marks=(columns.Mark("hold", "hold"),))
        html = columns.render([columns.Column("pr", (held, C("b", "open")))])
        self.assertEqual(html.count('class="columns-marks"'), 1)
        self.assertIn('<span class="columns-tag columns-mark" data-cat="hold">hold</span>', html)

    def test_a_flag_outlines_the_card_and_bars_its_foot_in_its_colour(self) -> None:
        held = C("a", "waiting", flag=columns.Mark("on hold", "hold"))
        html = columns.render([columns.Column("triage", (held, C("b", "open")))])
        self.assertEqual(html.count("columns-flagged"), 1)
        self.assertIn('<div class="columns-card columns-flagged" data-cat="hold">', html)
        self.assertTrue(html.split('data-cat="hold">', 1)[1].split("</div></div>")[0].endswith(
            '<div class="columns-tag columns-flag" data-cat="hold">on hold'), "the bar is the card's last line")
        sheet = columns.css()
        self.assertIn('.columns-card[data-cat="hold"]', sheet)
        self.assertIn(".columns-flagged{", sheet)

    def test_a_gate_column_is_a_class_and_its_word(self) -> None:
        html = columns.render([col("triage", 1, note="gate", kind="gate")])
        self.assertIn('<section class="columns-col columns-gate"', html)
        self.assertIn('<span class="columns-note">gate</span>', html)

    def test_overflow_folds_past_the_limit_and_tallies_what_it_hides(self) -> None:
        cards = (C("a", "running"), C("b", "running"), C("c", "open"), C("d", "open"), C("e", "done"))
        html = columns.render([columns.Column("implement", cards)], limit=2)
        shown, more = html.split('<details class="columns-more">')
        self.assertEqual(shown.count('class="columns-card"'), 2)
        self.assertIn("<summary>+ <span>2 open</span><span>1 done</span></summary>", more)
        self.assertEqual(more.count('class="columns-card"'), 3, "the folded cards are one click away, not dropped")
        self.assertNotIn("columns-more", columns.render([columns.Column("implement", cards)], limit=5))

    def test_the_footer_strip_tallies_its_cards(self) -> None:
        foot = columns.Column("no lane", (C("a", "open"), C("b", "waiting"), C("c", "open")))
        html = columns.render([col("implement", 1)], footer=foot)
        self.assertIn('<summary><span class="columns-foot-name">no lane · 3</span><span>2 open</span><span>1 waiting</span></summary>', html)
        self.assertNotIn("columns-foot", columns.render([col("implement", 1)]))

    def test_text_is_escaped(self) -> None:
        html = columns.render([columns.Column("<b>", (C("<script>", "open", owner='"x"'),))])
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>", html)

    def test_a_kind_must_be_a_class_name(self) -> None:
        with self.assertRaises(ValueError):
            columns.render([col("x", 0, kind='gate" onclick="x')])


class Css(unittest.TestCase):
    def test_one_rule_per_palette_entry(self) -> None:
        css = columns.css({"running": ("#0072B2", "#fbf8ef", "1px solid #0072B2")})
        self.assertIn('.columns-tag[data-cat="running"],.columns-card[data-cat="running"]{--c:#0072B2;--i:#fbf8ef;--e:1px solid #0072B2}', css)

    def test_the_default_palette_covers_every_tracker_state_and_the_hold(self) -> None:
        self.assertLessEqual({"open", "running", "waiting", "orphaned", "done", "hold"}, set(columns.PALETTE))

    def test_a_value_cannot_escape_its_declaration(self) -> None:
        with self.assertRaises(ValueError):
            columns.css({"x": ("red;}body{display:none", "#000", "0")})
        self.assertNotIn("</style>", columns.css({'a"</style>': ("#000", "#fff", "0")}))


TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Rate limit headers | Add headers | worker-07 | running 01:28 | 01:28 |  | M | build | implement | #118 | Checklist: a |
| Session timeout | Pick a timeout | Robin | waiting | 00:52 |  | S | build | triage | #107 | Checklist: b |
| Locale fallback | Fallback | Robin | done 00:10–00:41 | 00:10 |  | S | build | main | #98 | Checklist: c |
| Rotate key | Rotate the key | Robin | open | 09:00 |  | S |  |  |  | Checklist: d |
| Book room | Book the room | Robin | done | 09:00 |  | S |  |  |  | Checklist: e |
"""


class Adapter(unittest.TestCase):
    LANES = {"build": st.Lane(("implement", "pr", "review", "triage", "merge", "main"), ("triage", "merge"))}

    def build(self, lanes=None, kanban=None):
        return rb.build_columns(rb.parse_tracker(TRACKER).tasks, self.LANES if lanes is None else lanes, kanban)

    def test_every_lane_stage_is_a_column_even_an_empty_one(self) -> None:
        cols, _ = self.build()
        self.assertEqual([c.name for c in cols], ["implement", "review", "triage", "merge", "main"])
        self.assertEqual([len(c.cards) for c in cols], [1, 0, 1, 0, 1])

    def test_a_card_at_pr_draws_in_the_next_stage_column(self) -> None:
        tasks = [rb.Task("x", "w", "open", "", "", "", name="Open the PR", lane="build", stage="pr")]
        cols, _ = rb.build_columns(tasks, self.LANES)
        self.assertNotIn("pr", [c.name for c in cols])
        self.assertEqual([c.name for c in next(c for c in cols if c.name == "review").cards], ["Open the PR"])

    def test_a_card_is_the_task_in_tracker_words(self) -> None:
        card = self.build()[0][0].cards[0]
        self.assertEqual((card.name, card.kind, card.refs, card.owner, card.state),
                         ("Rate limit headers", "running", (("#118", ""),), "worker-07", "running 01:28"))

    def test_gates_come_from_the_lanes(self) -> None:
        cols, _ = self.build()
        self.assertEqual([c.name for c in cols if c.kind == "gate"], ["triage", "merge"])
        self.assertEqual({c.note for c in cols if c.kind == "gate"}, {"gate"})

    def test_tasks_with_no_lane_go_to_the_footer(self) -> None:
        _, foot = self.build()
        self.assertEqual((foot.name, [c.name for c in foot.cards]), ("no lane", ["Rotate key", "Book room"]))

    def test_a_kanban_hold_stage_flags_its_cards_on_hold(self) -> None:
        kanban = st.Kanban(("todo", "doing"), "needs-human", {"implement": 1, "triage": 1, "main": 1}, hold_stages=("triage",))
        held = [(c.name, c.flag, c.marks) for col in self.build(kanban=kanban)[0] for c in col.cards if c.flag]
        self.assertEqual(held, [("Session timeout", columns.Mark("ON HOLD", "hold"), ())])

    def test_lanes_merge_in_order_and_disjoint_lanes_stay_whole(self) -> None:
        lanes = {"code": st.Lane(("implement", "review", "merge")), "research": st.Lane(("implement", "triage", "review", "merge")),
                 "ops": st.Lane(("ask", "do"))}
        self.assertEqual(rb.stage_order(lanes), ["implement", "triage", "review", "merge", "ask", "do"])

    def test_a_stage_no_lane_names_still_gets_a_column(self) -> None:
        self.assertEqual([c.name for c in self.build(lanes={})[0]], ["implement", "triage", "main"])


if __name__ == "__main__":
    unittest.main()
