"""decision_page.py: a decision's full context as a page on the local pages server (Zach, 2026-09-25: "make sure
in the future you don't serve it as an artifact but serve it using the localhost server thing")."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import decision_page as dp  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
