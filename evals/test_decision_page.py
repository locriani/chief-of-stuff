"""decision_page.py: a decision's full context as a page on the local pages server (Zach, 2026-09-25: "make sure
in the future you don't serve it as an artifact but serve it using the localhost server thing")."""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import board_sources as bs  # noqa: E402
import decision_page as dp  # noqa: E402
import render_board as rb  # noqa: E402
import gantt  # noqa: E402
from backlog import Backlog, GitHubBacklog  # noqa: E402

BLOCK = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md` (sections: Tasks, Decisions, File ownership, Log)
- Timezone: America/Chicago
{board}
"""
BOARD = "- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`"


def decision(**over) -> dict:
    d = {
        "topic": "Storage",
        "headline": "The plan and the architecture disagree on where uploads go",
        "ask": "Should the worker follow the architecture?",
        "yes": "A yes tells impl-uploads to use the volume service.",
        "sections": [{"heading": "What the plan says", "text": ["Issue #7 writes to `s3://bucket` <now>."]}],
        "sides": [{"label": "Plan", "status": "open", "text": "S3."}, {"label": "Architecture", "status": "done", "text": "Volume."}],
        "timeline": [{"when": "09:40", "text": "impl-uploads stopped."}],
        "options": [{"key": "A", "title": "Follow the architecture", "text": "The bucket is dead."},
                    {"key": "B", "title": "Follow the plan", "text": "**Breaks** the architecture."}],
        "recommended": "A",
        "why": "The architecture is the newer decision.",
        "default": "The worker waits.",
        "references": [{"label": "Plan", "value": "https://github.com/o/backlog/issues/7"}],
    }
    d.update(over)
    return d


class RenderTest(unittest.TestCase):
    def test_a_full_document_holding_every_part(self):
        page = dp.render(dp.parse(decision()), "2026-09-25")
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn('<meta name="viewport"', page)
        for text in ("Decision · Storage · Fri 25 Sep", "Should the worker follow the architecture?",
                     "What the plan says", "Where it stands", "How it got here", "Options",
                     "Recommendation", "The architecture is the newer decision.", "If you don't answer",
                     "The worker waits.", "References", "Answer here or in chat: A, B, or your own words."):
            self.assertIn(text, page)

    def test_text_is_escaped_and_marked_up(self):
        page = dp.render(dp.parse(decision()), "2026-09-25")
        self.assertIn("<code>s3://bucket</code> &lt;now&gt;", page)
        self.assertIn("<b>Breaks</b>", page)
        self.assertIn('<a href="https://github.com/o/backlog/issues/7">https://github.com/o/backlog/issues/7</a>', page)
        self.assertNotIn("<now>", page)

    def test_no_option_is_selected_until_the_user_selects_one(self):
        # The user, 2026-09-26: "I want the decisions page to NOT have something selected until I actually select it
        # for options, and to always have a write in option." Recommended is a text tag, not a filled or bold row.
        page = dp.render(dp.parse(decision()), "2026-09-25", slug="uploads")
        self.assertIn('<form class="options" method="post" action="/decisions/uploads">', page)
        self.assertIn('<label class="opt"><span class="k"><input type="radio" name="key" value="A"> A</span>'
                      '<b>Follow the architecture<span class="tag">recommended</span></b>', page)
        self.assertIn('<input type="radio" name="key" value="B"> B</span><b>Follow the plan</b>', page)
        self.assertIn('<input type="text" name="words"', page)
        self.assertIn('<button type="submit">save</button>', page)
        self.assertNotIn("checked", page)
        self.assertNotIn("opt rec", page)
        self.assertNotRegex(page, r"<script[^>]* src=")

    def test_a_saved_page_answer_shows_the_decision_answered(self):
        saved = {"key": "B", "words": "B, and log it", "at": "2026-09-25T02:08:00-05:00"}
        page = dp.render(dp.parse(decision(answer=saved)), "2026-09-25", slug="uploads")
        self.assertIn(">ANSWERED · B<", page)
        self.assertIn("answered, saved 02:08", page)
        self.assertIn("“B, and log it”", page)
        self.assertIn('<input type="radio" name="key" value="B" checked>', page)
        written = dp.render(dp.parse(decision(answer={"key": None, "words": "neither; ask the owner", "at": saved["at"]})),
                            "2026-09-25", slug="uploads")
        self.assertIn(">ANSWERED<", written)
        self.assertIn("“neither; ask the owner”", written)
        self.assertNotIn("checked", written)

    def test_a_saved_answer_is_checked(self):
        for bad, word in (({"key": "Z", "words": "", "at": "2026-09-25T02:08:00-05:00"}, "Z"),
                          ({"key": None, "words": " ", "at": "2026-09-25T02:08:00-05:00"}, "words"),
                          ({"key": "A", "words": ""}, "at")):
            with self.subTest(word=word), self.assertRaisesRegex(dp.DecisionError, word):
                dp.parse(decision(answer=bad))

    def test_a_section_may_be_one_paragraph(self):
        page = dp.render(dp.parse(decision(sections=[{"heading": "What happened", "text": "It stopped."}])), "2026-09-25")
        self.assertIn("<p>It stopped.</p>", page)

    def test_optional_parts_are_left_out(self):
        d = decision()
        for key in ("sections", "sides", "timeline", "references", "topic", "yes"):
            del d[key]
        page = dp.render(dp.parse(d), "2026-09-25")
        self.assertNotIn("Where it stands", page)
        self.assertNotIn("How it got here", page)
        self.assertNotIn("References", page)


