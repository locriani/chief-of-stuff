"""tracker_write.py: every script-side tracker write takes one lock, and `log` appends a stamped Log line."""

import fcntl
import sys
import tempfile
import threading
import unittest
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

    def test_an_edit_waits_for_the_lock(self):
        with open(tw.lock_path(self.tracker), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            writer = threading.Thread(target=tw.edit, args=(self.tracker, lambda text: text + "late\n"))
            writer.start()
            writer.join(0.3)
            self.assertTrue(writer.is_alive())
            self.assertEqual(self.tracker.read_text(), TRACKER)
        writer.join(5)
        self.assertTrue(self.tracker.read_text().endswith("late\n"))


if __name__ == "__main__":
    unittest.main()
