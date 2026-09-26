"""decision_page.py: a decision's full context as a page on the local pages server (Zach, 2026-09-25: "make sure
in the future you don't serve it as an artifact but serve it using the localhost server thing")."""

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
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
        for text in ("Decision · Storage · 2026-09-25", "Should the worker follow the architecture?",
                     "What the plan says", "Where it stands", "How it got here", "Options",
                     "Recommendation", "The architecture is the newer decision.", "If you don't answer",
                     "The worker waits.", "References", "Answer in chat: A or B."):
            self.assertIn(text, page)

    def test_text_is_escaped_and_marked_up(self):
        page = dp.render(dp.parse(decision()), "2026-09-25")
        self.assertIn("<code>s3://bucket</code> &lt;now&gt;", page)
        self.assertIn("<b>Breaks</b>", page)
        self.assertIn('<a href="https://github.com/o/backlog/issues/7">https://github.com/o/backlog/issues/7</a>', page)
        self.assertNotIn("<now>", page)

    def test_the_recommended_option_is_marked(self):
        page = dp.render(dp.parse(decision()), "2026-09-25")
        self.assertIn('<div class="opt rec"><span class="k">A</span><b>Follow the architecture (recommended)</b>', page)
        self.assertIn('<div class="opt"><span class="k">B</span>', page)

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
              ("--brass", "--bg"), ("--dl", "--bg"), ("--dl", "--surface"), ("--est", "--surface"), ("--fg", "--done"),
              ("--seg-ink", "--eat"), ("--seg-ink", "--recreation"), ("--bar-ink", "--sleep"), ("--bar-ink", "--gym")] + [
              ("--bar-ink", f"--{bar}") for bar in ("open", "running", "noest", "est", "fold")] + [
              (f"--stage-{s}", "--bg") for s in STAGES]
PAGE_TEXT = [("--link", "--ref-bg"), ("--link", "--surface"), ("--deployed", "--surface"), ("--inflight", "--inflight-soft"),
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
        self.assertEqual(out.strip(), "http://127.0.0.1:8765/decision-uploads-storage.html")
        self.assertIn("Follow the architecture", (root / "pages" / "decision-uploads-storage.html").read_text())

    def test_reads_the_decision_beside_its_page_by_default(self):
        root = self.workspace()
        (root / "pages").mkdir()
        (root / "pages" / "decision-uploads-storage.json").write_text(json.dumps(decision()))
        code, out, _ = self.run_main("--root", str(root), "--name", "uploads-storage")
        self.assertEqual((code, out.strip()), (0, "http://127.0.0.1:8765/decision-uploads-storage.html"))

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

    def test_links_against_the_workspace_backlog(self):
        root = self.workspace(board=BOARD + "\n- Backlog: GitHub; repo o/app")
        code, _, _ = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "x")
        self.assertEqual(code, 0)
        self.assertIn('href="https://github.com/o/app/issues/7"', (root / "pages" / "decision-x.html").read_text())


if __name__ == "__main__":
    unittest.main()