class ParseTest(unittest.TestCase):
    def test_a_decision_missing_what_the_user_needs_is_refused(self):
        for key in ("headline", "ask", "options", "recommended", "why", "default"):
            d = decision()
            del d[key]
            with self.subTest(key=key), self.assertRaisesRegex(dp.DecisionError, key):
                dp.parse(d)

    def test_one_option_is_not_a_decision(self):
        with self.assertRaisesRegex(dp.DecisionError, "two options"):
            dp.parse(decision(options=decision()["options"][:1]))

    def test_the_recommendation_names_an_option(self):
        with self.assertRaisesRegex(dp.DecisionError, "recommended"):
            dp.parse(decision(recommended="C"))

    def test_a_side_is_open_or_done(self):
        with self.assertRaisesRegex(dp.DecisionError, "status"):
            dp.parse(decision(sides=[{"label": "x", "status": "maybe", "text": "y"}]))


GH = GitHubBacklog(repo="o/app")
GL = Backlog(host="https://gl.example", project="grp/app")


def refs(text: str, forge=None, root=None, at=None) -> str:
    return dp._md(text, dp.Linker(forge, root, at))


class LinkTest(unittest.TestCase):
    """References in the text link to the configured forge; nothing is linked that the forge cannot place."""

    def test_github(self):
        out = refs("#12, !58, a41c9e2, src/cache/pool.py:24-58, config/db.toml:14, pipeline #8812, src/gateway/ "
                   "and docs/architecture.md#connection-budgets", GH)
        for kind, href, label in (
                ("issue", "https://github.com/o/app/issues/12", "#12"),
                ("PR", "https://github.com/o/app/pull/58", "!58"),
                ("commit", "https://github.com/o/app/commit/a41c9e2", "a41c9e2"),
                ("code", "https://github.com/o/app/blob/HEAD/src/cache/pool.py#L24-L58", "src/cache/pool.py:24-58"),
                ("code", "https://github.com/o/app/blob/HEAD/config/db.toml#L14", "config/db.toml:14"),
                ("pipeline", "https://github.com/o/app/actions/runs/8812", "#8812"),
                ("code", "https://github.com/o/app/tree/HEAD/src/gateway/", "src/gateway/"),
                ("doc", "https://github.com/o/app/blob/HEAD/docs/architecture.md#connection-budgets",
                 "docs/architecture.md#connection-budgets")):
            with self.subTest(label=label):
                self.assertIn(f'<a class="ref" data-k="{kind}" href="{href}">{label}</a>', out)

    def test_gitlab(self):
        out = refs("#12, !58, !58#note_2291, a41c9e2, src/cache/pool.py:24-58, pipeline #8812", GL)
        for kind, href in (("issue", "https://gl.example/grp/app/-/issues/12"),
                           ("MR", "https://gl.example/grp/app/-/merge_requests/58"),
                           ("note", "https://gl.example/grp/app/-/merge_requests/58#note_2291"),
                           ("commit", "https://gl.example/grp/app/-/commit/a41c9e2"),
                           ("code", "https://gl.example/grp/app/-/blob/HEAD/src/cache/pool.py#L24-58"),
                           ("pipeline", "https://gl.example/grp/app/-/pipelines/8812")):
            with self.subTest(kind=kind):
                self.assertIn(f'data-k="{kind}" href="{href}"', out)

    def test_no_forge_links_nothing(self):
        self.assertNotIn("<a", refs("#12, !58, a41c9e2, src/cache/pool.py:24, pipeline #8812"))

    def test_what_is_not_a_reference_stays_text(self):
        text = "colour #c9533a, e.g. this, on 2026-09-25, a defaced cafe, o/backlog#7, v1.2 and 3.5 hours"
        self.assertEqual(refs(text, GH), text)

    def test_a_code_span_that_is_one_reference_links(self):
        self.assertEqual(refs("`src/app.py:3`", GH),
                         '<a class="ref" data-k="code" href="https://github.com/o/app/blob/HEAD/src/app.py#L3">src/app.py:3</a>')
        self.assertEqual(refs("`s3://bucket` and `make test`", GH), "<code>s3://bucket</code> and <code>make test</code>")

    def test_sections_and_symbols_resolve_against_a_local_copy(self):
        root = Path(tempfile.mkdtemp())
        (root / "docs").mkdir()
        (root / "docs" / "architecture.md").write_text("# Arch\n\n## 4.1 Queues\n\n## 4.2 Connection budgets\n")
        (root / "src").mkdir()
        (root / "src" / "pool.py").write_text("import os\n\n\ndef warm(n):\n    pass\n")
        self.assertIn('href="https://github.com/o/app/blob/HEAD/docs/architecture.md#42-connection-budgets">'
                      'docs/architecture.md §4.2</a>', refs("docs/architecture.md §4.2", GH, root))
        self.assertIn('href="https://github.com/o/app/blob/HEAD/src/pool.py#L4">src/pool.py:warm</a>',
                      refs("src/pool.py:warm", GH, root))
        # Without the copy the file is still the right place; the section is not guessed.
        self.assertIn('href="https://github.com/o/app/blob/HEAD/docs/architecture.md">docs/architecture.md §4.2</a>',
                      refs("docs/architecture.md §4.2", GH))

    def test_a_pinned_reference_links_at_its_commit(self):
        self.assertIn("https://github.com/o/app/blob/a41c9e2/src/cache/pool.py#L24-L58",
                      refs("src/cache/pool.py:24-58", GH, at="a41c9e2"))


