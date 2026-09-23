"""Unit tests for scripts/settings.py: the workspace's chief-of-stuff.toml, named by the Coordinator block.

Zach, 2026-09-22 22:20, on the day open and close times: "Configurable. We're going to need to pull
settings into a config file. Default to 6 and 22". A value that cannot be read is refused, never
guessed at.
"""

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import settings as st  # noqa: E402

FULL = """[notify]
adapter = "md-notify"
queue = "notes/queue.md"
day_open = "07:30"
day_close = "21:00"
deadline_warnings = ["24h", "90m"]
"""


class SettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write(self, text: str, name: str = "chief-of-stuff.toml") -> str:
        (self.root / name).write_text(text)
        return name

    def test_no_settings_line_is_off(self):
        self.assertEqual(st.load(self.root, None).notify.adapter, "off")

    def test_a_named_file_that_is_absent_is_off(self):
        self.assertEqual(st.load(self.root, "chief-of-stuff.toml").notify.adapter, "off")

    def test_no_notify_section_is_off(self):
        self.assertEqual(st.load(self.root, self.write("[other]\nx = 1\n")).notify.adapter, "off")

    def test_defaults_per_missing_key(self):
        got = st.load(self.root, self.write("[notify]\n")).notify
        self.assertEqual((got.adapter, got.queue, got.day_open, got.day_close),
                         ("md-notify", "Areas/notifications/NOTIFICATIONS.md", "06:00", "22:00"))
        self.assertEqual(got.warnings, (("24h", timedelta(hours=24)), ("3h", timedelta(hours=3)), ("1h", timedelta(hours=1))))

    def test_values_are_read(self):
        got = st.load(self.root, self.write(FULL)).notify
        self.assertEqual((got.queue, got.day_open, got.day_close), ("notes/queue.md", "07:30", "21:00"))
        self.assertEqual(got.warnings, (("24h", timedelta(hours=24)), ("90m", timedelta(minutes=90))))
        self.assertEqual(got.queue_path(self.root), (self.root / "notes/queue.md").resolve())

    def test_off_is_an_adapter(self):
        self.assertEqual(st.load(self.root, self.write('[notify]\nadapter = "off"\n')).notify.adapter, "off")

    def test_bad_values_are_refused(self):
        for line in ('adapter = "slack"', 'day_open = "25:00"', 'day_close = "6pm"', 'deadline_warnings = ["3x"]',
                     'deadline_warnings = "3h"', 'queue = "../outside.md"', 'queue = "/tmp/outside.md"'):
            with self.subTest(line=line):
                with self.assertRaises(st.SettingsError):
                    st.load(self.root, self.write(f"[notify]\n{line}\n"))

    def test_toml_that_does_not_parse_is_refused(self):
        with self.assertRaises(st.SettingsError):
            st.load(self.root, self.write("[notify\n"))


if __name__ == "__main__":
    unittest.main()
