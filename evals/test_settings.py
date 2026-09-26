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
                         ("off", "notifications/NOTIFICATIONS.md", "06:00", "22:00"))
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

    def test_reviewer_session_is_optional_and_validated(self):
        self.assertIsNone(st.load(self.root, None).workflow.reviewer_session)
        settings = st.load(self.root, self.write('[workflow]\nreviewer_session = "reviewer-01"\n'))
        self.assertEqual(settings.workflow.reviewer_session, "reviewer-01")
        with self.assertRaises(st.SettingsError):
            st.load(self.root, self.write('[workflow]\nreviewer_session = "a b"\n'))

    def test_code_delivery_and_merge_owner(self):
        self.assertEqual((st.load(self.root, None).workflow.delivery,
                          st.load(self.root, None).workflow.merge_owner), ("pull-request", "user"))
        workflow = st.load(self.root, self.write('[workflow]\ndelivery = "branch"\n')).workflow
        self.assertEqual(workflow.delivery, "branch")
        workflow = st.load(self.root, self.write('[workflow]\nmerge_owner = "worker"\n')).workflow
        self.assertEqual(workflow.merge_owner, "worker")
        for text in ('delivery = "direct"', 'merge_owner = "bot"',
                     'delivery = "branch"\nmerge_owner = "worker"'):
            with self.subTest(text=text), self.assertRaises(st.SettingsError):
                st.load(self.root, self.write('[workflow]\n' + text + '\n'))

    def test_worker_cap_is_absent_or_a_positive_whole_number(self):
        self.assertIsNone(st.load(self.root, None).workers.max_concurrency)
        self.assertEqual(st.load(self.root, self.write('[workers]\nmax_concurrency = 3\n')).workers.max_concurrency, 3)
        for bad in ("0", "-1", "2.5", '"3"', "true"):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                st.load(self.root, self.write(f"[workers]\nmax_concurrency = {bad}\n"))

    def test_worker_launcher_defaults_to_ghostty_and_accepts_tmux(self):
        self.assertEqual(st.load(self.root, None).workers.launcher, "ghostty")
        self.assertEqual(st.load(self.root, None).workers.mode, "interactive")
        self.assertEqual(st.load(self.root, self.write('[workers]\nlauncher = "tmux"\n')).workers.launcher, "tmux")
        with self.assertRaises(st.SettingsError):
            st.load(self.root, self.write('[workers]\nlauncher = "shell"\n'))

    def test_worker_mode_accepts_one_shot_and_refuses_unknown_values(self):
        self.assertEqual(st.load(self.root, self.write('[workers]\nmode = "one-shot"\n')).workers.mode,
                         "one-shot")
        for value in ('"background"', 'true', '17'):
            with self.subTest(value=value), self.assertRaises(st.SettingsError):
                st.load(self.root, self.write(f'[workers]\nmode = {value}\n'))


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


MODELS = '''[models.deep]
rotation = ["claude:opus@high", "codex:gpt-test:preview@medium"]

[models.implement]
rotation = ["codex:gpt-test-coder@medium", "claude:sonnet", "claude:haiku@low"]
'''