ARCH = {
    "summary": "Requests reach the cache, which the warm pool keeps connected to the database.",
    "nodes": [
        {"id": "clients", "name": "Clients", "state": "external", "detail": "browser, mobile"},
        {"id": "cache", "name": "Cache", "state": "deployed", "detail": "src/cache/client.py"},
        {"id": "db", "name": "Database", "state": "external", "detail": "limit 320 connections", "hot": True},
        {"id": "pool", "name": "Warm pool", "state": "inflight", "detail": "src/cache/pool.py"},
        {"id": "budget", "name": "Connection budget", "state": "designed", "detail": "docs/architecture.md §4.2"},
    ],
    "edges": [
        {"from": "clients", "to": "cache", "label": "get / set"},
        {"from": "cache", "to": "db", "label": "miss: query"},
        {"from": "pool", "to": "db", "label": "holds 20 open", "state": "inflight"},
        {"from": "pool", "to": "cache", "label": "warms", "state": "inflight"},
        {"from": "budget", "to": "pool", "label": "caps", "state": "designed"},
    ],
    "not_drawn": "auth, the job queue.",
}


class DesignTest(unittest.TestCase):
    def page(self, forge=None, **over) -> str:
        return dp.render(dp.parse(decision(**over)), "2026-09-25", forge)

    def test_where_it_sits_draws_the_architecture_in_the_review_encoding(self):
        page = self.page(GH, architecture=ARCH)
        self.assertIn("Where it sits", page)
        self.assertIn(f'aria-label="{ARCH["summary"]}"', page)
        for cls in ('class="ext"', 'class="n"', 'class="n-inflight"', 'class="n-designed"', 'class="ext hot"',
                    'class="e"', 'class="e-inflight"', 'class="e-designed"'):
            self.assertIn(cls, page)
        for word in ("get / set", "holds 20 open", "Warm pool", "Not drawn: auth, the job queue."):
            self.assertIn(word, page)
        self.assertIn('<a href="https://github.com/o/app/blob/HEAD/src/cache/pool.py">', page)
        for chip in ("DEPLOYED", "IN FLIGHT", "DESIGNED", "EXTERNAL", "WAITING ON THIS"):
            self.assertIn(chip, page)

    def test_every_edge_of_an_acyclic_figure_points_right(self):
        page = self.page(architecture=ARCH)
        x = {m[1]: float(m[2]) for m in re.finditer(r'<g data-node="([^"]+)"><rect[^>]* x="([\d.]+)"', page)}
        self.assertEqual(set(x), {n["id"] for n in ARCH["nodes"]})
        for e in ARCH["edges"]:
            with self.subTest(edge=e["label"]):
                self.assertLess(x[e["from"]], x[e["to"]])

    def test_an_architecture_is_checked(self):
        for arch, word in (({"nodes": [{"id": "a", "name": "A", "state": "shipped"}], "edges": []}, "state"),
                           ({"nodes": [{"id": "a", "name": "A"}], "edges": [{"from": "a", "to": "z"}]}, "z")):
            with self.subTest(word=word), self.assertRaisesRegex(dp.DecisionError, word):
                dp.parse(decision(architecture=arch))

    def test_how_it_got_here_colours_each_step(self):
        page = self.page(GH, timeline=[{"when": "Fri 16:20", "text": "Deploy timed out.", "kind": "hot"},
                                       {"when": "01:41", "text": "Held in triage. #111", "kind": "triage"},
                                       {"when": "02:00", "text": "No kind."}])
        self.assertIn('<span class="dot" data-k="hot"></span>', page)
        self.assertIn('<span class="dot" data-k="triage"></span>', page)
        self.assertIn('<span class="dot"></span>', page)
        self.assertIn('href="https://github.com/o/app/issues/111"', page)

    def test_references_carry_kind_status_and_pin(self):
        page = self.page(GH, references=[{"kind": "code", "value": "src/cache/pool.py:24-58", "at": "a41c9e2"},
                                         {"kind": "MR", "value": "!58", "label": "Warm the cache pool", "status": "open · passed"},
                                         {"label": "Plan", "value": "https://github.com/o/backlog/issues/7"}])
        self.assertIn("blob/a41c9e2/src/cache/pool.py#L24-L58", page)
        self.assertIn("@ a41c9e2", page)
        self.assertIn("Warm the cache pool", page)
        self.assertIn("open · passed", page)
        self.assertRegex(page, r'<span class="cap">Plan</span>')

    def test_the_chip_says_pending_until_the_answer_is_recorded(self):
        self.assertIn(">PENDING<", self.page())
        self.assertIn(">ANSWERED · B<", self.page(answer="B"))
        with self.assertRaisesRegex(dp.DecisionError, "answer"):
            dp.parse(decision(answer="Z"))

    def test_the_page_wears_the_board_scheme(self):
        page = self.page()
        self.assertIn(rb.FONTS, page)
        board, mine = scheme(board_css()), scheme(page)
        for theme in ("light", "dark"):
            for token in SHARED:
                with self.subTest(theme=theme, token=token):
                    self.assertEqual(mine[theme][token], board[theme][token])

    def test_a_node_name_is_never_stroked(self):
        page = self.page(architecture=ARCH)
        stroked = {"n", "n-inflight", "n-designed", "ext"}
        for classes in re.findall(r'<text class="([^"]*)"', page):
            with self.subTest(classes=classes):
                self.assertFalse(stroked & set(classes.split()))


TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 26, 2, 10, tzinfo=TZ)


def at(hhmm: str, day: int = 26) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def row(hhmm: str, item: str, words: str, day: int = 26) -> dp.Row:
    return dp.Row(date(2026, 9, day), at(hhmm, day), item, words)


def task(name: str, item: str, stage: str = "", issue: str = "") -> rb.Task:
    return rb.Task(item=item, owner="impl", state="running 01:00", since="01:00", due="", checklist="", name=name,
                   issue=issue, lane="build" if stage else "", stage=stage)


def change(ref: str, state: str = "open", pipeline: str | None = "passed", merged: datetime | None = None,
           issues: tuple[str, ...] = ()) -> bs.Change:
    return bs.Change(ref, f"https://x/{ref}", "t", state, False, pipeline, True, 1, merged, "main", (), issues)


def issue(ref: str, state: str = "open", labels: tuple[str, ...] = ()) -> bs.Issue:
    return bs.Issue(ref, f"https://x/{ref}", "t", state, labels)


SOURCES = bs.Sources({}, {"#111": issue("#111", labels=("status::hold",)), "#96": issue("#96", "closed")},
                     {"!58": change("!58"), "!52": change("!52", pipeline="failed"),
                      "!60": change("!60", "merged", merged=at("01:15"), issues=("#99",)),
                      "!61": change("!61", pipeline="running", issues=("#109",))}, (), {})


