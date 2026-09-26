"""tracker_write.py: every script-side tracker write takes one lock, and `log` appends a stamped Log line."""

import fcntl
import io
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tracker_write as tw  # noqa: E402

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Timezone: America/Chicago
"""
TRACKER = "# Tracker\n\n## Log\n\n- 09:00 opened\n\n## Resume\n\n- As of: 09:00\n"


class LogTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        (self.root / "daily").mkdir()
        self.tracker = self.root / "daily/2026-09-18-tracker.md"
        self.tracker.write_text(TRACKER)

    def log(self, *text: str) -> int:
        return tw.main(["--root", str(self.root), "--date", "2026-09-18", *text])

    def test_a_line_lands_at_the_end_of_the_log_stamped_in_the_workspace_zone(self):
        self.assertEqual(self.log("Robin:", "Checkr is done."), 0)
        stamp = datetime.now(ZoneInfo("America/Chicago")).strftime("%H:%M")
        self.assertEqual(self.tracker.read_text(),
                         TRACKER.replace("- 09:00 opened\n", f"- 09:00 opened\n- {stamp} Robin: Checkr is done.\n"))

    def test_empty_text_writes_nothing(self):
        self.assertEqual(self.log("  "), 2)
        self.assertEqual(self.tracker.read_text(), TRACKER)

    def test_no_log_section_writes_nothing(self):
        self.tracker.write_text("# Tracker\n\n## Tasks\n")
        self.assertEqual(self.log("hello"), 1)
        self.assertEqual(self.tracker.read_text(), "# Tracker\n\n## Tasks\n")

    def test_a_line_break_is_refused(self):
        # One call is one Log line; a newline would forge a second, stamped by nobody.
        self.assertEqual(self.log("one\n- 09:01 two"), 2)
        self.assertEqual(self.tracker.read_text(), TRACKER)

    def test_an_edit_leaves_no_file_beside_the_tracker(self):
        # A workspace that tracks its daily dir in git would gain an untracked lock file every day.
        before = sorted(p.name for p in self.tracker.parent.iterdir())
        tw.edit(self.tracker, lambda text: text + "late\n")
        self.assertEqual(sorted(p.name for p in self.tracker.parent.iterdir()), before)

    def test_an_edit_waits_for_the_lock(self):
        held = os.open(tw.lock_path(self.tracker), os.O_RDONLY)
        try:
            fcntl.flock(held, fcntl.LOCK_EX)
            writer = threading.Thread(target=tw.edit, args=(self.tracker, lambda text: text + "late\n"))
            writer.start()
            writer.join(0.3)
            self.assertTrue(writer.is_alive())
            self.assertEqual(self.tracker.read_text(), TRACKER)
        finally:
            os.close(held)
        writer.join(5)
        self.assertTrue(self.tracker.read_text().endswith("late\n"))


LANED = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Upload path check | Reject paths with `a\\|b` outside the root | impl-1 | running 09:10 | 09:00 |  | M | build | implement | #12 | Checklist: upload |
| Twin | first | Robin | open | 09:00 |  | S | build | implement |  |  |
| Twin | second | Robin | open | 09:00 |  | S | build | implement |  |  |
| Console key | Rotate the key | Robin | open | 09:00 |  | S |  |  |  |  |

## Log

- 09:00 opened
"""


class StageTest(unittest.TestCase):
    """`log --stage`: the stage cell and a structured Log line, written under one lock."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `cos.toml`\n")
        (self.root / "cos.toml").write_text('[lanes]\nbuild = { stages = ["implement", "pr", "review", "merge", "main"], gates = ["merge"] }\n')
        (self.root / "daily").mkdir()
        self.tracker = self.root / "daily/2026-09-18-tracker.md"
        self.tracker.write_text(LANED)

    def stage(self, name: str, stage: str) -> int:
        return tw.main(["--root", str(self.root), "--date", "2026-09-18", "--stage", name, stage])

    def test_the_stage_cell_moves_and_the_log_says_so(self):
        self.assertEqual(self.stage("upload path check", "pr"), 0)
        text = self.tracker.read_text()
        stamp = datetime.now(ZoneInfo("America/Chicago")).strftime("%H:%M")
        self.assertIn("| Upload path check | Reject paths with `a\\|b` outside the root | impl-1 | running 09:10 | 09:00 |  | M | build | pr | #12 | Checklist: upload |\n", text)
        self.assertTrue(text.endswith(f"- 09:00 opened\n- {stamp} stage: Upload path check → pr\n"), text)
        self.assertEqual(text.count("| implement |"), 2)

    def test_refusals_write_nothing(self):
        for name, stage, why in (("Nobody", "pr", "no task"), ("Twin", "pr", "2 tasks"),
                                 ("Upload path check", "deploy", "not a stage of build"),
                                 ("Console key", "pr", "no lane")):
            with self.subTest(name=name, stage=stage):
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(self.stage(name, stage), 1)
                self.assertIn(why, err.getvalue())
                self.assertEqual(self.tracker.read_text(), LANED)

    def test_the_stage_is_one_word(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(tw.main(["--root", str(self.root), "--date", "2026-09-18", "--stage", "Twin", "p", "r"]), 2)
        self.assertEqual(self.tracker.read_text(), LANED)

    def test_the_write_takes_the_tracker_lock(self):
        held = os.open(tw.lock_path(self.tracker), os.O_RDONLY)
        try:
            fcntl.flock(held, fcntl.LOCK_EX)
            writer = threading.Thread(target=self.stage, args=("Upload path check", "pr"))
            writer.start()
            writer.join(0.3)
            self.assertTrue(writer.is_alive())
            self.assertEqual(self.tracker.read_text(), LANED)
        finally:
            os.close(held)
        writer.join(5)
        self.assertIn("| build | pr |", self.tracker.read_text())


if __name__ == "__main__":
    unittest.main()
