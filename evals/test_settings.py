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

    def test_architecture_reviewer_is_optional_and_validated(self):
        self.assertIsNone(st.load(self.root, None).workflow.architecture_reviewer)
        settings = st.load(self.root, self.write('[workflow]\narchitecture_reviewer = "architecture-01"\n'))
        self.assertEqual(settings.workflow.architecture_reviewer, "architecture-01")
        with self.assertRaises(st.SettingsError):
            st.load(self.root, self.write('[workflow]\narchitecture_reviewer = "a b"\n'))


if __name__ == "__main__":
    unittest.main()


LANES = """[lanes]
build = { stages = ["implement", "pr", "review", "triage", "merge"], gates = ["triage", "merge"] }
"""


class LanesTest(unittest.TestCase):
    """#33 (Zach, 2026-09-23): "A lane: the sequence that a task has to move through" — named, in the toml."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, text: str):
        (self.root / "s.toml").write_text(text)
        return st.load(self.root, "s.toml")

    def test_no_lanes_table_is_no_lanes(self):
        self.assertEqual(self.load("[notify]\n").lanes, {})

    def test_a_lane_is_its_stages_and_gates(self):
        lane = self.load(LANES).lanes["build"]
        self.assertEqual(lane.stages, ("implement", "pr", "review", "triage", "merge"))
        self.assertEqual(lane.gates, ("triage", "merge"))

    def test_lanes_do_not_need_notify(self):
        got = self.load(LANES)
        self.assertEqual(got.notify.adapter, "off")
        self.assertIn("build", got.lanes)

    def test_a_lane_that_cannot_be_read_is_refused(self):
        for bad in ('[lanes]\nx = 1\n',
                    '[lanes]\nx = { stages = [] }\n',
                    '[lanes]\nx = { stages = ["a", "a"] }\n',
                    '[lanes]\nx = { stages = ["a"], gates = ["b"] }\n',
                    '[lanes]\nx = { stages = ["a", 2] }\n'):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                self.load(bad)


class BudgetsTest(unittest.TestCase):
    """#33 stage 3: a running task's time budget by size; XL has none."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, text: str):
        (self.root / "s.toml").write_text(text)
        return st.load(self.root, "s.toml")

    def test_no_budgets_table_is_no_budgets(self):
        self.assertEqual(self.load("").budgets, {})

    def test_budgets_by_size(self):
        got = self.load('[budgets]\nS = "30m"\nM = "90m"\nL = "3h"\n').budgets
        self.assertEqual(got, {"S": timedelta(minutes=30), "M": timedelta(minutes=90), "L": timedelta(hours=3)})

    def test_a_budget_that_cannot_be_read_is_refused(self):
        for bad in ('budgets = 1\n', '[budgets]\nQ = "1h"\n', '[budgets]\nM = "soon"\n', '[budgets]\nM = 90\n'):
            with self.assertRaises(st.SettingsError, msg=bad):
                self.load(bad)


KANBAN = '''[kanban]
stages = ["00 - PLAN", "01 - PLAN REVIEW", "02 - TEST", "03 - BUILD", "04 - REVIEW", "05 - FIX"]
human_review_label = "!! - HUMAN REVIEW REQUIRED"
hold_stages = ["plan review", "triage", "merge"]
terminal_stages = ["main"]

[kanban.map]
plan = 0
"plan review" = 1
test = 2
implement = 3
review = 4
triage = 4
fix = 5
merge = 4
'''


class KanbanSettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, text: str):
        (self.root / "s.toml").write_text(text)
        return st.load(self.root, "s.toml")

    def test_absent_kanban_is_disabled(self):
        self.assertIsNone(self.load("").kanban)

    def test_config_maps_fine_stages_and_hold_overlay(self):
        cfg = self.load(KANBAN).kanban
        self.assertEqual(cfg.label_for("implement"), "03 - BUILD")
        self.assertEqual(cfg.label_for("fix"), "05 - FIX")
        self.assertTrue(cfg.holds("triage"))
        self.assertFalse(cfg.holds("review"))
        self.assertIsNone(cfg.label_for("main"))

    def test_invalid_mapping_is_refused(self):
        for bad in (
            KANBAN.replace('plan = 0', 'plan = 6'),
            KANBAN.replace('plan = 0', 'plan = true'),
            KANBAN.replace('"triage", "merge"', '"unknown", "merge"'),
            KANBAN.replace('"00 - PLAN", ', '"01 - PLAN REVIEW", '),
            KANBAN.replace('terminal_stages = ["main"]', 'terminal_stages = ["review"]'),
        ):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                self.load(bad)

    def test_github_project_options_can_use_different_status_names(self):
        extra = '''\n[kanban.github_project]
owner = "example-org"
number = 7
field = "Status"
options = ["Plan", "Plan Review", "Test", "Build", "Review", "Fix"]
'''
        cfg = self.load(KANBAN + extra).kanban.github_project
        self.assertEqual((cfg.owner, cfg.number, cfg.field), ("example-org", 7, "Status"))
        self.assertEqual(cfg.options[4], "Review")

    def test_invalid_github_project_is_refused(self):
        for extra in ('owner = ""\nnumber = 7', 'owner = "org"\nnumber = 0',
                      'owner = "org"\nnumber = 7\noptions = ["one"]'):
            with self.subTest(extra=extra), self.assertRaises(st.SettingsError):
                self.load(KANBAN + "\n[kanban.github_project]\n" + extra + "\n")