class DerivationTest(unittest.TestCase):
    """Every field on the decisions pages comes from a source: the JSON, the tracker, or the forge."""

    def ctx(self, **over) -> dp.Context:
        return dp.Context(**{"now": NOW, **over})

    def test_asked_is_the_json_then_the_log_line_naming_the_page_then_the_file(self):
        mtime = at("00:30").timestamp()
        log = ((at("23:50", 25), "asked Robin about the cache"), (at("01:41"), "asked decision-cache-warmup"),
               (at("01:50"), "again decision-cache-warmup"))
        ctx = self.ctx(log=log)
        self.assertEqual(ctx.asked("cache-warmup", {"asked": "01:05"}, mtime), at("01:05"))
        self.assertEqual(ctx.asked("cache-warmup", {}, mtime), at("01:41"))
        self.assertEqual(ctx.asked("other", {}, mtime), at("00:30"))
        self.assertIsNone(ctx.asked("other", {}, None))

    def test_the_answer_is_the_latest_decisions_row_naming_the_page(self):
        ctx = self.ctx(rows=(row("00:10", "option A of decision-cache-warmup-v2", '"A"'),
                             row("00:20", "option B of decision-cache-warmup", '"B, smaller"'),
                             row("00:40", "decision-cache-warmup: changed to C", '"make it C"')))
        self.assertEqual(ctx.answer("cache-warmup").words, '"make it C"')
        self.assertIsNone(ctx.answer("cache"))

    def test_the_key_is_the_option_the_words_name(self):
        ctx = self.ctx()
        keys = ["A", "B", "C"]
        self.assertEqual(ctx.key(row("01:00", "decision-x", '"B, go"'), keys), "B")
        self.assertEqual(ctx.key(row("01:00", "decision-x", '"go with C please"'), keys), "C")
        self.assertEqual(ctx.key(row("01:00", "option A of decision-x", '"yes"'), keys), "A")
        self.assertEqual(ctx.key(row("01:00", "decision-x", '"a fine idea"'), keys), "")

    def test_holding_is_the_json_refs_and_the_tasks_that_name_the_page(self):
        d = decision(holds=["#111"], references=[{"kind": "MR", "value": "!58"}, {"kind": "code", "value": "src/a.py:3"},
                                                 {"kind": "issue", "value": "https://github.com/o/app/issues/111"}])
        tasks = (task("Cache", "Warm pool; waits on decision-cache-warmup. https://github.com/o/app/pull/61", "review", "#110"),
                 task("Other", "Unrelated #5", "fix", "#5"))
        self.assertEqual(self.ctx(tasks=tasks).holding("cache-warmup", d), ["#111", "!58", "#110", "#61"])
        self.assertEqual(self.ctx().holding("cache-warmup", decision()), ["#7"])  # the plan's issue URL
        self.assertEqual(self.ctx().holding("cache-warmup", decision(references=[])), [])

    def test_where_a_ref_stands_joins_the_tracker_stage_and_the_forge(self):
        tasks = (task("Budget", "Connection budget", "triage", "#111"), task("Pool", "Warm pool !58", "merge"),
                 task("Upload", "Upload limit", "review", "#109"))
        ctx = self.ctx(tasks=tasks, sources=SOURCES, hold="status::hold")
        self.assertEqual(ctx.where("#111"), "triage · hold")
        self.assertEqual(ctx.where("!58"), "merge · passed")
        self.assertEqual(ctx.where("!52"), "open · failed")
        self.assertEqual(ctx.where("#99"), "merged 01:15")
        self.assertEqual(ctx.where("#109"), "review · running")
        self.assertEqual(ctx.where("#96"), "closed")
        # With no sources the tracker alone speaks, and a ref it does not hold says nothing.
        self.assertEqual(self.ctx(tasks=tasks).where("#111"), "triage")
        self.assertEqual(self.ctx().where("#7"), "")

    def test_a_github_pr_written_with_a_bang_finds_its_change(self):
        ctx = self.ctx(sources=bs.Sources({}, {}, {"#58": change("#58")}, (), {}))
        self.assertEqual(ctx.where("!58"), "open · passed")

    def test_spans_read_as_the_design_writes_them(self):
        for minutes, text in ((29, "29m"), (78, "1h 18m"), (240, "4h"), (125, "2h 05m"), (1500, "1d 1h")):
            with self.subTest(minutes=minutes):
                self.assertEqual(dp.span(timedelta(minutes=minutes)), text)


def pages_dir(**decisions) -> Path:
    pages = Path(tempfile.mkdtemp())
    for n, (slug, d) in enumerate(decisions.items()):
        (pages / f"decision-{slug}.json").write_text(d if isinstance(d, str) else json.dumps(d))
        os.utime(pages / f"decision-{slug}.json", (at("00:00").timestamp() + 60 * n,) * 2)
    return pages