class ModelsTest(unittest.TestCase):
    """#111: "I want, as part of the toml, the suggested models and model rotations to use." Each entry is
    `runtime:model-id@effort`; effort is part of the entry (Zach: "effort should be part of the model id")."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, text: str) -> st.Settings:
        (self.root / "s.toml").write_text(text)
        return st.load(self.root, "s.toml")

    def test_absent_models_is_empty(self):
        self.assertEqual(self.load('[workers]\nmode = "one-shot"\n').models, {})

    def test_rotation_is_read_in_order(self):
        got = self.load(MODELS).models
        self.assertEqual([str(e) for e in got["implement"]],
                         ["codex:gpt-test-coder@medium", "claude:sonnet", "claude:haiku@low"])
        first = got["deep"][1]
        # The runtime splits on the first colon and effort on the last @: the ID keeps its own colon.
        self.assertEqual((first.runtime, first.model, first.effort), ("codex", "gpt-test:preview", "medium"))
        self.assertIsNone(got["implement"][1].effort)

    def test_bad_tables_are_refused(self):
        for bad in (
            '[models]\ndeep = "claude:opus"\n',
            '[models.deep]\n',
            '[models.deep]\nrotation = []\n',
            '[models.deep]\nrotation = "claude:opus"\n',
            '[models.deep]\nrotation = ["gemini:pro"]\n',
            '[models.deep]\nrotation = ["opus"]\n',
            '[models.deep]\nrotation = ["claude:"]\n',
            '[models.deep]\nrotation = ["claude:@high"]\n',
            '[models.deep]\nrotation = ["claude:opus@max"]\n',
            '[models.deep]\nrotation = ["claude:opus@"]\n',
            '[models.deep]\nrotation = ["claude:opus", "claude:opus"]\n',
            '[models.deep]\nrotation = [1]\n',
            '[models.deep]\nrotation = ["claude:opus"]\neffort = "high"\n',
        ):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                self.load(bad)

    def test_pick_suggests_the_first_entry(self):
        rotation = self.load(MODELS).models["implement"]
        self.assertEqual(str(st.pick(rotation)), "codex:gpt-test-coder@medium")

    def test_after_advances_the_rotation(self):
        rotation = self.load(MODELS).models["implement"]
        self.assertEqual(str(st.pick(rotation, after="codex:gpt-test-coder@medium")), "claude:sonnet")
        # The entry without its effort names the same entry.
        self.assertEqual(str(st.pick(rotation, after="codex:gpt-test-coder")), "claude:sonnet")
        with self.assertRaisesRegex(st.SettingsError, "rotation exhausted"):
            st.pick(rotation, after="claude:haiku@low")
        with self.assertRaisesRegex(st.SettingsError, "not in the rotation"):
            st.pick(rotation, after="claude:opus")

    def test_not_family_skips_that_runtime(self):
        rotation = self.load(MODELS).models["implement"]
        self.assertEqual(str(st.pick(rotation, not_family="codex")), "claude:sonnet")
        with self.assertRaisesRegex(st.SettingsError, "rotation exhausted"):
            st.pick(rotation, after="claude:sonnet", not_family="claude")


class GraphSettingsTest(unittest.TestCase):
    """The decision page's MODULE GRAPH: which clone branch-graph reads, its import root, depth and rules."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, text: str) -> st.Settings:
        (self.root / "s.toml").write_text(text)
        return st.load(self.root, "s.toml")

    def test_absent_graph_is_not_configured(self):
        self.assertIsNone(st.load(self.root, None).graph)
        self.assertIsNone(self.load('[workers]\nmode = "one-shot"\n').graph)

    def test_clone_and_root_with_the_default_depth_and_no_rules(self):
        got = self.load('[graph]\nclone = "repos/app"\nroot = "agent"\n').graph
        self.assertEqual((got.clone, got.root, got.depth, got.rules), ("repos/app", "agent", 2, None))

    def test_depth_and_rules_are_read(self):
        got = self.load('[graph]\nclone = "repos/app"\nroot = "agent"\ndepth = 3\nrules = "docs/architecture.md"\n').graph
        self.assertEqual((got.depth, got.rules), (3, "docs/architecture.md"))

    def test_a_graph_that_cannot_be_read_is_refused(self):
        base = 'clone = "repos/app"\nroot = "agent"\n'
        for bad in ('graph = 1\n',
                    '[graph]\nroot = "agent"\n',
                    '[graph]\nclone = "repos/app"\n',
                    '[graph]\nclone = ""\nroot = "agent"\n',
                    '[graph]\nclone = 3\nroot = "agent"\n',
                    '[graph]\nclone = "repos/app"\nroot = ""\n',
                    f'[graph]\n{base}depth = 0\n',
                    f'[graph]\n{base}depth = "2"\n',
                    f'[graph]\n{base}depth = 2.5\n',
                    f'[graph]\n{base}depth = true\n',
                    f'[graph]\n{base}rules = 5\n',
                    f'[graph]\n{base}rules = ""\n'):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                self.load(bad)