class IndexTest(unittest.TestCase):
    """decisions.html is a list of pending and completed decisions (Decisions.dc.html)."""

    def render(self, ctx: dp.Context, **decisions) -> str:
        return dp.index(pages_dir(**decisions), ctx, "2026-09-26")[0]

    def test_a_pending_row_carries_every_column(self):
        warm = decision(topic="cache", headline="Cache warmup", ask="Keep the pool?", holds=["#111"], references=[{"kind": "MR", "value": "!58"}],
                        options=[{"key": "A", "title": "keep the pool"}, {"key": "B", "title": "start cold"}],
                        default="The pool stays.")
        ctx = dp.Context(NOW, log=((at("01:41"), "asked decision-cache-warmup"),),
                         tasks=(task("Budget", "Connection budget", "triage", "#111"), task("Pool", "Warm pool !58", "merge")),
                         sources=SOURCES, hold="status::hold")
        page = self.render(ctx, **{"cache-warmup": warm})
        pending = page.split("COMPLETED")[0]
        self.assertIn("<h1>Decisions · Sat 26 Sep</h1>", page)
        for text in ('<span class="when">01:41</span>', '<span class="age">29m</span>', '<a class="headline" href="/decisions/cache-warmup">Cache warmup</a>',
                     '<span class="topic">cache</span>', "Keep the pool?",
                     '<label class="o"><input type="radio" name="key" value="A"><span class="key">A</span>keep the pool<span class="tag">recommended</span></label>',
                     '<label class="o"><input type="radio" name="key" value="B"><span class="key">B</span>start cold</label>',
                     '<form class="stack" method="post" action="/decisions/cache-warmup">', '<input type="hidden" name="back" value="/decisions">',
                     '<input type="text" name="words"', '<button type="submit">save</button>', '<a class="ref" href="https://x/#111">#111</a> triage · hold', '<a class="ref" href="https://x/!58">!58</a> merge · passed',
                     "The pool stays."):
            with self.subTest(text=text):
                self.assertIn(text, pending)
        self.assertNotIn("checked", pending)
        self.assertNotRegex(pending, r'class="[^"]*\brec\b')
        self.assertIn("rendered 02:10 CDT · 1 pending · oldest 29m · holding 1 issue, 1 merge request · 0 completed today", page)

    def test_a_saved_page_answer_is_an_answered_row_with_the_words(self):
        saved = {"key": "B", "words": "B, and log each retry", "at": at("02:08").isoformat()}
        page = self.render(dp.Context(NOW), retry=decision(answer=saved), other=decision())
        pending = page.split("COMPLETED")[0]
        self.assertIn('<div class="row pend saved">', pending)
        self.assertIn('<span class="key">B</span>Follow the plan', pending)
        self.assertIn('<span class="words">“B, and log each retry”</span>', pending)
        self.assertIn('<span class="saved">answered, saved 02:08</span>', pending)
        self.assertEqual(pending.count("<form"), 1)
        self.assertIn("· 2 pending · 1 saved ·", page)

    def test_without_a_topic_there_is_no_chip(self):
        page = self.render(dp.Context(NOW), x=decision(topic=None))
        self.assertNotIn('class="topic"', page)

    def test_an_unreadable_file_is_a_dashed_row_naming_it_and_why(self):
        d = decision()
        del d["default"]
        page = self.render(dp.Context(NOW), good=decision(), bad=d, broken="{")
        self.assertIn('<div class="row pend unread">', page)
        self.assertIn("decision-bad.json", page)
        self.assertIn("unreadable: the decision has no default", page)
        self.assertIn("decision-broken.json", page)
        self.assertIn("· 2 unreadable ·", page)
        self.assertIn("1 pending", page)

    def test_a_completed_row_carries_every_column(self):
        warm = decision(headline="Cache warmup", options=[{"key": "A", "title": "keep the pool"}, {"key": "B", "title": "start cold"}],
                        holds=["#99"])
        ctx = dp.Context(NOW, rows=(row("23:00", "decision-cache-warmup old", '"A"', 25),
                                    row("00:20", "Upload size limit #109", '"yes, dispatch it"'),
                                    row("01:15", "option B of decision-cache-warmup", '"B, start cold"')),
                         log=((at("23:10", 25), "asked decision-cache-warmup"),), sources=SOURCES,
                         tasks=(task("Upload", "Upload limit", "review", "#109"),))
        page = self.render(ctx, **{"cache-warmup": warm})
        done = page.split("COMPLETED")[1]
        self.assertLess(done.index("01:15"), done.index("00:20"))
        for text in ('<span class="when">01:15</span>', '<span class="took">in 2h 05m</span>',
                     '<a class="headline" href="/decisions/cache-warmup">Cache warmup</a>', '<span class="page">decision-cache-warmup</span>',
                     '<span class="key">B</span>start cold', '<span class="words">&quot;B, start cold&quot;</span>',
                     '<a class="ref">#99</a> merged 01:15',
                     "Upload size limit #109", '<span class="page">Decisions row</span>', '<span class="key">yes</span>dispatch it',
                     '<a class="ref">#109</a> review · running'):
            with self.subTest(text=text):
                self.assertIn(text, done)
        self.assertNotIn("decision-cache-warmup old", done)
        self.assertIn("0 pending", page)
        self.assertIn("2 completed today", page)

    def test_it_renders_with_empty_sources(self):
        page = self.render(dp.Context(NOW, sources=bs.EMPTY), x=decision(holds=["#111"]))
        self.assertIn('<a class="ref">#111</a>', page)

    def test_a_source_that_could_not_be_read_is_named_in_the_header(self):
        page = self.render(dp.Context(NOW, sources=bs.Sources({}, {}, {}, (), {"merge requests": "gh: not logged in"})))
        self.assertIn("merge requests: gh: not logged in", page)

    def test_text_is_escaped(self):
        page = self.render(dp.Context(NOW, rows=(row("01:00", "<b>x</b>", "<i>"),)), x=decision(headline="<script>", topic="<t>"))
        self.assertEqual(page.count("<script>"), 1)  # the page's own answer script; the headline is escaped
        self.assertNotIn("<b>x</b>", page)
        self.assertNotIn("<t>", page)

    def test_the_page_wears_the_board_scheme(self):
        page = self.render(dp.Context(NOW))
        board, mine = scheme(board_css()), scheme(page)
        for token in SHARED:
            self.assertEqual(mine["light"][token], board["light"][token])


class DerivedPageTest(unittest.TestCase):
    """The decision page applies the same derivations when its JSON does not carry them."""

    def test_asked_answer_and_the_chip_come_from_the_tracker(self):
        ctx = dp.Context(NOW, rows=(row("01:15", "option B of decision-x", '"B"'),), log=((at("00:40"), "asked decision-x"),))
        page = dp.render(dp.parse(decision()), "2026-09-26", ctx=ctx, slug="x")
        self.assertIn("asked 00:40", page)
        self.assertIn(">ANSWERED · B<", page)
        page = dp.render(dp.parse(decision(asked="00:10")), "2026-09-26", ctx=dp.Context(NOW), slug="x", mtime=at("00:05").timestamp())
        self.assertIn("asked 00:10", page)
        self.assertIn(">PENDING<", page)

    def test_a_reference_without_a_status_takes_it_from_the_sources(self):
        refs = [{"kind": "MR", "value": "!58"}, {"kind": "issue", "value": "#111"}, {"kind": "issue", "value": "#96", "status": "said"}]
        ctx = dp.Context(NOW, tasks=(task("Budget", "Connection budget", "triage", "#111"),), sources=SOURCES, hold="status::hold")
        page = dp.render(dp.parse(decision(references=refs)), "2026-09-26", ctx=ctx, slug="x")
        self.assertIn('<span class="st">open · passed</span>', page)
        self.assertIn('<span class="st">triage · hold</span>', page)
        self.assertIn('<span class="st">said</span>', page)

    def test_without_sources_the_page_still_renders(self):
        page = dp.render(dp.parse(decision(references=[{"kind": "MR", "value": "!58"}])), "2026-09-26", ctx=dp.Context(NOW), slug="x")
        self.assertIn('<span class="st"></span>', page)


def board_css() -> str:
    return (Path(__file__).resolve().parent.parent / "scripts" / "render_board.py").read_text().replace("{{", "{").replace("}}", "}")


def scheme(css: str) -> dict[str, dict[str, str]]:
    """The tokens of the light `:root` and of each dark block, which must agree."""
    def tokens(block: str) -> dict[str, str]:
        return dict(re.findall(r"(--[\w-]+):([^;}]+)", block))
    light = tokens(re.search(r"^:root\{(.*?)\}$", css, re.M)[1])
    auto = re.search(r'^@media \(prefers-color-scheme:dark\)\{:root:not\(\[data-theme="light"\]\)\{(.*?)\}', css, re.M)[1]
    forced = re.search(r'^:root\[data-theme="dark"\]\{(.*?)\}', css, re.M)[1]
    assert tokens(auto) == tokens(forced), "the two dark blocks differ"
    return {"light": light, "dark": tokens(auto)}


def contrast(a: str, b: str) -> float:
    """WCAG 2 contrast ratio of two #rrggbb colours."""
    def lum(h: str) -> float:
        c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        r, g, b_ = (x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


STAGES = ("implement", "pr", "review", "triage", "fix", "verify", "merge")
SHARED = ("--bg", "--surface", "--fg", "--muted", "--line", "--brass", "--dl") + tuple(f"--stage-{s}" for s in STAGES)
# (text, ground) pairs as the stylesheets use them: every one is text that must read at WCAG AA.
BOARD_TEXT = [("--fg", "--bg"), ("--fg", "--surface"), ("--muted", "--bg"), ("--muted", "--surface"), ("--brass", "--surface"),
              ("--brass", "--bg"), ("--dl", "--bg"), ("--dl", "--surface")] + [(f"--stage-{s}", "--bg") for s in STAGES]
PAGE_TEXT = [("--link", "--ref-bg"), ("--link", "--surface"), ("--surface", "--link"), ("--surface", "--now"), ("--deployed", "--surface"), ("--inflight", "--inflight-soft"),
             ("--designed", "--designed-soft"), ("--ext-ink", "--ext-soft"), ("--dl", "--ext-soft")]


class DarkSchemeTest(unittest.TestCase):
    """The user, 2026-09-26: "we need new dark mode colors for the entire scheme"."""

    def test_the_board_dark_scheme_reads_at_aa(self):
        dark = scheme(board_css())["dark"]
        for text, ground in BOARD_TEXT:
            with self.subTest(text=text, ground=ground):
                self.assertGreaterEqual(contrast(dark[text], dark[ground]), 4.5)

    def test_the_decision_page_dark_scheme_reads_at_aa(self):
        dark = scheme(dp.HEAD)["dark"]
        for text, ground in PAGE_TEXT:
            with self.subTest(text=text, ground=ground):
                self.assertGreaterEqual(contrast(dark[text], dark[ground]), 4.5)

    def test_stages_keep_okabe_ito_in_light_and_merge_has_a_dark_stand_in(self):
        themes = scheme(board_css())
        for stage, colour in gantt.PALETTE.items():
            with self.subTest(stage=stage):
                self.assertEqual(themes["light"][f"--stage-{stage}"].lower(), (colour if isinstance(colour, str) else colour[0]).lower())
        self.assertGreater(contrast(themes["dark"]["--stage-merge"], themes["dark"]["--bg"]), 7)

    def test_the_board_sets_every_gantt_colour_for_dark(self):
        css = board_css()
        colours = set(re.findall(r"(--gantt-[\w-]+):#", gantt.BASE_CSS))
        for block in (re.search(r'^@media \(prefers-color-scheme:dark\)\{.*$', css, re.M)[0],
                      "\n".join(re.findall(r'^:root\[data-theme="dark"\].*$', css, re.M))):
            set_here = set(re.findall(r"(--gantt-[\w-]+):var\(--[\w-]+\)", block))
            self.assertEqual(colours - set_here, set())


class MainTest(unittest.TestCase):
    def workspace(self, board: str = BOARD) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(BLOCK.format(board=board))
        (root / "d.json").write_text(json.dumps(decision()))
        return root

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dp.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_writes_into_the_pages_dir_and_prints_its_local_url(self):
        root = self.workspace()
        code, out, _ = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "uploads-storage",
                                     "--date", "2026-09-25")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "http://127.0.0.1:8765/decisions/uploads-storage")
        self.assertIn("Follow the architecture", (root / "pages" / "decision-uploads-storage.html").read_text())

    def test_reads_the_decision_beside_its_page_by_default(self):
        root = self.workspace()
        (root / "pages").mkdir()
        (root / "pages" / "decision-uploads-storage.json").write_text(json.dumps(decision()))
        code, out, _ = self.run_main("--root", str(root), "--name", "uploads-storage")
        self.assertEqual((code, out.strip()), (0, "http://127.0.0.1:8765/decisions/uploads-storage"))

    def test_a_name_that_is_not_a_slug_is_refused(self):
        root = self.workspace()
        for name in ("../x", "A B", ""):
            with self.subTest(name=name):
                code, _, err = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", name)
                self.assertEqual(code, 2)
                self.assertIn("name", err)
        self.assertFalse((root / "pages").exists())

    def test_no_board_line_means_no_page(self):
        root = self.workspace(board="")
        code, _, err = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "x")
        self.assertEqual(code, 1)
        self.assertIn("Board", err)

    def test_an_invalid_decision_writes_nothing(self):
        root = self.workspace()
        (root / "d.json").write_text(json.dumps(decision(recommended="Z")))
        code, _, err = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "x")
        self.assertEqual(code, 2)
        self.assertIn("recommended", err)
        self.assertFalse((root / "pages").exists())

    def test_the_cli_reads_asked_and_the_answer_from_the_trackers(self):
        root = self.workspace()
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-25-tracker.md").write_text(
            "# Tracker\n\n## Decisions\n\n| time | item | Robin's words |\n|---|---|---|\n"
            "| 10:05 | option B of decision-x | \"B, go\" |\n\n## Log\n\n- 09:40 asked Robin about decision-x\n")
        code, _, _ = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "x", "--date", "2026-09-25")
        page = (root / "pages" / "decision-x.html").read_text()
        self.assertEqual(code, 0)
        self.assertRegex(page, r"asked (?:Fri )?09:40")
        self.assertIn(">ANSWERED · B<", page)

    def test_links_against_the_workspace_backlog(self):
        root = self.workspace(board=BOARD + "\n- Backlog: GitHub; repo o/app")
        code, _, _ = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "x")
        self.assertEqual(code, 0)
        self.assertIn('href="https://github.com/o/app/issues/7"', (root / "pages" / "decision-x.html").read_text())


if __name__ == "__main__":
    unittest.main()
